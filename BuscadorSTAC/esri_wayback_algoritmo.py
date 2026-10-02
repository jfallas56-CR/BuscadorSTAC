# -*- coding: utf-8 -*-
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
Esri World Imagery y Wayback — imágenes de alta resolución sobre el AOI
======================================================================

Algoritmo aparte del buscador STAC, y a propósito: Esri no publica un
catálogo STAC ni escenas individuales. World Imagery es un MOSAICO de
teselas RGB de 8 bits, servido por XYZ, sin bandas espectrales ni
radiometría calibrada. No se le pueden calcular índices y no se compara
cuantitativamente entre fechas; lo que sí permite es ver e interpretar a
resolución submétrica, que es justo lo que Sentinel-2 no puede hacer con un
árbol aislado de 5-15 m de copa.

Wayback es el archivo histórico de ese mosaico: más de 115 versiones
publicadas desde 2014. Sobre un lote concreto, casi todas son idénticas —
Esri publica una versión nueva de una tesela solo cuando la imagen cambió.
Este algoritmo averigua CUÁLES cambian de verdad sobre el AOI y consulta la
FECHA DE CAPTURA real de cada una.

    La versión «Wayback 2026-08-05» es la fecha en que se publicó el
    mosaico, NO la fecha en que se tomó la imagen sobre su lote. Pueden
    diferir años, y la fecha de captura varía dentro de un mismo lote.

Esa distinción es la razón de ser de este algoritmo. Tomar la fecha de la
versión como fecha de captura mete un error sistemático y variable en el
espacio en cualquier análisis temporal.

Método de detección de cambio
-----------------------------
Es el de la propia aplicación Wayback de Esri (wayback-core, Apache 2.0,
src/change-detector/changeDetector.ts), no una aproximación:

  1. Se pide el «tilemap» de una tesela para la versión más reciente:
       .../MapServer/tilemap/{version}/{z}/{fila}/{columna}
     Devuelve data[0] (¿hay cambio local?), select[0] (en qué versión está
     ese cambio) y size[0] (bytes de la imagen).
  2. Si hay cambio, se anota y se salta a la versión anterior, hacia atrás
     hasta que no queden cambios.
  3. Versiones consecutivas del mismo tamaño pueden ser la misma imagen; se
     descargan y comparan byte a byte para descartar duplicados.
  4. De cada superviviente se consulta la capa de metadatos: SRC_DATE2
     (fecha de captura), proveedor y resolución.

Licencia de las IMÁGENES
------------------------
El código de este archivo es GPL, como el resto del complemento. Las
imágenes NO: están bajo el Esri Master License Agreement y no son dato
abierto. Ver y digitalizar está permitido para World Imagery; que ese
permiso cubra el archivo histórico de Wayback no está declarado por Esri.
Para productos vectoriales derivados destinados a reporte oficial, conviene
confirmarlo por escrito. El algoritmo muestra este aviso en cada ejecución.

