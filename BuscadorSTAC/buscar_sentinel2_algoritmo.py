# -*- coding: utf-8 -*-
# SPDX-License-Identifier: GPL-2.0-or-later
#
# Copyright (C) 2026 Jorge Fallas <jfallas56@gmail.com>
#
# Este programa es software libre: puede redistribuirlo y/o modificarlo bajo
# los términos de la Licencia Pública General GNU publicada por la Free
# Software Foundation, ya sea la versión 2 de la Licencia o (a su elección)
# cualquier versión posterior.
#
# Se distribuye con la esperanza de que sea útil, pero SIN NINGUNA GARANTÍA;
# ni siquiera la garantía implícita de COMERCIABILIDAD o APTITUD PARA UN
# PROPÓSITO PARTICULAR. Consulte la Licencia Pública General GNU para más
# detalles. Debería haber recibido una copia junto con este programa; si no,
# vea <https://www.gnu.org/licenses/>.
#
"""
Buscador y descargador de escenas Sentinel-2 L2A (STAC + COG)
=============================================================

Algoritmo Processing para QGIS. Consulta catálogos STAC públicos
(Element84 Earth Search / Microsoft Planetary Computer), genera una capa
vectorial de huellas con metadatos, y opcionalmente carga los assets como
capas remotas (/vsicurl/) o descarga recortes locales del AOI.

No requiere credenciales para Earth Search. Planetary Computer usa un
token SAS anónimo gratuito que el algoritmo solicita automáticamente.

Autor  : Jorge Fallas (jfallas56@gmail.com)
Versión: 1.1.0

Historial:
    1.0.0 (2026-10-02): Primera versión pública.
        El historial detallado del desarrollo previo a la publicación está en
        CHANGELOG.md del repositorio:
        https://github.com/jfallas56-CR/BuscadorSTAC
"""

import json
import math
import os
import re
import urllib.error
import urllib.parse
import urllib.request
import uuid
import warnings
from datetime import datetime, timedelta, timezone

import numpy as np

from qgis.PyQt.QtCore import QCoreApplication, QTimer, QUrl
from qgis.PyQt.QtGui import QColor, QDesktopServices, QIcon

from qgis.core import (
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsFeature,
    QgsFeatureRequest,
    QgsField,
    QgsFields,
    QgsGeometry,
    QgsPointXY,
    QgsProcessing,
    QgsProcessingAlgorithm,
    QgsProcessingContext,
    QgsProcessingException,
    QgsProcessingFeedback,
    QgsContrastEnhancement,
    QgsEditorWidgetSetup,
    QgsMultiBandColorRenderer,
    QgsRasterShader,
    QgsColorRampShader,
    QgsSingleBandGrayRenderer,
    QgsSingleBandPseudoColorRenderer,
    QgsProcessingLayerPostProcessorInterface,
    QgsProcessingOutputFolder,
    QgsProcessingParameterBoolean,
    QgsProcessingParameterEnum,
    QgsProcessingParameterExtent,
    QgsRasterMinMaxOrigin,
    QgsProcessingParameterFeatureSink,
    QgsProcessingParameterFeatureSource,
    QgsProcessingParameterFile,
    QgsProcessingParameterNumber,
    QgsProcessingParameterString,
    QgsProcessingUtils,
    QgsProject,
    QgsRasterLayer,
    QgsRectangle,
    QgsVectorFileWriter,
    QgsWkbTypes,
)

# Lógica pura, en un módulo que NO importa QGIS: así se prueba
# con pytest a secas, sin imitar QGIS. Ver core.py.
from .core import (
    INDICES_ESPECTRALES, _acotar_indice,
    _agrupar_por_periodo, _codigo_meses, _destino_sumidero,
    _escribir_plantilla_tc, _items_desde_capa, _mascaras_qa_pixel,
    _memoria_estimada, _parsear_limites, _parsear_meses,
    _periodos_sin_datos, _procedencia_huellas, _reducir_por_periodo,
    _res_nativa, _salida_volatil, _tamano_salida, _validar_ortonormalidad,
    CLAVES_BANDAS, ETIQUETAS_BANDAS, SIN_EQUIVALENTE_LS,
    alias_banda, bandas_duplicadas, etiqueta_banda, sufijo_banda)

from osgeo import gdal

# --------------------------------------------------------------------------
# Compatibilidad Qt5/Qt6 (QGIS 3.28 LTR / 3.44 LTR / 4.0)
# --------------------------------------------------------------------------


def _tipos_de_campo():
    """(_INT, _FLOAT, _STR) del tipo que acepte QgsField en ESTE QGIS.

    El tipo de campo pasó de QVariant.Type a QMetaType.Type en QGIS 3.38.
    Preguntar si QMetaType se puede importar NO sirve para distinguirlos:
    QMetaType existe también en Qt5, así que QGIS 3.28 —la versión mínima
    que declara el complemento— se llevaba el tipo nuevo y QgsField lo
    rechazaba con

        overload 2: argument 1 has unexpected type 'str'

    un mensaje que señala al NOMBRE del campo cuando lo que sobra es el
    tipo, y que por eso costó leer. Con eso, crear la capa de huellas era
    imposible en 3.28: no es un detalle de compatibilidad, es el producto
    principal del complemento.

    Se decide CONSTRUYENDO un QgsField, que es la pregunta de verdad:
    «¿acepta esta versión este tipo?», y no «¿existe esta clase?».
    """
    try:
        from qgis.PyQt.QtCore import QMetaType
        QgsField('comprobacion', QMetaType.Type.QString)
    except (ImportError, AttributeError, TypeError):
        from qgis.PyQt.QtCore import QVariant
        return QVariant.Int, QVariant.Double, QVariant.String
    return (QMetaType.Type.Int, QMetaType.Type.Double,
            QMetaType.Type.QString)


_INT, _FLOAT, _STR = _tipos_de_campo()


try:
    _CARPETA_BEHAVIOR = QgsProcessingParameterFile.Behavior.Folder
    _ARCHIVO_BEHAVIOR = QgsProcessingParameterFile.Behavior.File
except AttributeError:
    _CARPETA_BEHAVIOR = QgsProcessingParameterFile.Folder
    _ARCHIVO_BEHAVIOR = QgsProcessingParameterFile.File


# --------------------------------------------------------------------------
# Constantes de catálogo
# --------------------------------------------------------------------------
# La etiqueta incluye el identificador de colección: sin él no había forma de
# saber qué opción corresponde a «sentinel-2-c1-l2a». El orden NO cambia, así
# que los modelos de Processing existentes siguen apuntando a lo mismo.
CATALOGOS = [
    ("sentinel-2-l2a · Earth Search — línea base mezclada, sin credenciales",
     "https://earth-search.aws.element84.com/v1", "sentinel-2-l2a", False, 's2'),
    ("sentinel-2-c1-l2a · Earth Search Colección 1 — línea base uniforme",
     "https://earth-search.aws.element84.com/v1", "sentinel-2-c1-l2a", False, 's2'),
    ("sentinel-2-l2a · Planetary Computer — sin armonizar, token SAS",
     "https://planetarycomputer.microsoft.com/api/stac/v1", "sentinel-2-l2a",
     True, 's2'),
    ("landsat-c2-l2 · Planetary Computer — L4/5/7/8/9, desde 1982",
     "https://planetarycomputer.microsoft.com/api/stac/v1", "landsat-c2-l2",
     True, 'ls'),
]

# Landsat solo se ofrece vía Planetary Computer a propósito: en Earth Search
# los assets de landsat-c2-l2 son s3:// en un bucket «requester pays», que
# exige petición firmada con credenciales AWS de pago. AWS_NO_SIGN_REQUEST no
# sirve ahí, así que esa ruta fallaría de forma poco evidente.

# Plantilla de URL pública del servicio de tokens anónimos, no una credencial:
# Bandit la marca como B105 solo porque el nombre contiene «token».
PC_TOKEN_URL = "https://planetarycomputer.microsoft.com/api/sas/v1/token/{col}"  # nosec B105

# Nombres lógicos -> posibles claves de asset según catálogo.
# Earth Search v1 usa nombres comunes; Planetary Computer usa B02/B03/...
# Las tablas de bandas viven en core.py: una sola, con clave estable.
# Antes habia cuatro aqui, las cuatro indexadas POR LA ETIQUETA, y por
# eso la etiqueta no se podia corregir ni el sufijo del archivo podia
# depender del sensor. Ver el comentario de _BANDAS en core.py.

PREFIJO_FUENTE = {
    's2': 'Sent',
    'ls': 'Lands',
}

# Se extrae del campo «platform» del item STAC ('landsat-8', 'sentinel-2a'…)
# para dejar el satélite en el nombre del archivo. Importa en series largas:
# Landsat 5 (TM), 7 (ETM+) y 8/9 (OLI) no tienen la misma respuesta espectral,
# y mezclarlos sin saberlo sesga cualquier comparación multitemporal.
_RE_PLAT_LS = re.compile(r'landsat[-_ ]?(\d)')
_RE_PLAT_S2 = re.compile(r'sentinel[-_ ]?2[-_ ]?([a-d])?')


# Escalado a reflectancia de superficie, Collection 2 Nivel 2:
#     reflectancia = DN * 0.0000275 - 0.2
# NUNCA aplicar a qa_pixel ni qa_radsat: son máscaras de bits, no radiometría.
LS_ESCALA_MULT = 0.0000275
LS_ESCALA_SUMA = -0.2

# El valor de relleno de QA_PIXEL es 1 (bit 0 activo), no 0. Rellenar con 0 al
# recortar marcaría el exterior de la escena como «con dato y despejado».
QA_RELLENO = 1


# Composiciones RGB. Cada entrada: (etiqueta, sufijo, (banda_R, banda_G, banda_B))
# Se pueden seleccionar varias a la vez; no seleccionar ninguna equivale a
# trabajar con las bandas sueltas del parámetro «Bandas / assets individuales».
COMPOSICIONES = [
    # La etiqueta NO lleva numeros de banda: serian los de Sentinel-2 y
    # enganarian con Landsat, donde ademas la numeracion cambia entre
    # TM/ETM+ y OLI. Los nombres comunes valen en los dos sensores, y la
    # tabla por sensor esta en la ayuda del parametro.
    ("Color natural — rojo/verde/azul", "NAT",
     ("red", "green", "blue")),
    ("Infrarrojo color — NIR/rojo/verde", "IRC",
     ("nir", "red", "green")),
    ("Agricultura — SWIR 1/NIR/azul", "AGR",
     ("swir16", "nir", "blue")),
    ("Vegetación sana — NIR estrecho/SWIR 1/azul", "VEG",
     ("nir08", "swir16", "blue")),
    ("Análisis de vegetación — SWIR 1/NIR/rojo", "ANV",
     ("swir16", "nir", "red")),
    ("Falso color urbano / SWIR — SWIR 2/SWIR 1/rojo", "URB",
     ("swir22", "swir16", "red")),
    ("Penetración atmosférica — SWIR 2/SWIR 1/NIR estrecho", "PEN",
     ("swir22", "swir16", "nir08")),
    ("Geología — SWIR 2/SWIR 1/azul", "GEO",
     ("swir22", "swir16", "blue")),
]

# Estrategias de selección temporal. La segunda posición es el valor enviado a
# STAC en sortby, o una palabra clave tratada localmente.
ORDENES = [
    ("Más recientes primero", 'desc'),
    ("Más antiguas primero", 'asc'),
    ("Mejor escena por año (la de menos nubes)", 'anual'),
    ("Mejor escena por mes (la de menos nubes)", 'mensual'),
]

# Tope interno de candidatos a recuperar cuando hay que agrupar por periodo.
# Solo se descargan metadatos JSON, no píxeles.
MAX_CANDIDATOS = 600

REALCES = [
    "Corte acumulado 2–98 %",
    "Media ± N desviaciones estándar",
    "Mínimo / máximo absolutos",
]

AREAS = [
    "Capa de AOI (si está indicada); si no, la extensión",
    "Extensión indicada abajo / lienzo actual del mapa",
]


TC_COEFICIENTES = {
    'TM': {
        'ref': 'Crist (1985), factor de reflectancia de superficie',
        'tipo': 'superficie',
        'B': (0.2043, 0.4158, 0.5524, 0.5741, 0.3124, 0.2303),
        'G': (-0.1603, -0.2819, -0.4934, 0.7940, -0.0002, -0.1446),
        'W': (0.0315, 0.2021, 0.3102, 0.1594, -0.6806, -0.6109),
    },
    'ETM': {
        'ref': 'Huang et al. (2002), reflectancia at-satellite (TOA)',
        'tipo': 'TOA',
        'B': (0.3561, 0.3972, 0.3904, 0.6966, 0.2286, 0.1596),
        'G': (-0.3344, -0.3544, -0.4556, 0.6966, -0.0242, -0.2630),
        'W': (0.2626, 0.2141, 0.0926, 0.0656, -0.7629, -0.5388),
    },
    'OLI': {
        # Se usa Zhai y no Baig porque este script descarga Landsat C2 Nivel 2,
        # que es reflectancia de SUPERFICIE, y Zhai derivó precisamente para
        # ese producto. Validado por ortonormalidad: ‖v‖ = 1.0000/1.0000/1.0001
        # y v·w <= 1e-4; ángulos de 90.00° entre las tres componentes.
        # Baig et al. (2014), derivado sobre TOA, queda disponible a través del
        # archivo JSON de coeficientes alternativos.
        'ref': ('Zhai et al. (2022) RSE 274:112992, reflectancia de '
                'superficie, 6 bandas'),
        'tipo': 'superficie',
        'B': (0.3690, 0.4271, 0.4689, 0.5073, 0.3824, 0.2406),
        'G': (-0.2870, -0.2685, -0.4087, 0.8145, 0.0637, -0.1052),
        'W': (0.0382, 0.2137, 0.3536, 0.2270, -0.6108, -0.6351),
    },
    'MSI': {
        'ref': 'Shi & Xu (2019), reflectancia at-sensor (TOA, L1C)',
        'tipo': 'TOA',
        'B': (0.3510, 0.3813, 0.3437, 0.7196, 0.2396, 0.1949),
        'G': (-0.3599, -0.3533, -0.4734, 0.6633, 0.0087, -0.2856),
        'W': (0.2578, 0.2305, 0.0883, 0.1071, -0.7611, -0.5308),
    },
}


# Autoría, para el panel de ayuda y los metadatos de procedencia de las
# capas generadas. Un solo lugar: metadata.txt del complemento lo repite por
# exigencia del formato INI, y el chequeo de coherencia compara ambos.
AUTOR = 'Jorge Fallas'
AUTOR_EMAIL = 'jfallas56@gmail.com'

# Tope de memoria para el cálculo de índices y amplitud (GiB). El código no
# procesa por bloques: lee cada banda entera con ReadAsArray. Superarlo no
# degrada el rendimiento, mata el proceso de QGIS.
MEMORIA_MAX_GIB = 4.0


# Escalado de Sentinel-2 L2A a reflectancia. Desde la línea base de proceso
# 04.00 (enero de 2022) ESA aplica un desplazamiento BOA de -1000 que hay que
# restar antes de dividir; omitirlo sesga cualquier índice de esas fechas.
S2_DIVISOR = 10000.0
S2_OFFSET_BASELINE = -1000.0
S2_BASELINE_CON_OFFSET = '04.00'

MODOS = [
    "Solo catálogo (huellas + metadatos)",
    "Catálogo + cargar assets remotos (/vsicurl/, sin descarga)",
    "Catálogo + descargar recorte del AOI a disco",
]

_RE_FECHA = re.compile(r"^\d{4}-\d{2}-\d{2}$")


# --------------------------------------------------------------------------
# Utilidades HTTP (urllib — sin dependencia de `requests`)
# --------------------------------------------------------------------------
def _http_json(url, payload=None, timeout=90):
    """GET si payload es None, POST JSON en caso contrario."""
    datos = None
    cabeceras = {"Accept": "application/json",
                 "User-Agent": "QGIS-Sentinel2-STAC/1.0"}
    if payload is not None:
        datos = json.dumps(payload).encode("utf-8")
        cabeceras["Content-Type"] = "application/json"
    # Solo HTTPS. urllib.request.urlopen acepta también file:// y esquemas
    # personalizados; una URL tomada de un «next» de paginación STAC o de un
    # campo de la capa de huellas podría, en principio, apuntar a un archivo
    # local. Se rechaza cualquier esquema que no sea https antes de abrir.
    esquema = urllib.parse.urlparse(str(url)).scheme.lower()
    if esquema != "https":
        raise ValueError(
            f"Esquema de URL no permitido: '{esquema or '(vacío)'}'. "
            f"Solo se aceptan URL https.")
    req = urllib.request.Request(url, data=datos, headers=cabeceras)
    # El esquema ya se validó arriba: el aviso B310 de Bandit no aplica.
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # nosec B310
        return json.loads(resp.read().decode("utf-8"))


def _configurar_gdal():
    """Ajustes necesarios para lectura eficiente de COG por HTTP."""
    gdal.UseExceptions()
    for clave, valor in (
        ("GDAL_DISABLE_READDIR_ON_OPEN", "EMPTY_DIR"),
        ("CPL_VSIL_CURL_ALLOWED_EXTENSIONS", ".tif,.TIF,.tiff,.jp2"),
        ("GDAL_HTTP_MAX_RETRY", "5"),
        ("GDAL_HTTP_RETRY_DELAY", "2"),
        ("GDAL_HTTP_MULTIPLEX", "YES"),
        ("GDAL_HTTP_VERSION", "2"),
        ("VSI_CACHE", "TRUE"),
        ("VSI_CACHE_SIZE", "100000000"),
        ("AWS_NO_SIGN_REQUEST", "YES"),
    ):
        gdal.SetConfigOption(clave, valor)


def _vsi_desde_href(href):
    """Convierte un href de asset STAC en una ruta VSI de GDAL."""
    if not href:
        return None
    href = str(href)
    if href.startswith("s3://"):
        gdal.SetConfigOption("AWS_REQUEST_PAYER", "requester")
        return "/vsis3/" + href[len("s3://"):]
    if href.startswith(("http://", "https://")):
        return "/vsicurl/" + href
    return href


def _resolver_asset_obj(assets, clave_logica, familia="s2"):
    """Objeto asset completo del primer alias presente, o None.

    Devuelve el diccionario entero y no solo el href porque ahí vienen los
    metadatos de radiometría (raster:bands) que hacen falta para convertir a
    reflectancia sin adivinar.
    """
    alias = alias_banda(clave_logica, familia)
    for nombre in alias:
        item = assets.get(nombre)
        if item and item.get("href"):
            return item
    return None


def _resolver_asset(assets, clave_logica, familia="s2"):
    """Href del primer alias presente, según la familia de sensor.

    Las claves lógicas son comunes a Sentinel-2 y Landsat; lo que cambia es el
    nombre del asset en cada catálogo (nir -> B08 en S2, nir08 en Landsat).
    """
    obj = _resolver_asset_obj(assets, clave_logica, familia)
    return obj.get("href") if obj else None


def _escala_offset(asset):
    """(escala, desplazamiento) declarados en raster:bands, o None.

    Es la fuente autoritativa de radiometría del ítem STAC y evita inferir el
    desplazamiento a partir de la línea base de proceso. Element84 lo documenta
    explícitamente: si el desplazamiento no es cero, se aplica DESPUÉS de la
    escala, es decir reflectancia = DN * escala + desplazamiento. Ojo con las
    unidades: el desplazamiento viene en reflectancia (-0.1), no en DN (-1000).
    """
    if not isinstance(asset, dict):
        return None
    bandas = asset.get("raster:bands")
    if not isinstance(bandas, list) or not bandas:
        return None
    primera = bandas[0]
    if not isinstance(primera, dict):
        return None
    escala = primera.get("scale")
    if escala is None:
        return None
    try:
        return float(escala), float(primera.get("offset") or 0.0)
    except (TypeError, ValueError):
        return None


# Margen antes de la caducidad del token a partir del cual se renueva.
TOKEN_MARGEN_RENOVACION = timedelta(minutes=5)


def _parsear_expiracion(texto):
    """Convierte 'msft:expiry' (ej. 2026-09-22T00:45:38Z) en datetime UTC.

    Devuelve None si falta o no se reconoce el formato: en ese caso no hay
    renovación automática, que es preferible a renovar a ciegas.
    """
    if not texto:
        return None
    for formato in ('%Y-%m-%dT%H:%M:%SZ', '%Y-%m-%dT%H:%M:%S.%fZ'):
        try:
            return datetime.strptime(str(texto), formato).replace(
                tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def _firmar_pc(href, token):
    """Anexa el token SAS de Planetary Computer al href."""
    if not token or not href:
        return href
    if "?" in href:
        return href + "&" + token
    return href + "?" + token


# --------------------------------------------------------------------------
# Post-procesador de realce
# --------------------------------------------------------------------------
# Los post-procesadores deben sobrevivir al fin de processAlgorithm: QGIS los
# invoca después, en el hilo principal. Sin esta lista el recolector de basura
# de Python los destruye y la capa se carga con el estilo por defecto.
#
# Como la lista es de módulo, persiste entre ejecuciones: sin purga, cada
# ejecución le suma un objeto por capa y nada los retira nunca. Doce índices
# más su amplitud son trece por corrida, y en una sesión de trabajo de un día
# se acumulan cientos, cada uno con su configuración de realce. La purga no
# puede ocurrir en postProcessAlgorithm porque QGIS carga las capas DESPUÉS
# de ese método (en el registro se ve «Cargando las capas resultantes» al
# final), así que cada post-procesador se marca como usado cuando corre y la
# ejecución siguiente descarta los marcados. Los de una corrida cancelada
# antes de cargar capas nunca se marcan; para esos actúa el tope.
_POSTPROCESADORES = []
_POSTPROCESADORES_TOPE = 400


def _cargar_pendientes(pendientes, context, feedback):
    """Carga capas de disco desde el HILO PRINCIPAL, e informa de cada una.

    Alternativa a context.addLayerToLoadOnCompletion para los productos que
    DEBEN verse al terminar. Ese registro delega la resolución de la fuente en
    mapLayerFromString, y cuando no resuelve no hay nada en el registro que lo
    explique: la capa simplemente no aparece, el archivo está en disco, y el
    usuario no tiene forma de saber si falló el cálculo o la carga. Aquí se
    construye la capa, se comprueba isValid(), se aplica el mismo
    post-procesador y se informa del resultado, de modo que «no se cargó» deja
    de ser un síntoma sin diagnóstico.

    Es el remedio que ya funcionó en 1.24.1 con las capas XYZ de Esri.

    Solo debe llamarse desde postProcessAlgorithm: añadir capas al proyecto
    desde processAlgorithm es tocar el proyecto desde un hilo secundario.
    """
    proyecto = None
    if context is not None:
        try:
            proyecto = context.project()
        except (RuntimeError, AttributeError) as e:
            feedback.pushDebugInfo(f"[cargar_pendientes] context.project: {e}")
    if proyecto is None:
        proyecto = QgsProject.instance()

    cargadas = 0
    for ruta, nombre, pp in pendientes:
        base = os.path.basename(ruta)
        try:
            if not os.path.exists(ruta):
                feedback.pushWarning(
                    f"[!] No se encontró {base}; no se carga.")
                continue
            capa = QgsRasterLayer(ruta, nombre)
            if not capa.isValid():
                detalle = ''
                try:
                    detalle = capa.error().summary()
                except (RuntimeError, AttributeError):
                    detalle = ''
                feedback.pushWarning(
                    f"[!] QGIS no pudo abrir {base} como ráster"
                    f"{': ' + detalle if detalle else ''}. El archivo está en "
                    f"disco: ábralo a mano para verlo.")
                continue
            if pp is not None:
                pp.postProcessLayer(capa, context, feedback)
            proyecto.addMapLayer(capa)
            cargadas += 1
        except (RuntimeError, OSError, TypeError,
                AttributeError, ValueError) as e:
            feedback.pushWarning(f"[!] No se pudo cargar {base}: {e}")
    if pendientes:
        feedback.pushInfo(
            f"Capas añadidas al proyecto: {cargadas} de {len(pendientes)}")
    return cargadas


def _registrar_postproc(pp):
    """Guarda un post-procesador y purga los de ejecuciones ya terminadas."""
    vivos = [x for x in _POSTPROCESADORES if not getattr(x, 'usado', False)]
    if len(vivos) > _POSTPROCESADORES_TOPE:
        # Huérfanos de corridas canceladas: se sueltan los más antiguos. El
        # tope está muy por encima de lo que produce una corrida normal, de
        # modo que nunca alcanza a los pendientes de la corrida en curso.
        vivos = vivos[-_POSTPROCESADORES_TOPE:]
    vivos.append(pp)
    _POSTPROCESADORES[:] = vivos


# --------------------------------------------------------------------------
# Scene Classification Layer (SCL) — Sentinel-2 L2A, 20 m
# --------------------------------------------------------------------------
SCL_NODATA = 0
SCL_SATURADO = 1
SCL_SOMBRA_NUBE = 3
SCL_NUBE_MEDIA = 8
SCL_NUBE_ALTA = 9
SCL_CIRRO = 10

# Clases que invalidan un píxel para análisis. La sombra de nube (3) se incluye
# deliberadamente: en terreno montañoso costarricense la sombra proyectada suele
# cubrir más área que la nube misma, y arruina igual cualquier índice espectral.
SCL_NUBOSAS = (SCL_SOMBRA_NUBE, SCL_NUBE_MEDIA, SCL_NUBE_ALTA, SCL_CIRRO)
SCL_SIN_DATO = (SCL_NODATA, SCL_SATURADO)

# Plantilla del consejo emergente (map tip) de la capa de huellas.
#
# CUIDADO: QGIS ejecuta replaceExpressionText() sobre el texto CRUDO antes de
# interpretarlo como HTML. Escribir &quot; dentro de [% %] rompe el análisis de
# la expresión y devuelve cadena vacía. Se usa attribute(@feature, 'campo') con
# comillas simples para evitar por completo el problema de las comillas.
PLANTILLA_MAPTIP = """<div style="font-family:sans-serif; font-size:11px; text-align:center">
<img src="[% attribute(@feature, 'thumb_url') %]" width="360"><br>
<b>[% attribute(@feature, 'id') %]</b><br>
[% attribute(@feature, 'fecha') %] &nbsp;|&nbsp; tile [% attribute(@feature, 'tile') %]<br>
Nubes escena: <b>[% round(attribute(@feature, 'nubes_pct'), 1) %] %</b>
&nbsp;|&nbsp; <span style="color:#b00">dentro del AOI: <b>[% round(attribute(@feature, 'nubes_aoi'), 1) %] %</b></span><br>  # noqa: E501
P&iacute;xeles &uacute;tiles en AOI: [% round(attribute(@feature, 'datos_pct'), 1) %] %
</div>"""


class HuellasPostProcessor(QgsProcessingLayerPostProcessorInterface):
    """Configura la capa de huellas para revisión visual.

    Activa el consejo emergente con la miniatura, declara el campo `thumb`
    como recurso externo (visor de imagen en el formulario de atributos) y
    fija la expresión de visualización. Corre en el hilo principal.
    """

    # Puesto a True cuando QGIS ya invocó postProcessLayer: la ejecución
    # siguiente usa esta marca para soltar el objeto (ver _registrar_postproc).
    usado = False

    def postProcessLayer(self, capa, context, feedback):
        try:
            if capa is None or not capa.isValid():
                return
            capa.setMapTipTemplate(PLANTILLA_MAPTIP)
            # QGIS >= 3.32 apaga los consejos por capa de forma predeterminada.
            if hasattr(capa, 'setMapTipsEnabled'):
                capa.setMapTipsEnabled(True)
            capa.setDisplayExpression(
                '"fecha" || \'  —  \' || round("nubes_aoi", 1) || \' % nubes AOI\'')

            idx = capa.fields().indexOf('thumb')
            if idx >= 0:
                config = {
                    'DocumentViewer': 1,        # 1 = imagen
                    'DocumentViewerHeight': 300,
                    'DocumentViewerWidth': 0,
                    'RelativeStorage': 0,       # 0 = ruta absoluta
                    'FileWidget': True,
                    'FileWidgetButton': True,
                    'UseLink': False,
                    'StorageMode': 0,
                }
                capa.setEditorWidgetSetup(
                    idx, QgsEditorWidgetSetup('ExternalResource', config))
            capa.triggerRepaint()
        except Exception as e:
            feedback.pushDebugInfo(f"[huellas_postproc] {e}")
        finally:
            # En finally y no al final del try: un fallo aquí no debe dejar el
            # objeto retenido para siempre en _POSTPROCESADORES.
            self.usado = True


# --------------------------------------------------------------------------
# Compatibilidad de enums de PyQGIS
# --------------------------------------------------------------------------
def _enum_qgis(clase, contenedor, nombre, respaldo):
    """Resuelve un enum tanto en forma anidada como plana.

    En la migración a Qt6 varios enums de QGIS pasaron de ser atributos planos
    de la clase (QgsRasterMinMaxOrigin.WholeRaster) a estar anidados en su
    propio tipo (QgsRasterMinMaxOrigin.Extent.WholeRaster), y el cambio no fue
    simultáneo para todos: en una misma versión puede haber unos planos y otros
    anidados. Se prueban ambas formas y, si ninguna existe, se usa el valor
    numérico, que es estable porque forma parte de la API binaria.
    """
    tipo = getattr(clase, contenedor, None)
    valor = getattr(tipo, nombre, None) if tipo is not None else None
    if valor is None:
        valor = getattr(clase, nombre, None)
    return respaldo if valor is None else valor


_MMO_MINMAX = _enum_qgis(QgsRasterMinMaxOrigin, 'Limits', 'MinMax', 1)
_MMO_STDDEV = _enum_qgis(QgsRasterMinMaxOrigin, 'Limits', 'StdDev', 2)
_MMO_CUMCUT = _enum_qgis(QgsRasterMinMaxOrigin, 'Limits', 'CumulativeCut', 3)
_MMO_WHOLE = _enum_qgis(QgsRasterMinMaxOrigin, 'Extent', 'WholeRaster', 0)
_MMO_UPDATED = _enum_qgis(QgsRasterMinMaxOrigin, 'Extent', 'UpdatedCanvas', 2)
_CE_MINMAX = _enum_qgis(
    QgsContrastEnhancement, 'ContrastEnhancementAlgorithm',
    'StretchToMinimumMaximum', 1)
_SHADER_INTERP = _enum_qgis(QgsColorRampShader, 'Type', 'Interpolated', 0)
_SHADER_DISCRETE = _enum_qgis(QgsColorRampShader, 'Type', 'Discrete', 1)

# Estos dos se usaban en forma PLANA y sin respaldo, que es el caso que
# rompe al cambiar de versión. Los encontró tools/api_audit.py, no una
# lectura a mano: la auditoría lista cada constante con punto y deja a la
# vista cuáles no pasan por el resolutor.
#   - QgsWkbTypes.Polygon  -> QgsWkbTypes.Type.Polygon en Qt6. El valor 3
#     es el código WKB de polígono y no cambia: forma parte del formato.
#   - QgsProcessing.TypeVectorPolygon -> QgsProcessing.SourceType.*. Aquí
#     el respaldo es TypeVector (cualquier vectorial) en vez de un número:
#     si la constante específica desapareciera, aceptar cualquier capa
#     vectorial degrada la ayuda del parámetro pero deja el algoritmo
#     utilizable, mientras que un número equivocado filtraría la lista de
#     capas y parecería que el proyecto no tiene polígonos.
_WKB_POLIGONO = _enum_qgis(QgsWkbTypes, 'Type', 'Polygon', 3)
_TIPO_POLIGONO = _enum_qgis(
    QgsProcessing, 'SourceType', 'TypeVectorPolygon',
    _enum_qgis(QgsProcessing, 'SourceType', 'TypeVector', 5))


# --------------------------------------------------------------------------
# Paletas para capas monobanda
# --------------------------------------------------------------------------
# Rampas divergentes de ColorBrewer, seguras para daltonismo. Se evitan a
# propósito las rampas tipo arcoíris (Spectral, turbo): su espaciado
# perceptual no es uniforme, de modo que inventan bordes en las transiciones a
# verde y amarillo y ocultan variación dentro del azul; además la pareja
# verde-rojo es la que se pierde en deuteranopía.
#
# Las paradas se dan como (posición 0-1, color). La posición 0.5 es el color
# neutro y se ancla en el valor 0 siempre que el cero caiga dentro del rango
# mostrado — no en la media de la imagen, que cambiaría de escena a escena.
MODOS_CLASE = [
    "Continuo (gradiente)",
    "Intervalo igual (clases discretas)",
]

PALETAS_MONOBANDA = [
    ("Gris (sin paleta)", None),
    ("Divergente azul–rojo (azul = húmedo, rojo = seco)", (
        (0.000, "#a50026"), (0.167, "#d73027"), (0.333, "#fdae61"),
        (0.500, "#ffffbf"),
        (0.667, "#abd9e9"), (0.833, "#4575b4"), (1.000, "#313695"),
    )),
    ("Divergente marrón–verde (marrón = seco, verde = húmedo)", (
        (0.000, "#8c510a"), (0.167, "#bf812d"), (0.333, "#dfc27d"),
        (0.500, "#f5f5f5"),
        (0.667, "#80cdc1"), (0.833, "#35978f"), (1.000, "#01665e"),
    )),
]


class RealcePostProcessor(QgsProcessingLayerPostProcessorInterface):
    """Realce y visibilidad inicial de las capas ráster que se cargan.

    Atiende tanto composiciones RGB de tres bandas como capas monobanda
    (índices espectrales, amplitud fenológica). La versión anterior salía por
    un return cuando bandCount() < 3, de modo que las capas de índice se
    cargaban sin realce y QGIS les aplicaba su estiramiento mínimo-máximo por
    defecto: un par de píxeles atípicos aplastaban todo el rango útil.

    Se ejecuta en el hilo principal (contrato de la interfaz), por lo que es
    seguro tocar el renderizador y el árbol de capas aquí — no lo sería desde
    processAlgorithm().
    """

    def __init__(self, extension_calculo, visible=False, metodo=1,
                 n_sigma=2.0, paleta=0, modo_clase=0, n_clases=6,
                 limites_fijos=None):
        super().__init__()
        # WholeRaster para recortes locales pequeños; UpdatedCanvas para
        # fuentes remotas, donde leer la teselá completa por HTTP es inviable.
        self.extension_calculo = extension_calculo
        self.visible = bool(visible)
        self.metodo = int(metodo)
        self.n_sigma = float(n_sigma)
        self.paleta = int(paleta)
        self.modo_clase = int(modo_clase)
        self.n_clases = int(n_clases)
        # (mín, máx) declarados por el usuario, o None.
        self.limites_fijos = limites_fijos

    # Puesto a True cuando QGIS ya invocó postProcessLayer: la ejecución
    # siguiente usa esta marca para soltar el objeto (ver _registrar_postproc).
    usado = False

    def postProcessLayer(self, capa, context, feedback):
        try:
            if capa is None or not capa.isValid():
                return

            # La visibilidad se fija antes que nada: aplica a toda capa
            # cargada, monobanda o RGB.
            if not self.visible:
                self._ocultar(capa, feedback)

            origen = QgsRasterMinMaxOrigin()
            # El corte acumulado colapsa cuando el recorte tiene muchos nodata
            # o nube: los percentiles 2 y 98 se juntan y la capa sale negra.
            # Media ± N·sigma se apoya en la distribución y no en los extremos.
            if self.metodo == 1:
                limites = _MMO_STDDEV
                origen.setLimits(limites)
                origen.setStdDevFactor(self.n_sigma)
            elif self.metodo == 2:
                limites = _MMO_MINMAX
                origen.setLimits(limites)
            else:
                limites = _MMO_CUMCUT
                origen.setLimits(limites)
                origen.setCumulativeCutLower(0.02)
                origen.setCumulativeCutUpper(0.98)

            if self.extension_calculo == 'whole':
                origen.setExtent(_MMO_WHOLE)
            else:
                # Fuente remota: recalcular sobre el lienzo visible. Leer la
                # teselá completa (10980²) por HTTP bloquearía QGIS.
                origen.setExtent(_MMO_UPDATED)
            if capa.bandCount() >= 3:
                renderer = QgsMultiBandColorRenderer(
                    capa.dataProvider(), 1, 2, 3)
                renderer.setMinMaxOrigin(origen)
                capa.setRenderer(renderer)
                capa.setContrastEnhancement(_CE_MINMAX, limites)
            elif self.paleta > 0 and self._aplicar_paleta(capa, feedback):
                pass
            else:
                renderer = QgsSingleBandGrayRenderer(capa.dataProvider(), 1)
                renderer.setMinMaxOrigin(origen)
                capa.setRenderer(renderer)
                capa.setContrastEnhancement(_CE_MINMAX, limites)
            capa.triggerRepaint()
        except Exception as e:
            feedback.pushDebugInfo(f"[realce] {e}")
        finally:
            self.usado = True

    def _limites_monobanda(self, capa):
        """(mín, máx) de visualización.

        Los límites fijos declarados por el usuario tienen prioridad: son los
        que permiten comparar fechas, porque no dependen de la imagen.
        """
        if self.limites_fijos:
            return self.limites_fijos

        # bandStatistics(1) ya calcula todo por defecto; pedir
        # QgsRasterBandStats.All explícitamente añadiría otro enum frágil.
        stats = capa.dataProvider().bandStatistics(1)
        if self.metodo == 1 and stats.stdDev > 0:
            return (stats.mean - self.n_sigma * stats.stdDev,
                    stats.mean + self.n_sigma * stats.stdDev)
        return stats.minimumValue, stats.maximumValue

    def _aplicar_paleta(self, capa, feedback):
        """Pseudocolor divergente con el neutro anclado en cero.

        El anclaje es la razón de ser de este método. Con una rampa divergente
        estirada sin más, el color neutro cae en la media de la imagen y no en
        un valor con significado: dos fechas del mismo sitio se verían
        distintas sin que el terreno haya cambiado. Aquí las paradas por debajo
        de 0.5 se reparten entre el mínimo y 0, y las de arriba entre 0 y el
        máximo, de modo que el neutro marca siempre el cero.
        """
        try:
            paradas = PALETAS_MONOBANDA[self.paleta][1]
            if not paradas:
                return False
            lo, hi = self._limites_monobanda(capa)
            if not (hi > lo):
                return False

            if self.modo_clase == 1:
                items = self._items_intervalo_igual(paradas, lo, hi, feedback)
                rampa = QgsColorRampShader(lo, hi)
                rampa.setColorRampType(_SHADER_DISCRETE)
                rampa.setColorRampItemList(items)
                sombreador = QgsRasterShader()
                sombreador.setRasterShaderFunction(rampa)
                capa.setRenderer(QgsSingleBandPseudoColorRenderer(
                    capa.dataProvider(), 1, sombreador))
                return True

            ancla_cero = lo < 0.0 < hi
            items = []
            for posicion, hexa in paradas:
                if ancla_cero:
                    if posicion <= 0.5:
                        valor = lo + (posicion / 0.5) * (0.0 - lo)
                    else:
                        valor = 0.0 + ((posicion - 0.5) / 0.5) * (hi - 0.0)
                else:
                    valor = lo + posicion * (hi - lo)
                items.append(QgsColorRampShader.ColorRampItem(
                    float(valor), QColor(hexa), f"{valor:.3f}"))

            rampa = QgsColorRampShader(lo, hi)
            rampa.setColorRampType(_SHADER_INTERP)
            rampa.setColorRampItemList(items)

            sombreador = QgsRasterShader()
            sombreador.setRasterShaderFunction(rampa)
            capa.setRenderer(QgsSingleBandPseudoColorRenderer(
                capa.dataProvider(), 1, sombreador))
            return True
        except Exception as e:
            feedback.pushDebugInfo(f"[paleta] {e}")
            return False

    @staticmethod
    def _color_en(paradas, t):
        """Color de la paleta en la posición t (0-1), interpolando entre paradas."""
        t = min(max(float(t), 0.0), 1.0)
        for i in range(len(paradas) - 1):
            p0, hex0 = paradas[i]
            p1, hex1 = paradas[i + 1]
            if p0 <= t <= p1:
                f = 0.0 if p1 == p0 else (t - p0) / (p1 - p0)
                c0, c1 = QColor(hex0), QColor(hex1)
                return QColor(
                    int(round(c0.red() + (c1.red() - c0.red()) * f)),
                    int(round(c0.green() + (c1.green() - c0.green()) * f)),
                    int(round(c0.blue() + (c1.blue() - c0.blue()) * f)))
        return QColor(paradas[-1][1])

    def _items_intervalo_igual(self, paradas, lo, hi, feedback):
        """Clases discretas de igual anchura entre lo y hi.

        En modo discreto cada elemento lleva el límite SUPERIOR de su clase, y
        el color se toma del centro de la clase dentro de la paleta.

        No se ancla el cero: hacerlo rompería la igualdad de anchura, que es
        toda la razón de ser de este modo. En su lugar se comprueba si el cero
        cae sobre un borde y se avisa cuando no, porque entonces el color
        neutro deja de marcar la frontera con significado físico.
        """
        n = max(3, int(self.n_clases))
        ancho = (hi - lo) / n
        items = []
        for i in range(n):
            superior = lo + (i + 1) * ancho
            inferior = lo + i * ancho
            color = self._color_en(paradas, (i + 0.5) / n)
            items.append(QgsColorRampShader.ColorRampItem(
                float(superior), color, f"{inferior:.3f} – {superior:.3f}"))

        if lo < 0.0 < hi:
            bordes = [lo + k * ancho for k in range(n + 1)]
            desvio = min(abs(b) for b in bordes)
            if desvio > ancho * 0.05:
                feedback.pushWarning(
                    f"[!] Con límites {lo:.3f}–{hi:.3f} y {n} clases, el cero "
                    f"no cae sobre un borde (queda a {desvio:.3f} del más "
                    f"próximo, sobre clases de {ancho:.3f}). El color neutro "
                    f"deja de marcar la frontera física. Ajuste los límites o "
                    f"el número de clases para que el cero coincida con un "
                    f"borde.")
        return items

    def _ocultar(self, capa, feedback, intento=0):
        """Desmarca la capa en el árbol.

        El nodo del árbol puede no existir todavía cuando corre el
        post-procesador: QGIS añade la capa al proyecto y la inserta en el
        árbol en pasos distintos. Si falta, se reintenta con singleShot(0),
        que vuelve a ejecutarse tras procesar la cola de eventos — sigue
        siendo el hilo principal, así que es seguro.
        """
        try:
            nodo = QgsProject.instance().layerTreeRoot().findLayer(capa.id())
            if nodo is not None:
                nodo.setItemVisibilityChecked(False)
                return
            if intento < 5:
                QTimer.singleShot(
                    0, lambda: self._ocultar(capa, feedback, intento + 1))
            else:
                feedback.pushDebugInfo(
                    f"[ocultar] No se halló el nodo de {capa.name()} tras "
                    f"{intento} intentos.")
        except Exception as e:
            feedback.pushDebugInfo(f"[ocultar] {e}")


def _url_archivo(ruta):
    """Convierte una ruta local en URL file:// válida para QTextBrowser.

    QUrl.fromLocalFile resuelve las diferencias entre plataformas: en Windows
    C:\\datos\\a.png -> file:///C:/datos/a.png (tres barras), en Linux
    /home/a.png -> file:///home/a.png. Concatenar 'file:///' a mano falla en
    una de las dos.
    """
    if not ruta:
        return ''
    try:
        return QUrl.fromLocalFile(str(ruta)).toString()
    except Exception:
        return str(ruta).replace('\\', '/')


def _geom_desde_geojson(gj):
    """Construye una QgsGeometry desde la geometría GeoJSON de un item STAC."""
    tipo = (gj or {}).get("type")
    coords = (gj or {}).get("coordinates")
    if tipo == "Polygon" and coords:
        anillos = [[QgsPointXY(float(c[0]), float(c[1])) for c in anillo]
                   for anillo in coords]
        return QgsGeometry.fromPolygonXY(anillos)
    if tipo == "MultiPolygon" and coords:
        poligonos = [[[QgsPointXY(float(c[0]), float(c[1])) for c in anillo]
                      for anillo in poli] for poli in coords]
        return QgsGeometry.fromMultiPolygonXY(poligonos)
    return QgsGeometry()


# --------------------------------------------------------------------------
# Algoritmo
# --------------------------------------------------------------------------
class BuscarSentinel2Algorithm(QgsProcessingAlgorithm):

    VERSION = 'v1.1.0'

    # Lógica pura, definida en core.py y reenganchada aquí como
    # staticmethod. Así cada sitio de llamada sigue siendo
    # «self._foo(...)»: el traslado no toco ninguno, que es donde se
    # cuelan los errores de un refactor.
    _tamano_salida = staticmethod(_tamano_salida)
    _destino_sumidero = staticmethod(_destino_sumidero)
    _salida_volatil = staticmethod(_salida_volatil)
    _procedencia_huellas = staticmethod(_procedencia_huellas)
    _items_desde_capa = staticmethod(_items_desde_capa)
    _memoria_estimada = staticmethod(_memoria_estimada)
    _agrupar_por_periodo = staticmethod(_agrupar_por_periodo)
    _periodos_sin_datos = staticmethod(_periodos_sin_datos)
    _reducir_por_periodo = staticmethod(_reducir_por_periodo)
    _mascaras_qa_pixel = staticmethod(_mascaras_qa_pixel)
    _acotar_indice = staticmethod(_acotar_indice)
    _validar_ortonormalidad = staticmethod(_validar_ortonormalidad)
    _escribir_plantilla_tc = staticmethod(_escribir_plantilla_tc)
    _parsear_meses = staticmethod(_parsear_meses)
    _parsear_limites = staticmethod(_parsear_limites)
    _codigo_meses = staticmethod(_codigo_meses)

    # Familia de sensor activa ('s2' | 'ls'); se fija en processAlgorithm.
    _familia = 's2'
    # Ruta de la hoja de contactos y si hay que abrirla al terminar.
    _ruta_hoja = None
    _abrir_hoja = False
    _resultados = None
    # Registro de nombres de archivo de la ejecución en curso.
    _cache_nombres = None
    _duenos_nombre = None
    # Tabla de coeficientes Tasseled Cap en uso (interna o cargada).
    _coef_tc = None
    # Orígenes de radiometría ya informados en esta ejecución.
    _escala_reportada = None
    # Realce aplicado a las capas cargadas.
    _metodo_realce = 1
    _n_sigma = 2.0
    _paleta = 1
    _modo_clase = 0
    _n_clases = 6
    _limites_fijos = None
    # Colección STAC en uso, para los metadatos de procedencia.
    _coleccion_activa = None
    # Token SAS de Planetary Computer y su caducidad (UTC).
    _pc_token = None
    _pc_expira = None
    _pc_coleccion = None
    # Índices escritos: (ruta, mes, sufijo, fecha AAAA-MM-DD).
    _indices_escritos = None
    # Amplitudes a cargar en postProcessAlgorithm. Ver
    # _cargar_pendientes: addLayerToLoadOnCompletion no las
    # mostraba y no dejaba rastro de por que.
    _pendientes_amplitud = None

    EXTENSION = 'EXTENSION'
    AOI = 'AOI'
    AREA = 'AREA'
    CATALOGO = 'CATALOGO'
    FECHA_INI = 'FECHA_INI'
    FECHA_FIN = 'FECHA_FIN'
    NUBOSIDAD = 'NUBOSIDAD'
    NUBES_AOI = 'NUBES_AOI'
    MINIATURAS = 'MINIATURAS'
    ABRIR = 'ABRIR'
    PX_MINIATURA = 'PX_MINIATURA'
    SELECCION = 'SELECCION'
    MAX_ESCENAS = 'MAX_ESCENAS'
    ORDEN = 'ORDEN'
    CANDIDATOS = 'CANDIDATOS'
    MODO = 'MODO'
    COMPOSICION = 'COMPOSICION'
    ESTIRAR = 'ESTIRAR'
    INDICES = 'INDICES'
    AMPLITUD = 'AMPLITUD'
    MIN_OBS = 'MIN_OBS'
    MESES_SECA = 'MESES_SECA'
    MESES_LLUVIA = 'MESES_LLUVIA'
    COEF_TC = 'COEF_TC'
    VISIBLES = 'VISIBLES'
    REALCE = 'REALCE'
    REALCE_N = 'REALCE_N'
    PALETA = 'PALETA'
    CLASES = 'CLASES'
    N_CLASES = 'N_CLASES'
    LIMITES = 'LIMITES'
    BANDAS = 'BANDAS'
    CARPETA = 'CARPETA'
    SALIDA = 'SALIDA'
    HUELLAS = 'HUELLAS'

    def tr(self, cadena):
        return QCoreApplication.translate('BuscarSentinel2', cadena)

    def createInstance(self):
        return BuscarSentinel2Algorithm()

    def name(self):
        # Se conserva el identificador original para no romper modelos
        # de Processing que ya referencien este algoritmo.
        return 'buscarsentinel2stac'

    def displayName(self):
        return self.tr('Buscar y descargar Sentinel-2 / Landsat (STAC / COG)')

    def icon(self):
        """Icono del algoritmo en la Caja de Herramientas.

        Se busca icon.png junto a este módulo. Ejecutado como script suelto
        el archivo no existe y QGIS usa su icono genérico: un QIcon vacío es
        válido, no lanza.
        """
        ruta = os.path.join(os.path.dirname(__file__), 'icon.png')
        return QIcon(ruta) if os.path.exists(ruta) else QIcon()

    def group(self):
        return self.tr('Teledetección')

    def groupId(self):
        return 'teledeteccion'

    def shortHelpString(self):
        return self.tr(
            f"<b>Buscar y descargar Sentinel-2 / Landsat (STAC / COG)</b> — {self.VERSION}<br><br>"
            "Consulta catálogos STAC públicos y devuelve una capa de huellas con "
            "identificador, fecha, nubosidad, tile MGRS y URL de cada escena.<br><br>"
            "<b>Modos:</b><br>"
            "• <i>Solo catálogo</i>: únicamente la capa de huellas. Útil para "
            "explorar disponibilidad antes de transferir datos.<br>"
            "• <i>Cargar assets remotos</i>: añade las bandas como capas "
            "ráster leídas por HTTP mediante /vsicurl/. No ocupa disco; QGIS "
            "solo descarga los bloques visibles.<br>"
            "• <i>Descargar recorte</i>: escribe un GeoTIFF comprimido por banda "
            "y escena, recortado al AOI.<br><br>"
            "<b>Sensores:</b> Sentinel-2 L2A (10-20 m, desde 2017) y Landsat Collection 2 Nivel 2 (30 m, desde 1982). Las claves de banda son comunes a ambos, de modo que las composiciones RGB funcionan igual; Landsat no tiene red edge ni asset TCI, así que la vista previa se compone desde R/G/B y esas composiciones quedan vetadas. La nubosidad dentro del AOI se mide con SCL en Sentinel-2 y con los bits de QA_PIXEL en Landsat.<br><br>"  # noqa: E501
            "<b>Flujo recomendado en Costa Rica (nubosidad alta):</b><br>"
            "1. Ejecute en modo <i>Solo catálogo</i> con miniaturas activadas "
            "y nubosidad de escena permisiva (50-70 %). Marque «Capa de "
            "huellas» y <b>dele un archivo GeoPackage</b>, no una capa "
            "temporal: la temporal desaparece al cerrar QGIS y se lleva las "
            "URL que necesita el paso 3.<br>"
            "2. Revise la capa de huellas: el consejo emergente muestra la "
            "vista previa recortada a su AOI y la nubosidad real dentro de "
            "él. Ordene la tabla por <code>nubes_aoi</code> ascendente — "
            "ése es el número que importa, no <code>nubes_pct</code>, que es "
            "de la tesela de 110 km entera.<br>"
            "3. Quédese con las escenas útiles, <b>seleccionándolas</b> "
            "(marcando después «Entidades seleccionadas solamente») o "
            "<b>borrando de la tabla de atributos</b> las filas que no quiera "
            "y guardando la capa. Vuelva a ejecutar indicándola en «Huellas "
            "ya revisadas», esta vez en modo descarga o composición.<br>"
            "&nbsp;&nbsp;La vía de borrar filas deja un GeoPackage que "
            "documenta exactamente qué escenas usó el estudio, reejecutable "
            "meses después y transferible a otra persona; una selección se "
            "pierde al cerrar el proyecto. Para trabajo que haya que "
            "defender o repetir, use ésa.<br>"
            "&nbsp;&nbsp;Conserve el <b>mismo catálogo</b> en los dos pasos. "
            "Se verifica solo: la capa guarda su endpoint en "
            "el campo «catalogo» y la ejecución se rechaza si no coincide, "
            "porque «sentinel-2-l2a» existe en Earth Search y en Planetary "
            "Computer y las URL de una no sirven en la otra.<br><br>"
            "<b>Composiciones RGB:</b> si selecciona una (infrarrojo color, "
            "agricultura, SWIR…), el algoritmo apila las tres bandas en un VRT "
            "multibanda y aplica realce por percentiles 2-98 al cargar. En modo "
            "remoto el VRT no descarga píxeles; en modo descarga produce un "
            "único GeoTIFF de 3 bandas por escena.<br><br>"
            "<b>Notas:</b><br>"
            "• La extensión se reproyecta a EPSG:4326 para la consulta STAC; "
            "los recortes conservan el SRC nativo de la escena (UTM 16N/17N en "
            "Costa Rica).<br>"
            "• La capa de AOI (opcional) se usa como <i>cutline</i> y tiene "
            "prioridad sobre la extensión.<br>"
            "• Earth Search no requiere credenciales. Planetary Computer solicita "
            "un token SAS anónimo que caduca a los ~45 minutos; el algoritmo lo "
            "renueva solo durante la ejecución. En modo remoto, en cambio, las "
            "capas dejan de cargar pasado ese tiempo.<br><br>"
            "<b>Límite de memoria (importante).</b> Los índices y la amplitud "
            "se calculan cargando cada banda completa en memoria, sin proceso "
            "por bloques. Antes de descargar nada, el algoritmo estima cuánta "
            "memoria haría falta y rechaza la ejecución si pasa de "
            f"{MEMORIA_MAX_GIB:.0f} GiB, con el tamaño en píxeles en el "
            "mensaje. Tenga presente que <b>la extensión por omisión abarca "
            "todo Costa Rica</b>: ejecutarla sin tocar, con índices y "
            "amplitud, pediría unos 40 GiB. Indique una capa de AOI o encuadre "
            "la extensión en el lienzo. Un lote de 150 ha ocupa menos de 2 MB; "
            "un cuadrado de 100 km cabe de sobra.<br><br>"
            "<b>Control de calidad de los índices.</b> Cada índice se recorta "
            "a su rango físico (NDVI, NDMI, NBR y NDRE entre −1 y 1; MSI "
            "entre 0 y 10) y los píxeles fuera de rango salen como NaN con un "
            "aviso que dice cuántos fueron. Un porcentaje alto indica bandas "
            "corruptas o un escalado mal declarado en el catálogo, no un "
            "resultado a interpretar.<br><br>"
            "<b>Si la amplitud sale vacía</b>, el aviso distingue la causa: "
            "una estación entera nublada, «Mínimo de observaciones por píxel» "
            "demasiado alto (indica el valor que sí funcionaría), o escenas "
            "que no cubren el mismo trozo de AOI en las dos estaciones."
            "<hr>"
            f"<b>Autor:</b> {AUTOR} &lt;{AUTOR_EMAIL}&gt;<br>"
            f"<b>Versión:</b> {self.VERSION} · <b>Licencia:</b> GPL v2 o "
            f"posterior<br>"
            "<b>Datos:</b> Copernicus Sentinel-2 (ESA) y Landsat "
            "Collection 2 (USGS/NASA), de acceso abierto. Cite la fuente de "
            "las imágenes en cualquier producto derivado."
        )

    # ---------------------------------------------------------------- params
    def initAlgorithm(self, config=None):
        p = QgsProcessingParameterExtent(
            self.EXTENSION,
            self.tr('Extensión de búsqueda'),
            defaultValue='-86.0,-82.5,8.0,11.3 [EPSG:4326]'
        )
        p.setHelp(self.tr(
            'Valor por defecto: Costa Rica continental — sirve para explorar '
            'el catálogo, pero es demasiado grande para calcular índices: el '
            'algoritmo rechazaría la ejecución por memoria. Para trabajar, '
            'encuadre en el lienzo y use «Usar extensión del lienzo del mapa», '
            'dibuje el rectángulo, o indique una capa de AOI y deje «Área de '
            'búsqueda» en «Capa de AOI».'))
        self.addParameter(p)

        p = QgsProcessingParameterFeatureSource(
            self.AOI,
            self.tr('Capa de AOI para recorte (opcional)'),
            [_TIPO_POLIGONO],
            optional=True
        )
        p.setHelp(self.tr(
            'Se usa siempre como <i>cutline</i> del recorte. Además, si «Área '
            'de búsqueda» está en «Capa de AOI», su envolvente define el bbox '
            'de la consulta STAC, las estadísticas de nube y las miniaturas — '
            'no solo el corte final. Es la forma recomendada de acotar el '
            'trabajo: con ella la estimación de memoria sale del tamaño real '
            'del lote y no de la extensión por omisión.'))
        self.addParameter(p)

        p = QgsProcessingParameterEnum(
            self.AREA, self.tr('Área de búsqueda'),
            options=AREAS, defaultValue=0)
        p.setHelp(self.tr(
            'Decide qué manda cuando hay capa de AOI y extensión a la vez.<br>'
            '<b>Capa de AOI</b>: el área de trabajo es la envolvente de la '
            'capa. Es lo adecuado para un lote concreto.<br>'
            '<b>Extensión / lienzo</b>: manda el rectángulo indicado abajo. '
            'El botón «…» de ese parámetro ofrece «Usar extensión del lienzo '
            'del mapa» y «Dibujar en el lienzo», de modo que puede encuadrar '
            'en QGIS y buscar justo lo que ve. La capa de AOI, si la hay, '
            'se sigue usando como recorte en la descarga.'))
        self.addParameter(p)

        p = QgsProcessingParameterEnum(
            self.CATALOGO,
            self.tr('Catálogo STAC'),
            options=[c[0] for c in CATALOGOS],
            defaultValue=0)
        p.setHelp(self.tr(
            'Las tres opciones de Sentinel-2 entregan el mismo producto de ESA '
            '(L2A, reflectancia BOA por Sen2Cor); lo que cambia es el '
            'tratamiento de la línea base de proceso.<br><br>'
            '<b>sentinel-2-l2a (Earth Search)</b>: el archivo tal como se '
            'procesó en su momento, con líneas base mezcladas. Mayor cobertura '
            'histórica. Expone escala y desplazamiento en raster:bands.<br>'
            '<b>sentinel-2-c1-l2a (Colección 1)</b>: ESA reprocesó el archivo '
            'con línea base uniforme y calibración mejorada, pensado para '
            'series temporales. No elimina el desplazamiento de −1000: lo '
            'aplica igual en todas las fechas, que es lo que da la '
            'consistencia. El reprocesamiento se publica de forma progresiva, '
            'así que puede faltar cobertura antigua.<br>'
            '<b>sentinel-2-l2a (Planetary Computer)</b>: sin armonizar; '
            'Microsoft documenta que el cambio de enero de 2022 afecta a sus '
            'datos y deja la corrección al usuario. Es el único que además '
            'sirve Landsat sin coste.<br><br>'
            'Para análisis multitemporal que cruce enero de 2022, use la '
            'Colección 1: evita que el cambio de línea base aparezca como un '
            'salto espurio de cobertura.'))
        self.addParameter(p)

        p = QgsProcessingParameterString(
            self.FECHA_INI, self.tr('Fecha inicial (AAAA-MM-DD)'),
            defaultValue='2025-01-01')
        p.setHelp(self.tr(
            'Extremo inicial del intervalo, <b>incluido</b>. Se compara contra '
            'la fecha de toma que declara el catálogo, en UTC: una escena '
            'captada al final del día en hora local puede figurar con la fecha '
            'del día siguiente.<br><br>'
            'Un intervalo anterior a la misión devuelve cero escenas sin que '
            'eso sea un error: Landsat 8 empieza en 2013, Sentinel-2 en 2015 y '
            'Landsat 9 en 2021. La disponibilidad de productos corregidos a '
            'superficie arranca más tarde y depende de la colección.'))
        self.addParameter(p)

        p = QgsProcessingParameterString(
            self.FECHA_FIN, self.tr('Fecha final (AAAA-MM-DD)'),
            defaultValue=datetime.utcnow().strftime('%Y-%m-%d'))
        p.setHelp(self.tr(
            'Extremo final del intervalo, <b>incluido</b>. Por omisión es hoy '
            'en UTC. No puede ser anterior a la inicial; si lo es, el '
            'algoritmo se detiene antes de buscar.<br><br>'
            'Los últimos días suelen venir vacíos aunque la escena ya exista: '
            'el producto corregido a superficie se publica con retraso '
            'respecto a la toma. Para confirmar una fecha reciente, consulte '
            'primero en modo «Solo catálogo».'))
        self.addParameter(p)

        p = QgsProcessingParameterNumber(
            self.NUBOSIDAD, self.tr('Nubosidad máxima de la ESCENA COMPLETA (%)'),
            QgsProcessingParameterNumber.Double,
            defaultValue=70.0, minValue=0.0, maxValue=100.0)
        p.setHelp(self.tr(
            'ATENCIÓN: este filtro se aplica en el servidor sobre '
            'eo:cloud_cover, que mide la nube de toda la teselá de 110 x 110 km. '
            'Una escena descartada aquí NUNCA llega a evaluarse sobre su AOI. '
            'En la vertiente pacífica con estación lluviosa de mayo a noviembre, '
            'un umbral de 20 % elimina años enteros aunque su AOI estuviera '
            'despejado. Déjelo alto (70-90 %) y filtre después con «Nubosidad '
            'máxima DENTRO del AOI», que sí mide lo que importa.'))
        self.addParameter(p)

        p = QgsProcessingParameterNumber(
            self.NUBES_AOI, self.tr('Nubosidad máxima DENTRO del AOI (%)'),
            QgsProcessingParameterNumber.Double,
            defaultValue=100.0, minValue=0.0, maxValue=100.0)
        p.setHelp(self.tr(
            'Se calcula leyendo la banda SCL recortada al AOI: cuenta nube '
            'media, nube alta, cirro y sombra de nube sobre el total de '
            'píxeles con dato. Es el filtro que realmente importa — una '
            'escena con 60 % de nubes puede estar limpia sobre su área de '
            'estudio. 100 = desactivado (no se lee SCL, la ejecución es más '
            'rápida pero el campo nubes_aoi queda en -1).'))
        self.addParameter(p)

        p = QgsProcessingParameterBoolean(
            self.MINIATURAS,
            self.tr('Generar miniaturas PNG para revisión visual'),
            defaultValue=True)
        p.setHelp(self.tr(
            'Recorta el asset TCI al AOI y escribe un PNG por escena en '
            '<carpeta>/miniaturas. La capa de huellas resultante muestra la '
            'imagen en el consejo emergente y en el formulario de atributos. '
            'Requiere carpeta de salida.'))
        self.addParameter(p)

        p = QgsProcessingParameterNumber(
            self.PX_MINIATURA, self.tr('Ancho de miniatura (píxeles)'),
            QgsProcessingParameterNumber.Integer,
            defaultValue=512, minValue=128, maxValue=2048)
        p.setHelp(self.tr(
            'Solo tiene efecto si se piden miniaturas. El alto se deduce de la '
            'proporción del área, con corrección por cos(latitud), de modo que '
            'la vista previa no sale estirada.<br><br>'
            'Cada escena cuesta un recorte y una conversión a PNG, así que '
            'subir este valor alarga la búsqueda y engorda la carpeta. 512 '
            'basta para el consejo emergente y el formulario de atributos; '
            'solo conviene más si la miniatura va a ir en una composición '
            'impresa.'))
        self.addParameter(p)

        p = QgsProcessingParameterBoolean(
            self.ABRIR,
            self.tr('Abrir la hoja de contactos en el navegador al terminar'),
            defaultValue=True)
        p.setHelp(self.tr(
            'Al acabar, abre <carpeta>/miniaturas/index.html en el navegador '
            'predeterminado. Solo tiene efecto si se generaron miniaturas. '
            'La apertura ocurre en postProcessAlgorithm, es decir en el hilo '
            'principal: hacerlo durante el proceso sería tocar la interfaz '
            'desde un hilo de trabajo.'))
        self.addParameter(p)

        p = QgsProcessingParameterFile(
            self.CARPETA,
            self.tr('Carpeta de salida — seleccione un DIRECTORIO'),
            behavior=_CARPETA_BEHAVIOR,
            defaultValue=None,
            optional=True)
        p.setHelp(self.tr(
            'Directorio permanente donde se escriben las miniaturas, la hoja '
            'de contactos y los recortes descargados. El botón «…» abre un '
            'selector de carpetas: si le ofrece «Browse for Layer…» está '
            'pulsando otro parámetro, no éste. Obligatorio si activa las '
            'miniaturas o el modo de descarga.'))
        self.addParameter(p)

        p = QgsProcessingParameterFeatureSource(
            self.SELECCION,
            self.tr('Huellas ya revisadas — descargar solo estas (opcional)'),
            [_TIPO_POLIGONO], optional=True)
        p.setHelp(self.tr(
            'Segundo paso del flujo. Indique aquí la capa de huellas '
            'producida por una ejecución previa en modo «Solo catálogo»: se '
            'omite la búsqueda STAC y se bajan solo esas escenas, usando las '
            'URL guardadas en el campo «assets».<br><br>'
            'Hay DOS formas de quedarse con las escenas útiles, y la '
            'diferencia importa:<br>'
            '<b>a) Seleccionar</b> — seleccione las entidades en el lienzo o '
            'en la tabla y marque «Entidades seleccionadas solamente». '
            'Rápido, pero la selección se pierde al cerrar el proyecto.<br>'
            '<b>b) Depurar</b> — borre de la tabla de atributos las filas que '
            'no quiera, guarde la capa como GeoPackage, e indíquela aquí '
            '<i>sin</i> marcar «Entidades seleccionadas solamente»: se baja '
            'todo lo que quedó. Más lento, pero deja un archivo que registra '
            'exactamente qué escenas usó el estudio y se puede reejecutar '
            'meses después o pasar a otra persona.<br><br>'
            'El <b>catálogo debe ser el mismo</b> de la primera pasada, y '
            'el algoritmo lo comprueba: la capa guarda su endpoint '
            'en el campo «catalogo» y el algoritmo se niega a ejecutar si no '
            'coincide. Hace falta porque la colección «sentinel-2-l2a» existe '
            'en Earth Search y en Planetary Computer, de modo que el nombre '
            'de colección por sí solo no distingue una de otra, y las URL de '
            'una no sirven en la otra.'))
        self.addParameter(p)

        p = QgsProcessingParameterNumber(
            self.MAX_ESCENAS, self.tr('Número máximo de escenas'),
            QgsProcessingParameterNumber.Integer,
            defaultValue=20, minValue=1, maxValue=500)
        p.setHelp(self.tr(
            'Tope de escenas que devuelve la consulta STAC, aplicado '
            'ANTES de medir la nubosidad dentro del AOI. Con un tope bajo '
            'y nubosidad de escena permisiva puede quedarse sin '
            'candidatas despejadas: el catálogo devuelve las primeras N '
            'por el orden pedido, y si ésas resultan nubladas sobre su '
            'área no hay más de dónde elegir.<br>'
            'Si el aviso de «periodos sin datos» aparece para años que sí '
            'tienen escenas, suba este número antes de ampliar el '
            'periodo.'))
        self.addParameter(p)

        p = QgsProcessingParameterNumber(
            self.CANDIDATOS, self.tr('Candidatos a evaluar por periodo'),
            QgsProcessingParameterNumber.Integer,
            defaultValue=3, minValue=1, maxValue=8)
        p.setHelp(self.tr(
            'Solo en los modos «mejor escena por año/mes». De cada periodo se '
            'toman las N escenas de menor nubosidad de teselá, se mide la '
            'nubosidad real sobre el AOI con SCL, y se conserva la mejor según '
            'ESA medida. Con N=1 se decide únicamente por la nubosidad de la '
            'teselá completa, que es mal indicador para un AOI pequeño.'))
        self.addParameter(p)

        p = QgsProcessingParameterEnum(
            self.ORDEN, self.tr('Estrategia de selección temporal'),
            options=[o[0] for o in ORDENES], defaultValue=0)
        p.setHelp(self.tr(
            '«Más recientes primero» devuelve las N escenas más nuevas del '
            'periodo: con un rango largo y N bajo, todas saldrán del último '
            'año. Para análisis multitemporal use «Mejor escena por año», que '
            'recupera todos los candidatos del periodo y conserva, de cada '
            'año, la de menor nubosidad. «Por mes» es para series densas '
            'dentro de uno o dos años.'))
        self.addParameter(p)

        p = QgsProcessingParameterEnum(
            self.MODO, self.tr('Modo de ejecución'),
            options=MODOS, defaultValue=0)
        p.setHelp(self.tr(
            '<b>Solo catálogo</b>: consulta, mide la nube dentro del AOI y '
            'devuelve las huellas y la hoja de contactos. No transfiere '
            'píxeles, así que es el modo para explorar disponibilidad y el '
            'primer paso del flujo de dos pasos.<br>'
            '<b>Cargar assets remotos</b>: añade las bandas como capas '
            'leídas por HTTP con /vsicurl/. No ocupa disco y QGIS descarga '
            'solo los bloques que mira. Con Planetary Computer, la URL '
            'firmada caduca en unos 45 minutos y las capas dejan de cargar '
            'después.<br>'
            '<b>Descargar recorte</b>: escribe un GeoTIFF comprimido por '
            'banda y escena, recortado al AOI. Es el único modo en que '
            'tienen efecto los índices, la amplitud fenológica y el '
            'escalado a 8 bits, porque son lo que necesita los píxeles en '
            'disco.'))
        self.addParameter(p)

        p = QgsProcessingParameterEnum(
            self.COMPOSICION, self.tr('Composiciones RGB (puede elegir varias)'),
            options=[c[0] for c in COMPOSICIONES],
            allowMultiple=True, defaultValue=[], optional=True)
        p.setHelp(self.tr(
            'Genera una capa RGB de 3 bandas por escena y por composición '
            'elegida. Puede marcar varias: con N escenas y M composiciones se '
            'producen N x M capas, nombradas con el sufijo de cada una (IRC, '
            'AGR, URB…), de modo que se pueden comparar lado a lado.<br>'
            'Infrarrojo color (NIR/rojo/verde) resalta vegetación en rojo; '
            'Agricultura (SWIR 1/NIR/azul) separa cultivos de bosque; '
            'SWIR (SWIR 2/SWIR 1/rojo) penetra humo y delimita cicatrices de '
            'fuego.<br>'
            'Las composiciones se nombran por banda común y no por número, '
            'porque los números serían los de Sentinel-2 y en Landsat son '
            'otros —y cambian entre L4/5/7 y L8-9—. La tabla de '
            'equivalencias está en la ayuda de «Bandas / assets '
            'individuales».<br>'
            'Sin ninguna marcada se usan las bandas sueltas del parámetro '
            'siguiente.<br>'
            'Esto son capas RGB para interpretación visual. Los valores '
            'calculados (NDVI, NDMI, NBR, Tasseled Cap) están en el '
            'parámetro «Índices espectrales», más abajo.'))
        self.addParameter(p)

        p = QgsProcessingParameterEnum(
            self.BANDAS, self.tr('Bandas / assets individuales'),
            options=list(ETIQUETAS_BANDAS), allowMultiple=True,
            defaultValue=[],
            optional=True)
        p.setHelp(self.tr(
            'Se usa solo si NO ha marcado ninguna composición RGB, y si el '
            'modo no es «Solo catálogo».<br>'
            'Cada etiqueta dice lo que vale en los DOS sensores, porque no '
            'es lo mismo: «rojo — S2 B04 10 m · Landsat red 30 m».<br><br>'
            '<b>Equivalencias</b><br>'
            '<table border="0" cellpadding="2">'
            '<tr><td><b></b></td><td><b>Sentinel-2</b></td>'
            '<td><b>Landsat L8-9</b></td><td><b>Landsat L4/5/7</b></td></tr>'
            '<tr><td>azul</td><td>B02, 10 m</td><td>B2</td><td>B1</td></tr>'
            '<tr><td>verde</td><td>B03, 10 m</td><td>B3</td><td>B2</td></tr>'
            '<tr><td>rojo</td><td>B04, 10 m</td><td>B4</td><td>B3</td></tr>'
            '<tr><td>NIR</td><td>B08, 10 m</td><td>B5</td><td>B4</td></tr>'
            '<tr><td>NIR estrecho</td><td>B8A, 20 m</td><td>B5</td>'
            '<td>B4</td></tr>'
            '<tr><td>SWIR 1</td><td>B11, 20 m</td><td>B6</td><td>B5</td></tr>'
            '<tr><td>SWIR 2</td><td>B12, 20 m</td><td>B7</td><td>B7</td></tr>'
            '</table>'
            'Todas las de Landsat miden <b>30 m</b>. Por eso la etiqueta no '
            'trae número de banda para Landsat: cambiaría según el satélite, '
            'y la colección va de L4 a L9.<br><br>'
            '<b>NIR y NIR estrecho son la misma banda en Landsat.</b> En '
            'Sentinel-2 son B08 (842 nm, 10 m) y B8A (865 nm, 20 m), '
            'distintas; Landsat solo tiene una, así que marcar las dos se '
            'rechaza en vez de escribir dos veces los mismos píxeles.<br><br>'
            '«visual» (TCI) y las tres de borde rojo existen únicamente en '
            'Sentinel-2.<br><br>'
            'El nombre del archivo lleva el número real del sensor: '
            '<code>Sent2C_…_B04.tif</code> en Sentinel-2 y '
            '<code>Lands8_…_RED.tif</code> en Landsat, con el nombre común, '
            'que es el único correcto para toda la familia.'))
        self.addParameter(p)

        p = QgsProcessingParameterEnum(
            self.INDICES,
            self.tr('Índices espectrales — NDVI, NDMI, NBR, Tasseled Cap '
                    '(solo modo descarga)'),
            options=[i[0] for i in INDICES_ESPECTRALES],
            allowMultiple=True, defaultValue=[], optional=True)
        p.setHelp(self.tr(
            'Calcula un GeoTIFF Float32 por índice y escena, recortado al AOI '
            'y con los píxeles de nube y sombra puestos a NaN.<br>'
            'Para separar copas de árboles de vegetación herbácea y arbustiva, '
            'los más discriminantes son <b>NDMI</b> y <b>NBR</b>: el SWIR '
            'responde al agua del dosel, que es mucho mayor en leñosas que en '
            'pastos, sobre todo en estación seca. NDVI satura sobre dosel '
            'cerrado y en época lluviosa apenas distingue pasto vigoroso de '
            'árbol. NDRE y CIre añaden separación por clorofila, pero solo '
            'existen en Sentinel-2.<br>'
            'El cálculo se hace sobre reflectancia, no sobre DN: se aplica el '
            'escalado del sensor, incluido el desplazamiento BOA de la línea '
            'base 04.00 de Sentinel-2 y el desplazamiento aditivo de Landsat '
            'Collection 2, que no se cancelan en un cociente.'))
        self.addParameter(p)

        p = QgsProcessingParameterBoolean(
            self.AMPLITUD,
            self.tr('Calcular amplitud fenológica (seca − lluviosa)'),
            defaultValue=False)
        p.setHelp(self.tr(
            'Agrupa los índices calculados por estación, obtiene la mediana de '
            'cada una y escribe la diferencia seca − lluviosa como capa '
            'independiente (sufijo AMPL).<br>'
            'Es el producto más discriminante entre copas leñosas y vegetación '
            'herbácea: mide la capacidad de sostener agua a lo largo del año, '
            'no el vigor de un día concreto.<br>'
            '<b>Cómo leer el resultado.</b> La resta es seca − lluvia, de modo '
            'que los valores salen NEGATIVOS: el índice de humedad baja en '
            'seca. Cuanto más negativo, mayor es la caída estacional y más '
            'probable que sea pasto o herbácea; los valores cercanos a cero '
            'corresponden a vegetación que sostiene agua todo el año, es decir '
            'dosel leñoso.<br>'
            'Se usa la mediana y no la media porque resiste mejor las nubes '
            'que hayan escapado al enmascarado. Requiere al menos una escena '
            'en cada estación, así que conviene ejecutarlo con «Mejor escena '
            'por mes».<br>'
            'Si la capa sale sin píxeles válidos, el aviso dice por qué: una '
            'estación completamente nublada, «Mínimo de observaciones por '
            'píxel» por encima de lo que dan las escenas disponibles (indica '
            'el valor que sí funcionaría), o escenas que no cubren el mismo '
            'trozo del AOI en ambas estaciones. Son tres soluciones distintas, '
            'de ahí que se distingan.'))
        self.addParameter(p)

        p = QgsProcessingParameterNumber(
            self.MIN_OBS,
            self.tr('Mínimo de observaciones válidas por píxel y estación'),
            QgsProcessingParameterNumber.Integer,
            defaultValue=3, minValue=1, maxValue=20)
        p.setHelp(self.tr(
            'Un píxel entra en la amplitud solo si tiene al menos este número '
            'de observaciones sin nube en CADA estación; si no, sale como '
            'NaN.<br>'
            '<b>Esto sustituye al filtrado por escena.</b> La máscara de nubes '
            'trabaja píxel a píxel, así que una escena con 70 % de nube sobre '
            'el AOI aporta igualmente su 30 % despejado. Descartarla entera '
            'tira datos buenos; lo que hay que evitar es que un píxel quede '
            'sustentado en una sola observación.<br>'
            'Estrategia recomendada: ponga «Nubosidad máxima DENTRO del AOI» '
            'en 90 para que entren muchas escenas, y controle la calidad aquí. '
            'Con 3 observaciones la mediana ya resiste una nube no detectada. '
            'Se escribe además una capa NOBS con el recuento por píxel para '
            'que compruebe dónde hay soporte suficiente.<br>'
            'Si ningún píxel alcanza el mínimo, la amplitud sale vacía y el '
            'aviso indica a cuánto habría que bajarlo — no hace falta '
            'tantear.'))
        self.addParameter(p)

        p = QgsProcessingParameterString(
            self.MESES_SECA, self.tr('Meses de estación seca'),
            defaultValue='12,1,2,3,4')
        p.setHelp(self.tr(
            'Números de mes separados por comas. El valor por omisión '
            'corresponde al Pacífico norte de Costa Rica.'))
        self.addParameter(p)

        p = QgsProcessingParameterString(
            self.MESES_LLUVIA, self.tr('Meses de estación lluviosa'),
            defaultValue='5,6,7,8,9,10,11')
        p.setHelp(self.tr(
            'Números de mes separados por comas. El valor por omisión '
            'corresponde al Pacífico norte de Costa Rica.<br><br>'
            'Ningún mes puede figurar también en la estación seca: la '
            'amplitud sería una diferencia contra sí misma y el algoritmo lo '
            'rechaza antes de descargar. Un mes que no aparezca en ninguna de '
            'las dos listas queda fuera del cálculo y se reporta como '
            'huérfano, para que una escena excluida no pase inadvertida.'))
        self.addParameter(p)

        p = QgsProcessingParameterFile(
            self.COEF_TC,
            self.tr('Coeficientes Tasseled Cap alternativos (.json, opcional)'),
            behavior=_ARCHIVO_BEHAVIOR, extension='json',
            defaultValue=None, optional=True)
        p.setHelp(self.tr(
            'Sustituye los coeficientes internos por los de un archivo JSON. '
            'Sirve para aplicar conjuntos derivados sobre reflectancia de '
            'SUPERFICIE, que es lo que descarga este script, en lugar de los '
            'derivados sobre TOA (Huang, Baig, Shi &amp; Xu).<br>'
            'Al cargarlo se comprueba la ortonormalidad de la rotación '
            '(‖v‖ = 1, v·w = 0) y se avisa si algún vector no la cumple, lo '
            'que delata errores de transcripción.<br>'
            'Si marca algún índice Tasseled Cap y no indica archivo, se '
            'escribe una plantilla con el esquema exacto en la carpeta de '
            'salida (tc_coeficientes_plantilla.json).'))
        self.addParameter(p)

        p = QgsProcessingParameterEnum(
            self.REALCE, self.tr('Método de realce de las capas'),
            options=REALCES, defaultValue=1)
        p.setHelp(self.tr(
            '<b>Corte acumulado 2–98 %</b>: descarta el 2 % de píxeles más '
            'oscuros y el 2 % más claros. Va bien en escenas completas, pero '
            'si el recorte tiene muchos píxeles sin dato o nube, los extremos '
            'se juntan y la capa sale NEGRA.<br>'
            '<b>Media ± N desviaciones</b> (por omisión, N = 2): centra el '
            'realce en la distribución real de valores y no en sus extremos, '
            'así que resiste los nodata y las nubes. Es la opción recomendada '
            'para recortes pequeños como un AOI de pocas hectáreas. Con N = 2 '
            'se cubre alrededor del 95 % de los valores; baje a 1,5 para más '
            'contraste, suba a 3 si aparecen zonas saturadas.<br>'
            '<b>Mínimo / máximo</b>: usa los extremos sin recortar. Un solo '
            'píxel atípico aplasta todo el resto; úselo solo para inspeccionar '
            'el rango completo.'))
        self.addParameter(p)

        p = QgsProcessingParameterNumber(
            self.REALCE_N, self.tr('N (desviaciones estándar para el realce)'),
            QgsProcessingParameterNumber.Double,
            defaultValue=2.0, minValue=0.5, maxValue=5.0)
        p.setHelp(self.tr(
            'Solo se aplica con el método «Media ± N desviaciones».'))
        self.addParameter(p)

        p = QgsProcessingParameterEnum(
            self.PALETA, self.tr('Paleta para capas monobanda'),
            options=[x[0] for x in PALETAS_MONOBANDA], defaultValue=1)
        p.setHelp(self.tr(
            'Solo afecta a índices y amplitud, que son de una banda; las '
            'composiciones RGB no la usan.<br>'
            'Las dos paletas son divergentes de ColorBrewer, seguras para '
            'daltonismo, y su <b>color neutro queda anclado en el valor 0</b> '
            'siempre que el cero caiga dentro del rango mostrado. Eso importa: '
            'con una rampa estirada sin anclar, el neutro caería en la media '
            'de cada imagen, y dos fechas del mismo sitio se verían distintas '
            'sin que el terreno hubiera cambiado.<br>'
            'En NDMI el cero separa NIR &gt; SWIR de NIR &lt; SWIR; en la '
            'amplitud marca la ausencia de cambio estacional, que es el '
            'criterio entre leñosa y herbácea.<br>'
            'Se evitan a propósito las rampas tipo arcoíris (Spectral, turbo): '
            'su espaciado perceptual no es uniforme, inventan bordes en las '
            'transiciones a verde y amarillo, y la pareja verde-rojo se pierde '
            'en deuteranopía.'))
        self.addParameter(p)

        p = QgsProcessingParameterEnum(
            self.CLASES, self.tr('Modo de clasificación (capas monobanda)'),
            options=MODOS_CLASE, defaultValue=0)
        p.setHelp(self.tr(
            '<b>Continuo</b>: gradiente sin clases. Conserva todo el detalle.'
            '<br><b>Intervalo igual</b>: clases discretas de igual anchura '
            'entre los límites indicados abajo. Se lee mejor en papel y '
            'permite una leyenda con categorías nombradas.<br><br>'
            'No se ofrece clasificación por cuantiles a propósito: sus '
            'límites salen de la distribución de cada imagen, de modo que un '
            'mismo valor cae en clases distintas según la fecha y la '
            'comparación entre escenas deja de ser válida. Además reparte la '
            'paleta completa incluso sobre terreno homogéneo, lo que aparenta '
            'estructura inexistente.'))
        self.addParameter(p)

        p = QgsProcessingParameterNumber(
            self.N_CLASES, self.tr('Número de clases'),
            QgsProcessingParameterNumber.Integer,
            defaultValue=6, minValue=3, maxValue=12)
        p.setHelp(self.tr(
            'Solo tiene efecto con «Intervalo igual»; en modo continuo se '
            'ignora. Las clases reparten por igual el rango entre los '
            'límites.<br><br>'
            'Con «Límites fijos» vacío los límites se calculan por capa, de '
            'modo que una misma clase significa cosas distintas en cada fecha. '
            'Con límites simétricos (−1,1 en los índices habituales) un '
            'número <b>par</b> deja el cero justo en un borde de clase; uno '
            'impar lo mete dentro de una clase y el color neutro deja de '
            'marcar la frontera con significado físico, sobre lo que el '
            'algoritmo avisa en el registro.'))
        self.addParameter(p)

        p = QgsProcessingParameterString(
            self.LIMITES,
            self.tr('Límites fijos mín,máx (vacío = calculados por capa)'),
            defaultValue='', optional=True)
        p.setHelp(self.tr(
            'Dos números separados por coma, por ejemplo <code>-0.1,0.5</code> '
            'para NDMI o <code>-0.6,0.1</code> para la amplitud.<br>'
            '<b>Fíjelos si va a comparar fechas.</b> Dejados en blanco, cada '
            'capa calcula los suyos y el mismo valor recibe colores distintos '
            'según la escena — el mismo defecto que hace inservible la '
            'clasificación por cuantiles.<br>'
            'Para que el color neutro siga marcando el cero, elija límites '
            'donde el cero caiga sobre un borde de clase; el registro avisa '
            'si no es así.'))
        self.addParameter(p)

        p = QgsProcessingParameterBoolean(
            self.VISIBLES,
            self.tr('Cargar las capas de imagen visibles'),
            defaultValue=False)
        p.setHelp(self.tr(
            'Desactivado (recomendado): las capas ráster se añaden al proyecto '
            'desmarcadas. Con una serie multitemporal, cargarlas todas visibles '
            'apila una decena de imágenes opacas y solo se ve la última. '
            'La capa de huellas siempre se carga visible, porque es la que se '
            'usa para revisar.'))
        self.addParameter(p)

        p = QgsProcessingParameterBoolean(
            self.ESTIRAR,
            self.tr('Escalar composición a 8 bits (percentiles 2-98)'),
            defaultValue=False)
        p.setHelp(self.tr(
            'Solo en modo descarga. Produce un GeoTIFF RGB listo para mapas, '
            'de ~1/2 del tamaño, pero destruye los valores de reflectancia: '
            'no lo use si va a calcular índices sobre el resultado. '
            'Desactivado, se conserva el entero de 16 bits y el realce se '
            'aplica solo como estilo de visualización.'))
        self.addParameter(p)

        self.addOutput(QgsProcessingOutputFolder(
            self.SALIDA, self.tr('Carpeta con resultados')))

        p = QgsProcessingParameterFeatureSink(
            self.HUELLAS, self.tr('Capa de huellas (opcional)'),
            _TIPO_POLIGONO,
            optional=True, createByDefault=False)
        p.setHelp(self.tr(
            'Desmarcada por omisión: dibuja teselas de 110 km que junto a un '
            'AOI pequeño solo estorban, y la información útil ya está en la '
            'hoja de contactos y en el registro.<br>'
            'Actívela únicamente si va a usar el flujo de dos pasos: es la '
            'capa que guarda las URL de los assets en el campo «assets», y '
            'sin ella el parámetro «Huellas ya revisadas» no tiene de dónde '
            'leer.<br><br>'
            '<b>Elija un archivo, no una capa temporal</b>, si piensa '
            'continuar en otra sesión. El destino por omisión de un sumidero '
            'es una capa en memoria: desaparece al cerrar QGIS y se lleva con '
            'ella las URL de «assets», así que el segundo paso se queda sin '
            'nada que leer. El algoritmo avisa cuando detecta ese caso.<br>'
            'Campos útiles de la capa: «nubes_aoi» (nubes dentro del AOI, no '
            'de la tesela entera), «datos_pct» (porcentaje del AOI con dato), '
            '«thumb_url» (miniatura), «catalogo» y «coleccion» '
            '(procedencia, que el segundo paso verifica).'))
        self.addParameter(p)

    # ----------------------------------------------------------- validación
    def checkParameterValues(self, parameters, context):
        f_ini = self.parameterAsString(parameters, self.FECHA_INI, context)
        f_fin = self.parameterAsString(parameters, self.FECHA_FIN, context)

        for etiqueta, valor in (('inicial', f_ini), ('final', f_fin)):
            if not _RE_FECHA.match((valor or '').strip()):
                return False, (
                    f"[!] Fecha {etiqueta} inválida: '{valor}'. "
                    f"Formato esperado AAAA-MM-DD (ej: 2025-03-15)."
                )
            try:
                datetime.strptime(valor.strip(), '%Y-%m-%d')
            except ValueError as e:
                return False, f"[!] Fecha {etiqueta} inexistente: '{valor}' ({e})."

        if f_ini.strip() > f_fin.strip():
            return False, (
                f"[!] La fecha inicial ({f_ini}) es posterior a la final ({f_fin}). "
                f"Invierta el orden."
            )

        nubes = self.parameterAsDouble(parameters, self.NUBOSIDAD, context)
        if not 0.0 <= nubes <= 100.0:
            return False, (
                f"[!] Nubosidad fuera de rango: {nubes}. Esperado 0-100. "
                f"Ajuste el parámetro."
            )

        modo = self.parameterAsEnum(parameters, self.MODO, context)
        bandas = self.parameterAsEnums(parameters, self.BANDAS, context)
        comps = self.parameterAsEnums(parameters, self.COMPOSICION, context)
        idx_cat_val = self.parameterAsEnum(parameters, self.CATALOGO, context)
        familia = CATALOGOS[idx_cat_val][4]
        etq_cat_val = CATALOGOS[idx_cat_val][0]
        base_cat_val = CATALOGOS[idx_cat_val][1]
        col_cat_val = CATALOGOS[idx_cat_val][2]

        # --- Procedencia de las huellas revisadas -------------------------
        # El flujo de dos pasos reutiliza las URL guardadas en «assets». Esas
        # URL pertenecen a UN endpoint concreto, así que cambiar de catálogo
        # entre la primera y la segunda pasada deja href de un servicio con
        # la lógica de firma del otro. Antes esto solo se pedía en la ayuda;
        # ahora se comprueba.
        fuente_rev = self.parameterAsSource(
            parameters, self.SELECCION, context)
        if fuente_rev is not None:
            cat_capa, col_capa, n_rev = self._procedencia_huellas(fuente_rev)
            if n_rev == 0:
                return False, (
                    "[!] «Huellas ya revisadas» no aporta ninguna entidad. "
                    "Si marcó «Entidades seleccionadas solamente», "
                    "seleccione al menos una escena en la capa; si depuró la "
                    "capa borrando filas de la tabla de atributos, "
                    "compruebe que quedó alguna."
                )
            if cat_capa and cat_capa.rstrip('/') != base_cat_val.rstrip('/'):
                return False, (
                    f"[!] «Huellas ya revisadas» viene de otro catálogo.\n"
                    f"    La capa se produjo con : {cat_capa}\n"
                    f"      (colección «{col_capa or '?'}»)\n"
                    f"    Ahora está elegido    : {base_cat_val}\n"
                    f"      («{etq_cat_val}»)\n"
                    f"Las URL del campo «assets» pertenecen al primero y no "
                    f"son válidas en el segundo, aunque el nombre de "
                    f"colección coincida. Elija en «Catálogo» el mismo de la "
                    f"primera pasada, o vuelva a generar las huellas con el "
                    f"catálogo actual."
                )
            if cat_capa and col_capa and col_capa != col_cat_val:
                return False, (
                    f"[!] «Huellas ya revisadas» es de la colección "
                    f"«{col_capa}» y ahora está elegida «{col_cat_val}». "
                    f"Mantenga la misma colección o regenere las huellas."
                )

        indices_sel = self.parameterAsEnums(parameters, self.INDICES, context)
        texto_lim = self.parameterAsString(parameters, self.LIMITES, context)
        if str(texto_lim or '').strip() and \
                self._parsear_limites(texto_lim) is None:
            return False, (
                f"[!] «Límites fijos» no se pudo interpretar: '{texto_lim}'. "
                f"Formato esperado: dos números separados por coma y en orden "
                f"creciente, por ejemplo -0.1,0.5. Déjelo vacío para que cada "
                f"capa calcule los suyos."
            )
        if self.parameterAsBool(parameters, self.AMPLITUD, context):
            if not indices_sel:
                return False, (
                    "[!] La amplitud fenológica se calcula sobre los índices "
                    "espectrales. Marque al menos uno en el parámetro "
                    "«Índices espectrales», que está debajo de «Bandas / "
                    "assets individuales» — no en «Composiciones RGB», que son "
                    "capas de color y no valores calculados. Para vegetación "
                    "leñosa, NDMI es el más discriminante."
                )
            seca = self._parsear_meses(self.parameterAsString(
                parameters, self.MESES_SECA, context))
            lluvia = self._parsear_meses(self.parameterAsString(
                parameters, self.MESES_LLUVIA, context))
            if not seca or not lluvia:
                return False, (
                    "[!] Los meses deben indicarse como números de 1 a 12 "
                    "separados por comas (ej. 12,1,2,3,4)."
                )
            comunes = sorted(set(seca) & set(lluvia))
            if comunes:
                return False, (
                    f"[!] Los meses {comunes} figuran en ambas estaciones. "
                    f"La amplitud sería una diferencia contra sí misma en esas "
                    f"fechas; asigne cada mes a una sola estación."
                )
        if indices_sel and modo != 2:
            return False, (
                "[!] Los índices espectrales solo se calculan en el modo "
                "«Catálogo + descargar recorte del AOI a disco»: hay que leer "
                "los píxeles para operarlos, y en modo remoto no se descarga "
                "ninguno. Cambie el modo o desmarque los índices."
            )
        if familia == 'ls':
            solo_s2 = [INDICES_ESPECTRALES[i][1] for i in indices_sel
                       if INDICES_ESPECTRALES[i][3]]
            if solo_s2:
                return False, (
                    f"[!] Estos índices necesitan bandas de borde rojo, que "
                    f"Landsat no tiene: {', '.join(solo_s2)}. Use NDMI o NBR, "
                    f"que funcionan en ambos sensores."
                )

        if modo in (1, 2) and not comps and not bandas and not indices_sel:
            return False, (
                "[!] El modo seleccionado requiere al menos una composición RGB "
                "o al menos una banda individual. Marque alguna de las dos, o "
                "cambie a 'Solo catálogo'."
            )

        if familia == 'ls':
            vetadas = []
            for idx in comps:
                etiqueta, _, bandas_comp = COMPOSICIONES[idx]
                faltan = [etiqueta_banda(b) for b in (bandas_comp or ())
                          if b in SIN_EQUIVALENTE_LS]
                if faltan:
                    vetadas.append(f"«{etiqueta}» (necesita {', '.join(faltan)})")
            if vetadas:
                return False, (
                    f"[!] Estas composiciones usan bandas que Landsat no "
                    f"tiene: {'; '.join(vetadas)}. Landsat carece de bandas "
                    f"red edge. Desmárquelas y use Color natural, Infrarrojo "
                    f"color, Agricultura, Análisis de vegetación, SWIR urbano "
                    f"o Geología, que sí tienen equivalente."
                )
            # Las bandas sueltas solo se usan cuando NO hay composiciones
            # marcadas; validarlas siempre rechazaba ejecuciones válidas por
            # culpa del valor por defecto del parámetro.
            if not comps:
                # Dos claves distintas pueden ser la MISMA banda en
                # Landsat: «nir» y «nir08» son las dos su unico infrarrojo
                # cercano. Antes eso escribia el mismo raster dos veces,
                # con nombres que sugerian longitudes de onda distintas.
                elegidas = [CLAVES_BANDAS[i] for i in bandas]
                repetidas = bandas_duplicadas(elegidas, 'ls')
                if repetidas:
                    detalle = '; '.join(
                        '{} → todas son «{}»'.format(
                            ', '.join('«%s»' % etiqueta_banda(c)
                                      for c in cs), a)
                        for a, cs in repetidas)
                    return False, (
                        '[!] En Landsat estas bandas son la misma, así que '
                        'se escribirían archivos distintos con los mismos '
                        'píxeles: {d}. Deje marcada solo una.'
                    ).format(d=detalle)
                sueltas = [etiqueta_banda(CLAVES_BANDAS[i])
                           for i in bandas
                           if CLAVES_BANDAS[i] in SIN_EQUIVALENTE_LS]
                if sueltas:
                    return False, (
                        f"[!] Bandas sin equivalente en Landsat: "
                        f"{', '.join(sueltas)}. Landsat no publica TCI ni red "
                        f"edge. Para una vista en color use una composición "
                        f"RGB (Color natural o Infrarrojo color), que el "
                        f"algoritmo arma desde red/green/blue."
                    )

        estirar = self.parameterAsBool(parameters, self.ESTIRAR, context)
        if estirar and not comps:
            return False, (
                "[!] El escalado a 8 bits requiere al menos una composición "
                "RGB. Marque una composición o desactive el escalado."
            )

        miniaturas = self.parameterAsBool(parameters, self.MINIATURAS, context)
        carpeta = (self.parameterAsFile(parameters, self.CARPETA, context)
                   or '').strip()

        if miniaturas and not carpeta:
            return False, (
                "[!] Las miniaturas requieren una carpeta de salida. Use el "
                "botón «…» del parámetro «Carpeta de salida» para elegir un "
                "directorio permanente (ej. C:/Protocolo bosque 2026/sentinel), "
                "o desactive las miniaturas."
            )

        if carpeta:
            valida, motivo = self._carpeta_utilizable(carpeta)
            if not valida:
                return False, f"[!] Carpeta de salida inutilizable: {motivo}"

        if estirar and modo != 2:
            return False, (
                "[!] «Escalar a 8 bits» solo tiene efecto en el modo de "
                "descarga: en modo remoto no se escribe ningún archivo que "
                "escalar. Desactívelo o cambie a modo descarga."
            )

        crudo_huellas = parameters.get(self.HUELLAS)
        if modo == 0 and not crudo_huellas:
            miniaturas_on = self.parameterAsBool(
                parameters, self.MINIATURAS, context)
            if not miniaturas_on:
                return False, (
                    "[!] En modo «Solo catálogo» sin capa de huellas y sin "
                    "miniaturas no se produce nada. Active las miniaturas "
                    "(hoja de contactos HTML) o marque la capa de huellas."
                )

        if modo == 2 and not carpeta:
            return False, (
                "[!] El modo de descarga requiere una carpeta de salida. "
                "Elija un directorio con el botón «…» del parámetro "
                "«Carpeta de salida»."
            )

        ext = self.parameterAsExtent(
            parameters, self.EXTENSION, context,
            QgsCoordinateReferenceSystem('EPSG:4326'))
        if ext.isEmpty():
            return False, (
                "[!] Extensión vacía. Defina un rectángulo de búsqueda o use "
                "el valor por defecto de Costa Rica."
            )

        # Tope de memoria. El cálculo lee bandas completas con ReadAsArray y
        # las apila: no hay procesamiento por bloques. La extensión POR
        # OMISIÓN de este algoritmo es Costa Rica entera, de modo que
        # ejecutarla sin tocar en modo descarga + índices pediría ~15 GB y
        # mataría el proceso de QGIS sin un mensaje que explique por qué.
        if modo == 2 and (indices_sel or comps or bandas):
            fuente_aoi_mem = self.parameterAsSource(parameters, self.AOI, context)
            idx_area = self.parameterAsEnum(parameters, self.AREA, context)
            rect = None
            if idx_area == 0 and fuente_aoi_mem is not None:
                bbox_aoi = self._extent_fuente_4326(
                    fuente_aoi_mem, context, QgsProcessingFeedback())
                if bbox_aoi:
                    rect = bbox_aoi
            if rect is None:
                rect = [ext.xMinimum(), ext.yMinimum(),
                        ext.xMaximum(), ext.yMaximum()]

            # Fechas previsibles: el tope de escenas, acotado por los meses
            # que abarca el rango (las estrategias «mejor por mes/año» no
            # devuelven más de una por periodo).
            d_ini = datetime.strptime(f_ini.strip(), '%Y-%m-%d')
            d_fin = datetime.strptime(f_fin.strip(), '%Y-%m-%d')
            n_meses = max(1, (d_fin.year - d_ini.year) * 12
                          + d_fin.month - d_ini.month + 1)
            n_fechas = min(
                self.parameterAsInt(parameters, self.MAX_ESCENAS, context),
                n_meses)

            gib, ancho_px, alto_px = self._memoria_estimada(
                rect, indices_sel,
                self.parameterAsBool(parameters, self.AMPLITUD, context),
                n_fechas, familia, estirar)
            if gib > MEMORIA_MAX_GIB:
                return False, (
                    f"[!] El área pedida necesitaría unos {gib:.1f} GiB de "
                    f"memoria ({ancho_px:.0f} x {alto_px:.0f} píxeles), por "
                    f"encima del tope de {MEMORIA_MAX_GIB:.0f} GiB. El cálculo "
                    f"carga las bandas completas en memoria, así que QGIS se "
                    f"cerraría sin aviso. Reduzca «Extensión de búsqueda» o "
                    f"indique una «Capa de AOI para recorte»; recuerde que la "
                    f"extensión por omisión abarca todo Costa Rica."
                )

        return super().checkParameterValues(parameters, context)

    # ------------------------------------------------------------ ejecución
    def processAlgorithm(self, parameters, context, feedback):
        _configurar_gdal()

        idx_cat = self.parameterAsEnum(parameters, self.CATALOGO, context)
        (etiqueta_cat, base_url, coleccion, requiere_token,
         familia) = CATALOGOS[idx_cat]
        # Guardado en la instancia: Processing crea un objeto nuevo por
        # ejecución (createInstance), así que no hay estado compartido.
        self._familia = familia
        self._coleccion_activa = coleccion

        f_ini = self.parameterAsString(parameters, self.FECHA_INI, context).strip()
        f_fin = self.parameterAsString(parameters, self.FECHA_FIN, context).strip()
        nubes = self.parameterAsDouble(parameters, self.NUBOSIDAD, context)
        max_esc = self.parameterAsInt(parameters, self.MAX_ESCENAS, context)
        idx_orden = self.parameterAsEnum(parameters, self.ORDEN, context)
        etiqueta_orden, estrategia = ORDENES[idx_orden]
        n_cand = self.parameterAsInt(parameters, self.CANDIDATOS, context)
        modo = self.parameterAsEnum(parameters, self.MODO, context)
        bandas = [CLAVES_BANDAS[i]
                  for i in self.parameterAsEnums(parameters, self.BANDAS, context)]
        idx_comps = self.parameterAsEnums(parameters, self.COMPOSICION, context)
        composiciones = [COMPOSICIONES[i] for i in idx_comps]
        estirar = self.parameterAsBool(parameters, self.ESTIRAR, context)
        visibles = self.parameterAsBool(parameters, self.VISIBLES, context)
        self._metodo_realce = self.parameterAsEnum(
            parameters, self.REALCE, context)
        self._n_sigma = self.parameterAsDouble(
            parameters, self.REALCE_N, context)
        self._paleta = self.parameterAsEnum(
            parameters, self.PALETA, context)
        self._modo_clase = self.parameterAsEnum(
            parameters, self.CLASES, context)
        self._n_clases = self.parameterAsInt(
            parameters, self.N_CLASES, context)
        self._limites_fijos = self._parsear_limites(
            self.parameterAsString(parameters, self.LIMITES, context))
        if self._limites_fijos:
            feedback.pushInfo(
                f"Límites de simbolización fijos: "
                f"{self._limites_fijos[0]:.3f} … "
                f"{self._limites_fijos[1]:.3f}")
        indices = [INDICES_ESPECTRALES[i] for i in
                   self.parameterAsEnums(parameters, self.INDICES, context)]
        ruta_coef = (self.parameterAsFile(
            parameters, self.COEF_TC, context) or '').strip()
        amplitud = self.parameterAsBool(
            parameters, self.AMPLITUD, context)
        min_obs = self.parameterAsInt(
            parameters, self.MIN_OBS, context)
        meses_seca = self._parsear_meses(self.parameterAsString(
            parameters, self.MESES_SECA, context))
        meses_lluvia = self._parsear_meses(self.parameterAsString(
            parameters, self.MESES_LLUVIA, context))
        carpeta = (self.parameterAsFile(parameters, self.CARPETA, context)
                   or '').strip()

        nubes_aoi_max = self.parameterAsDouble(parameters, self.NUBES_AOI, context)
        miniaturas = self.parameterAsBool(parameters, self.MINIATURAS, context)
        px_min = self.parameterAsInt(parameters, self.PX_MINIATURA, context)
        self._abrir_hoja = self.parameterAsBool(
            parameters, self.ABRIR, context)
        self._ruta_hoja = None
        self._cache_nombres = {}
        self._duenos_nombre = {}
        self._coef_tc = TC_COEFICIENTES
        self._escala_reportada = set()
        self._indices_escritos = []
        self._pendientes_amplitud = []
        fuente_sel = self.parameterAsSource(parameters, self.SELECCION, context)

        ext = self.parameterAsExtent(
            parameters, self.EXTENSION, context,
            QgsCoordinateReferenceSystem('EPSG:4326'))
        bbox = [ext.xMinimum(), ext.yMinimum(), ext.xMaximum(), ext.yMaximum()]

        # La capa de AOI, si existe, MANDA sobre el parámetro de extensión.
        # Usar solo la extensión producía búsquedas sobre todo el país y
        # estadísticas SCL calculadas fuera del área de interés real.
        fuente_aoi = self.parameterAsSource(parameters, self.AOI, context)
        idx_area = self.parameterAsEnum(parameters, self.AREA, context)

        if idx_area == 0 and fuente_aoi is not None:
            bbox_aoi = self._extent_fuente_4326(fuente_aoi, context, feedback)
            if bbox_aoi:
                bbox = bbox_aoi
                feedback.pushInfo(
                    "Área de búsqueda: envolvente de la capa de AOI "
                    "(se ignora el parámetro «Extensión de búsqueda»).")
            else:
                feedback.pushWarning(
                    "[!] No se pudo obtener la extensión de la capa de AOI; "
                    "se usa el parámetro «Extensión de búsqueda».")
        else:
            feedback.pushInfo(
                "Área de búsqueda: extensión indicada / lienzo del mapa.")
            if fuente_aoi is not None:
                feedback.pushInfo(
                    "La capa de AOI se usará solo como recorte en la descarga.")

        feedback.pushInfo(f"Catálogo   : {etiqueta_cat}")
        feedback.pushInfo(f"Colección  : {coleccion}")
        if familia == 'ls':
            feedback.pushInfo(
                f"Landsat C2 L2: los recortes se guardan en DN sin escalar. "
                f"Reflectancia = DN * {LS_ESCALA_MULT} {LS_ESCALA_SUMA}. "
                f"No aplicar ese escalado a qa_pixel.")
        feedback.pushInfo(f"BBOX (4326): {[round(v, 6) for v in bbox]}")
        ancho_km = ((bbox[2] - bbox[0]) * 111.32
                    * math.cos(math.radians((bbox[1] + bbox[3]) / 2.0)))
        alto_km = (bbox[3] - bbox[1]) * 110.57
        feedback.pushInfo(f"Área útil  : {ancho_km:.2f} x {alto_km:.2f} km aprox.")

        # Un área mucho mayor que una teselá vuelve inútiles las estadísticas
        # por escena: ninguna teselá cubre el rectángulo entero, así que la
        # fracción de nube se calcula sobre un recorte casi vacío y sale n/d.
        if ancho_km > 100.0 or alto_km > 100.0:
            feedback.pushWarning(
                f"[!] El área de búsqueda ({ancho_km:.0f} x {alto_km:.0f} km) "
                f"supera el tamaño de una teselá Sentinel-2 (110 km). La "
                f"nubosidad dentro del AOI y las miniaturas se calcularán sobre "
                f"un rectángulo que ninguna escena cubre por completo, y "
                f"muchas darán «n/d». Acerque el lienzo o use la capa de AOI.")

        # --- 1. Origen de las escenas ----------------------------------------
        if fuente_sel is not None:
            cat_rev, col_rev, _n_rev = self._procedencia_huellas(fuente_sel)
            if cat_rev:
                feedback.pushInfo(
                    f"Procedencia de las huellas: {cat_rev} "
                    f"(colección «{col_rev or '?'}») — coincide con el "
                    f"catálogo elegido.")
            else:
                # Capa de una versión anterior a 1.31.0: no se puede
                # verificar. Se avisa en vez de rechazarla, para no dejar
                # inservibles las huellas ya guardadas.
                feedback.pushWarning(
                    "[!] Esta capa de huellas no trae el campo «catalogo» "
                    "(la escribió una versión anterior del complemento), así "
                    "que NO se puede comprobar de qué catálogo salieron sus "
                    "URL. Asegúrese usted de que «Catálogo» es el mismo de "
                    "la primera pasada: la colección «sentinel-2-l2a» existe "
                    "en Earth Search y en Planetary Computer, y mezclarlos "
                    "falla de un modo que parece un problema de datos. "
                    "Regenerando las huellas con esta versión queda "
                    "verificado de forma automática.")
            items = self._items_desde_capa(fuente_sel, feedback)
            feedback.pushInfo(
                f"Modo revisión: {len(items)} escenas tomadas de la capa de "
                f"huellas (se omite la búsqueda STAC).")
            revisadas = True
            reducir_por = None
        else:
            feedback.pushInfo(f"Periodo    : {f_ini} / {f_fin}   Nubes < {nubes} %")
            feedback.pushInfo(f"Selección  : {etiqueta_orden}")
            if familia == 's2' and f_ini < '2017-01-01':
                feedback.pushInfo(
                    "Nota: el archivo L2A de Earth Search es escaso antes de "
                    "2017 y solo global desde finales de 2018. Las fechas "
                    "anteriores no devolverán resultados.")

            if estrategia in ('anual', 'mensual'):
                candidatos = self._buscar_stac(
                    base_url, coleccion, bbox, f_ini, f_fin, nubes,
                    MAX_CANDIDATOS, feedback, direccion='asc')
                feedback.pushInfo(f"Candidatos en el periodo: {len(candidatos)}")
                grupos = self._agrupar_por_periodo(
                    candidatos, estrategia, n_cand, feedback)
                self._periodos_sin_datos(
                    grupos, f_ini, f_fin, estrategia, nubes, feedback)

                claves = sorted(grupos)
                if claves:
                    feedback.pushInfo(
                        f"Periodos con datos: {len(claves)} "
                        f"[{claves[0]} … {claves[-1]}]")
                if len(claves) > max_esc:
                    feedback.pushWarning(
                        f"[!] Hay {len(claves)} periodos pero el tope es "
                        f"{max_esc}: se conservan los {max_esc} más recientes. "
                        f"Suba «Número máximo de escenas» para la serie completa.")
                    claves = claves[-max_esc:]

                items = []
                for clave in claves:
                    items.extend(grupos[clave])
                feedback.pushInfo(
                    f"Se evaluarán con SCL {len(items)} candidatos "
                    f"({n_cand} por periodo como máximo).")
                reducir_por = estrategia
            else:
                reducir_por = None
                items = self._buscar_stac(
                    base_url, coleccion, bbox, f_ini, f_fin, nubes, max_esc,
                    feedback, direccion=estrategia)
            revisadas = False
            if not items:
                feedback.pushWarning(
                    "[!] Sin resultados. Amplíe el rango de fechas, suba el "
                    "umbral de nubosidad o verifique que el AOI esté en una "
                    "zona cubierta.")
            feedback.pushInfo(f"Escenas encontradas: {len(items)}")
            if items:
                fechas = sorted(str(it.get('properties', {}).get('datetime', ''))[:10]
                                for it in items)
                feedback.pushInfo(
                    f"Rango recuperado: {fechas[0]} / {fechas[-1]} "
                    f"(solicitado {f_ini} / {f_fin})")
                if fechas[0][:4] == fechas[-1][:4] and f_ini[:4] != f_fin[:4]:
                    feedback.pushWarning(
                        f"[!] Todas las escenas son de {fechas[0][:4]} aunque "
                        f"pidió desde {f_ini}. Con «Más recientes primero» el "
                        f"tope de {max_esc} escenas se agota en el año más "
                        f"reciente. Use «Mejor escena por año» para cubrir "
                        f"todo el periodo.")

        # --- 2. Token SAS (solo Planetary Computer) --------------------------
        token = None
        if requiere_token and items:
            token = self._token_pc(coleccion, feedback)

        # --- 3. Revisión: nubosidad real en el AOI + miniaturas --------------
        carpeta_min = None
        if miniaturas and carpeta:
            carpeta_min = os.path.join(carpeta, 'miniaturas')
            os.makedirs(carpeta_min, exist_ok=True)

        analizar = (nubes_aoi_max < 100.0) or bool(carpeta_min)

        if items and reducir_por:
            # Pasada 1: solo SCL sobre todos los candidatos del periodo. Sin
            # miniaturas todavía — generarlas para escenas que se van a
            # descartar sería trabajo y disco tirados.
            feedback.pushInfo(
                "\nEvaluando nubosidad sobre el AOI de cada candidato...")
            self._revisar_escenas(items, bbox, None, px_min, token,
                                  requiere_token, feedback)
            items = self._reducir_por_periodo(items, reducir_por, feedback)

        if items and analizar and not revisadas:
            # Pasada 2: SCL ya calculado se reutiliza; aquí se hacen los PNG.
            self._revisar_escenas(items, bbox, carpeta_min, px_min, token,
                                  requiere_token, feedback)

        if nubes_aoi_max < 100.0 and not revisadas:
            antes = len(items)
            items = [it for it in items
                     if it.get('_nubes_aoi', -1.0) < 0
                     or it['_nubes_aoi'] <= nubes_aoi_max]
            descartadas = antes - len(items)
            if descartadas:
                feedback.pushInfo(
                    f"Descartadas por nubosidad en AOI (> {nubes_aoi_max} %): "
                    f"{descartadas} de {antes}")

        # --- 4. Capa de huellas ---------------------------------------------
        campos = QgsFields()
        campos.append(QgsField('id', _STR))
        campos.append(QgsField('fecha', _STR))
        campos.append(QgsField('hora_utc', _STR))
        campos.append(QgsField('nubes_pct', _FLOAT))
        campos.append(QgsField('nubes_aoi', _FLOAT))
        campos.append(QgsField('datos_pct', _FLOAT))
        campos.append(QgsField('plataforma', _STR))
        campos.append(QgsField('tile', _STR))
        campos.append(QgsField('epsg', _INT))
        campos.append(QgsField('thumb', _STR))
        campos.append(QgsField('thumb_url', _STR))
        campos.append(QgsField('url_visual', _STR))
        campos.append(QgsField('assets', _STR))
        campos.append(QgsField('coleccion', _STR))
        # «catalogo» guarda el ENDPOINT que produjo las URL de «assets».
        # Sin él el flujo de dos pasos no puede verificarse: la colección
        # «sentinel-2-l2a» existe en Earth Search Y en Planetary Computer,
        # así que el nombre de colección no distingue uno de otro. Con las
        # huellas de Earth Search y PC seleccionado en la segunda pasada,
        # la colección coincide mientras los href son de Element84 y el
        # código intenta firmarlos con un token SAS: falla de un modo que
        # parece un problema de datos.
        campos.append(QgsField('catalogo', _STR))

        sink, dest_id = self.parameterAsSink(
            parameters, self.HUELLAS, context, campos,
            _WKB_POLIGONO, QgsCoordinateReferenceSystem('EPSG:4326'))
        for item in (items if sink is not None else []):
            props = item.get('properties', {})
            feat = QgsFeature(campos)
            feat.setGeometry(_geom_desde_geojson(item.get('geometry')))
            fecha_hora = str(props.get('datetime', ''))
            href_vis = _resolver_asset(item.get('assets', {}),
                                       'visual (RGB 8-bit)', familia)
            feat.setAttributes([
                str(item.get('id', '')),
                fecha_hora[:10],
                fecha_hora[11:19],
                float(props.get('eo:cloud_cover') or 0.0),
                float(item.get('_nubes_aoi', -1.0)),
                float(item.get('_datos_pct', -1.0)),
                str(props.get('platform', '')),
                self._tile_mgrs(props),
                int(props.get('proj:epsg')
                    or props.get('proj:code', '0').replace('EPSG:', '')
                    or 0),
                str(item.get('_thumb', '')),
                _url_archivo(item.get('_thumb', '')),
                (_firmar_pc(href_vis, self._token_vigente(feedback, token))
                 if requiere_token else (href_vis or '')),
                self._serializar_assets(item),
                coleccion,
                base_url,
            ])
            sink.addFeature(feat)

        # Post-procesador de la capa de huellas: consejo emergente + visor.
        try:
            if sink is not None and dest_id and \
                    context.willLoadLayerOnCompletion(dest_id):
                pp_huellas = HuellasPostProcessor()
                _registrar_postproc(pp_huellas)
                context.layerToLoadOnCompletionDetails(dest_id).setPostProcessor(
                    pp_huellas)
        except Exception as e:
            feedback.pushDebugInfo(f"[postproc_huellas] {e}")

        if items and not revisadas and analizar:
            if sink is not None:
                feedback.pushInfo(
                    "\nRevise la capa de huellas y quédese con las escenas "
                    "útiles, de cualquiera de estas dos formas:")
                feedback.pushInfo(
                    "  a) SELECCIONAR: pase el cursor sobre cada polígono "
                    "(Ver → Consejos de mapa) o abra la tabla de atributos "
                    "en vista de formulario, seleccione las escenas útiles y "
                    "vuelva a ejecutar marcando «Entidades seleccionadas "
                    "solamente» en «Huellas ya revisadas».")
                feedback.pushInfo(
                    "  b) DEPURAR: borre de la tabla de atributos las filas "
                    "que no quiera, guarde la capa, y vuelva a ejecutar "
                    "indicándola en «Huellas ya revisadas» SIN marcar "
                    "«Entidades seleccionadas solamente». Se descarga lo que "
                    "quedó.")
                feedback.pushInfo(
                    "  La opción (b) deja un archivo que registra exactamente "
                    "qué escenas usó el estudio; una selección se pierde al "
                    "cerrar el proyecto.")
                if self._salida_volatil(parameters.get(self.HUELLAS)):
                    # Una capa temporal muere con la sesión, y con ella las
                    # URL de «assets». El usuario no lo descubre hoy, sino
                    # mañana, cuando el segundo paso no tenga de dónde leer.
                    feedback.pushWarning(
                        "[!] La capa de huellas se está creando como CAPA "
                        "TEMPORAL: desaparece al cerrar QGIS, y con ella las "
                        "URL del campo «assets» que necesita el segundo "
                        "paso. Si va a continuar en otra sesión, guárdela "
                        "ahora (clic derecho sobre la capa → Exportar → "
                        "Guardar entidades como… → GeoPackage) o vuelva a "
                        "ejecutar eligiendo un archivo en el parámetro "
                        "«Capa de huellas».")
            elif carpeta_min:
                feedback.pushInfo(
                    "\nAbra la hoja de contactos para revisar las escenas: "
                    f"{os.path.join(carpeta_min, 'index.html')}")

        resultados = {}
        if sink is not None and dest_id:
            resultados[self.HUELLAS] = dest_id
        if modo == 0 or not items:
            return resultados

        # --- 4. Cutline opcional --------------------------------------------
        cutline = None
        if fuente_aoi is not None:
            cutline = self._escribir_cutline(fuente_aoi, context, feedback)

        # --- 5. Carga remota o descarga --------------------------------------
        self._avisar_colisiones(items, feedback)

        if composiciones:
            feedback.pushInfo(
                f"\nComposiciones seleccionadas ({len(composiciones)}): "
                + ", ".join(c[0] for c in composiciones))
            feedback.pushInfo(
                f"Se generarán {len(items) * len(composiciones)} capas "
                f"({len(items)} escenas x {len(composiciones)} composiciones).")

        if modo == 1 and requiere_token:
            # La renovación de _token_vigente() protege las lecturas que hace
            # el algoritmo, pero no las que QGIS hará después: la URL firmada
            # queda escrita dentro del VRT, y cada desplazamiento del mapa la
            # vuelve a usar. Pasada la caducidad, las capas dejan de cargar.
            feedback.pushWarning(
                "[!] Modo remoto con Planetary Computer: las capas cargadas "
                "llevan un token que caduca en unos 45 minutos. Después, "
                "QGIS no podrá leer más bloques y las zonas no visitadas "
                "quedarán vacías. Para trabajo que dure más, use el modo "
                "de descarga o un catálogo de Earth Search, que no requiere "
                "token.")

        if modo == 1:
            for etiqueta_comp, sufijo_comp, bandas_rgb in composiciones:
                if feedback.isCanceled():
                    return resultados
                feedback.pushInfo(f"\n— {etiqueta_comp}")
                self._componer_remoto(items, bandas_rgb, sufijo_comp, token,
                                      requiere_token, context, feedback, bbox,
                                      visibles)
            if not composiciones:
                self._cargar_remoto(items, bandas, token, requiere_token,
                                    context, feedback, bbox, visibles)
        else:
            os.makedirs(carpeta, exist_ok=True)
            for etiqueta_comp, sufijo_comp, bandas_rgb in composiciones:
                if feedback.isCanceled():
                    return resultados
                feedback.pushInfo(f"\n— {etiqueta_comp}")
                self._componer_descarga(items, bandas_rgb, sufijo_comp, token,
                                        requiere_token, bbox, cutline, carpeta,
                                        estirar, context, feedback, visibles)
            if not composiciones and not indices:
                self._descargar(items, bandas, token, requiere_token, bbox,
                                cutline, carpeta, feedback)
            if indices:
                feedback.pushInfo(
                    f"\nÍndices espectrales ({len(indices)}): "
                    + ", ".join(i[1] for i in indices))
                feedback.pushInfo(
                    "Se calculan sobre reflectancia, con nube y sombra a NaN.")
                hay_tc = any(i[1] in ('TCB', 'TCG', 'TCW') for i in indices)
                if hay_tc:
                    if ruta_coef:
                        cargados = self._cargar_coeficientes_tc(
                            ruta_coef, feedback)
                        if cargados:
                            self._coef_tc = cargados
                    else:
                        self._escribir_plantilla_tc(carpeta, feedback)
                self._avisar_tc(indices, items, feedback)
                self._calcular_indices(items, indices, token, requiere_token,
                                       bbox, cutline, carpeta, context,
                                       feedback, visibles)
                if amplitud:
                    feedback.pushInfo(
                        f"\nAmplitud fenológica — seca {meses_seca} vs "
                        f"lluvia {meses_lluvia}")
                    self._amplitud_fenologica(
                        meses_seca, meses_lluvia, carpeta, context,
                        feedback, min_obs)
            resultados[self.SALIDA] = carpeta

        self._resultados = resultados
        return resultados

    def postProcessAlgorithm(self, context, feedback):
        """Se ejecuta en el HILO PRINCIPAL tras terminar el algoritmo.

        Es el único punto seguro para abrir el navegador: processAlgorithm()
        corre en un hilo de trabajo y llamar allí a QDesktopServices sería
        tocar la interfaz desde fuera del hilo de la GUI.
        """
        if self._abrir_hoja and self._ruta_hoja:
            if os.path.exists(self._ruta_hoja):
                try:
                    abierto = QDesktopServices.openUrl(
                        QUrl.fromLocalFile(self._ruta_hoja))
                    if abierto:
                        feedback.pushInfo(
                            f"Hoja de contactos abierta en el navegador: "
                            f"{self._ruta_hoja}")
                    else:
                        feedback.pushWarning(
                            f"[!] El sistema no pudo abrir el navegador. "
                            f"Abra manualmente: {self._ruta_hoja}")
                except Exception as e:
                    # Ejecución sin entorno gráfico (qgis_process, servidor):
                    # no es un fallo del proceso, solo no hay navegador.
                    feedback.pushInfo(
                        f"No se pudo abrir el navegador ({e}). "
                        f"Hoja de contactos en: {self._ruta_hoja}")
            else:
                feedback.pushWarning(
                    f"[!] No se encontró la hoja de contactos en "
                    f"{self._ruta_hoja}; no se abrió nada.")

        if self._pendientes_amplitud:
            _cargar_pendientes(self._pendientes_amplitud, context, feedback)
            self._pendientes_amplitud = []

        return self._resultados if self._resultados is not None else {}

    # ------------------------------------------------------------- internos
    def _buscar_stac(self, base_url, coleccion, bbox, f_ini, f_fin,
                     nubes, max_esc, feedback, direccion='desc'):
        """Consulta paginada al endpoint /search."""
        url = base_url.rstrip('/') + '/search'
        payload = {
            'collections': [coleccion],
            'bbox': bbox,
            'datetime': f'{f_ini}T00:00:00Z/{f_fin}T23:59:59Z',
            'limit': min(100, max_esc),
            'query': {'eo:cloud_cover': {'lt': float(nubes)}},
            'sortby': [{'field': 'properties.datetime',
                        'direction': 'asc' if direccion == 'asc' else 'desc'}],
        }

        items = []
        pagina = 0
        tope_paginas = 10 if max_esc <= 100 else 20
        while len(items) < max_esc and pagina < tope_paginas:
            if feedback.isCanceled():
                break
            try:
                datos = _http_json(url, payload)
            except urllib.error.HTTPError as e:
                cuerpo = ''
                try:
                    cuerpo = e.read().decode('utf-8', 'replace')[:400]
                except Exception as e2:
                    feedback.pushDebugInfo(f"[lectura_error_http] {e2}")
                raise QgsProcessingException(
                    f"[!] El catálogo respondió HTTP {e.code}. {cuerpo}\n"
                    f"Si el error es 400, la colección '{coleccion}' puede no "
                    f"aceptar el parámetro 'query'; pruebe el otro catálogo.")
            except urllib.error.URLError as e:
                raise QgsProcessingException(
                    f"[!] Sin conexión al catálogo STAC: {e.reason}\n"
                    f"Verifique red, proxy corporativo o firewall.")
            except Exception as e:
                raise QgsProcessingException(f"[!] Error en la búsqueda STAC: {e}")

            nuevos = datos.get('features', [])
            items.extend(nuevos)
            feedback.pushInfo(f"  página {pagina + 1}: {len(nuevos)} escenas")

            siguiente = None
            for enlace in datos.get('links', []):
                if enlace.get('rel') == 'next':
                    siguiente = enlace
                    break
            if not siguiente or not nuevos:
                break
            url = siguiente.get('href', url)
            cuerpo_sig = siguiente.get('body')
            if isinstance(cuerpo_sig, dict):
                payload = dict(payload)
                payload.update(cuerpo_sig)
            else:
                break
            pagina += 1

        return items[:max_esc]

    def _token_pc(self, coleccion, feedback):
        """Pide un token SAS anónimo y guarda su caducidad.

        El token dura unos 45 minutos desde su emisión (comprobado con una
        respuesta real: expiración = emisión + 45 min, con el inicio
        retrasado 24 h para tolerar desfases de reloj). Una ejecución larga
        lo agota a mitad de camino; por eso se guarda msft:expiry y
        _token_vigente() lo renueva antes de firmar cada asset.
        """
        self._pc_coleccion = coleccion
        try:
            datos = _http_json(PC_TOKEN_URL.format(col=coleccion))
            self._pc_token = datos.get('token')
            self._pc_expira = _parsear_expiracion(datos.get('msft:expiry'))
            if self._pc_expira:
                restante = self._pc_expira - datetime.now(timezone.utc)
                feedback.pushInfo(
                    f"Token SAS obtenido; caduca a las "
                    f"{self._pc_expira:%H:%M} UTC "
                    f"(en {int(restante.total_seconds() // 60)} min).")
            else:
                feedback.pushInfo(
                    "Token SAS obtenido (sin fecha de caducidad declarada; "
                    "no se podrá renovar automáticamente).")
            return self._pc_token
        except Exception as e:
            feedback.pushWarning(
                f"[_token_pc] No se pudo obtener el token SAS: {e}. "
                f"Los assets no serán accesibles; use Earth Search.")
            return None

    def _token_vigente(self, feedback, respaldo=None):
        """Token utilizable para firmar AHORA; lo renueva si está por caducar.

        Se llama justo antes de cada firma, no una vez por ejecución. Con un
        margen de 5 minutos, ninguna lectura de un asset debería empezar con
        un token a punto de expirar.
        """
        if self._pc_token is None or self._pc_coleccion is None:
            return respaldo
        if self._pc_expira is not None:
            restante = self._pc_expira - datetime.now(timezone.utc)
            if restante < TOKEN_MARGEN_RENOVACION:
                feedback.pushInfo(
                    f"Token SAS a {max(0, int(restante.total_seconds()))} s "
                    f"de caducar: se renueva.")
                self._token_pc(self._pc_coleccion, feedback)
        return self._pc_token or respaldo

    def _tile_mgrs(self, props):
        """Identificador de la malla del sensor.

        Sentinel-2 usa teselas MGRS (16PFS); Landsat usa la malla WRS-2, que
        se identifica con path y row (P015R053). Devolver 'NA' para Landsat,
        como hacía antes, dejaba todos los archivos de una serie con el mismo
        nombre salvo la fecha.
        """
        ya_resuelta = props.get('_malla')
        if ya_resuelta:
            return str(ya_resuelta)

        if self._familia == 'ls':
            path = props.get('landsat:wrs_path') or props.get('wrs_path')
            row = props.get('landsat:wrs_row') or props.get('wrs_row')
            if path and row:
                try:
                    return f"P{int(path):03d}R{int(row):03d}"
                except (TypeError, ValueError):
                    return f"P{path}R{row}"
            return ''

        zona = props.get('mgrs:utm_zone') or props.get('s2:mgrs_tile')
        if isinstance(zona, str):
            return zona
        banda = props.get('mgrs:latitude_band', '')
        cuadro = props.get('mgrs:grid_square', '')
        if zona and banda:
            return f"{zona}{banda}{cuadro}"
        return str(props.get('grid:code', ''))

    def _escribir_cutline(self, fuente, context, feedback):
        """Exporta el AOI a GeoJSON EPSG:4326 temporal para usarlo como cutline.

        La reproyección es obligatoria: el formato GeoJSON asume CRS84, de modo
        que un AOI en CRTM05 (EPSG:8908) escrito sin transformar produciría un
        recorte desplazado sin emitir error.
        """
        ruta = os.path.join(QgsProcessingUtils.tempFolder(), 'aoi_cutline.geojson')
        wgs84 = QgsCoordinateReferenceSystem('EPSG:4326')

        try:
            capa = fuente.materialize(QgsFeatureRequest())
            opciones = QgsVectorFileWriter.SaveVectorOptions()
            opciones.driverName = 'GeoJSON'
            opciones.fileEncoding = 'UTF-8'
            if capa.crs().isValid() and capa.crs() != wgs84:
                opciones.ct = QgsCoordinateTransform(
                    capa.crs(), wgs84, context.transformContext())
                feedback.pushInfo(
                    f"Cutline reproyectado {capa.crs().authid()} → EPSG:4326")

            resultado = QgsVectorFileWriter.writeAsVectorFormatV3(
                capa, ruta, context.transformContext(), opciones)
            if resultado and resultado[0] != QgsVectorFileWriter.NoError:
                feedback.pushWarning(f"[_escribir_cutline] {resultado[1]}")
                return None
            feedback.pushInfo(f"Cutline preparado: {ruta}")
            return ruta
        except Exception as e:
            feedback.pushWarning(
                f"[_escribir_cutline] No se pudo preparar el AOI como cutline: {e}. "
                f"Se recortará por extensión rectangular.")
            return None

    def _prefijo_fuente(self, props):
        """Prefijo de archivo con el satélite concreto: Sent2C, Lands5, Lands8.

        Si el catálogo no informa la plataforma se cae al prefijo genérico
        (Sent / Lands) en lugar de inventar un número.
        """
        base = PREFIJO_FUENTE.get(self._familia, 'Sat')
        plataforma = str(props.get('platform') or '').lower().strip()
        if not plataforma:
            return base

        if self._familia == 'ls':
            m = _RE_PLAT_LS.search(plataforma)
            return f"Lands{m.group(1)}" if m else base

        m = _RE_PLAT_S2.search(plataforma)
        if m:
            return 'Sent2' + (m.group(1).upper() if m.group(1) else '')
        return base

    def _nombre_salida(self, item, banda, sufijo_comp=None):
        """Nombre de archivo: <Fuente>_<AAAA_MM>_<malla>_<sufijo>.tif

        Ej.: Sent2C_2026_09_16PFS_AGR.tif
             Lands5_2002_03_P015R053_AGR.tif

        La fecha lleva solo año y mes, así que dos escenas del mismo mes sobre
        la misma malla colisionarían. Cuando ocurre se añade el día (_d07) a
        partir de la segunda, en vez de dejar que la comprobación de «ya
        existe» descarte la escena en silencio. El resultado se memoriza por
        (escena, sufijo) para que la miniatura y la composición de una misma
        escena no reciban nombres distintos en llamadas sucesivas.
        """
        props = item.get('properties', {})
        fecha_iso = str(props.get('datetime', ''))[:10]
        anio_mes = fecha_iso[:7].replace('-', '_') or '0000_00'
        dia = fecha_iso[8:10] or '00'
        malla = self._tile_mgrs(props) or 'NA'
        sufijo = (sufijo_comp if sufijo_comp
                  else sufijo_banda(banda, self._familia))
        ident = str(item.get('id', ''))

        clave = (ident, sufijo)
        if clave in self._cache_nombres:
            return self._cache_nombres[clave]

        base = f"{self._prefijo_fuente(props)}_{anio_mes}_{malla}_{sufijo}"
        nombre = f"{base}.tif"
        dueno = self._duenos_nombre.get(nombre)
        if dueno is not None and dueno != ident:
            nombre = f"{base}_d{dia}.tif"
            contador = 2
            while True:
                otro = self._duenos_nombre.get(nombre)
                if otro is None or otro == ident:
                    break
                nombre = f"{base}_d{dia}_{contador}.tif"
                contador += 1

        self._duenos_nombre[nombre] = ident
        self._cache_nombres[clave] = nombre
        return nombre

    def _avisar_colisiones(self, items, feedback):
        """Avisa por adelantado de escenas que comparten año, mes y malla."""
        grupos = {}
        for item in items:
            props = item.get('properties', {})
            clave = (str(props.get('datetime', ''))[:7],
                     self._tile_mgrs(props) or 'NA')
            grupos.setdefault(clave, []).append(
                str(props.get('datetime', ''))[:10])
        repetidos = {k: v for k, v in grupos.items() if len(v) > 1}
        if repetidos:
            feedback.pushWarning(
                f"[!] {len(repetidos)} grupo(s) de escenas comparten año, mes "
                f"y malla. Como el nombre de archivo solo lleva año_mes, a "
                f"partir de la segunda se añade el día (_dDD) para no "
                f"sobrescribir:")
            for (mes, malla), fechas in sorted(repetidos.items()):
                feedback.pushWarning(
                    f"    {mes} / {malla}: {', '.join(sorted(fechas))}")

    def _cargar_remoto(self, items, bandas, token, firmar, context, feedback,
                       bbox=None, visibles=False):
        total = max(1, len(items) * len(bandas))
        hecho = 0
        carpeta_tmp = QgsProcessingUtils.tempFolder()
        for item in items:
            for banda in bandas:
                if feedback.isCanceled():
                    return
                href = _resolver_asset(item.get('assets', {}), banda,
                                       self._familia)
                if not href:
                    feedback.pushWarning(
                        f"[_cargar_remoto] Asset ausente '{banda}' en {item.get('id')}")
                    continue
                if firmar:
                    href = _firmar_pc(href, self._token_vigente(feedback, token))
                uri = _vsi_desde_href(href)
                nombre = self._nombre_salida(item, banda)[:-4]

                # Acotar al AOI envolviendo en un VRT de una banda; de lo
                # contrario QGIS carga la teselá completa de 110 km.
                limites = self._bbox_en_epsg(bbox, item, feedback)
                if limites:
                    try:
                        ruta_vrt = os.path.join(
                            carpeta_tmp, f"{nombre}_{uuid.uuid4().hex[:6]}.vrt")
                        vrt = gdal.BuildVRT(
                            ruta_vrt, [uri],
                            options=gdal.BuildVRTOptions(outputBounds=limites))
                        if vrt is not None:
                            vrt = None
                            uri = ruta_vrt
                    except Exception as e:
                        feedback.pushWarning(
                            f"[_cargar_remoto] No se pudo acotar {nombre}: {e}. "
                            f"Se carga la teselá completa.")
                try:
                    detalles = QgsProcessingContext.LayerDetails(
                        nombre, context.project(), nombre)
                    pp = RealcePostProcessor(
                        'canvas', visibles, self._metodo_realce,
                        self._n_sigma, self._paleta, self._modo_clase,
                        self._n_clases, self._limites_fijos)
                    _registrar_postproc(pp)
                    detalles.setPostProcessor(pp)
                    context.addLayerToLoadOnCompletion(uri, detalles)
                    feedback.pushInfo(f"  remoto → {nombre}")
                except Exception as e:
                    feedback.pushWarning(f"[_cargar_remoto] {nombre}: {e}")
                hecho += 1
                feedback.setProgress(int(100 * hecho / total))

    def _descargar(self, items, bandas, token, firmar, bbox, cutline,
                   carpeta, feedback):
        total = max(1, len(items) * len(bandas))
        hecho = 0
        for item in items:
            for banda in bandas:
                if feedback.isCanceled():
                    return
                href = _resolver_asset(item.get('assets', {}), banda,
                                       self._familia)
                if not href:
                    feedback.pushWarning(
                        f"[_descargar] Asset ausente '{banda}' en {item.get('id')}")
                    hecho += 1
                    continue
                if firmar:
                    href = _firmar_pc(href, self._token_vigente(feedback, token))
                origen = _vsi_desde_href(href)
                destino = os.path.join(carpeta, self._nombre_salida(item, banda))

                if os.path.exists(destino):
                    feedback.pushInfo(f"  omitido (ya existe) → {os.path.basename(destino)}")
                    hecho += 1
                    feedback.setProgress(int(100 * hecho / total))
                    continue

                res = _res_nativa(banda, self._familia)
                kwargs = dict(
                    format='GTiff',
                    xRes=res,
                    yRes=res,
                    targetAlignedPixels=True,
                    resampleAlg='near',
                    multithread=True,
                    warpMemoryLimit=512,
                    creationOptions=['COMPRESS=DEFLATE', 'PREDICTOR=2',
                                     'TILED=YES', 'BIGTIFF=IF_SAFER'],
                )
                if cutline:
                    kwargs['cutlineDSName'] = cutline
                    kwargs['cropToCutline'] = True
                else:
                    kwargs['outputBounds'] = (bbox[0], bbox[1], bbox[2], bbox[3])
                    kwargs['outputBoundsSRS'] = 'EPSG:4326'

                try:
                    ds = gdal.Warp(destino, origen, options=gdal.WarpOptions(**kwargs))
                    if ds is None:
                        raise RuntimeError('gdal.Warp devolvió None')
                    ds = None
                    tam = os.path.getsize(destino) / (1024 * 1024)
                    feedback.pushInfo(
                        f"  descargado → {os.path.basename(destino)} ({tam:.1f} MB)")
                except Exception as e:
                    feedback.pushWarning(
                        f"[_descargar] Falló {os.path.basename(destino)}: {e}")
                    if os.path.exists(destino):
                        try:
                            os.remove(destino)
                        except Exception as e2:
                            feedback.pushDebugInfo(f"[limpieza] {e2}")

                hecho += 1
                feedback.setProgress(int(100 * hecho / total))

    # ------------------------------------------------------ composiciones RGB
    def _log_mapeo_bandas(self, bandas_rgb, items, feedback):
        """Muestra a qué asset real resuelve cada banda lógica.

        Se lee del primer item devuelto por el catálogo, no de una tabla
        interna: si el proveedor cambia los nombres de asset, esto lo delata
        en el registro en lugar de fallar escena por escena.
        """
        if not items:
            return
        assets = items[0].get('assets', {})
        canal = ('R', 'G', 'B')
        partes = []
        for i, banda in enumerate(bandas_rgb):
            resuelto = None
            for nombre in alias_banda(banda, self._familia):
                if nombre in assets:
                    resuelto = nombre
                    break
            partes.append(f"{canal[i]}={resuelto or 'AUSENTE'}")
        feedback.pushInfo(
            f"    assets: {'  '.join(partes)}  "
            f"[{'Landsat' if self._familia == 'ls' else 'Sentinel-2'}, "
            f"{_res_nativa(bandas_rgb[0], self._familia):.0f} m]")
        ausentes = [p for p in partes if 'AUSENTE' in p]
        if ausentes:
            feedback.pushWarning(
                f"[_log_mapeo_bandas] Assets no encontrados en el catálogo: "
                f"{', '.join(ausentes)}. Los nombres disponibles son: "
                f"{', '.join(sorted(assets)[:20])}")

    def _hrefs_rgb(self, item, bandas_rgb, token, firmar, feedback):
        """Resuelve los tres assets de una composición. None si falta alguno."""
        rutas = []
        for banda in bandas_rgb:
            href = _resolver_asset(item.get('assets', {}), banda,
                                   self._familia)
            if not href:
                feedback.pushWarning(
                    f"[_hrefs_rgb] Asset ausente '{banda}' en {item.get('id')}; "
                    f"escena omitida para esta composición.")
                return None
            if firmar:
                href = _firmar_pc(href, self._token_vigente(feedback, token))
            rutas.append(_vsi_desde_href(href))
        return rutas

    def _componer_remoto(self, items, bandas_rgb, sufijo, token, firmar,
                         context, feedback, bbox=None, visibles=False):
        """VRT multibanda sobre /vsicurl/ — sin descarga de píxeles.

        El VRT se acota al AOI mediante outputBounds. Sin esto se cargaba la
        teselá completa de 110 x 110 km, que además arruina el realce: el corte
        acumulado se calculaba sobre nubes situadas a decenas de km del AOI.
        """
        carpeta_tmp = QgsProcessingUtils.tempFolder()
        total = max(1, len(items))
        self._log_mapeo_bandas(bandas_rgb, items, feedback)

        for i, item in enumerate(items):
            if feedback.isCanceled():
                return
            rutas = self._hrefs_rgb(item, bandas_rgb, token, firmar, feedback)
            if not rutas:
                continue

            nombre = self._nombre_salida(item, None, sufijo)[:-4]
            ruta_vrt = os.path.join(carpeta_tmp, f"{nombre}_{uuid.uuid4().hex[:6]}.vrt")
            try:
                opciones = dict(separate=True, resolution='highest',
                                resampleAlg='bilinear')
                limites = self._bbox_en_epsg(bbox, item, feedback)
                if limites:
                    opciones['outputBounds'] = limites
                # resolution='highest' alinea las bandas de 20 m a la malla de
                # 10 m; sin esto BuildVRT rechaza fuentes de distinto tamaño.
                vrt = gdal.BuildVRT(
                    ruta_vrt, rutas,
                    options=gdal.BuildVRTOptions(**opciones))
                if vrt is None:
                    raise RuntimeError('gdal.BuildVRT devolvió None')
                vrt = None
            except Exception as e:
                feedback.pushWarning(f"[_componer_remoto] {nombre}: {e}")
                continue

            try:
                detalles = QgsProcessingContext.LayerDetails(
                    nombre, context.project(), nombre)
                pp = RealcePostProcessor(
                    'canvas', visibles, self._metodo_realce,
                    self._n_sigma, self._paleta, self._modo_clase,
                    self._n_clases, self._limites_fijos)
                _registrar_postproc(pp)
                detalles.setPostProcessor(pp)
                context.addLayerToLoadOnCompletion(ruta_vrt, detalles)
                feedback.pushInfo(f"  composición remota → {nombre}")
            except Exception as e:
                feedback.pushWarning(f"[_componer_remoto] carga {nombre}: {e}")

            feedback.setProgress(int(100 * (i + 1) / total))

    def _componer_descarga(self, items, bandas_rgb, sufijo, token, firmar,
                           bbox, cutline, carpeta, estirar, context, feedback,
                           visibles=False):
        """Recorta las tres bandas a malla común y escribe un GeoTIFF RGB."""
        carpeta_tmp = QgsProcessingUtils.tempFolder()
        res = min(_res_nativa(b, self._familia) for b in bandas_rgb)
        total = max(1, len(items))
        self._log_mapeo_bandas(bandas_rgb, items, feedback)
        feedback.pushInfo(f"Resolución de salida: {res:.0f} m (la más fina de la tríada)")

        for i, item in enumerate(items):
            if feedback.isCanceled():
                return
            rutas = self._hrefs_rgb(item, bandas_rgb, token, firmar, feedback)
            if not rutas:
                continue

            destino = os.path.join(carpeta, self._nombre_salida(item, None, sufijo))
            if os.path.exists(destino):
                feedback.pushInfo(f"  omitido (ya existe) → {os.path.basename(destino)}")
                feedback.setProgress(int(100 * (i + 1) / total))
                continue

            marca = uuid.uuid4().hex[:8]
            temporales = []
            try:
                for j, origen in enumerate(rutas):
                    kwargs = dict(
                        format='GTiff',
                        xRes=res, yRes=res,
                        targetAlignedPixels=True,
                        resampleAlg='bilinear',
                        multithread=True,
                        warpMemoryLimit=512,
                        creationOptions=['COMPRESS=DEFLATE', 'TILED=YES'],
                    )
                    if cutline:
                        kwargs['cutlineDSName'] = cutline
                        kwargs['cropToCutline'] = True
                    else:
                        kwargs['outputBounds'] = tuple(bbox)
                        kwargs['outputBoundsSRS'] = 'EPSG:4326'

                    tmp = os.path.join(carpeta_tmp, f"rgb_{marca}_{j}.tif")
                    ds = gdal.Warp(tmp, origen, options=gdal.WarpOptions(**kwargs))
                    if ds is None:
                        raise RuntimeError(f'gdal.Warp devolvió None en banda {j + 1}')
                    ds = None
                    temporales.append(tmp)

                ruta_vrt = os.path.join(carpeta_tmp, f"rgb_{marca}.vrt")
                vrt = gdal.BuildVRT(ruta_vrt, temporales,
                                    options=gdal.BuildVRTOptions(separate=True))
                if vrt is None:
                    raise RuntimeError('gdal.BuildVRT devolvió None')
                vrt = None

                opciones = dict(
                    format='GTiff',
                    creationOptions=['COMPRESS=DEFLATE', 'PREDICTOR=2',
                                     'TILED=YES', 'BIGTIFF=IF_SAFER'],
                )
                if estirar:
                    cortes = self._percentiles_vrt(ruta_vrt, feedback)
                    opciones['outputType'] = gdal.GDT_Byte
                    opciones['scaleParams'] = [[c[0], c[1], 1, 255] for c in cortes]
                    opciones['noData'] = 0

                ds = gdal.Translate(destino, ruta_vrt,
                                    options=gdal.TranslateOptions(**opciones))
                if ds is None:
                    raise RuntimeError('gdal.Translate devolvió None')
                ds = None

                tam = os.path.getsize(destino) / (1024 * 1024)
                feedback.pushInfo(
                    f"  compuesto → {os.path.basename(destino)} ({tam:.1f} MB)")

                detalles = QgsProcessingContext.LayerDetails(
                    os.path.basename(destino)[:-4], context.project(),
                    os.path.basename(destino)[:-4])
                pp = RealcePostProcessor(
                    'whole', visibles, self._metodo_realce,
                    self._n_sigma, self._paleta, self._modo_clase,
                    self._n_clases, self._limites_fijos)
                _registrar_postproc(pp)
                detalles.setPostProcessor(pp)
                context.addLayerToLoadOnCompletion(destino, detalles)

            except Exception as e:
                feedback.pushWarning(
                    f"[_componer_descarga] Falló {os.path.basename(destino)}: {e}")
                if os.path.exists(destino):
                    try:
                        os.remove(destino)
                    except Exception as e2:
                        feedback.pushDebugInfo(f"[limpieza] {e2}")
            finally:
                for tmp in temporales:
                    try:
                        if os.path.exists(tmp):
                            os.remove(tmp)
                    except Exception as e2:
                        feedback.pushDebugInfo(f"[limpieza_tmp] {e2}")

            feedback.setProgress(int(100 * (i + 1) / total))

    def _percentiles_vrt(self, ruta_vrt, feedback, p_bajo=2, p_alto=98):
        """Percentiles 2-98 por banda, descartando nodata y ceros de borde."""
        cortes = []
        ds = gdal.Open(ruta_vrt)
        if ds is None:
            raise RuntimeError(f'No se pudo abrir el VRT: {ruta_vrt}')
        try:
            for idx in range(1, ds.RasterCount + 1):
                banda = ds.GetRasterBand(idx)
                arr = banda.ReadAsArray()
                if arr is None:
                    raise RuntimeError(f'No se pudo leer la banda {idx}')
                nodata = banda.GetNoDataValue()
                arr = arr.astype(np.float32).ravel()
                if nodata is not None:
                    arr = arr[arr != nodata]
                arr = arr[np.isfinite(arr)]
                arr = arr[arr > 0]  # ceros = relleno fuera de la teselá
                if arr.size < 100:
                    feedback.pushWarning(
                        f"[_percentiles_vrt] Banda {idx} con {arr.size} píxeles "
                        f"válidos; realce poco fiable.")
                    cortes.append((0.0, 10000.0))
                    continue
                vmin = float(np.percentile(arr, p_bajo))
                vmax = float(np.percentile(arr, p_alto))
                if vmax <= vmin:
                    vmin, vmax = float(arr.min()), float(arr.max())
                    if vmax <= vmin:
                        vmax = vmin + 1.0
                cortes.append((vmin, vmax))
                feedback.pushInfo(
                    f"    banda {idx}: corte {vmin:.0f} – {vmax:.0f}")
        finally:
            ds = None
        return cortes

    # ---------------------------------------------------- revisión de escenas
    def _revisar_escenas(self, items, bbox, carpeta_min, px, token, firmar,
                         feedback):
        """Calcula nubosidad dentro del AOI y genera miniaturas PNG.

        Anota cada item in situ con las claves privadas _nubes_aoi, _datos_pct
        y _thumb. Un valor de -1 indica que no se pudo determinar.
        """
        total = max(1, len(items))
        feedback.pushInfo("\nRevisando escenas (SCL + miniaturas)...")

        for i, item in enumerate(items):
            if feedback.isCanceled():
                return
            ident = str(item.get('id', '?'))

            item.setdefault('_thumb', '')

            if '_nubes_aoi' in item:
                # Ya medido en una pasada anterior; leer SCL otra vez sería
                # repetir una descarga por HTTP sin ganar nada.
                nubes = item['_nubes_aoi']
                datos = item.get('_datos_pct', -1.0)
            else:
                nubes, datos = self._nubes_en_aoi(
                    item, bbox, token, firmar, feedback)
                item['_nubes_aoi'] = nubes
                item['_datos_pct'] = datos

            if carpeta_min:
                ruta = self._miniatura(item, bbox, carpeta_min, px, token,
                                       firmar, feedback)
                item['_thumb'] = ruta or ''

            etiqueta = f"{nubes:5.1f} %" if nubes >= 0 else "  n/d"
            feedback.pushInfo(
                f"  [{i + 1:>3}/{total}] {ident[:44]:<44} nubes AOI {etiqueta}")
            feedback.setProgress(int(100 * (i + 1) / total))

        if carpeta_min:
            escritas = sum(1 for it in items
                           if it.get('_thumb') and os.path.exists(it['_thumb']))
            feedback.pushInfo(
                f"Miniaturas escritas: {escritas}/{len(items)} en {carpeta_min}")
            if escritas == 0:
                feedback.pushWarning(
                    "[!] No se generó ninguna miniatura. Revise las advertencias "
                    "[_miniatura] anteriores: causas habituales son ausencia del "
                    "asset TCI en la colección elegida, o falta de permisos de "
                    "escritura en la carpeta.")
            else:
                self._ruta_hoja = self._hoja_contactos(
                    items, carpeta_min, feedback)

    def _nubes_en_aoi(self, item, bbox, token, firmar, feedback):
        """Porcentaje de nube/sombra y de píxeles con dato dentro del AOI.

        Lee la banda SCL recortada al AOI a resolución degradada. El remuestreo
        DEBE ser 'near': SCL es categórica y cualquier interpolación produciría
        códigos de clase inexistentes.
        """
        href = _resolver_asset(item.get('assets', {}),
                               'scl (máscara de clases)', self._familia)
        if not href:
            banda_qa = 'QA_PIXEL' if self._familia == 'ls' else 'SCL'
            feedback.pushWarning(
                f"[_nubes_en_aoi] Sin banda {banda_qa} en {item.get('id')}; "
                f"no se puede evaluar la nubosidad local.")
            return -1.0, -1.0
        if firmar:
            href = _firmar_pc(href, self._token_vigente(feedback, token))

        es_ls = self._familia == 'ls'
        ruta = os.path.join(QgsProcessingUtils.tempFolder(),
                            f"qa_{uuid.uuid4().hex[:8]}.tif")
        try:
            ancho, alto = self._tamano_salida(bbox, 256)
            ds = gdal.Warp(
                ruta, _vsi_desde_href(href),
                options=gdal.WarpOptions(
                    format='GTiff',
                    dstSRS='EPSG:4326',
                    outputBounds=tuple(bbox),
                    outputBoundsSRS='EPSG:4326',
                    width=ancho, height=alto,
                    resampleAlg='near',
                    dstNodata=QA_RELLENO if es_ls else SCL_NODATA))
            if ds is None:
                raise RuntimeError('gdal.Warp devolvió None')
            arr = ds.GetRasterBand(1).ReadAsArray()
            ds = None
            if arr is None:
                raise RuntimeError('No se pudo leer la banda de calidad')

            if es_ls:
                sin_dato, nube = self._mascaras_qa_pixel(arr)
            else:
                arr = arr.astype(np.uint8)
                sin_dato = np.isin(arr, SCL_SIN_DATO)
                nube = np.isin(arr, SCL_NUBOSAS)

            validos = int((~sin_dato).sum())
            totales = int(arr.size)
            if validos == 0:
                return -1.0, 0.0

            nubosos = int((nube & ~sin_dato).sum())
            return (100.0 * nubosos / validos, 100.0 * validos / totales)
        except Exception as e:
            feedback.pushWarning(f"[_nubes_en_aoi] {item.get('id')}: {e}")
            return -1.0, -1.0
        finally:
            try:
                if os.path.exists(ruta):
                    os.remove(ruta)
            except Exception as e2:
                feedback.pushDebugInfo(f"[limpieza_scl] {e2}")

    def _miniatura(self, item, bbox, carpeta_min, px, token, firmar, feedback):
        """PNG del asset TCI recortado al AOI. Devuelve la ruta o None.

        TCI ya viene en 8 bits con estiramiento aplicado por ESA, de modo que
        no hace falta calcular percentiles: es la fuente correcta para una
        vista previa rápida (y la equivocada para cualquier cálculo).
        """
        href = _resolver_asset(item.get('assets', {}),
                               'visual (RGB 8-bit)', self._familia)
        nombre = self._nombre_salida(item, None, 'PREVIEW')[:-4] + '.png'
        destino = os.path.join(carpeta_min, nombre)
        if os.path.exists(destino):
            return destino

        if not href:
            # Landsat no tiene TCI: se compone la vista previa desde R/G/B.
            return self._miniatura_compuesta(
                item, bbox, destino, px, token, firmar, feedback)
        if firmar:
            href = _firmar_pc(href, self._token_vigente(feedback, token))

        tmp = os.path.join(QgsProcessingUtils.tempFolder(),
                           f"thumb_{uuid.uuid4().hex[:8]}.tif")
        try:
            ancho, alto = self._tamano_salida(bbox, px)
            ds = gdal.Warp(
                tmp, _vsi_desde_href(href),
                options=gdal.WarpOptions(
                    format='GTiff',
                    dstSRS='EPSG:4326',
                    outputBounds=tuple(bbox),
                    outputBoundsSRS='EPSG:4326',
                    width=ancho, height=alto,
                    resampleAlg='average'))
            if ds is None:
                raise RuntimeError('gdal.Warp devolvió None')
            ds = None

            # El controlador PNG solo admite CreateCopy, de ahí el paso doble.
            ds = gdal.Translate(destino, tmp,
                                options=gdal.TranslateOptions(format='PNG'))
            if ds is None:
                raise RuntimeError('gdal.Translate devolvió None')
            ds = None
            return destino
        except Exception as e:
            feedback.pushWarning(f"[_miniatura] {item.get('id')}: {e}")
            return None
        finally:
            for basura in (tmp, destino + '.aux.xml'):
                try:
                    if os.path.exists(basura):
                        os.remove(basura)
                except Exception as e2:
                    feedback.pushDebugInfo(f"[limpieza_thumb] {e2}")

    # ------------------------------------------- ida y vuelta capa <-> items

    def _serializar_assets(self, item):
        """Guarda las URL sin firmar de los assets soportados, como JSON."""
        assets = item.get('assets', {})
        compacto = {}
        for clave in CLAVES_BANDAS:
            href = _resolver_asset(assets, clave, self._familia)
            if href:
                # Se descarta la firma SAS: caduca en ~1 h y no sirve de nada
                # en una capa que el usuario revisará mañana.
                nombres = alias_banda(clave, self._familia)
                compacto[nombres[0]] = href.split('?')[0]
        return json.dumps(compacto, separators=(',', ':'))

    # ------------------------------------------------------ geometría del AOI

    def _extent_fuente_4326(self, fuente, context, feedback):
        """Extensión de una capa vectorial reproyectada a EPSG:4326."""
        try:
            rect = fuente.sourceExtent()
            if rect is None or rect.isEmpty():
                feedback.pushWarning(
                    "[_extent_fuente_4326] La capa de AOI no tiene extensión "
                    "válida; se mantiene el parámetro de extensión.")
                return None
            crs = fuente.sourceCrs()
            wgs84 = QgsCoordinateReferenceSystem('EPSG:4326')
            if crs.isValid() and crs != wgs84:
                tr = QgsCoordinateTransform(crs, wgs84, context.transformContext())
                rect = tr.transformBoundingBox(rect)
                feedback.pushInfo(
                    f"AOI reproyectado {crs.authid()} → EPSG:4326 para la consulta.")
            return [rect.xMinimum(), rect.yMinimum(),
                    rect.xMaximum(), rect.yMaximum()]
        except Exception as e:
            feedback.pushWarning(f"[_extent_fuente_4326] {e}")
            return None

    def _bbox_en_epsg(self, bbox, item, feedback):
        """Traduce el bbox WGS84 al SRC nativo de la escena (UTM).

        BuildVRT exige outputBounds en el SRC de las fuentes, no en grados.
        Devuelve None si no se conoce el EPSG, en cuyo caso no se acota.
        """
        if not bbox:
            return None
        props = item.get('properties', {})
        try:
            epsg = props.get('proj:epsg')
            if not epsg:
                codigo = str(props.get('proj:code', ''))
                epsg = int(codigo.replace('EPSG:', '')) if codigo else 0
            epsg = int(epsg or 0)
            if epsg <= 0:
                return None
            destino = QgsCoordinateReferenceSystem(f'EPSG:{epsg}')
            if not destino.isValid():
                return None
            tr = QgsCoordinateTransform(
                QgsCoordinateReferenceSystem('EPSG:4326'), destino,
                QgsProject.instance())
            rect = tr.transformBoundingBox(
                QgsRectangle(bbox[0], bbox[1], bbox[2], bbox[3]))
            return (rect.xMinimum(), rect.yMinimum(),
                    rect.xMaximum(), rect.yMaximum())
        except Exception as e:
            feedback.pushWarning(
                f"[_bbox_en_epsg] {item.get('id')}: {e}. Se carga sin acotar.")
            return None

    # ----------------------------------------------------- hoja de contactos
    def _hoja_contactos(self, items, carpeta_min, feedback):
        """Escribe un index.html con todas las miniaturas y sus estadísticas.

        Es la vía de revisión que no depende de los consejos de mapa de QGIS
        (que el usuario debe activar a mano en Ver → Consejos de mapa) ni del
        widget de recurso externo. Se abre en cualquier navegador.
        """
        ruta = os.path.join(carpeta_min, 'index.html')

        def clave(it):
            v = it.get('_nubes_aoi', -1.0)
            return v if v is not None and v >= 0 else 999.0

        tarjetas = []
        for it in sorted(items, key=clave):
            thumb = it.get('_thumb') or ''
            if not thumb or not os.path.exists(thumb):
                continue
            props = it.get('properties', {})
            nubes_aoi = it.get('_nubes_aoi', -1.0)
            datos = it.get('_datos_pct', -1.0)
            etiqueta = f"{nubes_aoi:.1f} %" if nubes_aoi >= 0 else "n/d"
            color = '#1a7f37' if 0 <= nubes_aoi <= 10 else (
                '#9a6700' if 0 <= nubes_aoi <= 30 else '#b00')
            tarjetas.append(f"""
  <figure>
    <img src="{os.path.basename(thumb)}" loading="lazy">
    <figcaption>
      <b>{str(props.get('datetime', ''))[:10]}</b> &middot; tile {self._tile_mgrs(props)}<br>
      nubes AOI <span style="color:{color}"><b>{etiqueta}</b></span>
      &middot; escena {float(props.get('eo:cloud_cover') or 0):.0f} %<br>
      p&iacute;xeles con dato {datos:.0f} %<br>
      <code>{it.get('id', '')}</code>
    </figcaption>
  </figure>""")

        if not tarjetas:
            feedback.pushWarning(
                "[_hoja_contactos] Ninguna miniatura disponible; no se generó "
                "la hoja de contactos.")
            return None

        html = f"""<!DOCTYPE html>
<html lang="es"><head><meta charset="utf-8">
<title>Revisi&oacute;n de escenas Sentinel-2</title>
<style>
 body {{ font-family: sans-serif; background:#f6f6f6; margin:16px; }}
 h1 {{ font-size:16px; }}
 .rejilla {{ display:flex; flex-wrap:wrap; gap:14px; }}
 figure {{ margin:0; background:#fff; border:1px solid #ddd; border-radius:6px;
           padding:8px; width:300px; }}
 img {{ width:100%; height:auto; display:block; border-radius:3px;
        background:#000; }}
 figcaption {{ font-size:11px; line-height:1.5; margin-top:6px; color:#333; }}
 code {{ font-size:10px; color:#666; }}
</style></head><body>
<h1>Escenas ordenadas por nubosidad dentro del AOI ({len(tarjetas)})</h1>
<div class="rejilla">{''.join(tarjetas)}
</div></body></html>"""

        try:
            with open(ruta, 'w', encoding='utf-8', newline='\n') as fh:
                fh.write(html)
            feedback.pushInfo(f"Hoja de contactos: {ruta}")
            return ruta
        except Exception as e:
            feedback.pushWarning(f"[_hoja_contactos] {e}")
            return None

    def _carpeta_utilizable(self, ruta):
        """Verifica que la carpeta exista (o se pueda crear) y sea escribible.

        Devuelve (True, '') o (False, motivo). Se comprueba aquí, en
        checkParameterValues, para que el usuario lo sepa antes de que el
        algoritmo pase veinte minutos descargando contra un destino inválido.
        """
        try:
            if os.path.isfile(ruta):
                return False, (
                    f"«{ruta}» es un archivo, no una carpeta. Seleccione un "
                    f"directorio.")
            if not os.path.isdir(ruta):
                try:
                    os.makedirs(ruta, exist_ok=True)
                except Exception as e:
                    return False, f"no se pudo crear «{ruta}»: {e}"
            if not os.access(ruta, os.W_OK):
                return False, (
                    f"sin permisos de escritura en «{ruta}». Elija una carpeta "
                    f"de su perfil de usuario.")

            # Advertir si cae bajo la carpeta temporal de Processing: los PNG
            # sobrevivirían a la ejecución pero no al cierre de QGIS.
            try:
                temporal = os.path.normcase(
                    os.path.abspath(QgsProcessingUtils.tempFolder()))
                elegida = os.path.normcase(os.path.abspath(ruta))
                if elegida.startswith(temporal):
                    return False, (
                        f"«{ruta}» está dentro de la carpeta temporal de "
                        f"Processing, que QGIS borra al cerrar la sesión. "
                        f"Elija una carpeta permanente.")
            except Exception as e:
                import logging
                logging.getLogger('BuscarSentinel2').debug(f"[temp_check] {e}")

            return True, ''
        except Exception as e:
            return False, f"error al comprobar «{ruta}»: {e}"

    # --------------------------------------------- miniatura sin asset visual

    def _miniatura_compuesta(self, item, bbox, destino, px, token, firmar,
                             feedback):
        """Miniatura RGB a partir de red/green/blue, con realce 2-98 %.

        Landsat no publica un asset TCI equivalente al de Sentinel-2, así que
        la vista previa hay que componerla y estirarla aquí. Las bandas son
        enteros de 16 bits en DN de reflectancia, no bytes listos para mostrar.
        """
        bandas = ("red (B04, 10 m)", "green (B03, 10 m)", "blue (B02, 10 m)")
        rutas = self._hrefs_rgb(item, bandas, token, firmar, feedback)
        if not rutas:
            return None

        marca = uuid.uuid4().hex[:8]
        carpeta_tmp = QgsProcessingUtils.tempFolder()
        temporales = []
        try:
            ancho, alto = self._tamano_salida(bbox, px)
            for j, origen in enumerate(rutas):
                tmp = os.path.join(carpeta_tmp, f"prev_{marca}_{j}.tif")
                ds = gdal.Warp(
                    tmp, origen,
                    options=gdal.WarpOptions(
                        format='GTiff',
                        dstSRS='EPSG:4326',
                        outputBounds=tuple(bbox),
                        outputBoundsSRS='EPSG:4326',
                        width=ancho, height=alto,
                        resampleAlg='average'))
                if ds is None:
                    raise RuntimeError(f'gdal.Warp devolvió None en banda {j + 1}')
                ds = None
                temporales.append(tmp)

            ruta_vrt = os.path.join(carpeta_tmp, f"prev_{marca}.vrt")
            vrt = gdal.BuildVRT(ruta_vrt, temporales,
                                options=gdal.BuildVRTOptions(separate=True))
            if vrt is None:
                raise RuntimeError('gdal.BuildVRT devolvió None')
            vrt = None
            temporales.append(ruta_vrt)

            cortes = self._percentiles_vrt(ruta_vrt, feedback)
            ds = gdal.Translate(
                destino, ruta_vrt,
                options=gdal.TranslateOptions(
                    format='PNG',
                    outputType=gdal.GDT_Byte,
                    scaleParams=[[c[0], c[1], 1, 255] for c in cortes]))
            if ds is None:
                raise RuntimeError('gdal.Translate devolvió None')
            ds = None
            return destino
        except Exception as e:
            feedback.pushWarning(
                f"[_miniatura_compuesta] {item.get('id')}: {e}")
            return None
        finally:
            for basura in temporales + [destino + '.aux.xml']:
                try:
                    if os.path.exists(basura):
                        os.remove(basura)
                except Exception as e2:
                    feedback.pushDebugInfo(f"[limpieza_prev] {e2}")

    # --------------------------------------------------------- reflectancia
    def _a_reflectancia(self, arr, props, escala_offset=None, banda='',
                        feedback=None):
        """Convierte DN a reflectancia de superficie.

        Fuente preferente: los campos scale y offset de raster:bands del propio
        asset, que es lo que declara el catálogo. Reflectancia = DN * escala +
        desplazamiento, en ese orden.

        Respaldo, solo si el ítem no declara radiometría: constantes por sensor
        e inferencia del desplazamiento BOA a partir de la línea base de
        proceso. Es menos fiable — un ítem reprocesado o servido por un tercero
        puede no encajar con la regla — de ahí que se avise en el registro.

        En cualquier caso el término aditivo NO se cancela en un cociente, así
        que calcular índices sobre DN crudo da valores desplazados.
        """
        arr = arr.astype(np.float32)

        if escala_offset is not None:
            escala, desplazamiento = escala_offset
            self._reportar_escala(
                banda, f"raster:bands (escala {escala:g}, "
                       f"desplazamiento {desplazamiento:g})", feedback)
            return arr * np.float32(escala) + np.float32(desplazamiento)

        if self._familia == 'ls':
            self._reportar_escala(
                banda, f"constantes internas Landsat C2 "
                       f"({LS_ESCALA_MULT:g}, {LS_ESCALA_SUMA:g})", feedback,
                aviso=True)
            return arr * LS_ESCALA_MULT + LS_ESCALA_SUMA

        baseline = str(props.get('s2:processing_baseline') or '')
        aplicado = props.get('earthsearch:boa_offset_applied')
        con_offset = bool(
            baseline and baseline >= S2_BASELINE_CON_OFFSET and not aplicado)
        self._reportar_escala(
            banda, f"inferencia por línea base '{baseline or 'desconocida'}'"
                   f"{' con desplazamiento BOA' if con_offset else ''}",
            feedback, aviso=True)
        if con_offset:
            arr = arr + S2_OFFSET_BASELINE
        return arr / S2_DIVISOR

    def _reportar_escala(self, banda, origen, feedback, aviso=False):
        """Informa una vez por banda de dónde salió la radiometría aplicada."""
        if feedback is None:
            return
        clave = (banda, origen)
        if self._escala_reportada is None:
            self._escala_reportada = set()
        if clave in self._escala_reportada:
            return
        self._escala_reportada.add(clave)
        etiqueta = sufijo_banda(banda, self._familia)
        if aviso:
            feedback.pushWarning(
                f"[!] {etiqueta}: el ítem no declara raster:bands; se usa "
                f"{origen}. Verifique la radiometría antes de publicar "
                f"resultados.")
        else:
            feedback.pushInfo(f"    {etiqueta}: {origen}")

    def _formula_indice(self, sufijo, bandas, props=None):
        """Calcula el índice a partir de las bandas ya en reflectancia."""
        if sufijo in ('TCB', 'TCG', 'TCW'):
            return self._tasseled_cap(sufijo, bandas, props or {})

        with np.errstate(divide='ignore', invalid='ignore'):
            if sufijo == 'NDVI':
                nir, red = bandas
                return (nir - red) / (nir + red)
            if sufijo == 'SAVI':
                nir, red = bandas
                return 1.5 * (nir - red) / (nir + red + 0.5)
            if sufijo == 'NDMI':
                nir, swir1 = bandas
                return (nir - swir1) / (nir + swir1)
            if sufijo == 'NBR':
                nir, swir2 = bandas
                return (nir - swir2) / (nir + swir2)
            if sufijo == 'MSI':
                swir1, nir = bandas
                return swir1 / nir
            if sufijo == 'NDRE':
                nir, re1 = bandas
                return (nir - re1) / (nir + re1)
            if sufijo == 'CIre':
                re3, re1 = bandas
                return (re3 / re1) - 1.0
        raise ValueError(f"Índice no implementado: {sufijo}")

    def _mascara_nubes(self, item, bbox, ancho, alto, token, firmar, feedback):
        """Máscara booleana de píxeles inválidos (nube, sombra o sin dato)."""
        href = _resolver_asset(item.get('assets', {}),
                               'scl (máscara de clases)', self._familia)
        if not href:
            feedback.pushWarning(
                f"[_mascara_nubes] Sin banda de calidad en {item.get('id')}; "
                f"el índice saldrá sin enmascarar.")
            return None
        if firmar:
            href = _firmar_pc(href, self._token_vigente(feedback, token))

        ruta = os.path.join(QgsProcessingUtils.tempFolder(),
                            f"msk_{uuid.uuid4().hex[:8]}.tif")
        try:
            es_ls = self._familia == 'ls'
            ds = gdal.Warp(
                ruta, _vsi_desde_href(href),
                options=gdal.WarpOptions(
                    format='GTiff', dstSRS='EPSG:4326',
                    outputBounds=tuple(bbox), outputBoundsSRS='EPSG:4326',
                    width=ancho, height=alto,
                    resampleAlg='near',
                    dstNodata=QA_RELLENO if es_ls else SCL_NODATA))
            if ds is None:
                raise RuntimeError('gdal.Warp devolvió None')
            arr = ds.GetRasterBand(1).ReadAsArray()
            ds = None
            if arr is None:
                raise RuntimeError('No se pudo leer la banda de calidad')
            if es_ls:
                sin_dato, nube = self._mascaras_qa_pixel(arr)
            else:
                arr = arr.astype(np.uint8)
                sin_dato = np.isin(arr, SCL_SIN_DATO)
                nube = np.isin(arr, SCL_NUBOSAS)
            return sin_dato | nube
        except Exception as e:
            feedback.pushWarning(f"[_mascara_nubes] {item.get('id')}: {e}")
            return None
        finally:
            try:
                if os.path.exists(ruta):
                    os.remove(ruta)
            except Exception as e2:
                feedback.pushDebugInfo(f"[limpieza_msk] {e2}")

    def _calcular_indices(self, items, indices, token, firmar, bbox, cutline,
                          carpeta, context, feedback, visibles):
        """Escribe un GeoTIFF Float32 por índice y escena, con nube a NaN."""
        total = max(1, len(items) * len(indices))
        hecho = 0
        carpeta_tmp = QgsProcessingUtils.tempFolder()

        for item in items:
            props = item.get('properties', {})
            for etiqueta, sufijo, bandas_log, solo_s2 in indices:
                if feedback.isCanceled():
                    return
                hecho += 1
                feedback.setProgress(int(100 * hecho / total))

                if solo_s2 and self._familia == 'ls':
                    feedback.pushWarning(
                        f"[_calcular_indices] {sufijo} requiere borde rojo; "
                        f"Landsat no lo tiene. Omitido.")
                    continue

                destino = os.path.join(
                    carpeta, self._nombre_salida(item, None, sufijo))
                mes_item = int(str(props.get('datetime', ''))[5:7] or 0)

                if os.path.exists(destino):
                    coincide, motivo = self._mismo_encuadre(
                        destino, bbox, cutline)
                    if coincide:
                        feedback.pushInfo(
                            f"  omitido (ya existe) → "
                            f"{os.path.basename(destino)}")
                        # Registrarlo igualmente: si no, queda fuera de la
                        # amplitud y la estación pierde una observación sin
                        # que nada lo indique.
                        self._indices_escritos.append(
                            (destino, mes_item, sufijo,
                             str(props.get('datetime', ''))[:10]))
                        continue
                    feedback.pushWarning(
                        f"[!] {os.path.basename(destino)} existe pero {motivo}. "
                        f"Se recalcula para que toda la serie comparta malla.")
                    try:
                        os.remove(destino)
                    except Exception as e2:
                        feedback.pushWarning(
                            f"[_calcular_indices] No se pudo borrar "
                            f"{destino}: {e2}")
                        continue

                res = min(_res_nativa(b, self._familia) for b in bandas_log)
                marca = uuid.uuid4().hex[:8]
                temporales = []
                try:
                    arrays = []
                    geo = proj = None
                    for j, banda in enumerate(bandas_log):
                        asset = _resolver_asset_obj(
                            item.get('assets', {}), banda, self._familia)
                        if not asset or not asset.get('href'):
                            raise RuntimeError(f"asset ausente: {banda}")
                        href = asset['href']
                        radiometria = _escala_offset(asset)
                        if firmar:
                            href = _firmar_pc(href, self._token_vigente(feedback, token))

                        kwargs = dict(
                            format='GTiff', xRes=res, yRes=res,
                            targetAlignedPixels=True, resampleAlg='bilinear',
                            multithread=True)
                        if cutline:
                            kwargs['cutlineDSName'] = cutline
                            kwargs['cropToCutline'] = True
                        else:
                            kwargs['outputBounds'] = tuple(bbox)
                            kwargs['outputBoundsSRS'] = 'EPSG:4326'

                        tmp = os.path.join(carpeta_tmp, f"ix_{marca}_{j}.tif")
                        ds = gdal.Warp(tmp, _vsi_desde_href(href),
                                       options=gdal.WarpOptions(**kwargs))
                        if ds is None:
                            raise RuntimeError(f'gdal.Warp None en {banda}')
                        if geo is None:
                            geo = ds.GetGeoTransform()
                            proj = ds.GetProjection()
                            alto, ancho = ds.RasterYSize, ds.RasterXSize
                        arrays.append(
                            self._a_reflectancia(
                                ds.GetRasterBand(1).ReadAsArray(), props,
                                radiometria, banda, feedback))
                        ds = None
                        temporales.append(tmp)

                    valores = self._formula_indice(sufijo, arrays, props)
                    valores = np.where(np.isfinite(valores), valores, np.nan)
                    valores = self._acotar_indice(sufijo, valores, feedback)

                    mascara = self._mascara_nubes(
                        item, bbox, ancho, alto, token, firmar, feedback)
                    if mascara is not None and mascara.shape == valores.shape:
                        valores = np.where(mascara, np.nan, valores)

                    driver = gdal.GetDriverByName('GTiff')
                    salida = driver.Create(
                        destino, ancho, alto, 1, gdal.GDT_Float32,
                        options=['COMPRESS=DEFLATE', 'PREDICTOR=3',
                                 'TILED=YES', 'BIGTIFF=IF_SAFER'])
                    if salida is None:
                        raise RuntimeError('No se pudo crear el GeoTIFF')
                    salida.SetGeoTransform(geo)
                    salida.SetProjection(proj)
                    banda_out = salida.GetRasterBand(1)
                    banda_out.WriteArray(valores.astype(np.float32))
                    banda_out.SetNoDataValue(float('nan'))
                    banda_out.SetDescription(sufijo)
                    salida.FlushCache()
                    salida = None

                    validos = np.isfinite(valores)
                    if validos.any():
                        feedback.pushInfo(
                            f"  {sufijo} → {os.path.basename(destino)}  "
                            f"[{np.nanmin(valores):.3f} … "
                            f"{np.nanmax(valores):.3f}], "
                            f"{100.0 * validos.mean():.0f} % de píxeles válidos")
                    else:
                        feedback.pushWarning(
                            f"[!] {os.path.basename(destino)} no tiene ningún "
                            f"píxel válido: la escena está totalmente cubierta "
                            f"sobre el AOI.")

                    self._indices_escritos.append(
                        (destino, mes_item, sufijo,
                         str(props.get('datetime', ''))[:10]))

                    nombre_capa = os.path.basename(destino)[:-4]
                    detalles = QgsProcessingContext.LayerDetails(
                        nombre_capa, context.project(), nombre_capa)
                    pp = RealcePostProcessor(
                        'whole', visibles, self._metodo_realce,
                        self._n_sigma, self._paleta, self._modo_clase,
                        self._n_clases, self._limites_fijos)
                    _registrar_postproc(pp)
                    detalles.setPostProcessor(pp)
                    context.addLayerToLoadOnCompletion(destino, detalles)

                except Exception as e:
                    feedback.pushWarning(
                        f"[_calcular_indices] {sufijo} en {item.get('id')}: {e}")
                    if os.path.exists(destino):
                        try:
                            os.remove(destino)
                        except Exception as e2:
                            feedback.pushDebugInfo(f"[limpieza_ix] {e2}")
                finally:
                    for tmp in temporales:
                        try:
                            if os.path.exists(tmp):
                                os.remove(tmp)
                        except Exception as e2:
                            feedback.pushDebugInfo(f"[limpieza_ix_tmp] {e2}")

    # ------------------------------------------------------- Tasseled Cap
    def _grupo_tc(self, props):
        """Grupo de coeficientes Tasseled Cap según el satélite del item."""
        plataforma = str(props.get('platform') or '').lower()
        if self._familia == 'ls':
            m = _RE_PLAT_LS.search(plataforma)
            numero = int(m.group(1)) if m else 0
            if numero in (4, 5):
                return 'TM'
            if numero == 7:
                return 'ETM'
            if numero in (8, 9):
                return 'OLI'
            return None
        return 'MSI'

    def _tasseled_cap(self, sufijo, bandas, props):
        """Componente Tasseled Cap como combinación lineal de las seis bandas.

        Los coeficientes dependen del sensor Y del tipo de reflectancia para el
        que se derivaron. Aplicar un conjunto TOA a un producto de superficie
        desalinea los ejes de la rotación, sobre todo en azul y verde, que es
        donde más pesa la corrección atmosférica.
        """
        grupo = self._grupo_tc(props)
        if grupo is None:
            raise RuntimeError(
                f"No hay coeficientes Tasseled Cap para la plataforma "
                f"'{props.get('platform')}'")
        tabla = self._coef_tc or TC_COEFICIENTES
        conjunto = tabla[grupo]
        coef = conjunto[sufijo[-1]]   # TCB->B, TCG->G, TCW->W
        if len(coef) != len(bandas):
            raise RuntimeError(
                f"Tasseled Cap {grupo}: se esperaban {len(coef)} bandas y "
                f"llegaron {len(bandas)}")

        acumulado = np.zeros(bandas[0].shape, dtype=np.float32)
        for peso, banda in zip(coef, bandas):
            acumulado = acumulado + np.float32(peso) * banda
        return acumulado

    def _avisar_tc(self, indices, items, feedback):
        """Advierte del desajuste entre tipo de reflectancia y coeficientes."""
        if not any(i[1] in ('TCB', 'TCG', 'TCW') for i in indices):
            return
        grupos = set()
        for item in items:
            g = self._grupo_tc(item.get('properties', {}))
            if g:
                grupos.add(g)
        producto = ('Landsat C2 Nivel 2 (superficie)' if self._familia == 'ls'
                    else 'Sentinel-2 L2A (BOA, superficie)')
        feedback.pushInfo(f"\nTasseled Cap — producto descargado: {producto}")
        for g in sorted(grupos):
            conjunto = (self._coef_tc or TC_COEFICIENTES)[g]
            feedback.pushInfo(f"  {g}: {conjunto['ref']}")
            if conjunto['tipo'] == 'TOA':
                feedback.pushWarning(
                    f"[!] Los coeficientes {g} se derivaron sobre reflectancia "
                    f"TOA y se están aplicando a reflectancia de SUPERFICIE. "
                    f"Los ejes de la rotación no quedan alineados; los valores "
                    f"son utilizables de forma relativa dentro de una misma "
                    f"fecha y sensor, pero NO son comparables entre sensores "
                    f"ni interpretables como los del artículo original.")
        if len(grupos) > 1:
            feedback.pushWarning(
                "[!] La selección mezcla sensores con conjuntos de coeficientes "
                "distintos. No interprete las diferencias entre fechas como "
                "cambio de cobertura sin armonizar antes.")

    def _cargar_coeficientes_tc(self, ruta, feedback):
        """Lee y valida un JSON de coeficientes; devuelve dict o None."""
        try:
            with open(ruta, 'r', encoding='utf-8') as fh:
                datos = json.load(fh)
        except Exception as e:
            feedback.pushWarning(
                f"[_cargar_coeficientes_tc] No se pudo leer «{ruta}»: {e}. "
                f"Se usan los coeficientes internos.")
            return None

        if not isinstance(datos, dict) or not datos:
            feedback.pushWarning(
                "[_cargar_coeficientes_tc] El archivo no contiene un objeto "
                "con grupos de sensor. Se usan los coeficientes internos.")
            return None

        validos = {}
        feedback.pushInfo(f"\nCoeficientes Tasseled Cap desde {ruta}:")
        for grupo, conjunto in datos.items():
            if grupo not in TC_COEFICIENTES:
                feedback.pushWarning(
                    f"[_cargar_coeficientes_tc] Grupo desconocido «{grupo}»; "
                    f"esperados: {', '.join(sorted(TC_COEFICIENTES))}. Omitido.")
                continue
            if not isinstance(conjunto, dict):
                continue
            etiqueta = f"{grupo} — {conjunto.get('ref', 'sin referencia')}"
            self._validar_ortonormalidad(conjunto, etiqueta, feedback)
            validos[grupo] = conjunto
            if str(conjunto.get('tipo', '')).lower() != 'superficie':
                feedback.pushWarning(
                    f"[!] {grupo}: el archivo declara tipo de reflectancia "
                    f"«{conjunto.get('tipo', 'no indicado')}». Este script "
                    f"descarga productos de superficie.")

        if not validos:
            feedback.pushWarning(
                "[_cargar_coeficientes_tc] Ningún grupo utilizable. "
                "Se usan los coeficientes internos.")
            return None

        completo = {g: dict(c) for g, c in TC_COEFICIENTES.items()}
        completo.update(validos)
        feedback.pushInfo(
            f"  Grupos sustituidos: {', '.join(sorted(validos))}")
        return completo

    def _amplitud_fenologica(self, meses_seca, meses_lluvia, carpeta,
                             context, feedback, min_obs=3):
        """Diferencia de medianas estacionales para cada índice calculado.

        Los recortes de distintas teselas pueden estar en husos UTM distintos,
        así que antes de apilar se reproyectan todos a la malla del primero.
        Sin ese paso los arreglos tendrían tamaños incompatibles o, peor, se
        apilarían desalineados sin dar error.
        """
        if not self._indices_escritos:
            feedback.pushWarning(
                "[_amplitud_fenologica] No hay índices calculados; nada que "
                "combinar. Marque al menos un índice espectral.")
            return

        por_indice = {}
        meses_presentes = set()
        for ruta, mes, sufijo, fecha in self._indices_escritos:
            por_indice.setdefault(sufijo, []).append((ruta, mes, fecha))
            meses_presentes.add(mes)

        # Un mes que no figure en ninguna lista produce un índice que se
        # calcula y luego no se usa. Suele ser un descuido al editar los meses.
        huerfanos = sorted(meses_presentes - set(meses_seca) - set(meses_lluvia))
        if huerfanos:
            feedback.pushWarning(
                f"[!] Meses sin estación asignada: {huerfanos}. Sus índices se "
                f"calcularon pero NO entran en la amplitud. Añádalos a «Meses "
                f"de estación seca» o «lluviosa» si deben contar.")

        carpeta_tmp = QgsProcessingUtils.tempFolder()
        for sufijo, entradas in sorted(por_indice.items()):
            if feedback.isCanceled():
                return
            secas = [r for r, m, _f in entradas if m in meses_seca]
            lluvias = [r for r, m, _f in entradas if m in meses_lluvia]
            fechas_seca = sorted(f for _r, m, f in entradas if m in meses_seca)
            fechas_lluvia = sorted(
                f for _r, m, f in entradas if m in meses_lluvia)
            feedback.pushInfo(
                f"\n  {sufijo}: {len(secas)} escena(s) de seca, "
                f"{len(lluvias)} de lluvia")
            if not secas or not lluvias:
                feedback.pushWarning(
                    f"[!] {sufijo}: falta al menos una estación, no se calcula "
                    f"la amplitud. Amplíe el rango de fechas o revise los "
                    f"meses declarados.")
                continue

            referencia = secas[0]
            # temporales se define ANTES del try: el finally lo recorre, y si
            # gdal.Open falla el except ya habría pasado cuando el finally
            # tropieza con un nombre sin asignar (UnboundLocalError que nada
            # captura, abortando el algoritmo justo donde el manejo de errores
            # debía actuar). _componer_descarga y _calcular_indices ya lo
            # declaraban fuera; esto alinea los tres.
            temporales = []
            try:
                ds_ref = gdal.Open(referencia)
                if ds_ref is None:
                    raise RuntimeError(f'No se pudo abrir {referencia}')
                geo = ds_ref.GetGeoTransform()
                proj = ds_ref.GetProjection()
                ancho, alto = ds_ref.RasterXSize, ds_ref.RasterYSize
                ds_ref = None

                pilas = {}
                for nombre, rutas in (('seca', secas), ('lluvia', lluvias)):
                    capas = []
                    for ruta in rutas:
                        ds = gdal.Open(ruta)
                        if ds is None:
                            feedback.pushWarning(
                                f"[_amplitud_fenologica] No se pudo abrir "
                                f"{os.path.basename(ruta)}; omitido.")
                            continue
                        mismo = (ds.GetProjection() == proj
                                 and ds.RasterXSize == ancho
                                 and ds.RasterYSize == alto)
                        if mismo:
                            capas.append(ds.GetRasterBand(1).ReadAsArray())
                            ds = None
                            continue
                        ds = None
                        tmp = os.path.join(
                            carpeta_tmp, f"amp_{uuid.uuid4().hex[:8]}.tif")
                        alineado = gdal.Warp(
                            tmp, ruta,
                            options=gdal.WarpOptions(
                                format='GTiff', dstSRS=proj,
                                outputBounds=(geo[0], geo[3] + alto * geo[5],
                                              geo[0] + ancho * geo[1], geo[3]),
                                width=ancho, height=alto,
                                resampleAlg='bilinear',
                                dstNodata=float('nan')))
                        if alineado is None:
                            feedback.pushWarning(
                                f"[_amplitud_fenologica] No se pudo alinear "
                                f"{os.path.basename(ruta)}; omitido.")
                            continue
                        capas.append(alineado.GetRasterBand(1).ReadAsArray())
                        alineado = None
                        temporales.append(tmp)
                    pilas[nombre] = capas

                if not pilas['seca'] or not pilas['lluvia']:
                    feedback.pushWarning(
                        f"[!] {sufijo}: no quedaron capas utilizables tras el "
                        f"alineado.")
                    continue

                # np.errstate NO alcanza a «All-NaN slice encountered»: ese
                # aviso sale por warnings.warn, no por el estado de error de
                # coma flotante, y nanmedian lo emite UNA VEZ POR COLUMNA
                # todo-NaN. Con un recorte nublado de 5000 x 5000 son hasta
                # 25 millones de líneas en el registro de QGIS, que se llena
                # y sepulta los mensajes útiles. Un píxel sin observaciones
                # ya se informa por su cuenta más abajo, así que el aviso no
                # aporta nada que no se diga mejor.
                with np.errstate(invalid='ignore'), warnings.catch_warnings():
                    warnings.filterwarnings(
                        'ignore', message='All-NaN slice encountered',
                        category=RuntimeWarning)
                    warnings.filterwarnings(
                        'ignore', message='Mean of empty slice',
                        category=RuntimeWarning)
                    pila_seca = np.stack(pilas['seca']).astype(np.float32)
                    pila_lluvia = np.stack(pilas['lluvia']).astype(np.float32)

                    # Recuento por píxel ANTES de la mediana: es lo que decide
                    # si el valor tiene soporte suficiente. Filtrar escenas
                    # enteras por su nubosidad media descarta píxeles
                    # despejados, y a la vez no impide que un píxel quede
                    # sustentado en una sola observación.
                    n_seca = np.isfinite(pila_seca).sum(axis=0)
                    n_lluvia = np.isfinite(pila_lluvia).sum(axis=0)

                    med_seca = np.nanmedian(pila_seca, axis=0)
                    med_lluvia = np.nanmedian(pila_lluvia, axis=0)
                    amplitud = med_seca - med_lluvia

                    suficiente = (n_seca >= min_obs) & (n_lluvia >= min_obs)
                    descartados = int((~suficiente).sum())
                    amplitud = np.where(suficiente, amplitud, np.nan)

                feedback.pushInfo(
                    f"    observaciones por píxel — seca: "
                    f"{int(n_seca.min())}–{int(n_seca.max())} "
                    f"(mediana {int(np.median(n_seca))}), lluvia: "
                    f"{int(n_lluvia.min())}–{int(n_lluvia.max())} "
                    f"(mediana {int(np.median(n_lluvia))})")
                if descartados:
                    feedback.pushWarning(
                        f"[!] {descartados} píxel(es) "
                        f"({100.0 * descartados / n_seca.size:.1f} %) quedan a "
                        f"NaN por no alcanzar {min_obs} observaciones en alguna "
                        f"estación. Baje el mínimo, amplíe el rango de fechas o "
                        f"suba «Nubosidad máxima DENTRO del AOI» para que "
                        f"entren más escenas parcialmente despejadas.")

                cod_seca = self._codigo_meses(meses_seca)
                cod_lluvia = self._codigo_meses(meses_lluvia)
                base = f"AMPL_{sufijo}_S{cod_seca}_L{cod_lluvia}"
                destino = os.path.join(carpeta, f"{base}.tif")
                driver = gdal.GetDriverByName('GTiff')
                salida = driver.Create(
                    destino, ancho, alto, 1, gdal.GDT_Float32,
                    options=['COMPRESS=DEFLATE', 'PREDICTOR=3', 'TILED=YES'])
                if salida is None:
                    raise RuntimeError('No se pudo crear el GeoTIFF')
                salida.SetGeoTransform(geo)
                salida.SetProjection(proj)
                banda = salida.GetRasterBand(1)
                banda.WriteArray(amplitud.astype(np.float32))
                banda.SetNoDataValue(float('nan'))
                banda.SetDescription(
                    f'AMPL_{sufijo}  seca[{cod_seca}] - lluvia[{cod_lluvia}]')
                salida.SetMetadata({
                    'INDICE': sufijo,
                    'SIGNO': 'seca menos lluvia',
                    'MESES_SECA': ','.join(str(m) for m in sorted(meses_seca)),
                    'MESES_LLUVIA': ','.join(
                        str(m) for m in sorted(meses_lluvia)),
                    'FECHAS_SECA': ','.join(fechas_seca),
                    'FECHAS_LLUVIA': ','.join(fechas_lluvia),
                    'N_ESCENAS_SECA': str(len(secas)),
                    'N_ESCENAS_LLUVIA': str(len(lluvias)),
                    'MIN_OBS_POR_PIXEL': str(min_obs),
                    'ESTADISTICO': 'mediana por estacion',
                    'COLECCION': str(self._coleccion_activa or ''),
                    'GENERADO_POR': f'buscar_sentinel2_cr {self.VERSION}',
                    'AUTOR': f'{AUTOR} <{AUTOR_EMAIL}>',
                })
                salida.FlushCache()
                salida = None

                validos = np.isfinite(amplitud)
                if validos.any():
                    mediana = float(np.nanmedian(amplitud))
                    feedback.pushInfo(
                        f"  amplitud → {os.path.basename(destino)}  "
                        f"[{np.nanmin(amplitud):+.3f} … "
                        f"{np.nanmax(amplitud):+.3f}], mediana "
                        f"{mediana:+.3f}, "
                        f"{100.0 * validos.mean():.0f} % válidos")
                    feedback.pushInfo(
                        "    Signo: seca − lluvia. Valores MUY NEGATIVOS = "
                        "gran caída estacional = herbáceas/pasto. Valores "
                        "CERCANOS A CERO = sin caída = leñosas/dosel.")
                else:
                    # El mensaje anterior culpaba siempre al solapamiento
                    # espacial, que casi nunca es la causa: las escenas ya
                    # vienen alineadas a la malla de la primera. Lo habitual
                    # es que MIN_OBS no se alcance en ningún píxel, o que una
                    # estación entera esté nublada. Se distingue con los
                    # recuentos, que ya están calculados.
                    max_s, max_l = int(n_seca.max()), int(n_lluvia.max())
                    if max_s == 0 or max_l == 0 or not np.isfinite(
                            med_seca).any() or not np.isfinite(
                            med_lluvia).any():
                        causa = (
                            f"ningún píxel tiene observación válida en "
                            f"{'seca' if max_s == 0 else 'lluvia'}"
                            f" (nube o sombra en todas sus escenas). Suba "
                            f"«Nubosidad máxima DENTRO del AOI» o amplíe el "
                            f"rango de fechas para que entre otra escena de "
                            f"esa estación.")
                    elif max_s < min_obs or max_l < min_obs:
                        causa = (
                            f"el mejor píxel llega a {max_s} observación(es) "
                            f"en seca y {max_l} en lluvia, por debajo del "
                            f"mínimo de {min_obs}. Baje «Mínimo de "
                            f"observaciones por píxel» a {min(max_s, max_l)} "
                            f"o menos, o amplíe el rango de fechas.")
                    else:
                        causa = (
                            f"hay hasta {max_s} y {max_l} observaciones por "
                            f"píxel, pero ninguna coincide en el mismo píxel "
                            f"en ambas estaciones: las dos estaciones no se "
                            f"solapan espacialmente. Revise que las escenas "
                            f"cubran el AOI completo.")
                    feedback.pushWarning(
                        f"[!] {os.path.basename(destino)} sin píxeles "
                        f"válidos: {causa}")

                ruta_nobs = os.path.join(carpeta, f"{base}_NOBS.tif")
                try:
                    ds_n = driver.Create(
                        ruta_nobs, ancho, alto, 2, gdal.GDT_Int16,
                        options=['COMPRESS=DEFLATE', 'TILED=YES'])
                    if ds_n is not None:
                        ds_n.SetGeoTransform(geo)
                        ds_n.SetProjection(proj)
                        for idx_b, (arr_n, etiqueta_n) in enumerate(
                                ((n_seca, 'n_obs_seca'),
                                 (n_lluvia, 'n_obs_lluvia')), start=1):
                            b_n = ds_n.GetRasterBand(idx_b)
                            b_n.WriteArray(arr_n.astype(np.int16))
                            b_n.SetDescription(etiqueta_n)
                        ds_n.SetMetadata({
                            'MESES_SECA': ','.join(
                                str(m) for m in sorted(meses_seca)),
                            'MESES_LLUVIA': ','.join(
                                str(m) for m in sorted(meses_lluvia)),
                            'MIN_OBS_POR_PIXEL': str(min_obs),
                            'BANDA_1': 'observaciones validas en seca',
                            'BANDA_2': 'observaciones validas en lluvia',
                        })
                        ds_n.FlushCache()
                        ds_n = None
                        feedback.pushInfo(
                            f"  recuento → {os.path.basename(ruta_nobs)} "
                            f"(banda 1 = seca, banda 2 = lluvia)")
                except Exception as e:
                    feedback.pushWarning(f"[_amplitud_fenologica] NOBS: {e}")

                # La amplitud es EL producto de la corrida: tiene que
                # verse al terminar. Se carga explicitamente en
                # postProcessAlgorithm en vez de registrarla aqui, porque
                # addLayerToLoadOnCompletion no la mostraba y no dejaba
                # ningun rastro de por que. Ver _cargar_pendientes.
                nombre_capa = os.path.basename(destino)[:-4]
                pp = RealcePostProcessor(
                    'whole', False, self._metodo_realce,
                    self._n_sigma, self._paleta, self._modo_clase,
                    self._n_clases, self._limites_fijos)
                _registrar_postproc(pp)
                if self._pendientes_amplitud is None:
                    self._pendientes_amplitud = []
                self._pendientes_amplitud.append((destino, nombre_capa, pp))

            except Exception as e:
                feedback.pushWarning(f"[_amplitud_fenologica] {sufijo}: {e}")
            finally:
                for tmp in temporales:
                    try:
                        if os.path.exists(tmp):
                            os.remove(tmp)
                    except Exception as e2:
                        feedback.pushDebugInfo(f"[limpieza_amp] {e2}")

    def _mismo_encuadre(self, ruta, bbox, cutline, tolerancia_px=1.0):
        """¿El archivo existente cubre el mismo encuadre que se pediría ahora?

        Un archivo de una corrida anterior puede corresponder a otro AOI. Si se
        reutiliza sin comprobarlo, la serie mezcla mallas distintas: la
        amplitud lo disimula reproyectando, pero el resultado queda recortado
        al encuadre de la escena que actúe de referencia.

        Con cutline no se comprueba: el recorte lo define la geometría, no un
        rectángulo, y predecirlo aquí sería reimplementar gdal.Warp.
        """
        if cutline:
            return True, ''
        try:
            ds = gdal.Open(ruta)
            if ds is None:
                return False, 'no se pudo abrir'
            geo = ds.GetGeoTransform()
            ancho, alto = ds.RasterXSize, ds.RasterYSize
            proj = ds.GetProjection()
            ds = None

            crs_destino = QgsCoordinateReferenceSystem()
            if not crs_destino.createFromWkt(proj):
                return True, ''      # sin CRS legible, no se penaliza
            tr = QgsCoordinateTransform(
                QgsCoordinateReferenceSystem('EPSG:4326'), crs_destino,
                QgsProject.instance())
            pedido = tr.transformBoundingBox(
                QgsRectangle(bbox[0], bbox[1], bbox[2], bbox[3]))

            actual_xmin = geo[0]
            actual_ymax = geo[3]
            actual_xmax = geo[0] + ancho * geo[1]
            actual_ymin = geo[3] + alto * geo[5]
            tol = abs(geo[1]) * tolerancia_px

            desvios = (
                abs(actual_xmin - pedido.xMinimum()),
                abs(actual_xmax - pedido.xMaximum()),
                abs(actual_ymin - pedido.yMinimum()),
                abs(actual_ymax - pedido.yMaximum()),
            )
            if max(desvios) <= tol:
                return True, ''
            return False, (
                f"cubre otro encuadre (desvío máximo "
                f"{max(desvios):.0f} m frente a una tolerancia de {tol:.0f} m)")
        except Exception as e:
            return False, f'no se pudo comprobar su encuadre ({e})'