Autor  : Jorge Fallas (jfallas56@gmail.com)
Versión: 1.0.0
Licencia: GPL v2 o posterior
"""

import json
import logging
import math
import os
import re
import time
import uuid
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

from qgis.PyQt.QtCore import QCoreApplication
from qgis.PyQt.QtGui import QIcon
from qgis.core import (
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsProcessingAlgorithm,
    QgsProcessingContext,
    QgsProcessingException,
    QgsProcessingLayerPostProcessorInterface,
    QgsProcessingOutputFolder,
    QgsProcessingParameterBoolean,
    QgsProcessingParameterEnum,
    QgsProcessingParameterExtent,
    QgsProcessingParameterFeatureSource,
    QgsProcessingParameterFile,
    QgsProcessingParameterNumber,
    QgsProcessingParameterString,
    QgsProject,
    QgsRasterLayer,
)

import numpy as np

from osgeo import gdal

from .buscar_sentinel2_algoritmo import (
    AUTOR, AUTOR_EMAIL, _POSTPROCESADORES, _TIPO_POLIGONO, _registrar_postproc)

# --------------------------------------------------------------- servicios
CONFIG_URL = ('https://s3-us-west-2.amazonaws.com/'
              'config.maptiles.arcgis.com/waybackconfig.json')
SUBDOMINIOS = ('wayback', 'wayback-a', 'wayback-b')
BASE_WAYBACK = ('https://{sub}.maptiles.arcgis.com/arcgis/rest/services/'
                'World_Imagery/MapServer')
# World Imagery actual (no Wayback): mosaico vigente, un solo «momento».
URL_ACTUAL = ('https://server.arcgisonline.com/ArcGIS/rest/services/'
              'World_Imagery/MapServer/tile/{z}/{y}/{x}')

ATRIBUCION = ('Esri, Maxar, Earthstar Geographics y la comunidad de '
              'usuarios SIG')

# La capa de metadatos se reparte por nivel de zoom: id = 23 - zoom, tope 13.
ZOOM_MAX_META, ZOOM_MIN_META = 23, 10

TIEMPO_ESPERA = 60
# Los servicios de metadatos de Wayback responden más lento que el de teselas
# y a veces no responden. Como el dato es prescindible —se informa «—» y la
# ejecución sigue— se les da una espera corta para no encadenar minutos de
# bloqueo por una columna informativa.
TIEMPO_ESPERA_META = 25
# Ya no es fatal que fallen, asi que conviene ser paciente: en una corrida
# real fallaron 3 de 5 con 15 s y un reintento, mientras las otras 2
# respondieron sin problema. El servicio responde, solo que despacio.
REINTENTOS_META = 2
# Puntos del AOI en que se pide la fecha de captura. Mas puntos detectan mejor
# un lote que abarca varios footprints, pero cada uno es una consulta lenta.
MAX_PUNTOS_META = 3
# Pausa antes de reintentar los metadatos que fallaron. El servicio responde
# de forma intermitente y darle aire recupera buena parte de los fallos.
PAUSA_REINTENTO_META = 5.0

# Diferencia media por canal (0-255) por debajo de la cual dos teselas se
# consideran la MISMA fotografía. Esri republica el mosaico recomprimiendo o
# reajustando el color sin imagen nueva: eso mueve unos pocos DN. Una imagen
# de verdad distinta —otra fecha, otro sensor— mueve decenas.
UMBRAL_PIXEL = 2.0
TOPE_PIXELES = 40_000_000      # ~4.8 GB en RGB uint8 con holgura de GDAL

MODOS = [
    'Listar versiones con cambios sobre el AOI (no descarga nada)',
    'Cargar versiones como capas XYZ (sin ocupar disco)',
    'Descargar recorte GeoTIFF del AOI por versión',
]

# Sustitución de marcadores de tesela, en una pasada (ver _url_tms).
_TMS_PLACEHOLDERS = {
    '{level}': '${z}', '{row}': '${y}', '{col}': '${x}', '{column}': '${x}',
    '{z}': '${z}', '{y}': '${y}', '{x}': '${x}',
}
_RE_PLACEHOLDER = re.compile(r'\{(?:level|row|col|column|z|y|x)\}')
# Una sola fecha AAAA-MM-DD; un rango no sirve para un nombre de archivo.
_RE_FECHA_SOLA = re.compile(r'\d{4}-\d{2}-\d{2}')

_contador_sub = [0]


# ------------------------------------------------------------------- red
def _url_https(url):
    esquema = urllib.parse.urlparse(str(url)).scheme.lower()
    if esquema != 'https':
        raise ValueError(f"Esquema no permitido: '{esquema or '(vacío)'}'")
    return url


def _obtener(url, binario=False, espera=TIEMPO_ESPERA, reintentos=1):
    """GET. Devuelve dict (JSON) o bytes. SIEMPRE lanza RuntimeError al fallar.

    Que el fallo salga siempre como RuntimeError no es cosmético. TimeoutError
    NO hereda de urllib.error.URLError —hereda de OSError—, así que una versión
    anterior que solo capturaba HTTPError y URLError dejaba escapar el tiempo
    de espera agotado tal cual. Los llamadores, que capturan
    (RuntimeError, ValueError) para seguir adelante sin ese dato, tampoco lo
    veían, y un servicio de metadatos lento abortaba la ejecución entera
    DESPUÉS de haber hecho el trabajo caro de detectar los cambios. Se vio en
    un registro real: 90 s y todo perdido.

    Se reintenta una vez ante fallos transitorios (tiempo agotado, 5xx), que
    es el modo de fallo habitual de estos servicios bajo carga. Un 4xx no se
    reintenta: no va a cambiar.
    """
    _url_https(url)
    req = urllib.request.Request(url, headers={'Accept': '*/*'})
    ultimo = None
    for intento in range(max(1, reintentos + 1)):
        try:
            # Esquema validado arriba: el aviso B310 ya no aplica.
            with urllib.request.urlopen(req, timeout=espera) as resp:  # nosec B310
                crudo = resp.read()
            return crudo if binario else json.loads(crudo.decode('utf-8'))
        except urllib.error.HTTPError as e:
            ultimo = RuntimeError(f"HTTP {e.code}")
            if e.code < 500:
                break              # 4xx no mejora reintentando
        except urllib.error.URLError as e:
            ultimo = RuntimeError(f"red: {e.reason}")
        except TimeoutError:
            ultimo = RuntimeError(f"tiempo de espera agotado ({espera:g} s)")
        except OSError as e:
            # Socket cerrado, DNS, TLS: todo lo que no sea de los anteriores.
            ultimo = RuntimeError(f"conexión: {e}")
        except ValueError as e:
            # JSON ilegible: el servidor respondió algo que no era JSON.
            ultimo = RuntimeError(f"respuesta ilegible: {e}")
            break
        if intento < reintentos:
            time.sleep(1.0 + intento)
    raise ultimo if ultimo else RuntimeError('fallo desconocido')


def _base_wayback():
    """Rota subdominios, como el cliente oficial, para repartir la carga."""
    sub = SUBDOMINIOS[_contador_sub[0] % len(SUBDOMINIOS)]
    _contador_sub[0] += 1
    return BASE_WAYBACK.format(sub=sub)


# -------------------------------------------------------------- geometría
def lon_a_columna(lon, zoom):
    return int(math.floor(((lon + 180.0) / 360.0) * (2 ** zoom)))


def lat_a_fila(lat, zoom):
    rad = math.radians(max(-85.05, min(85.05, lat)))
    return int(math.floor(
        (1.0 - math.log(math.tan(rad) + 1.0 / math.cos(rad)) / math.pi)
        / 2.0 * (2 ** zoom)))


def metros_por_pixel(zoom, lat):
    return 156543.03392 * math.cos(math.radians(lat)) / (2 ** zoom)


def zoom_para_resolucion(res_m, lat):
    """Zoom cuyo tamaño de píxel es igual o menor que res_m."""
    z = math.log2(156543.03392 * math.cos(math.radians(lat)) / float(res_m))
    return max(0, min(23, int(math.ceil(z))))


def _misma_imagen(a, b):
    """¿Dos teselas decodificadas son la misma fotografía?

    No se exige igualdad exacta: recomprimir en JPEG mueve unos pocos DN sin
    que haya imagen nueva. Se compara la diferencia media por canal contra
    UMBRAL_PIXEL. Formas distintas significan teselas distintas, no hay nada
    que comparar.
    """
    if a is None or b is None or a.shape != b.shape:
        return False
    return float(np.mean(np.abs(a - b))) < UMBRAL_PIXEL


def _fecha_epoch(valor):
    """SRC_DATE2 llega como epoch en milisegundos, o como texto."""
    if valor in (None, ''):
        return '—'
    if isinstance(valor, (int, float)):
        try:
            return datetime.fromtimestamp(
                valor / 1000.0, tz=timezone.utc).strftime('%Y-%m-%d')
        except (OverflowError, OSError, ValueError):
            return str(valor)
    return str(valor)[:10]


class EsriWaybackAlgorithm(QgsProcessingAlgorithm):
    """World Imagery actual e histórico (Wayback) recortado al AOI."""

    VERSION = 'v1.0.0'

    AOI = 'AOI'
    EXTENSION = 'EXTENSION'
    MODO = 'MODO'
    ACTUAL = 'ACTUAL'
    RESOLUCION = 'RESOLUCION'
    FECHA_INI = 'FECHA_INI'
    FECHA_FIN = 'FECHA_FIN'
    REJILLA = 'REJILLA'
    COMPARAR = 'COMPARAR'
    UNICA_CAPTURA = 'UNICA_CAPTURA'
    CARPETA = 'CARPETA'
    SALIDA = 'SALIDA'

    _versiones = None
    _indice = None
    _cache_meta = None
    # Capas XYZ pendientes de añadir en el hilo principal.
    _xyz_pendientes = None
    _resultados = None

    def tr(self, cadena):
        return QCoreApplication.translate('EsriWayback', cadena)

    def createInstance(self):
        return EsriWaybackAlgorithm()

    def name(self):
        return 'esriworldimagerywayback'

    def displayName(self):
        return self.tr('Esri World Imagery / Wayback (histórico de alta '
                       'resolución)')

    def group(self):
        return self.tr('Teledetección')

    def groupId(self):
        return 'teledeteccion'

    def icon(self):
        ruta = os.path.join(os.path.dirname(__file__), 'icon.png')
        return QIcon(ruta) if os.path.exists(ruta) else QIcon()

    def shortHelpString(self):
        return self.tr(
            f"<b>Esri World Imagery / Wayback</b> — {self.VERSION}<br><br>"
            "Imágenes RGB de alta resolución (submétricas en buena parte del "
            "territorio) sobre su área de interés, en la versión vigente y en "
            "las versiones históricas del archivo Wayback, que arranca en "
            "2014.<br><br>"
            "<b>Para qué sirve y para qué no.</b> World Imagery es un mosaico "
            "de 8 bits sin radiometría calibrada: no tiene infrarrojo, no se "
            "le pueden calcular NDVI ni NDMI, y dos fechas no se comparan "
            "cuantitativamente. Lo que sí permite es <i>ver</i> e interpretar "
            "a una resolución que Sentinel-2 no alcanza. Para árboles "
            "aislados en potrero —subpíxel a 20 m— es la herramienta "
            "adecuada; para dosel continuo y series densas, use el buscador "
            "Sentinel-2 / Landsat de este mismo complemento.<br><br>"
            "<b>Versión publicada ≠ fecha de captura.</b> Es lo más "
            "importante de esta herramienta. «Wayback 2026-08-05» es cuando "
            "Esri publicó el mosaico, no cuando se tomó la foto sobre su "
            "lote: pueden diferir años. El algoritmo consulta la capa de "
            "metadatos y le da la <b>fecha de captura real</b>, que es la que "
            "debe usar en cualquier análisis temporal.<br><br>"
            "<b>Cómo leer la columna «capturada».</b> La fecha se pide en "
            "varios puntos del AOI, no en uno solo, y puede salir de tres "
            "formas:<br>"
            "• <code>2022-02-03</code> — toda el área muestreada tiene la "
            "misma imagen de origen.<br>"
            "• <code>2022-02-03…2023-07-01 (2)</code> — <b>su lote abarca más "
            "de un footprint de origen</b> y hay dos fechas distintas dentro "
            "de él. Importa: al fotointerpretar, una parte de la imagen puede "
            "ser un año más vieja que el resto, y la costura no se ve.<br>"
            "• <code>?</code> — el servicio de metadatos de Esri no respondió. "
            "Es frecuente y no afecta a las imágenes, que se descargan igual; "
            "la fecha puede consultarse después, o en la aplicación Wayback de "
            "Esri.<br><br>"
            "<b>Reprocesamiento frente a imagen nueva.</b> Esri republica el "
            "mosaico ajustando color o compresión sin que haya fotografía "
            "nueva. Eso cambia los bytes de la tesela, de modo que la "
            "detección de cambio la da por distinta, pero la imagen es la "
            "misma. Por eso la comparación es de píxeles y no de bytes, y por "
            "eso funciona aunque la capa de metadatos no responda: sin fecha "
            "de captura no habría forma de agrupar después.<br><br>"
            "<b>Solo las versiones que cambian.</b> El archivo ronda las "
            "doscientas versiones globales, pero Esri publica una nueva para "
            "una tesela solo cuando esa imagen cambió. Sobre un lote rural es "
            "habitual que queden cinco o seis versiones distintas en doce "
            "años, no doscientas: medido sobre un lote de Guanacaste, 5 de "
            "196. El algoritmo detecta cuáles son, con el mismo método que la "
            "aplicación Wayback de Esri.<br><br>"
            "<b>Modos:</b><br>"
            "• <i>Listar</i>: solo el informe —versiones con cambio, fecha "
            "de captura y proveedor—. No descarga imágenes y tarda segundos; "
            "es lo que conviene ejecutar primero para saber con qué fechas "
            "cuenta antes de bajar nada.<br>"
            "• <i>Cargar XYZ</i>: añade cada versión como capa remota al "
            "terminar. No ocupa disco y se dibuja al vuelo. Solo la más "
            "reciente queda activada: son mosaicos opacos, de modo que "
            "activarlas todas no deja ver más pero sí las descarga todas; "
            "actívelas de una en una para comparar fechas.<br>"
            "• <i>Descargar</i>: escribe un GeoTIFF RGB recortado al AOI por "
            "versión, reproyectable y utilizable sin conexión.<br><br>"
            "<b>Licencia de las imágenes.</b> No son dato abierto. Están "
            "bajo el Esri Master License Agreement. Ver y digitalizar está "
            "permitido para World Imagery; que ese permiso cubra el archivo "
            "histórico no está declarado por Esri. Si va a derivar cartografía "
            "vectorial para reporte oficial, confírmelo antes.<br><br>"
            "<b>Procedencia de la imagen.</b> Además de la fecha, se "
            "recupera de la capa de metadatos el proveedor y el sensor, la "
            "resolución del original y la exactitud posicional — lo mismo "
            "que muestra la aplicación Wayback de Esri al pinchar un punto. "
            "Sale en el registro como «Vantor (LG01), captada 2026-02-13, "
            "mostrada en la versión 2026-08-05 del mapa World Imagery · "
            "resolución de origen 0.60 m/píxel · exactitud posicional "
            "5.00 m», y se escribe dentro del GeoTIFF descargado, donde se "
            "lee con gdalinfo o en Propiedades de la capa. Dos campos que "
            "conviene mirar: la <i>resolución de origen</i> dice si pedir "
            "0.3 m aportaría algo o solo ampliaría el píxel, y la "
            "<i>exactitud posicional</i> acota cuánto puede desplazarse un "
            "árbol digitalizado respecto de su posición real, que es lo que "
            "importa al contrastar con GPS de campo.<br><br>"
            "<b>Una fotografía puede aparecer en varias versiones.</b> Esri "
            "republica el mosaico y ajusta el procesado sin que haya imagen "
            "nueva: dos versiones con distinta fecha de publicación pueden "
            "llevar la misma fecha de captura y ser indistinguibles a la "
            "vista. Por omisión se conserva una sola por fecha de captura, de "
            "modo que el número de archivos coincida con el de fotografías "
            "reales; las equivalentes quedan anotadas en el registro y en los "
            "metadatos del archivo conservado. Para un análisis temporal, "
            "agrupe siempre por fecha de captura, nunca por la de "
            "publicación.<br><br>"
            "<b>Nombres de archivo</b> (modo descarga): "
            "<code>EsriWI_&lt;publicada&gt;_cap&lt;capturada&gt;.tif</code>, "
            "por ejemplo <code>EsriWI_20230831_cap20220203.tif</code>. El "
            "mosaico vigente sale como <code>EsriWI_vigente.tif</code>. Cuando "
            "la captura no es una fecha única —porque falló la consulta o "
            "porque hay varias dentro del lote— el tramo se sustituye por "
            "<code>capND</code>: un rango o un «?» no son válidos en un nombre "
            "de archivo de Windows. La fecha de captura queda igualmente "
            "escrita en los metadatos del GeoTIFF.<br><br>"
            "<b>Nota para QGIS:</b> el servicio de Wayback responde con "
            "redirecciones relativas que algunas versiones de QGIS no "
            "resuelven (incidencia qgis/QGIS#54161); si una capa XYZ sale en "
            "blanco, esa es la causa y el modo de descarga la evita.<hr>"
            f"<b>Autor:</b> {AUTOR} &lt;{AUTOR_EMAIL}&gt;<br>"
            f"<b>Versión:</b> {self.VERSION} · <b>Licencia del código:</b> "
            f"GPL v2 o posterior<br>"
            f"<b>Imágenes:</b> {ATRIBUCION}"
        )

    # ---------------------------------------------------------------- params
    def initAlgorithm(self, config=None):
        p = QgsProcessingParameterFeatureSource(
            self.AOI, self.tr('Capa de AOI (tiene prioridad sobre la '
                              'extensión)'),
            [_TIPO_POLIGONO], optional=True)
        p.setHelp(self.tr(
            'Su envolvente define el área consultada y el recorte. Es la '
            'forma recomendada de acotar: con un lote concreto el análisis '
            'tarda segundos.'))
        self.addParameter(p)

        p = QgsProcessingParameterExtent(
            self.EXTENSION, self.tr('Extensión (si no hay capa de AOI)'),
            optional=True)
        p.setHelp(self.tr(
            'Rectángulo de trabajo. El botón «…» ofrece «Usar extensión del '
            'lienzo del mapa», de modo que puede encuadrar en QGIS y analizar '
            'justo lo que ve.'))
        self.addParameter(p)

        p = QgsProcessingParameterEnum(
            self.MODO, self.tr('Modo'), options=MODOS, defaultValue=0)
        p.setHelp(self.tr(
            'Empiece por <b>Listar</b>: le dice cuántas versiones distintas '
            'hay realmente sobre su AOI y en qué fecha se capturó cada una, '
            'sin descargar nada. Con esa información decide cuáles vale la '
            'pena cargar o descargar.<br>'
            'La descarga tarda: son teselas de 256 px pedidas una a una. Un '
            'lote de 200 ha a 0.6 m ronda los tres minutos para seis '
            'versiones. Sobre áreas mayores, suba la «Resolución objetivo» '
            'antes que reducir el AOI.'))
        self.addParameter(p)

        p = QgsProcessingParameterBoolean(
            self.ACTUAL, self.tr('Incluir World Imagery vigente (además del '
                                 'histórico)'),
            defaultValue=True)
        p.setHelp(self.tr(
            'El mosaico que Esri sirve ahora mismo, desde un host distinto al '
            'del archivo. No figura en el catálogo de Wayback, así que no '
            'tiene capa de metadatos y su fecha de captura no es consultable: '
            'el archivo sale sin fecha.<br>'
            'En el caso normal es exactamente la misma imagen que la versión '
            'más reciente del archivo —esa versión es lo que Esri está '
            'sirviendo—, de modo que añadirlo duplicaría el archivo y, encima, '
            'sin fecha. Por eso no se supone: se compara una tesela de cada '
            'uno. Si coinciden, se avisa y no se añade; si difieren, significa '
            'que Esri publicó imagen más nueva de la que recoge el catálogo y '
            'entonces sí vale la pena, que es el único caso en que esta '
            'casilla aporta algo.<br>'
            'Con «Una sola imagen por fecha de captura» desactivada se añade '
            'igualmente, por si quiere el archivo aunque sea duplicado.'))
        self.addParameter(p)

        p = QgsProcessingParameterNumber(
            self.RESOLUCION, self.tr('Resolución objetivo (m/píxel)'),
            QgsProcessingParameterNumber.Double,
            defaultValue=1.0, minValue=0.15, maxValue=150.0)
        p.setHelp(self.tr(
            'Determina el nivel de zoom de las teselas. 1 m es un buen punto '
            'de partida para interpretar copas; 0.3 m aprovecha el máximo '
            'detalle donde exista, a costa de descargar dieciséis veces más '
            'teselas. Pedir más detalle del que tiene la imagen original no '
            'añade información, solo la amplía.'))
        self.addParameter(p)

        p = QgsProcessingParameterString(
            self.FECHA_INI, self.tr('Fecha inicial (AAAA-MM-DD, opcional)'),
            defaultValue='', optional=True)
        p.setHelp(self.tr(
            'Filtra por FECHA DE PUBLICACIÓN de la versión, que es lo único '
            'conocido antes de consultar los metadatos. La fecha de captura '
            'aparece después, en el informe.'))
        self.addParameter(p)

        p = QgsProcessingParameterString(
            self.FECHA_FIN, self.tr('Fecha final (AAAA-MM-DD, opcional)'),
            defaultValue='', optional=True)
        p.setHelp(self.tr('Deje ambas vacías para todo el archivo, 2014 a hoy.'))
        self.addParameter(p)

        p = QgsProcessingParameterNumber(
            self.REJILLA, self.tr('Puntos de muestreo por lado'),
            QgsProcessingParameterNumber.Integer,
            defaultValue=3, minValue=1, maxValue=6)
        p.setHelp(self.tr(
            'El cambio se detecta tesela a tesela, así que se muestrea una '
            'rejilla NxN sobre el AOI y se unen los resultados: partes '
            'distintas de un mismo lote se actualizan en fechas distintas. '
            'Con 1 se consulta solo el centro, que es más rápido pero pierde '
            'las actualizaciones que solo tocan un borde.<br>'
            'Estos mismos puntos se usan para pedir la fecha de captura (los '
            'tres más cercanos al centro), que es lo que permite detectar que '
            'un lote abarca varios footprints de origen. Con rejilla 1 esa '
            'comprobación no es posible y la fecha sale siempre como un valor '
            'único, aunque el lote no sea homogéneo.'))
        self.addParameter(p)

        p = QgsProcessingParameterBoolean(
            self.COMPARAR, self.tr('Comparar teselas byte a byte'),
            defaultValue=True)
        p.setHelp(self.tr(
            'Descarga una tesela de cada versión candidata y compara los '
            'PÍXELES, no los bytes. Es lo que distingue una fotografía nueva '
            'de la misma reprocesada: Esri republica el mosaico '
            'recomprimiendo o reajustando el color, lo que cambia los bytes y '
            'el tamaño del archivo pero mueve la imagen apenas un par de '
            'niveles de gris; una imagen realmente distinta mueve decenas.<br>'
            'Sin esta comparación se conservan todas las versiones '
            'candidatas, de modo que la misma fotografía puede descargarse '
            'varias veces. Cuesta una tesela por versión —cinco o seis en un '
            'lote típico— y funciona aunque la capa de metadatos no responda, '
            'que es justo cuando no hay fecha de captura con la que agrupar '
            'más tarde.'))
        self.addParameter(p)

        p = QgsProcessingParameterBoolean(
            self.UNICA_CAPTURA,
            self.tr('Una sola imagen por fecha de captura'),
            defaultValue=True)
        p.setHelp(self.tr(
            'Varias versiones del archivo pueden contener LA MISMA fotografía, '
            'reprocesada: Esri republica el mosaico y ajusta balance de color '
            'o compresión sin que haya imagen nueva. Sus teselas difieren byte '
            'a byte, de modo que la detección de cambio las da por distintas, '
            'pero la fecha de captura es la misma y visualmente son '
            'indistinguibles.<br>'
            'Con esta opción activada se conserva una sola por fecha de '
            'captura —la de publicación más reciente, que suele llevar el '
            'procesamiento más cuidado— y las equivalentes se anotan en el '
            'registro y en los metadatos del archivo conservado. Así el número '
            'de archivos coincide con el número de fotografías distintas, que '
            'es lo que cuenta para un análisis temporal.<br>'
            'Desactívela si quiere comparar el reprocesamiento en sí, o si '
            'prefiere quedarse con una versión concreta. Las versiones sin '
            'fecha de captura conocida nunca se agrupan: no se puede saber si '
            'son la misma imagen.'))
        self.addParameter(p)

        p = QgsProcessingParameterFile(
            self.CARPETA, self.tr('Carpeta de salida (solo modo descarga)'),
            behavior=QgsProcessingParameterFile.Folder, optional=True)
        p.setHelp(self.tr(
            'Destino de los GeoTIFF. Solo se exige en el modo de descarga.<br>'
            'Se escribe un archivo por versión, con la fecha de publicación y '
            'la de captura en el nombre, y ambas más la atribución y la '
            'licencia en los metadatos del propio GeoTIFF, de modo que la '
            'procedencia viaja con el archivo. Un archivo que ya exista no se '
            'vuelve a descargar: bórrelo si quiere rehacerlo.'))
        self.addParameter(p)

        self.addOutput(QgsProcessingOutputFolder(
            self.SALIDA, self.tr('Carpeta de salida')))

    # ------------------------------------------------------------ validación
    def checkParameterValues(self, parameters, context):
        modo = self.parameterAsEnum(parameters, self.MODO, context)
        carpeta = (self.parameterAsFile(parameters, self.CARPETA, context)
                   or '').strip()
        if modo == 2 and not carpeta:
            return False, self.tr(
                '[!] El modo de descarga requiere una carpeta de salida. '
                'Elíjala con el botón «…» del parámetro «Carpeta de salida», '
                'o cambie a «Listar» o «Cargar».')
        if carpeta and os.path.isfile(carpeta):
            return False, self.tr(
                f'[!] «{carpeta}» es un archivo, no una carpeta.')

        for etiqueta, clave in (('inicial', self.FECHA_INI),
                                ('final', self.FECHA_FIN)):
            txt = (self.parameterAsString(parameters, clave, context)
                   or '').strip()
            if txt:
                try:
                    datetime.strptime(txt, '%Y-%m-%d')
                except ValueError:
                    return False, self.tr(
                        f'[!] Fecha {etiqueta} inválida: «{txt}». Formato '
                        f'AAAA-MM-DD, o déjela vacía.')

        fuente = self.parameterAsSource(parameters, self.AOI, context)
        ext = self.parameterAsExtent(
            parameters, self.EXTENSION, context,
            QgsCoordinateReferenceSystem('EPSG:4326'))
        if fuente is None and ext.isEmpty():
            return False, self.tr(
                '[!] Indique una capa de AOI o una extensión. Sin área no hay '
                'nada que consultar.')

        # Tope de tamaño solo en descarga: cargar XYZ no materializa píxeles.
        if modo == 2:
            rect = self._rect_4326(parameters, context, None)
            if rect:
                res = self.parameterAsDouble(
                    parameters, self.RESOLUCION, context)
                ancho, alto = self._dimension_px(rect, res)
                if ancho * alto > TOPE_PIXELES:
                    return False, self.tr(
                        f'[!] A {res:g} m/píxel el recorte sería de '
                        f'{ancho:.0f} x {alto:.0f} píxeles '
                        f'({ancho * alto / 1e6:.0f} millones), por encima del '
                        f'tope de {TOPE_PIXELES / 1e6:.0f}. Reduzca el área o '
                        f'suba la resolución objetivo (un número mayor = '
                        f'píxel más grande).')
        return super().checkParameterValues(parameters, context)

    # ------------------------------------------------------------- ejecución
    def processAlgorithm(self, parameters, context, feedback):
        modo = self.parameterAsEnum(parameters, self.MODO, context)
        res_obj = self.parameterAsDouble(parameters, self.RESOLUCION, context)
        rejilla = self.parameterAsInt(parameters, self.REJILLA, context)
        comparar = self.parameterAsBool(parameters, self.COMPARAR, context)
        incluir_actual = self.parameterAsBool(parameters, self.ACTUAL, context)
        carpeta = (self.parameterAsFile(parameters, self.CARPETA, context)
                   or '').strip()
        f_ini = (self.parameterAsString(
            parameters, self.FECHA_INI, context) or '').strip()
        f_fin = (self.parameterAsString(
            parameters, self.FECHA_FIN, context) or '').strip()

        rect = self._rect_4326(parameters, context, feedback)
        if not rect:
            raise QgsProcessingException(
                'No se pudo determinar el área de trabajo.')

        lat_c = (rect[1] + rect[3]) / 2.0
        lon_c = (rect[0] + rect[2]) / 2.0
        zoom = zoom_para_resolucion(res_obj, lat_c)
        res_real = metros_por_pixel(zoom, lat_c)
        ancho_px, alto_px = self._dimension_px(rect, res_real)

        feedback.pushInfo(f"AOI (4326): {[round(v, 6) for v in rect]}")
        feedback.pushInfo(
            f"Zoom {zoom} → {res_real:.2f} m/píxel "
            f"(pedidos {res_obj:g}); recorte ≈ {ancho_px:.0f} x {alto_px:.0f} px")
        feedback.pushInfo(f"Imágenes: {ATRIBUCION}")
        feedback.pushWarning(
            "[!] Las imágenes de Esri no son dato abierto: se rigen por el "
            "Esri Master License Agreement. Ver y digitalizar está permitido "
            "para World Imagery; que ese permiso alcance al archivo histórico "
            "de Wayback no está declarado. Confírmelo antes de derivar "
            "cartografía para reporte oficial.")

        feedback.pushInfo("\nDescargando el catálogo de versiones…")
        try:
            versiones = self._cargar_versiones()
        except (RuntimeError, ValueError) as e:
            raise QgsProcessingException(
                f"No se pudo leer el catálogo de Wayback: {e}") from e
        self._versiones = versiones
        self._cache_meta = {}
        self._xyz_pendientes = None
        self._resultados = None
        self._indice = {v['num']: i for i, v in enumerate(versiones)}
        feedback.pushInfo(
            f"  {len(versiones)} versiones globales "
            f"({versiones[-1]['fecha']} … {versiones[0]['fecha']})")

        # --------------------------------------------- versiones con cambio
        feedback.pushInfo("\nBuscando versiones con cambios sobre el AOI…")
        encontradas = {}
        puntos = self._puntos(rect, rejilla)
        for k, (lon, lat) in enumerate(puntos, start=1):
            if feedback.isCanceled():
                return {}
            fila, col = lat_a_fila(lat, zoom), lon_a_columna(lon, zoom)
            try:
                cands = self._con_cambio(zoom, fila, col, feedback)
                nums = self._sin_duplicados(cands, zoom, fila, col, comparar)
            except (RuntimeError, ValueError) as e:
                feedback.pushWarning(f"  punto {k}: {e}")
                continue
            feedback.pushInfo(
                f"  punto {k}/{len(puntos)} · tesela {zoom}/{fila}/{col} → "
                f"{len(nums)} versión(es)")
            for n in nums:
                encontradas.setdefault(n, 0)
                encontradas[n] += 1
            feedback.setProgress(int(60.0 * k / len(puntos)))

        if not encontradas:
            raise QgsProcessingException(
                'Ninguna versión de Wayback registra cambios sobre este AOI. '
                'Suele indicar un AOI fuera de cobertura, o un zoom tan bajo '
                'que el servicio no distingue cambios: pruebe una resolución '
                'objetivo más fina.')

        orden = sorted(encontradas, key=lambda n: self._indice.get(n, 1 << 30))
        orden = self._filtrar_fechas(orden, f_ini, f_fin, feedback)
        if not orden:
            raise QgsProcessingException(
                'Ninguna versión cae dentro del rango de fechas indicado. '
                'Recuerde que filtra por fecha de PUBLICACIÓN, no de captura.')

        # ------------------------------------------------------- metadatos
        feedback.pushInfo(
            f"\n{len(orden)} versiones distintas sobre el AOI "
            f"(de {len(versiones)} globales)\n")
        # Los puntos del muestreo, reordenados para empezar por el centro:
        # si solo se alcanza a consultar uno, que sea el más representativo.
        puntos_meta = sorted(
            puntos, key=lambda p: (p[0] - lon_c) ** 2 + (p[1] - lat_c) ** 2)
        feedback.pushInfo(
            f"{'publicada':<12}{'capturada':<18}{'res':>6}  proveedor")
        feedback.pushInfo('-' * 62)
        filas = []
        fallos_meta = 0
        for i, num in enumerate(orden):
            if feedback.isCanceled():
                return {}
            v = versiones[self._indice[num]]
            try:
                meta = self._metadatos_aoi(num, zoom, puntos_meta, feedback)
            except Exception as e:
                # _metadatos ya captura lo suyo; esto cubre lo imprevisto
                # (un JSON con forma inesperada, por ejemplo). La detección
                # de cambios ya está hecha y no se tira por una columna.
                meta = {'fechas': [], 'resumen': '?', 'fuentes': [],
                        'res': None, 'exactitud': None, 'fallos': 1,
                        'detalle': f'{type(e).__name__}: {e}'}
            res_txt = (f"{meta['res']:g}"
                       if isinstance(meta['res'], (int, float)) else '—')
            fallos_meta += meta['fallos']
            fuente_txt = (' / '.join(meta['fuentes']) if meta['fuentes']
                          else '(sin metadatos)')
            feedback.pushInfo(
                f"{v['fecha']:<12}{meta['resumen']:<18}{res_txt:>6}  "
                f"{fuente_txt[:30]}")
            filas.append({'num': num, 'publicada': v['fecha'],
                          'captura': meta['resumen'], 'url': v['url'],
                          'meta': meta})
            feedback.setProgress(60 + int(20.0 * (i + 1) / len(orden)))

        feedback.pushInfo('-' * 62)

        # Segunda pasada. En una corrida real fallaron 3 de 5 consultas
        # mientras las otras 2 respondían sin problema: el servicio de Esri
        # responde de forma intermitente, no está caído. Una pausa y otro
        # intento recuperan buena parte, y como la caché guarda los aciertos
        # solo se repiten las que faltan.
        pendientes = [f for f in filas if f['meta']['fallos']
                      and not f['meta']['fechas']]
        if pendientes:
            feedback.pushInfo(
                f"\nReintentando {len(pendientes)} consulta(s) de metadatos "
                f"tras una pausa…")
            time.sleep(PAUSA_REINTENTO_META)
            for f in pendientes:
                if feedback.isCanceled():
                    break
                # Se limpian de la caché solo los fallos, para que se repitan.
                for clave in [k for k, val in self._cache_meta.items()
                              if k[0] == f['num'] and isinstance(val, dict)
                              and 'error' in val]:
                    del self._cache_meta[clave]
                try:
                    meta = self._metadatos_aoi(
                        f['num'], zoom, puntos_meta, feedback)
                except Exception as e:
                    # Se registra en vez de callar: un «continue» mudo es un
                    # hallazgo B112 de Bandit y, sobre todo, deja al usuario
                    # sin saber por que una version sigue sin fecha.
                    feedback.pushDebugInfo(
                        f"[reintento_meta] {f['publicada']}: {e}")
                    continue
                if meta['fechas']:
                    fallos_meta -= f['meta']['fallos']
                    f['meta'], f['captura'] = meta, meta['resumen']
                    feedback.pushInfo(
                        f"  recuperado: {f['publicada']} → {meta['resumen']}")

        # Procedencia completa, al estilo de la aplicación de Esri.
        feedback.pushInfo("\nProcedencia de cada versión:")
        for f in filas:
            feedback.pushInfo(f"  {f['publicada']} — {f['meta']['detalle']}")

        if fallos_meta:
            feedback.pushWarning(
                f"[!] {fallos_meta} consulta(s) de metadatos fallaron; las "
                f"versiones sin ninguna respuesta salen con «?» en "
                f"«capturada». El servicio de metadatos de Esri responde "
                f"lento de forma intermitente; no afecta a las imágenes, que "
                f"sí se obtienen. Reintente más tarde, o consulte la fecha en "
                f"la aplicación Wayback de Esri.")
        feedback.pushInfo(
            "\n«publicada» es la versión del mosaico; «capturada» es la fecha "
            "real de\nla imagen sobre el centro del AOI. Para análisis "
            "temporal use la segunda.\nPuede variar dentro del mismo lote.")

        if modo == 0:
            feedback.pushInfo(
                "\nModo «Listar»: no se descargó ni cargó ninguna imagen. "
                "Cambie de modo para\ntraer las versiones que le interesen.")
            self._resultados = {self.SALIDA: carpeta or ''}
            return self._resultados

        # --------------------------------------------------- cargar / bajar
        agrupar = self.parameterAsBool(
            parameters, self.UNICA_CAPTURA, context)
        if agrupar:
            filas = self._una_por_captura(filas, feedback)

        if incluir_actual:
            filas = self._anadir_vigente(
                filas, zoom, lon_c, lat_c, agrupar, feedback)

        if modo == 1:
            self._cargar_xyz(filas, rect, context, feedback)
        else:
            self._descargar(filas, rect, zoom, ancho_px, alto_px,
                            carpeta, context, feedback, res_real)
        self._resultados = {self.SALIDA: carpeta or ''}
        return self._resultados

    def _anadir_vigente(self, filas, zoom, lon, lat, agrupar, feedback):
        """Añade el mosaico vigente SOLO si difiere del archivo más reciente.

        El mosaico vigente de World Imagery se sirve desde otro host y no
        figura en waybackconfig.json, así que no tiene capa de metadatos que
        consultar: su archivo sale sin fecha de captura. Y en el caso normal
        es LA MISMA imagen que la versión más nueva del archivo, porque esa
        versión es justamente lo que Esri está sirviendo. Comprobado sobre un
        lote real: idénticas.

        Añadirlo a ciegas deja un archivo duplicado y, peor, sin fecha, que es
        justo lo que un análisis temporal no puede usar. Así que en vez de
        suponer se compara una tesela de cada uno. Eso convierte la casilla en
        algo útil: responde si hay imagen más nueva que el archivo.
        """
        col, fila_t = lon_a_columna(lon, zoom), lat_a_fila(lat, zoom)
        reciente = filas[0] if filas else None
        iguales = None
        if reciente is not None:
            try:
                a = _obtener(self._url_tesela(URL_ACTUAL, zoom, fila_t, col),
                             binario=True)
                b = _obtener(
                    self._url_tesela(reciente['url'], zoom, fila_t, col),
                    binario=True)
                iguales = (a == b)
            except (RuntimeError, ValueError) as e:
                feedback.pushWarning(
                    f"[vigente] No se pudo comparar con el archivo ({e}); "
                    f"se incluye por si acaso.")

        if iguales:
            feedback.pushInfo(
                f"\nMosaico vigente: idéntico a la versión "
                f"{reciente['publicada']} (captura {reciente['captura']}). "
                f"No se añade\npor separado — sería el mismo archivo sin "
                f"fecha de captura.")
            if not agrupar:
                # Sin agrupado el usuario pidió explícitamente todo.
                filas.insert(0, {'num': None, 'publicada': 'vigente',
                                 'captura': 'n/d', 'url': URL_ACTUAL,
                                 'meta': {'detalle': (
                                     f'mosaico vigente, idéntico a la versión '
                                     f'{reciente["publicada"]}')}})
            return filas

        if iguales is False:
            feedback.pushInfo(
                f"\nMosaico vigente: DIFERENTE de la versión más reciente del "
                f"archivo\n({reciente['publicada']}). Esri publicó imagen más "
                f"nueva que la que recoge el catálogo de Wayback; se añade, "
                f"pero sin fecha\nde captura consultable.")
        filas.insert(0, {'num': None, 'publicada': 'vigente',
                         'captura': 'n/d', 'url': URL_ACTUAL,
                         'meta': {'detalle': (
                             'mosaico vigente de World Imagery; el servicio en '
                             'vivo no publica capa de metadatos, de modo que '
                             'su fecha de captura no es consultable por esta '
                             'vía')}})
        return filas

    @staticmethod
    def _una_por_captura(filas, feedback):
        """Deja una versión por fecha de captura: la publicada más tarde.

        Dos versiones con la misma fecha de captura son la misma fotografía
        reprocesada. Sus teselas difieren byte a byte —por eso la detección de
        cambio las separa— pero la imagen es la misma; comprobado sobre un
        lote real, visualmente indistinguibles.

        Se conserva la publicación más reciente porque suele llevar el
        procesamiento más cuidado, y las descartadas se anotan para que la
        información no se pierda en silencio.

        Las fechas desconocidas («?», «—», «capND») NO se agrupan: sin fecha
        no hay forma de saber si son la misma imagen, y fusionarlas a ciegas
        perdería una fotografía distinta.
        """
        grupos, sueltas = {}, []
        for f in filas:
            captura = str(f.get('captura') or '')
            if not _RE_FECHA_SOLA.fullmatch(captura):
                sueltas.append(f)
                continue
            grupos.setdefault(captura, []).append(f)

        salida, fusionadas = [], 0
        for captura, grupo in grupos.items():
            # `filas` viene de la más reciente a la más antigua, así que la
            # primera del grupo es la publicación más nueva.
            conservada = grupo[0]
            if len(grupo) > 1:
                descartadas = [g['publicada'] for g in grupo[1:]]
                conservada['equivalentes'] = descartadas
                fusionadas += len(descartadas)
                feedback.pushInfo(
                    f"  captura {captura}: se conserva la versión "
                    f"{conservada['publicada']} y se omiten "
                    f"{', '.join(descartadas)} (misma fotografía, "
                    f"reprocesada)")
            salida.append(conservada)

        salida.extend(sueltas)
        # Reordenar como venían: por posición original.
        orden = {id(f): i for i, f in enumerate(filas)}
        salida.sort(key=lambda f: orden.get(id(f), 1 << 30))

        if fusionadas:
            feedback.pushInfo(
                f"\n{len(salida)} fotografía(s) distinta(s) tras agrupar por "
                f"fecha de captura ({fusionadas} versión(es) equivalentes "
                f"omitidas).")
        return salida

    # --------------------------------------------------------- hilo principal
    def postProcessAlgorithm(self, context, feedback):
        """Añade las capas XYZ. Corre en el HILO PRINCIPAL.

        Es el único punto seguro para crear capas y tocar el árbol del
        proyecto: processAlgorithm corre en un hilo de trabajo y hacerlo allí
        provoca caídas intermitentes.
        """
        if not self._xyz_pendientes:
            return self._resultados or {}

        proyecto = context.project() or QgsProject.instance()
        anadidas = falladas = 0
        for i, pend in enumerate(self._xyz_pendientes):
            try:
                # El proveedor «wms» es obligatorio para XYZ y no se infiere.
                capa = QgsRasterLayer(pend['uri'], pend['nombre'], 'wms')
                if capa is None or not capa.isValid():
                    falladas += 1
                    feedback.pushWarning(
                        f"[!] No se pudo crear «{pend['nombre']}». "
                        f"Compruebe la conexión y si su versión de QGIS sufre "
                        f"la incidencia qgis/QGIS#54161.")
                    continue
                if pend['detalle']:
                    meta = capa.metadata()
                    meta.setRights([ATRIBUCION,
                                    'Esri Master License Agreement'])
                    meta.setAbstract(pend['detalle'])
                    capa.setMetadata(meta)
                proyecto.addMapLayer(capa, False)
                nodo = proyecto.layerTreeRoot().insertLayer(i, capa)
                # Solo la primera visible: son mosaicos opacos y apilarlos
                # visibles deja ver uno solo, pero los dibuja todos.
                if nodo is not None:
                    nodo.setItemVisibilityChecked(i == 0)
                anadidas += 1
            except Exception as e:
                falladas += 1
                feedback.pushWarning(f"[carga_xyz] {pend['nombre']}: {e}")

        feedback.pushInfo(
            f"\nCapas añadidas al proyecto: {anadidas}"
            + (f" ({falladas} fallida(s))" if falladas else ""))
        if anadidas:
            feedback.pushInfo(
                "Solo la más reciente queda visible: son mosaicos opacos y "
                "activarlas todas\nno deja ver más, pero las descarga todas. "
                "Actívelas de una en una para comparar.")
        self._xyz_pendientes = None
        return self._resultados or {}

    # ------------------------------------------------------------- internos
    def _rect_4326(self, parameters, context, feedback):
        """[oeste, sur, este, norte] del AOI, o None."""
        fuente = self.parameterAsSource(parameters, self.AOI, context)
        if fuente is not None:
            try:
                rect = fuente.sourceExtent()
                if rect is not None and not rect.isEmpty():
                    crs = fuente.sourceCrs()
                    wgs = QgsCoordinateReferenceSystem('EPSG:4326')
                    if crs.isValid() and crs != wgs:
                        tr = QgsCoordinateTransform(
                            crs, wgs, context.transformContext())
                        rect = tr.transformBoundingBox(rect)
                    return [rect.xMinimum(), rect.yMinimum(),
                            rect.xMaximum(), rect.yMaximum()]
            except Exception as e:
                if feedback is not None:
                    feedback.pushWarning(f"[_rect_4326] AOI ilegible: {e}")
        ext = self.parameterAsExtent(
            parameters, self.EXTENSION, context,
            QgsCoordinateReferenceSystem('EPSG:4326'))
        if ext.isEmpty():
            return None
        return [ext.xMinimum(), ext.yMinimum(),
                ext.xMaximum(), ext.yMaximum()]

    @staticmethod
    def _dimension_px(rect, res_m):
        lat_m = math.radians((rect[1] + rect[3]) / 2.0)
        ancho_m = abs(rect[2] - rect[0]) * 111320.0 * math.cos(lat_m)
        alto_m = abs(rect[3] - rect[1]) * 110570.0
        return max(1.0, ancho_m / res_m), max(1.0, alto_m / res_m)

    @staticmethod
    def _puntos(rect, n):
        oeste, sur, este, norte = rect
        if n <= 1:
            return [((oeste + este) / 2.0, (sur + norte) / 2.0)]
        pts = []
        for i in range(n):
            for j in range(n):
                pts.append((oeste + (i + 0.5) / n * (este - oeste),
                            sur + (j + 0.5) / n * (norte - sur)))
        return pts

    def _cargar_versiones(self):
        """[{num, fecha, titulo, url, meta}] de la más nueva a la más vieja."""
        cfg = _obtener(CONFIG_URL)
        out = []
        for clave, val in cfg.items():
            try:
                num = int(clave)
            except (TypeError, ValueError):
                continue
            titulo = val.get('itemTitle') or ''
            fecha = ''
            if '(' in titulo and ')' in titulo:
                fecha = (titulo[titulo.rindex('(') + 1:titulo.rindex(')')]
                         .replace('Wayback', '').strip())
            out.append({'num': num, 'fecha': fecha, 'titulo': titulo,
                        'url': val.get('itemURL') or '',
                        'meta': val.get('metadataLayerUrl') or ''})
        out.sort(key=lambda v: (v['fecha'], v['num']), reverse=True)
        return out

    def _con_cambio(self, zoom, fila, col, feedback):
        """[(version, bytes)] con cambio local, de la más nueva a la más vieja.

        Iterativo y no recursivo: el archivo pasa de cien versiones y no hay
        razón para acercarse al límite de recursión de Python.
        """
        res, actual, vistos = [], self._versiones[0]['num'], set()
        while actual is not None and actual not in vistos:
            if feedback is not None and feedback.isCanceled():
                break
            vistos.add(actual)
            url = f"{_base_wayback()}/tilemap/{actual}/{zoom}/{fila}/{col}"
            try:
                r = _obtener(url)
            except (RuntimeError, ValueError):
                break
            datos = r.get('data') or []
            if not datos or not datos[0]:
                break
            sel = r.get('select') or []
            version = int(sel[0]) if sel and sel[0] else actual
            res.append((version, (r.get('size') or [0])[0] or 0))
            pos = self._indice.get(version)
            actual = (self._versiones[pos + 1]['num']
                      if pos is not None and pos + 1 < len(self._versiones)
                      else None)
        return res

    def _sin_duplicados(self, candidatos, zoom, fila, col, comparar):
        """Descarta versiones cuya tesela muestra la MISMA fotografía.

        Se comparan los PÍXELES DECODIFICADOS, no los bytes. La versión
        anterior agrupaba por tamaño en bytes y solo comparaba dentro de cada
        grupo, siguiendo la optimización de la aplicación de Esri. Eso falla
        justo en el caso que más importa aquí: cuando Esri republica el
        mosaico recomprimiendo la misma imagen, el tamaño CAMBIA, de modo que
        las dos versiones caían en grupos distintos, nunca se comparaban y las
        dos sobrevivían. Se vio en dos corridas reales: dos versiones
        publicadas con un año de diferencia, misma fecha de captura,
        visualmente indistinguibles, descargadas por duplicado.

        Comparar píxeles cuesta descargar una tesela por candidato —cinco o
        seis en un lote típico, nada— y a cambio detecta el reprocesamiento
        aunque la capa de metadatos no responda, que es cuando la fecha de
        captura no está disponible para agrupar más tarde.
        """
        if len(candidatos) <= 1:
            return [c[0] for c in candidatos]
        if not comparar:
            # Sin comparación se conservan todas: es preferible un duplicado
            # a perder una imagen distinta por una suposición de tamaño.
            return [c[0] for c in candidatos]

        # De la más antigua a la más reciente, para conservar la primera
        # aparición de cada fotografía.
        unicos, previa = [], None
        for num, _tam in reversed(candidatos):
            arr = self._pixeles_tesela(num, zoom, fila, col)
            if arr is None:
                # Sin poder decodificar no se descarta nada.
                unicos.append(num)
                previa = None
                continue
            if previa is None or not _misma_imagen(arr, previa):
                unicos.append(num)
                previa = arr
        return list(reversed(unicos))

    def _pixeles_tesela(self, num, zoom, fila, col):
        """Tesela decodificada a arreglo, o None.

        Se decodifica en memoria con /vsimem/ para no tocar el disco: son
        teselas de 256x256 y escribir temporales por cada una sería lento y
        dejaría basura si la ejecución se cancela.
        """
        datos = self._tesela(num, zoom, fila, col)
        if not datos:
            return None
        ruta = f'/vsimem/wayback_{uuid.uuid4().hex[:12]}'
        try:
            gdal.FileFromMemBuffer(ruta, datos)
            ds = gdal.Open(ruta)
            if ds is None:
                return None
            arr = ds.ReadAsArray()
            ds = None
            return None if arr is None else np.asarray(arr, dtype=np.int16)
        except Exception as e:
            # Registrado, no silenciado: si GDAL no sabe leer el formato de
            # tesela que sirve Esri, conviene enterarse por el registro y no
            # que la deteccion de duplicados deje de funcionar en silencio.
            logging.getLogger('BuscadorSTAC').debug(
                f"[decodificar_tesela] version {num}: {e}")
            return None
        finally:
            try:
                gdal.Unlink(ruta)
            except Exception as e:
                logging.getLogger('BuscadorSTAC').debug(
                    f"[vsimem] {e}")

    def _tesela(self, num, zoom, fila, col):
        pos = self._indice.get(num)
        if pos is None:
            return None
        try:
            return _obtener(self._url_tesela(self._versiones[pos]['url'],
                                             zoom, fila, col), binario=True)
        except (RuntimeError, ValueError):
            return None

    @staticmethod
    def _url_tesela(plantilla, zoom, fila, col):
        """La ruta de Esri es /tile/{nivel}/{fila}/{columna} = z/y/x."""
        return (plantilla.replace('{level}', str(zoom))
                .replace('{row}', str(fila))
                .replace('{col}', str(col))
                .replace('{column}', str(col)))

    @staticmethod
    def _url_xyz(plantilla):
        return (plantilla.replace('{level}', '{z}')
                .replace('{row}', '{y}')
                .replace('{col}', '{x}')
                .replace('{column}', '{x}'))

    def _filtrar_fechas(self, nums, f_ini, f_fin, feedback):
        if not f_ini and not f_fin:
            return nums
        fuera, dentro = 0, []
        for n in nums:
            fecha = self._versiones[self._indice[n]]['fecha']
            if (f_ini and fecha < f_ini) or (f_fin and fecha > f_fin):
                fuera += 1
                continue
            dentro.append(n)
        if fuera:
            feedback.pushInfo(
                f"  {fuera} versión(es) fuera del rango de publicación "
                f"indicado.")
        return dentro

    def _metadatos_aoi(self, num, zoom, puntos, feedback=None):
        """Metadatos de una versión SOBRE TODO EL AOI, no en un punto.

        Consultar solo el centro daba resultados enganosos, y se vio en un
        caso real: dos versiones publicadas con un ano de diferencia salian
        ambas con captura 2022-02-03, pese a que la comparacion byte a byte
        habia determinado que sus teselas eran distintas. La razon es que la
        deteccion de cambio muestrea una rejilla y la fecha se pedia en un
        solo punto: una version puede diferir en una esquina del lote y no en
        el centro, y entonces la fecha del centro no describe lo que cambio.

        Se consultan varios puntos y se agregan los resultados distintos. Si
        el lote abarca dos footprints de origen, eso se ve.

        Devuelve un dict: fechas (lista), resumen (texto para la tabla),
        fuentes (lista «Proveedor (SENSOR)»), res, exactitud, fallos, y
        detalle (la frase completa, al estilo de la aplicacion de Esri).
        """
        fechas, fuentes, res_m, exactitud, fallos = [], [], None, None, 0
        # Se acota el muestreo: cada consulta puede tardar, y con dos o tres
        # puntos ya se detecta que el lote no es homogeneo, que es lo que
        # interesa saber.
        for lon, lat in puntos[:MAX_PUNTOS_META]:
            r = self._metadatos(num, zoom, lon, lat)
            if not r:
                continue
            if 'error' in r:
                fallos += 1
                continue
            if r['fecha'] not in fechas:
                fechas.append(r['fecha'])
            fuente = r['proveedor']
            if r['sensor'] and r['sensor'] != r['proveedor']:
                fuente = f"{fuente} ({r['sensor']})" if fuente else r['sensor']
            if fuente and fuente not in fuentes:
                fuentes.append(fuente)
            if res_m is None:
                res_m = r['res']
            if exactitud is None:
                exactitud = r['exactitud']
            if feedback is not None and feedback.isCanceled():
                break

        if not fechas:
            return {'fechas': [], 'resumen': '?' if fallos else '—',
                    'fuentes': [], 'res': None, 'exactitud': None,
                    'fallos': fallos,
                    'detalle': (f'metadatos no disponibles '
                                f'({fallos} consulta(s) fallida(s))' if fallos
                                else 'la capa de metadatos no cubre el punto')}

        orden = sorted(fechas)
        resumen = (orden[0] if len(orden) == 1
                   else f"{orden[0]}…{orden[-1]} ({len(orden)})")
        return {'fechas': orden, 'resumen': resumen, 'fuentes': fuentes,
                'res': res_m, 'exactitud': exactitud, 'fallos': fallos,
                'detalle': self._frase_origen(orden, fuentes, res_m,
                                              exactitud, num)}

    def _frase_origen(self, fechas, fuentes, res_m, exactitud, num):
        """La descripción que muestra la aplicación Wayback de Esri.

        Ej.: «Vantor (LG01), captada 2026-02-13, mostrada en la versión
        2026-08-05 · resolución de origen 0.60 m · exactitud 5.00 m».
        Se reproduce porque es la procedencia que hay que citar al publicar
        cualquier producto derivado de la imagen.
        """
        pos = self._indice.get(num)
        publicada = self._versiones[pos]['fecha'] if pos is not None else '?'
        quien = ' / '.join(fuentes) if fuentes else 'origen no declarado'
        cuando = (fechas[0] if len(fechas) == 1
                  else f"{fechas[0]} a {fechas[-1]} según la zona")
        partes = [f"{quien}, captada {cuando}, mostrada en la versión "
                  f"{publicada} del mapa World Imagery"]
        if isinstance(res_m, (int, float)):
            partes.append(f"resolución de origen {res_m:.2f} m/píxel")
        if isinstance(exactitud, (int, float)):
            partes.append(f"exactitud posicional {exactitud:.2f} m")
        return ' · '.join(partes)

    def _metadatos(self, num, zoom, lon, lat):
        """Metadatos de la imagen de origen en UN punto.

        Devuelve un dict con los cinco campos que publica la capa, que son
        exactamente los que la aplicación Wayback de Esri muestra al pinchar
        un punto: proveedor, sensor, fecha de captura, resolución del original
        y exactitud posicional. Se conservan todos, no solo la fecha: la
        resolución dice si merece la pena bajar a 0.3 m, y la exactitud es lo
        que acota cuánto puede desplazarse un árbol digitalizado respecto de
        su posición real, que importa al comparar con GPS de campo.

        Devuelve None si la capa no cubre el punto, o {'error': motivo}.
        """
        pos = self._indice.get(num)
        if pos is None or not self._versiones[pos]['meta']:
            return None
        capa = max(0, min(ZOOM_MAX_META - zoom,
                          ZOOM_MAX_META - ZOOM_MIN_META))
        params = urllib.parse.urlencode({
            'f': 'json',
            'where': '1=1',
            'outFields': 'SRC_DATE2,NICE_DESC,SRC_DESC,SAMP_RES,SRC_ACC',
            'geometry': json.dumps({'spatialReference': {'wkid': 4326},
                                    'x': lon, 'y': lat}),
            'geometryType': 'esriGeometryPoint',
            'spatialRel': 'esriSpatialRelIntersects',
            'returnGeometry': 'false',
        })
        if self._cache_meta is None:
            # processAlgorithm la inicializa por ejecución; esto cubre una
            # llamada directa al método, que si no reventaría con TypeError
            # al probar la pertenencia sobre None.
            self._cache_meta = {}
        clave = (num, round(lon, 5), round(lat, 5))
        if clave in self._cache_meta:
            return self._cache_meta[clave]
        try:
            r = _obtener(f"{self._versiones[pos]['meta']}/{capa}/query?{params}",
                         espera=TIEMPO_ESPERA_META,
                         reintentos=REINTENTOS_META)
        except Exception as e:
            # Deliberadamente amplio: la fecha de captura es informativa y
            # NINGÚN fallo suyo debe tumbar una ejecución que ya hizo el
            # trabajo caro. Se devuelve el motivo para poder informarlo.
            resultado = {'error': str(e)}
            self._cache_meta[clave] = resultado
            return resultado
        rasgos = r.get('features') or []
        if not rasgos:
            self._cache_meta[clave] = None
            return None
        a = rasgos[0].get('attributes') or {}
        resultado = {
            'fecha': _fecha_epoch(a.get('SRC_DATE2')),
            'proveedor': (a.get('NICE_DESC') or '').strip(),
            'sensor': (a.get('SRC_DESC') or '').strip(),
            'res': a.get('SAMP_RES'),
            'exactitud': a.get('SRC_ACC'),
        }
        self._cache_meta[clave] = resultado
        return resultado

    # --------------------------------------------------------- salida: XYZ
    def _cargar_xyz(self, filas, rect, context, feedback):
        """Encola las capas XYZ; se añaden en postProcessAlgorithm.

        NO se usa context.addLayerToLoadOnCompletion, y no por capricho: ese
        camino resuelve la fuente con QgsProcessingUtils.mapLayerFromString,
        que prueba los proveedores de archivo (ogr, gdal). Una URI
        «type=xyz&url=…» necesita el proveedor «wms» indicado de forma
        explícita —no se deduce de la cadena—, así que todas las capas salían
        nulas y QGIS las listaba como «no se generaron correctamente». Se vio
        en un registro real: seis de seis.

        Construirlas a mano exige el hilo principal, porque tocan el proyecto;
        de ahí que se encolen aquí y se materialicen en postProcessAlgorithm.
        """
        self._xyz_pendientes = []
        for fila in filas:
            url = self._url_xyz(fila['url'])
            uri = (f"type=xyz&url={urllib.parse.quote(url, safe='')}"
                   f"&zmin=0&zmax=23")
            etiqueta = ("Esri vigente" if fila['num'] is None else
                        f"Wayback {fila['publicada']} "
                        f"(captura {fila['captura']})")
            detalle = (fila.get('meta') or {}).get('detalle', '')
            if fila.get('equivalentes'):
                detalle += (f" · misma fotografía publicada también en "
                            f"{', '.join(fila['equivalentes'])}")
            self._xyz_pendientes.append({
                'uri': uri, 'nombre': etiqueta, 'detalle': detalle})
            feedback.pushInfo(f"  capa XYZ → {etiqueta}")
        feedback.pushInfo(
            "\nLas capas se añadirán al terminar. Se dibujan al vuelo desde el "
            "servicio;\nsi alguna sale en blanco es la incidencia "
            "qgis/QGIS#54161 (redirecciones\nrelativas): use el modo de "
            "descarga.")

    # ---------------------------------------------------- salida: GeoTIFF
    def _descargar(self, filas, rect, zoom, ancho_px, alto_px,
                   carpeta, context, feedback, res_descarga=0.0):
        """Recorta cada versión a un GeoTIFF RGB mediante el driver WMS/TMS."""
        os.makedirs(carpeta, exist_ok=True)
        total = max(1, len(filas))
        for i, fila in enumerate(filas):
            if feedback.isCanceled():
                return
            if fila['num'] is None:
                etiqueta = 'vigente'
            elif (fila['captura'] in ('?', '—', '')
                  or not _RE_FECHA_SOLA.fullmatch(str(fila['captura']))):
                # Sin fecha limpia se omite ese tramo. Cubre «?», «—» y el
                # rango «2022-02-03…2024-05-11 (2)», que lleva caracteres
                # inválidos en un nombre de archivo de Windows.
                etiqueta = f"{fila['publicada']}_capND"
            else:
                etiqueta = f"{fila['publicada']}_cap{fila['captura']}"
            nombre = f"EsriWI_{etiqueta.replace('-', '')}.tif"
            destino = os.path.join(carpeta, nombre)
            if os.path.exists(destino):
                feedback.pushInfo(f"  omitido (ya existe) → {nombre}")
                continue

            xml = self._xml_tms(self._url_tms(fila['url']), zoom)
            try:
                ds = gdal.Translate(
                    destino, xml,
                    options=gdal.TranslateOptions(
                        format='GTiff',
                        projWin=[rect[0], rect[3], rect[2], rect[1]],
                        projWinSRS='EPSG:4326',
                        width=int(round(ancho_px)), height=int(round(alto_px)),
                        creationOptions=['COMPRESS=DEFLATE', 'TILED=YES',
                                         'PHOTOMETRIC=RGB']))
                if ds is None:
                    raise RuntimeError('gdal.Translate devolvió None')
                # La procedencia viaja DENTRO del GeoTIFF: un archivo que
                # sale de la carpeta sin ella es una imagen sin fecha, y la
                # fecha de captura es justo lo que no se puede deducir
                # mirándola. Se leen con gdalinfo o en Propiedades de la capa.
                m = fila.get('meta') or {}
                etiquetas = {
                    'FUENTE': 'Esri World Imagery',
                    'VERSION_WAYBACK': str(fila['publicada']),
                    'FECHA_CAPTURA': str(fila['captura']),
                    'ATRIBUCION': ATRIBUCION,
                    'LICENCIA': 'Esri Master License Agreement',
                    'GENERADO_POR': f'esri_wayback {self.VERSION}',
                    'AUTOR': f'{AUTOR} <{AUTOR_EMAIL}>',
                }
                if m.get('detalle'):
                    etiquetas['PROCEDENCIA'] = str(m['detalle'])
                if m.get('fuentes'):
                    etiquetas['PROVEEDOR_SENSOR'] = ' / '.join(m['fuentes'])
                if m.get('fechas'):
                    etiquetas['FECHAS_CAPTURA'] = ','.join(m['fechas'])
                if isinstance(m.get('res'), (int, float)):
                    etiquetas['RESOLUCION_ORIGEN_M'] = f"{m['res']:.2f}"
                if isinstance(m.get('exactitud'), (int, float)):
                    etiquetas['EXACTITUD_POSICIONAL_M'] = f"{m['exactitud']:.2f}"
                etiquetas['RESOLUCION_DESCARGA_M'] = f"{res_descarga:.2f}"
                if fila.get('equivalentes'):
                    # La misma fotografia se publico tambien en estas
                    # versiones, reprocesada. Se anota para no perder la
                    # trazabilidad de lo que se omitio.
                    etiquetas['VERSIONES_EQUIVALENTES'] = ','.join(
                        fila['equivalentes'])
                ds.SetMetadata(etiquetas)
                ds.FlushCache()
                ds = None
            except Exception as e:
                feedback.pushWarning(f"[descarga] {nombre}: {e}")
                continue

            feedback.pushInfo(f"  descargado → {nombre}")
            capa = nombre[:-4]
            detalles = QgsProcessingContext.LayerDetails(capa, context.project(), capa)
            pp = _AtribucionPostProcessor(ATRIBUCION)
            _registrar_postproc(pp)
            detalles.setPostProcessor(pp)
            context.addLayerToLoadOnCompletion(destino, detalles)
            feedback.setProgress(80 + int(20.0 * (i + 1) / total))

    @staticmethod
    def _url_tms(plantilla):
        """Plantilla en la forma que espera el driver WMS/TMS de GDAL.

        Conviven dos formas: waybackconfig.json trae {level}/{row}/{col} y la
        URL del mosaico vigente trae {z}/{y}/{x}. La sustitución va en UNA
        sola pasada con re.sub, no encadenando replace(): encadenando, la
        tanda que atiende la segunda forma volvía a tocar el «${z}» recién
        escrito por la primera y lo dejaba en «$${z}», que GDAL no resuelve.
        """
        return _RE_PLACEHOLDER.sub(
            lambda m: _TMS_PLACEHOLDERS[m.group(0)], plantilla)

    @staticmethod
    def _xml_tms(server_url, zoom):
        """Descripción GDAL_WMS de un servicio TMS en Web Mercator.

        YOrigin=top porque las teselas de Esri se numeran desde el norte,
        como todo esquema XYZ de web. Con 'bottom' la imagen sale reflejada
        verticalmente, que es un fallo silencioso: produce un GeoTIFF válido
        con el contenido equivocado.
        """
        borde = 20037508.342789244
        return f"""<GDAL_WMS>
  <Service name="TMS">
    <ServerUrl>{server_url}</ServerUrl>
  </Service>
  <DataWindow>
    <UpperLeftX>-{borde}</UpperLeftX>
    <UpperLeftY>{borde}</UpperLeftY>
    <LowerRightX>{borde}</LowerRightX>
    <LowerRightY>-{borde}</LowerRightY>
    <TileLevel>{zoom}</TileLevel>
    <TileCountX>1</TileCountX>
    <TileCountY>1</TileCountY>
    <YOrigin>top</YOrigin>
  </DataWindow>
  <Projection>EPSG:3857</Projection>
  <BlockSizeX>256</BlockSizeX>
  <BlockSizeY>256</BlockSizeY>
  <BandsCount>3</BandsCount>
  <MaxConnections>4</MaxConnections>
  <Cache/>
</GDAL_WMS>"""


class _AtribucionPostProcessor(QgsProcessingLayerPostProcessorInterface):
    """Fija la atribución de Esri en las propiedades de la capa.

    Corre en el hilo principal (contrato de la interfaz), así que tocar los
    metadatos de la capa aquí es seguro; desde processAlgorithm no lo sería.
    """

    usado = False

    def __init__(self, atribucion):
        super().__init__()
        self.atribucion = atribucion

    def postProcessLayer(self, capa, context, feedback):
        try:
            if capa is None or not capa.isValid():
                return
            meta = capa.metadata()
            meta.setRights([self.atribucion,
                            'Esri Master License Agreement'])
            capa.setMetadata(meta)
        except Exception as e:
            feedback.pushDebugInfo(f"[atribucion] {e}")
        finally:
            self.usado = True


__all__ = ['EsriWaybackAlgorithm', 'ATRIBUCION', 'MODOS',
           'lon_a_columna', 'lat_a_fila', 'metros_por_pixel',
           'zoom_para_resolucion', '_POSTPROCESADORES']
