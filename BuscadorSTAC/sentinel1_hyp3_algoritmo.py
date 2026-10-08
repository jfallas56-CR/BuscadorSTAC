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
Sentinel-1 RTC vía ASF HyP3 — amplitud estacional de retrodispersión
====================================================================

Tercer algoritmo del complemento. Pide a ASF HyP3 productos Sentinel-1 RTC
(corregidos radiométrica y geométricamente por terreno), los colecta cuando
están listos y calcula la amplitud estacional seca − lluviosa sobre el AOI.

Para qué sirve, frente al camino óptico que ya hace el primer algoritmo: el
radar no ve nubes. La mitad débil de una amplitud NDMI es la mediana de
estación lluviosa, construida con pocas escenas y contaminada por nube — es
la razón de ser del umbral de observaciones mínimas. En radar esa mediana se
sustenta en TODAS las adquisiciones de la estación, porque no hay pérdida por
nube. El conteo de escenas óptico es bruto; el de radar es neto.

Lo que el radar NO resuelve: la retrodispersión en banda C responde con
fuerza a la HUMEDAD DEL SUELO, así que un potrero desnudo también presenta
amplitud estacional grande sin que haya vegetación de por medio. No es un
discriminante limpio de leñosas, es uno confundido de otra manera. El valor
está en combinarlo con la amplitud óptica y, si hace falta, con HV en banda L
(ALOS PALSAR): los tres fallan por motivos distintos.

LA REGLA QUE NO SE PUEDE ROMPER: no se mezclan trazas. Cada órbita relativa
observa con otro ángulo de incidencia y otra dirección de mirada, de modo que
el retrodispersado del MISMO suelo cambia entre trazas por geometría, no por
vegetación. Una amplitud calculada sobre trazas mezcladas mide, en buena
parte, el cambio de geometría. Por eso el modo de inventario obliga a elegir
una traza antes de pedir nada, y la traza va en el nombre del archivo: un
apilado mezclado se ve a simple vista en el panel de capas en lugar de
descubrirse en los resultados.

Requiere una cuenta gratuita de Earthdata Login y un token portador:
    https://urs.earthdata.nasa.gov/documentation/for_users/user_token
El token se escribe en el parámetro correspondiente y NO se guarda en
ninguna parte: ni en el proyecto, ni en el manifiesto, ni en el registro.

Autor  : Jorge Fallas (jfallas56@gmail.com)
Versión: 1.1.0
Licencia: GPL v2 o posterior

Historial:
    1.0.0 (2026-10-02): Primera versión pública.
        El historial detallado del desarrollo previo a la publicación está en
        CHANGELOG.md del repositorio:
        https://github.com/jfallas56-CR/BuscadorSTAC
"""

import base64
import binascii
import json
import logging
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import warnings
from collections import defaultdict
from datetime import datetime, timezone

from qgis.PyQt.QtCore import QCoreApplication
from qgis.PyQt.QtGui import QIcon
from qgis.core import (
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsProcessingAlgorithm,
    QgsProcessingException,
    QgsProcessingOutputFolder,
    QgsProcessingParameterAuthConfig,
    QgsProcessingParameterBoolean,
    QgsProcessingParameterCrs,
    QgsProcessingParameterEnum,
    QgsProcessingParameterExtent,
    QgsProcessingParameterFeatureSource,
    QgsProcessingParameterFile,
    QgsProcessingParameterNumber,
    QgsProcessingParameterString,
    QgsProcessingUtils,
    QgsProject,
)

import numpy as np

from osgeo import gdal

from .buscar_sentinel2_algoritmo import (
    AUTOR, AUTOR_EMAIL, RealcePostProcessor, _TIPO_POLIGONO, _cargar_pendientes,
    _enum_qgis, _registrar_postproc)
from .core import (CREDITOS_RTC, lista_con_zip,
                   opciones_asequibles, ordenar_trazas,
                   sin_firma_url, sin_prefijo_bearer)

_LOG = logging.getLogger('BuscadorSTAC')

# --------------------------------------------------------------------------
# Servicios
# --------------------------------------------------------------------------
# Búsqueda de gránulos. La fuente es ASF y no Planetary Computer por una
# razón comprobada contra ambos servicios: el identificador final de 4 hex del
# nombre SAFE identifica la VERSIÓN del producto, no la adquisición, y las dos
# colecciones guardan versiones distintas de la misma toma. Para 2025 en la
# traza 157 coincidían los seis primeros campos y diferían solo en ese último:
#
#   Planetary Computer  S1A_..._057279_070C02_A80F
#   ASF                 S1A_..._057279_070C02_BD01
#
# HyP3 solo procesa lo que hay en el archivo de ASF, así que un nombre tomado
# de Planetary Computer se rechaza con «Some requested scenes could not be
# found» aunque la escena exista en las dos partes. Buscar donde se va a pedir
# es la única forma de que el inventario y el pedido no puedan discrepar.
API_ASF = 'https://api.daac.asf.alaska.edu/services/search/param'
API_HYP3 = 'https://hyp3-api.asf.alaska.edu'

# Tope de resultados por consulta a ASF. Un año de una traza sobre un lote
# ronda las 60 escenas; el tope está muy por encima y se avisa si se alcanza,
# porque un resultado truncado en silencio falsearía el inventario.
TOPE_RESULTADOS = 1000

# CREDITOS_RTC vive en core.py, donde se prueba sin QGIS.
# Asignación mensual gratuita de HyP3 Basic.
# Fuente: https://hyp3-docs.asf.alaska.edu/using/credits/
CREDITOS_LIBRES_MES = 8000

# HyP3 acepta hasta 200 definiciones de trabajo por peticion.
MAX_TRABAJOS_POR_PETICION = 200

TIEMPO_ESPERA = 90
TIEMPO_ESPERA_ENVIO = 120
REINTENTOS = 1

# --------------------------------------------------------------------------
# Radiometría
# --------------------------------------------------------------------------
# Se pide escala «power» y no «dB» a propósito. La MEDIANA es invariante al
# cambio monótono, así que para la amplitud daría igual; pero cualquier
# COCIENTE (RVI, VH/VV) y cualquier PROMEDIO hay que calcularlos en potencia.
# Promediar decibelios da un número que no es el promedio de nada. La
# conversión a dB se deja para el despliegue.
ESCALA = 'power'
RADIOMETRIA = 'gamma0'

# Rango físico admisible, para acotar artefactos igual que hace
# _acotar_indice con los índices ópticos. Los límites son derivados, no
# convencionales:
#   gamma0 en potencia es una potencia: negativo es IMPOSIBLE. Por arriba, la
#   vegetación y el suelo quedan muy por debajo de 1; se supera solo con
#   doble rebote fuerte (estructuras, troncos inundados). 3.0 deja eso dentro
#   y recorta lo que ya no es medida.
#   RVI dual-pol = 4*VH/(VV+VH). Si VV tiende a 0 la razón crece sin cota, de
#   modo que el límite superior es algebraico, no físico: con VH = VV da 2.
#   Se acota en 4.0 para dejar margen y marcar lo demás como artefacto.
LIMITES_SAR = {
    'VH': (0.0, 3.0),
    'VV': (0.0, 3.0),
    'RVI': (0.0, 4.0),
}

# gamma0 exactamente 0 no es una medida posible: es relleno. Se usa como
# nodata de respaldo cuando la banda no declara el suyo.
NODATA_RESPALDO = 0.0

MODOS = [
    "Inventario — qué hay sobre el AOI (no consume créditos)",
    "Pedir trabajos RTC a HyP3 (CONSUME créditos)",
    "Colecta de datos: productos y amplitud estacional",
]

ORBITAS = ["Cualquiera", "Ascendente", "Descendente"]
RESOLUCIONES = ["30 m (5 créditos/escena)",
                "20 m (15 créditos/escena)",
                "10 m (60 créditos/escena)"]
RESOLUCION_M = (30, 20, 10)
POLARIZACIONES = ["VH (recomendada para estructura)",
                  "VV",
                  "VH y VV (permite RVI)"]

_FILE_FOLDER = _enum_qgis(QgsProcessingParameterFile, 'Behavior', 'Folder', 1)
_FILE_ARCHIVO = _enum_qgis(QgsProcessingParameterFile, 'Behavior', 'File', 0)

# Variable de entorno de respaldo si no se indica archivo de token.
VAR_ENTORNO_CLAVE = 'EARTHDATA_TOKEN'

_RE_FECHA = re.compile(r'^\d{4}-\d{2}-\d{2}$')
# Nombre SAFE: ..._20220203T001234_...  El grupo da AAAAMMDD.
_RE_FECHA_SAFE = re.compile(r'_(\d{8})T\d{6}_')
_RE_SELLO_SAFE = re.compile(r'_(\d{8}T\d{6})_')


# --------------------------------------------------------------------------
# Red — un solo tipo de excepción hacia fuera
# --------------------------------------------------------------------------
def _url_https(url):
    """Rechaza cualquier esquema que no sea https (requisito de B310)."""
    esquema = urllib.parse.urlparse(url).scheme.lower()
    if esquema != 'https':
        raise ValueError(f"Esquema no permitido: '{esquema or '(vacío)'}'")
    return url


def _saldo_creditos(token, feedback):
    """(disponibles, por_mes) del endpoint /user de HyP3, o (None, None).

    La asignación mensual es un número fijo; el SALDO es el que decide si
    un pedido entra. Darlos por lo mismo hizo que el inventario
    recomendara gastar 1860 créditos a quien le quedaban 1630, y el
    rechazo llegara del servidor como un HTTP 400 después de haber
    elegido traza y haber marcado la confirmación de gasto.

    Si no se puede leer, se devuelve (None, None) y quien llama sigue con
    la asignación como referencia: no saber el saldo no es motivo para
    impedir un pedido que el servidor puede aceptar.
    """
    try:
        datos = _peticion(API_HYP3.rstrip('/') + '/user', token=token,
                          reintentos=1)
    except RuntimeError as e:
        feedback.pushWarning(
            f"[!] No se pudo leer el saldo de créditos: "
            f"{_sin_secreto(e, token)}. Se sigue con la asignación mensual "
            f"como referencia, así que el servidor podría rechazar el "
            f"pedido por saldo.")
        return None, None
    if not isinstance(datos, dict):
        return None, None
    saldo = datos.get('remaining_credits')
    por_mes = datos.get('credits_per_month')
    try:
        saldo = None if saldo is None else float(saldo)
        por_mes = None if por_mes is None else float(por_mes)
    except (TypeError, ValueError):
        return None, None
    return saldo, por_mes


def _sin_secreto(texto, token):
    """Quita el token de un texto antes de registrarlo.

    El token va en una cabecera, así que urllib no lo incluye en sus
    excepciones; pero el cuerpo de un error del servidor es texto ajeno y no
    hay garantía de lo que trae. Esto es defensa en profundidad: el token no
    debe poder aparecer en el registro de QGIS por ninguna vía.
    """
    if token and len(token) >= 8:
        return str(texto).replace(token, '<token oculto>')
    return str(texto)


def _peticion(url, payload=None, token=None, espera=TIEMPO_ESPERA,
              reintentos=REINTENTOS):
    """GET si payload es None, POST JSON si no. SIEMPRE lanza RuntimeError.

    Un solo tipo de excepción hacia fuera, porque TimeoutError no hereda de
    urllib.error.URLError —es OSError— y escaparía de un manejador que solo
    atrape HTTPError y URLError, abortando la corrida DESPUÉS de la parte
    cara. El orden importa: URLError es subclase de OSError, así que va antes.
    """
    _url_https(url)
    cabeceras = {'Accept': 'application/json'}
    datos = None
    if payload is not None:
        datos = json.dumps(payload).encode('utf-8')
        cabeceras['Content-Type'] = 'application/json'
    if token:
        cabeceras['Authorization'] = f'Bearer {token}'
    req = urllib.request.Request(url, data=datos, headers=cabeceras)

    ultimo = None
    for intento in range(max(1, reintentos + 1)):
        try:
            with urllib.request.urlopen(req, timeout=espera) as resp:  # nosec B310
                crudo = resp.read()
            return json.loads(crudo.decode('utf-8'))
        except urllib.error.HTTPError as e:
            try:
                # Sin recortar a unos cientos de caracteres: el cuerpo de
                # HyP3 repite la peticion completa ANTES de decir qué campo
                # rechazó, de modo que un tope corto deja fuera justo la
                # parte que explica el fallo. Se recorta por el FINAL.
                cuerpo = e.read().decode('utf-8', 'replace')
                if len(cuerpo) > 3000:
                    cuerpo = ('…(recortado el principio)… '
                              + cuerpo[-3000:])
            except (OSError, ValueError, AttributeError) as e2:
                cuerpo = f'(cuerpo ilegible: {e2})'
            # El cuerpo de HyP3 dice QUÉ campo rechazó, que es justo lo que
            # hace falta para corregir; se conserva en lugar de resumirlo.
            ultimo = RuntimeError(
                f"HTTP {e.code}: {_sin_secreto(cuerpo, token)}")
            if e.code in (401, 403):
                ultimo = RuntimeError(
                    f"HTTP {e.code}: el token de Earthdata no fue aceptado. "
                    f"Compruebe que no esté vencido y que lo haya copiado "
                    f"completo (son varios cientos de caracteres). Genere uno "
                    f"nuevo en urs.earthdata.nasa.gov → Generate Token.")
                break
            if e.code < 500:
                break
        except urllib.error.URLError as e:
            ultimo = RuntimeError(f"red: {e.reason}")
        except TimeoutError:
            ultimo = RuntimeError(
                f"tiempo de espera agotado ({espera:g} s)")
        except OSError as e:
            ultimo = RuntimeError(f"conexión: {_sin_secreto(e, token)}")
        except ValueError as e:
            ultimo = RuntimeError(f"respuesta ilegible: {e}")
            break
        if intento < reintentos:
            time.sleep(1.0 + intento)
    raise ultimo if ultimo else RuntimeError('fallo desconocido')


def _sello_granulo(granulo):
    """«20250203T001234» del nombre SAFE, o '' si no se puede leer.

    Se usa la marca de tiempo COMPLETA y no solo la fecha: una misma traza
    puede entregar varios gránulos del mismo día —IW se publica en lonjas,
    `s1:slice_number`— y dos archivos con el mismo nombre se sobreescriben o,
    peor, el segundo se da por descargado y se reutiliza el primero.
    """
    m = _RE_SELLO_SAFE.search(str(granulo or ''))
    return m.group(1) if m else ''


def _nombre_granulo(ident, props):
    """Nombre SAFE completo, que es lo que HyP3 acepta en «granules».

    Con ASF como fuente, `_normalizar_asf` siempre rellena
    `s1:product_identifier` con el `granuleName`, que es exactamente lo que
    HyP3 acepta, y el respaldo no llega a usarse. Se conserva porque es la
    única defensa si alguna vez se vuelve a alimentar esto desde un catálogo
    que entregue un identificador abreviado: el `id` de un ítem STAC de
    Planetary Computer, por ejemplo, omite el grupo final de 4 hex
    («…_003EE1» en lugar de «…_003EE1_60D5») y HyP3 lo rechaza sin que el
    motivo sea evidente, porque el prefijo sí encaja con su expresión regular.
    """
    completo = str(props.get('s1:product_identifier') or '').strip()
    if completo:
        return completo, True
    return str(ident or '').strip(), False


def _clave_desde_auth(auth_id):
    """Token desde una configuración de autenticación de QGIS.

    Es la más segura de las tres vías: la base de autenticación de QGIS está
    cifrada y protegida por la contraseña maestra, y lo que viaja por el
    diálogo —y por tanto por el registro y el historial— es un identificador
    de siete caracteres, no el secreto.

    Devuelve (token, origen) si lo consigue, o ('', motivo) si se indicó una
    configuración y falló. Si NO se indicó ninguna devuelve ('', ''), que el
    llamador distingue de un fallo para seguir con el archivo.
    """
    auth_id = (auth_id or '').strip()
    if not auth_id:
        return '', ''
    try:
        from qgis.core import QgsApplication, QgsAuthMethodConfig
    except ImportError as e:
        return '', f'el gestor de autenticación no está disponible: {e}'
    gestor = QgsApplication.authManager()
    if gestor is None:
        return '', 'el gestor de autenticación de QGIS no respondió'
    cfg = QgsAuthMethodConfig()
    # El tercer argumento pide los secretos, lo que exige que la base esté
    # desbloqueada. QGIS pregunta la contraseña maestra la primera vez, y esa
    # pregunta es de interfaz: por eso esto se llama también desde
    # checkParameterValues, que corre en el hilo principal.
    if not gestor.loadAuthenticationConfig(auth_id, cfg, True):
        return '', (
            f'no se pudo leer la configuración de autenticación «{auth_id}». '
            f'Si la base está bloqueada, ábrala en Configuración → Opciones → '
            f'Autenticación, introduzca la contraseña maestra y vuelva a '
            f'ejecutar.')
    mapa = dict(cfg.configMap() or {})
    # El método «API Header» guarda pares arbitrarios; el convenido para un
    # token portador es Authorization: Bearer <token>.
    for clave in mapa:
        if str(clave).strip().lower() == 'authorization':
            valor = sin_prefijo_bearer(mapa[clave])
            if valor:
                return valor, f'configuración de autenticación «{auth_id}»'
    # Respaldo: el método «Básico» guarda el secreto en «password».
    for clave in ('password', 'token', 'bearer'):
        valor = sin_prefijo_bearer(mapa.get(clave))
        if valor:
            return valor, (f'configuración de autenticación «{auth_id}», '
                           f'campo «{clave}»')
    return '', (
        f'la configuración «{auth_id}» existe pero no trae el token. Valen '
        f'dos vías: el método «API Header» con la clave «Authorization», o '
        f'el método «Básico» con el token en el campo de contraseña. El '
        f'prefijo «Bearer » es opcional: se añade solo si falta. Claves '
        f'presentes: {sorted(mapa) if mapa else "ninguna"}.')


def _obtener_clave(auth_id, ruta):
    """Token probando, en orden: configuración de QGIS, archivo, entorno.

    Una configuración indicada que falla NO cae al archivo en silencio: se
    informa ese fallo, porque indicarla fue una elección explícita y un
    respaldo callado dejaría al usuario creyendo que usó la vía cifrada.
    """
    token, origen = _clave_desde_auth(auth_id)
    if token:
        return token, origen
    if origen:
        return '', origen
    return _leer_clave(ruta)


def _leer_clave(ruta):
    """Token desde la primera línea del archivo, o de la variable de entorno.

    Devuelve (token, origen) o ('', motivo). El token NUNCA se devuelve junto
    a texto que vaya al registro: el llamador decide qué informar, y lo que
    informa es el ORIGEN, no el valor.
    """
    ruta = (ruta or '').strip()
    if ruta:
        if not os.path.isfile(ruta):
            return '', f"no existe el archivo «{ruta}»"
        try:
            with open(ruta, encoding='utf-8', errors='replace') as fh:
                for linea in fh:
                    linea = sin_prefijo_bearer(linea)
                    if linea:
                        return linea, f"archivo «{os.path.basename(ruta)}»"
        except OSError as e:
            return '', f"no se pudo leer «{ruta}»: {e}"
        return '', f"«{ruta}» está vacío"
    desde_entorno = sin_prefijo_bearer(os.environ.get(VAR_ENTORNO_CLAVE))
    if desde_entorno:
        return desde_entorno, f"variable de entorno {VAR_ENTORNO_CLAVE}"
    return '', (f"no se indicó archivo de token y la variable "
                f"{VAR_ENTORNO_CLAVE} no está definida")


def _caducidad_clave(token):
    """(estado, texto) leyendo «exp» del JWT, SIN verificar la firma.

    No se valida nada criptográficamente: solo se lee la fecha para poder
    decir «su token caducó el X» en vez de dejar que HyP3 responda un 401
    desnudo, que no dice por qué. Un token que no sea un JWT se acepta tal
    cual: el servidor es quien manda.
    """
    partes = str(token or '').split('.')
    if len(partes) != 3:
        return 'desconocida', 'el token no tiene forma de JWT'
    try:
        relleno = '=' * (-len(partes[1]) % 4)
        carga = json.loads(
            base64.urlsafe_b64decode(partes[1] + relleno).decode('utf-8'))
        exp = int(carga['exp'])
    except (ValueError, KeyError, TypeError, binascii.Error):
        return 'desconocida', 'no se pudo leer la fecha de caducidad'
    cuando = datetime.fromtimestamp(exp, timezone.utc)
    ahora = datetime.now(timezone.utc)
    dias = (cuando - ahora).days
    sello = cuando.strftime('%Y-%m-%d %H:%M UTC')
    if cuando <= ahora:
        return 'caducado', f'caducó el {sello}'
    if dias <= 3:
        return 'por_caducar', f'caduca el {sello} (en {dias} día(s))'
    return 'vigente', f'caduca el {sello} (en {dias} días)'


def _wkt_bbox(bbox):
    """POLYGON WKT en sentido antihorario a partir de [O, S, E, N]."""
    o, s, e, n = (float(v) for v in bbox)
    return (f"POLYGON(({o:.6f} {s:.6f},{e:.6f} {s:.6f},{e:.6f} {n:.6f},"
            f"{o:.6f} {n:.6f},{o:.6f} {s:.6f}))")


def _normalizar_asf(registro):
    """Un resultado de ASF con los nombres de propiedad que usa el resto.

    Se traduce al vocabulario de las extensiones STAC `sat:` y `sar:` en lugar
    de cambiar los ayudantes que ya estaban probados contra él. Nombres de
    ASF que conviene no confundir: la órbita relativa es `path` —no
    `pathNumber`— y `flightDirection` viene en mayúsculas.
    """
    nombre = str(registro.get('granuleName') or '').strip()
    pol = str(registro.get('polarization') or '')
    return nombre, {
        'datetime': registro.get('startTime') or '',
        'sat:orbit_state': str(
            registro.get('flightDirection') or '?').strip().lower(),
        'sat:relative_orbit': registro.get('path', '?'),
        'sar:polarizations': [p for p in re.split(r'[+,\s]+', pol) if p],
        'sar:instrument_mode': registro.get('beamMode') or '?',
        'platform': registro.get('dataset') or '',
        # El nombre de ASF ES el que acepta HyP3, así que se guarda donde
        # _nombre_granulo lo busca y esa función sigue valiendo sin cambios.
        's1:product_identifier': nombre,
    }


def _mes_de(props, ident):
    """Mes de adquisición, de las propiedades o del nombre SAFE."""
    fecha = props.get('datetime') or props.get('start_datetime') or ''
    if len(fecha) >= 7 and fecha[4] == '-':
        try:
            return int(fecha[5:7])
        except ValueError:
            pass
    m = _RE_FECHA_SAFE.search(ident or '')
    return int(m.group(1)[4:6]) if m else None


def _fecha_de(props, ident):
    """Fecha AAAA-MM-DD de adquisición, o cadena vacía."""
    fecha = props.get('datetime') or props.get('start_datetime') or ''
    if len(fecha) >= 10 and fecha[4] == '-':
        return fecha[:10]
    m = _RE_FECHA_SAFE.search(ident or '')
    if m:
        c = m.group(1)
        return f"{c[:4]}-{c[4:6]}-{c[6:8]}"
    return ''


def _parsear_meses(texto, respaldo):
    """«12,1,2» → (12, 1, 2). Devuelve respaldo si no hay nada utilizable."""
    meses = []
    for trozo in re.split(r'[,;\s]+', str(texto or '')):
        trozo = trozo.strip()
        if not trozo:
            continue
        try:
            m = int(trozo)
        except ValueError:
            continue
        if 1 <= m <= 12 and m not in meses:
            meses.append(m)
    return tuple(meses) if meses else tuple(respaldo)


def _codigo_meses(meses):
    """(12, 1, 2) → '1212' — mismo criterio que el algoritmo óptico."""
    return ''.join(f"{m:02d}" for m in sorted(meses))


def _acotar(sufijo, valores, feedback):
    """Pone a NaN lo que cae fuera del rango físico, e informa cuántos."""
    limites = LIMITES_SAR.get(sufijo)
    if limites is None:
        return valores
    lo, hi = limites
    with np.errstate(invalid='ignore'):
        fuera = np.isfinite(valores) & ((valores < lo) | (valores > hi))
    n = int(fuera.sum())
    if n:
        valores = np.where(fuera, np.nan, valores)
        feedback.pushWarning(
            f"[!] {sufijo}: {n} píxel(es) fuera del rango físico "
            f"[{lo:g}, {hi:g}] pasan a NaN. En gamma0 en potencia un valor "
            f"negativo es imposible y uno muy alto es doble rebote, no "
            f"vegetación.")
    return valores


def _probar_vsicurl(url):
    """Si GDAL puede leer los primeros bytes por /vsicurl/, sin el ZIP.

    Todo lo demás de este diagnóstico lo pregunta urllib, que NO es el
    cliente que falla: GDAL trae su propio curl, con su propio almacén de
    certificados y su propia configuración de proxy. Que urllib reciba un
    206 impecable no demuestra que el curl de GDAL pueda hacer la misma
    petición —menos aún en Windows, donde los certificados y el proxy se
    configuran por separado—. Preguntárselo al cliente que importa separa
    «GDAL no llega al archivo» de «GDAL llega y no sabe leer el índice»,
    que son dos averías distintas y hasta ahora se confundían.
    """
    try:
        fh = gdal.VSIFOpenL('/vsicurl/' + url, 'rb')
    except RuntimeError as e:
        return f'GDAL por /vsicurl/: excepción ({e}).'
    if fh is None:
        return (f'GDAL NO pudo abrir la URL por /vsicurl/ '
                f'({gdal.GetLastErrorMsg() or "sin mensaje"}): la avería '
                f'está en la capa HTTP de GDAL —certificados o proxy—, no '
                f'en el ZIP. urllib sí llega, así que no es la red.')
    try:
        cabeza = gdal.VSIFReadL(1, 4, fh) or b''
    finally:
        gdal.VSIFCloseL(fh)
    if cabeza[:2] == b'PK':
        return ('GDAL sí lee los primeros bytes por /vsicurl/ y empiezan '
                'por PK, de modo que llega al archivo: la avería está en '
                'la lectura del índice, no en el acceso.')
    return (f'GDAL leyó por /vsicurl/, pero los primeros bytes no son los '
            f'de un ZIP: {cabeza!r}.')


def _estado_head(url, espera):
    """Qué contesta el servidor a una petición HEAD, en una línea."""
    try:
        pet = urllib.request.Request(_url_https(url), method='HEAD')
        # Esquema validado por _url_https arriba: B310 ya no aplica.
        with urllib.request.urlopen(pet, timeout=espera) as r:  # nosec B310
            codigo = getattr(r, 'status', None) or r.getcode()
            largo = r.headers.get('Content-Length') or '(sin tamaño)'
            return f'HTTP {codigo}, Content-Length {largo}'
    except urllib.error.HTTPError as e:
        return (f'HTTP {e.code} — GDAL averigua el tamaño con HEAD para '
                f'saltar al índice del ZIP; si no la contesta, no puede '
                f'listarlo')
    except (urllib.error.URLError, TimeoutError, ValueError) as e:
        return f'no respondió ({e})'


def _total_de_rango(cabecera):
    """Tamaño total que declara un Content-Range «bytes 0-1/985515996»."""
    m = re.search(r'/(\d+)\s*$', str(cabecera or ''))
    return int(m.group(1)) if m else 0


class Sentinel1Hyp3Algorithm(QgsProcessingAlgorithm):
    """Sentinel-1 RTC vía ASF HyP3, con amplitud estacional."""

    AOI = 'AOI'
    EXTENSION = 'EXTENSION'
    MODO = 'MODO'
    AUTH_EDL = 'AUTH_EDL'
    # Nombre deliberado: Bandit B105 marca cualquier literal
    # asignado a un identificador que contenga «token», aunque
    # sea la clave de un parametro del dialogo y no un secreto.
    # Se comprobo con la propia herramienta: el criterio es el
    # IDENTIFICADOR, no el valor. El portal informa severidad
    # Low, asi que se evita el hallazgo en vez de silenciarlo.
    CLAVE_EDL = 'CLAVE_EDL'
    ANIO = 'ANIO'
    TRAZA = 'TRAZA'
    ORBITA = 'ORBITA'
    RESOLUCION = 'RESOLUCION'
    POLARIZACION = 'POLARIZACION'
    FILTRO_SPECKLE = 'FILTRO_SPECKLE'
    MESES_SECA = 'MESES_SECA'
    MESES_LLUVIA = 'MESES_LLUVIA'
    MIN_OBS = 'MIN_OBS'
    CONFIRMAR = 'CONFIRMAR'
    TOPE_CREDITOS = 'TOPE_CREDITOS'
    MANIFIESTO = 'MANIFIESTO'
    SRC_SALIDA = 'SRC_SALIDA'
    CARPETA = 'CARPETA'
    SALIDA = 'SALIDA'

    VERSION = 'v1.1.0'

    def __init__(self):
        super().__init__()
        # Amplitudes a cargar en postProcessAlgorithm, en el hilo principal.
        self._pendientes = []
        # Se enciende si el servidor obliga a averiguar el tamaño por
        # rangos en vez de con HEAD. Ver _listar_zip.
        self._sin_head = False
        # SRC de salida pedido, y filas del informe de auditoría.
        self._crs_salida = None
        self._prod_informe = []
        self._avisos_informe = []

    # ------------------------------------------------------------ identidad
    def tr(self, cadena):
        return QCoreApplication.translate('Sentinel1Hyp3Algorithm', cadena)

    def createInstance(self):
        return Sentinel1Hyp3Algorithm()

    def name(self):
        return 'sentinel1rtchyp3'

    def displayName(self):
        return self.tr('Sentinel-1 RTC (ASF HyP3) — inventario por traza, '
                       'pedido de trabajos y amplitud estacional')

    def group(self):
        return self.tr('Teledetección')

    def groupId(self):
        return 'teledeteccion'

    def icon(self):
        ruta = os.path.join(os.path.dirname(__file__), 'icon.png')
        return QIcon(ruta) if os.path.exists(ruta) else QIcon()

    def shortHelpString(self):
        return self.tr(f"""<p><b>Versión {self.VERSION}</b> — {AUTOR}
&lt;{AUTOR_EMAIL}&gt;</p>

<p>Sentinel-1 <b>RTC</b> (corregido radiométrica y geométricamente por
terreno) a través de <b>ASF HyP3</b>, con amplitud estacional
seca&nbsp;&minus;&nbsp;lluviosa sobre el AOI.</p>

<p><b>Por qué radar, si ya hay óptico.</b> El radar no ve nubes. La mitad
débil de una amplitud NDMI es la mediana de estación lluviosa: pocas escenas,
contaminadas por nube — es la razón de ser del umbral de observaciones
mínimas. En radar esa mediana se sustenta en TODAS las adquisiciones de la
estación. El conteo de escenas óptico es bruto; el de radar es neto.</p>

<p><b>Lo que el radar no resuelve.</b> La retrodispersión en banda C responde
con fuerza a la <b>humedad del suelo</b>: un potrero desnudo también presenta
amplitud estacional grande, sin vegetación de por medio. No es un
discriminante limpio de leñosas, es uno confundido de otra manera. El valor
está en cruzarlo con la amplitud óptica.</p>

<p><b>No se mezclan trazas.</b> Cada órbita relativa mira con otro ángulo de
incidencia, de modo que el retrodispersado del mismo suelo cambia entre
trazas por geometría y no por vegetación. Elija UNA traza en el inventario.
La traza va en el nombre del archivo para que un apilado mezclado se vea en
el panel de capas.</p>

<p><b>Los tres modos, en orden.</b></p>
<ol>
<li><b>Inventario</b> — no consume créditos y no necesita token. Dice qué
trazas cubren el AOI, cuántas adquisiciones tiene cada una por estación, y
cuántos créditos costaría pedirlas. Empiece aquí.<br>
Los gránulos se buscan en el archivo de <b>ASF</b>, que es donde HyP3 va a
procesarlos. No es indiferente de dónde salga la lista: el grupo final de
cuatro caracteres del nombre identifica la VERSIÓN del producto, no la
adquisición, y otros catálogos guardan versiones distintas de la misma toma.
Un nombre tomado de otra parte se rechaza con «scenes could not be found»
aunque la escena exista en ambos sitios. Buscando donde se va a pedir, el
inventario y el pedido no pueden discrepar.</li>
<li><b>Pedir trabajos</b> — envía los trabajos RTC a HyP3 y escribe un
<i>manifiesto</i> JSON en la carpeta de salida. Consume créditos, así que
exige marcar la casilla de confirmación. Los productos tardan: de minutos a
horas según la cola.</li>
<li><b>Colecta de datos</b> — lee el manifiesto, consulta el estado, y con los
productos listos calcula la amplitud. Puede ejecutarlo varias veces: lo que
aún no esté listo se informa y se vuelve a intentar más tarde.</li>
</ol>

<p><b>Qué escribe la colecta.</b></p>
<ul>
<li><code>AMPL_…</code> — la amplitud: mediana de seca menos mediana de
lluvia, en potencia.</li>
<li><code>MEDSECA_…</code> y <code>MEDLLUV_…</code> — las dos medianas por
separado. La amplitud sola no dice sobre qué nivel se mide: una diferencia de
0,001 no significa lo mismo sobre un fondo de 0,005 que sobre uno de 0,05.
Llevan la misma máscara que la amplitud, así que restarlas da exactamente el
ráster de amplitud.</li>
<li><code>…_NOBS.tif</code> — observaciones válidas por píxel, banda 1 seca y
banda 2 lluvia.</li>
<li><code>INFORME_&lt;lote&gt;.html</code> — informe de auditoría: de dónde
salió el dato, con qué parámetros se pidió, con cuáles se procesó, qué fechas
entraron en cada estación y qué salió. Autocontenido, sin recursos remotos:
se abre dentro de años y sin red.</li>
</ul>

<p>Todo sale en el SRC nativo del producto —el UTM de la escena, EPSG:32616 o
32617 en Costa Rica— salvo que indique otro en «SRC de salida»: entonces los
productos finales se reproyectan al terminar, por vecino más próximo para
conservar los valores medidos.</p>

<p><b>Cuenta y token.</b> Hace falta una cuenta gratuita de Earthdata Login y
un token portador, que se genera en
<i>urs.earthdata.nasa.gov → Generate Token</i>.</p>

<p><b>El token nunca se escribe en el diálogo</b>, y no por gusto: Processing
vuelca todos los parámetros de entrada en el registro y en el historial antes
de ejecutar el algoritmo. Un token escrito en un campo queda ahí, lo guarda
QGIS y no este código, y acaba pegado en cualquier registro que se comparta
para pedir ayuda. Hay tres vías, y se prueban en este orden:</p>

<ol>
<li><b>Configuración de autenticación de QGIS</b> — recomendada. Base cifrada,
protegida por la contraseña maestra; por el diálogo pasa solo un
identificador de siete caracteres. Método <b>API Header</b>, clave
<code>Authorization</code>, valor <code>Bearer &lt;token&gt;</code>; también
sirve el método <b>Básico</b> con el token en el campo de contraseña.</li>
<li><b>Archivo de texto</b> cuya primera línea es el token. Más simple, sin
contraseña maestra, pero el token queda en claro en el disco. En el registro
aparece la ruta.</li>
<li><b>Variable de entorno</b> <code>EARTHDATA_TOKEN</code>, si no indica
ninguna de las dos anteriores. Útil para <code>qgis_process</code> desde la
línea de órdenes.</li>
</ol>

<p>Una configuración de autenticación indicada que falle NO cae al archivo en
silencio: se avisa, porque elegirla fue una decisión y un respaldo callado le
dejaría creyendo que usó la vía cifrada.</p>

<p>El token no se escribe en el manifiesto ni en los metadatos de las capas.
Los de Earthdata duran unos 60 días; el algoritmo lee esa fecha del propio
token para avisar antes de que caduque en medio de un lote. Si alguna vez
aparece en un registro, revóquelo y genere otro.</p>

<p><b>Créditos.</b> HyP3 Basic da {CREDITOS_LIBRES_MES} créditos gratis al
mes. Un trabajo RTC cuesta 5 créditos a 30 m, 15 a 20 m y 60 a 10 m. Una
traza de un año a 10 m ronda los 1&nbsp;500–3&nbsp;000 créditos, así que cabe
de sobra; el inventario le da la cifra exacta antes de gastar nada.</p>

<p><b>Radiometría.</b> Se pide escala <i>power</i> y no dB. La mediana es
invariante al cambio monótono, así que para la amplitud daría igual, pero
cualquier cociente (RVI, VH/VV) y cualquier promedio hay que calcularlos en
potencia: promediar decibelios da un número que no es el promedio de nada.</p>

<p><b>Datos.</b> Copernicus Sentinel data, procesados por ESA; productos RTC
generados por ASF DAAC HyP3 con software GAMMA. Cite ambas fuentes en
cualquier producto derivado.</p>
""")

    # ----------------------------------------------------------- parámetros
    def initAlgorithm(self, config=None):
        p = QgsProcessingParameterFeatureSource(
            self.AOI, self.tr('Capa de AOI (tiene prioridad sobre la '
                              'extensión)'),
            [_TIPO_POLIGONO], optional=True)
        p.setHelp(self.tr(
            'Su envolvente define el área consultada y el recorte. Es la '
            'forma recomendada de acotar.'))
        self.addParameter(p)

        p = QgsProcessingParameterExtent(
            self.EXTENSION, self.tr('Extensión (si no hay capa de AOI)'),
            optional=True)
        p.setHelp(self.tr(
            'Rectángulo de trabajo. El botón «…» ofrece «Usar extensión del '
            'lienzo del mapa».'))
        self.addParameter(p)

        p = QgsProcessingParameterEnum(
            self.MODO, self.tr('Modo'), options=MODOS, defaultValue=0)
        p.setHelp(self.tr(
            'Empiece por <b>Inventario</b>: no consume créditos, no necesita '
            'token, y le dice qué trazas hay y cuánto costaría cada una.<br>'
            '<b>Pedir trabajos</b> gasta créditos de verdad y es '
            'irreversible, por eso exige la casilla de confirmación.<br>'
            '<b>La colecta de datos</b> se puede repetir: los trabajos que aún no estén '
            'listos se informan y quedan para la siguiente vez.'))
        self.addParameter(p)

        p = QgsProcessingParameterAuthConfig(
            self.AUTH_EDL,
            self.tr('Configuración de autenticación de QGIS (recomendado)'),
            optional=True)
        p.setHelp(self.tr(
            '<b>La vía más segura de las tres.</b> La base de autenticación '
            'de QGIS está cifrada y protegida por la contraseña maestra, y lo '
            'que pasa por el diálogo —y por tanto por el registro y el '
            'historial— es solo un identificador de siete caracteres.<br>'
            '<b>Cómo se crea:</b> pulse el botón «+» de este parámetro, o vaya '
            'a Configuración → Opciones → Autenticación. Elija el método '
            '<b>API Header</b> y añada una entrada con clave '
            '<code>Authorization</code> y valor <code>Bearer </code> seguido '
            'de su token. También se acepta el método <b>Básico</b> con el '
            'token en el campo de contraseña. El prefijo <code>Bearer </code> '
            'es opcional en ambos casos: si falta se añade, y si está no se '
            'duplica.<br>'
            'Si indica una configuración y falla, se avisa en vez de pasar en '
            'silencio al archivo: elegirla fue una decisión y un respaldo '
            'callado le dejaría creyendo que usó la vía cifrada.<br>'
            'La primera vez de cada sesión QGIS pedirá la contraseña maestra.'))
        self.addParameter(p)

        p = QgsProcessingParameterFile(
            self.CLAVE_EDL,
            self.tr('…o archivo con el token (alternativa sencilla)'),
            behavior=_FILE_ARCHIVO, optional=True)
        p.setHelp(self.tr(
            'Alternativa a la configuración de autenticación, para quien no '
            'quiera manejar la contraseña maestra: la ruta de un archivo de '
            'texto cuya PRIMERA LÍNEA es el token portador de Earthdata '
            'Login, que se genera gratis en '
            '<i>urs.earthdata.nasa.gov → Generate Token</i>.<br>'
            'Se usa solo si no hay configuración de autenticación indicada. '
            'El token queda en claro en el disco, así que guárdelo fuera de '
            'las carpetas que sincroniza o comparte.<br>'
            '<b>Por qué un archivo y no un campo de texto.</b> El diálogo de '
            'Processing escribe TODOS los parámetros de entrada en el '
            'registro antes de ejecutar el algoritmo, y los guarda en el '
            'historial. Un token escrito en un campo quedaría ahí, visible y '
            'copiable, y acabaría pegado en cualquier registro que se '
            'comparta para pedir ayuda. Con un archivo, lo que aparece en el '
            'registro es la RUTA.<br>'
            'Guárdelo fuera de las carpetas que sincroniza o comparte. Si no '
            'indica archivo, se busca la variable de entorno '
            '<code>EARTHDATA_TOKEN</code>.<br>'
            'El modo de inventario no lo necesita.'))
        self.addParameter(p)

        p = QgsProcessingParameterNumber(
            self.ANIO, self.tr('Año'),
            QgsProcessingParameterNumber.Integer,
            defaultValue=2025, minValue=2014, maxValue=2100)
        p.setHelp(self.tr(
            'Año de las adquisiciones. La amplitud se calcula DENTRO del año, '
            'de modo que la estación seca es la de diciembre de ese año más '
            'enero a abril del mismo año — no la que cruza el fin de año. '
            'Para un año completo de seca que cruce diciembre, pida dos años '
            'y combine los resultados a mano.'))
        self.addParameter(p)

        p = QgsProcessingParameterNumber(
            self.TRAZA, self.tr('Traza (órbita relativa; 0 = todas, solo '
                                'para inventario)'),
            QgsProcessingParameterNumber.Integer,
            defaultValue=0, minValue=0, maxValue=175)
        p.setHelp(self.tr(
            'Número de órbita relativa. El inventario con 0 las lista todas; '
            'para pedir trabajos hay que fijar UNA.<br>'
            'No es un capricho del formato: mezclar trazas en una serie '
            'temporal mide cambio de geometría de observación, no cambio de '
            'vegetación. Elija la traza que mejor reparta entre estación seca '
            'y lluviosa, no la que tenga más adquisiciones en total.'))
        self.addParameter(p)

        p = QgsProcessingParameterEnum(
            self.ORBITA, self.tr('Dirección de órbita'),
            options=ORBITAS, defaultValue=0)
        p.setHelp(self.tr(
            'Ascendente y descendente pasan a horas locales distintas '
            '(aproximadamente las 18 h y las 6 h), así que entre ellas hay un '
            'desplazamiento sistemático por rocío y humedad. Sirven como '
            'comprobación cruzada: lo que debe coincidir es el PATRÓN '
            'espacial, no el valor absoluto.'))
        self.addParameter(p)

        p = QgsProcessingParameterEnum(
            self.RESOLUCION, self.tr('Espaciamiento de píxel'),
            options=RESOLUCIONES, defaultValue=2)
        p.setHelp(self.tr(
            'A 10 m una copa aislada de 5–15 m ocupa uno o dos píxeles; a 30 m '
            'vuelve a ser subpíxel, que es justo la limitación del SWIR que '
            'motivó buscar radar. El costo en créditos es el que aparece '
            'entre paréntesis, por escena.'))
        self.addParameter(p)

        p = QgsProcessingParameterEnum(
            self.POLARIZACION, self.tr('Polarización'),
            options=POLARIZACIONES, defaultValue=0)
        p.setHelp(self.tr(
            'VH es la polarización cruzada y responde a dispersión de volumen '
            '—hojas y ramas—, que es lo que interesa para estructura. VV '
            'responde más al suelo y a la rugosidad.<br>'
            'Con ambas se calcula además el <b>RVI</b> dual-pol, '
            '4·VH/(VV+VH) en potencia, que normaliza parte del efecto de '
            'humedad del suelo. Pedir las dos NO cuesta el doble: un mismo '
            'trabajo RTC entrega las dos polarizaciones del gránulo.'))
        self.addParameter(p)

        p = QgsProcessingParameterBoolean(
            self.FILTRO_SPECKLE, self.tr('Filtro de speckle (Enhanced Lee)'),
            defaultValue=False)
        p.setHelp(self.tr(
            'Lo aplica HyP3 en el servidor, con factor de amortiguamiento 1 y '
            'ventana de 7x7.<br>'
            'Desactivado por omisión a propósito: la mediana de una docena de '
            'fechas ya promedia el speckle por sí misma, y filtrar antes '
            'suaviza los bordes de las copas, que es precisamente el detalle '
            'que se quiere conservar a 10 m. Actívelo si va a interpretar una '
            'fecha suelta.'))
        self.addParameter(p)

        p = QgsProcessingParameterString(
            self.MESES_SECA, self.tr('Meses de estación seca'),
            defaultValue='12,1,2,3,4', optional=True)
        p.setHelp(self.tr(
            'Números de mes separados por coma. El valor por omisión es el '
            'régimen del Pacífico Norte de Costa Rica.'))
        self.addParameter(p)

        p = QgsProcessingParameterString(
            self.MESES_LLUVIA, self.tr('Meses de estación lluviosa'),
            defaultValue='5,6,7,8,9,10,11', optional=True)
        p.setHelp(self.tr(
            'Un mes que no figure en ninguna de las dos listas se descarga y '
            'luego no entra en la amplitud; se avisa cuando ocurre.'))
        self.addParameter(p)

        p = QgsProcessingParameterCrs(
            self.SRC_SALIDA,
            self.tr('SRC de salida (vacío = el nativo del producto)'),
            optional=True)
        p.setHelp(self.tr(
            'Los productos de HyP3 vienen en el UTM de la escena —en Costa '
            'Rica, EPSG:32616 o 32617— y los recortes lo conservan, porque '
            'reproyectar la retrodispersión antes de calcular la mediana '
            'la resamplearía sin necesidad.<br><br>'
            'Indique aquí un SRC solo si quiere los PRODUCTOS FINALES en '
            'otro: la amplitud, las medianas y el recuento se reproyectan '
            'al terminar, cuando el cálculo ya está hecho. Para Costa Rica '
            'lo habitual es CRTM05 (EPSG:8908).<br><br>'
            'Se remuestrea por vecino más próximo, a propósito: conserva '
            'exactamente los valores medidos en vez de inventar promedios '
            'entre píxeles. El coste es geométrico, hasta medio píxel.'))
        self.addParameter(p)

        p = QgsProcessingParameterNumber(
            self.MIN_OBS, self.tr('Mínimo de observaciones por píxel y '
                                  'estación'),
            QgsProcessingParameterNumber.Integer,
            defaultValue=3, minValue=1, maxValue=60)
        p.setHelp(self.tr(
            'Un píxel que no alcance este número de observaciones válidas en '
            'alguna de las dos estaciones sale como NaN.<br>'
            'En radar casi todos los píxeles tienen dato en todas las fechas, '
            'porque no hay nube; lo que sí produce huecos es la máscara de '
            'sombra y solapamiento en terreno quebrado, que es sistemática y '
            'siempre afecta a los mismos píxeles.'))
        self.addParameter(p)

        p = QgsProcessingParameterBoolean(
            self.CONFIRMAR,
            self.tr('Confirmo el gasto de créditos (obligatorio en el modo '
                    'de pedido)'),
            defaultValue=False)
        p.setHelp(self.tr(
            'Enviar trabajos gasta créditos y no se puede deshacer. Esta '
            'casilla existe para que el gasto sea un acto deliberado y no el '
            'efecto de dejar el modo mal puesto.<br>'
            'Ejecute antes el inventario: le dirá la cifra exacta.'))
        self.addParameter(p)

        p = QgsProcessingParameterNumber(
            self.TOPE_CREDITOS, self.tr('Tope de créditos por ejecución'),
            QgsProcessingParameterNumber.Integer,
            defaultValue=3000, minValue=5, maxValue=CREDITOS_LIBRES_MES)
        p.setHelp(self.tr(
            'Se rechaza en el diálogo cualquier pedido que supere este tope, '
            'antes de enviar nada. Protege del error de teclear 10 m sobre un '
            'rango de diez años, que serían decenas de miles de créditos.'))
        self.addParameter(p)

        p = QgsProcessingParameterFile(
            self.MANIFIESTO,
            self.tr('Manifiesto de trabajos (solo para la colecta de datos)'),
            extension='json', optional=True)
        p.setHelp(self.tr(
            'El JSON que escribió el modo de pedido. Lleva los identificadores '
            'de los trabajos, la traza, el año y el espaciamiento, de modo que '
            'la colecta de datos no puede mezclar por accidente lo que se pidió por '
            'separado.<br>'
            'NO contiene el token.'))
        self.addParameter(p)

        p = QgsProcessingParameterFile(
            self.CARPETA, self.tr('Carpeta de salida'),
            behavior=_FILE_FOLDER, optional=True)
        p.setHelp(self.tr(
            'Destino del manifiesto y de los GeoTIFF. Se exige en los modos de '
            'pedido y de colecta de datos.<br>'
            'La amplitud y el recuento de observaciones se escriben con la '
            'traza en el nombre, y con la procedencia completa dentro del '
            'GeoTIFF, legible con gdalinfo.'))
        self.addParameter(p)

        self.addOutput(QgsProcessingOutputFolder(
            self.SALIDA, self.tr('Carpeta de salida')))

    # ------------------------------------------------------------ validación
    def checkParameterValues(self, parameters, context):
        modo = self.parameterAsEnum(parameters, self.MODO, context)
        auth_id = (self.parameterAsString(
            parameters, self.AUTH_EDL, context) or '').strip()
        ruta_clave = (self.parameterAsFile(
            parameters, self.CLAVE_EDL, context) or '').strip()
        carpeta = (self.parameterAsFile(parameters, self.CARPETA, context)
                   or '').strip()
        # SRC de salida: vacio = el nativo del producto. Se guarda en la
        # instancia porque lo usa _escribir, al final de una cadena de
        # llamadas por la que no vale la pena arrastrar un parametro mas.
        crs_pedido = self.parameterAsCrs(parameters, self.SRC_SALIDA, context)
        self._crs_salida = (crs_pedido.authid()
                            if crs_pedido and crs_pedido.isValid() else None)
        traza = self.parameterAsInt(parameters, self.TRAZA, context)
        min_obs = self.parameterAsInt(parameters, self.MIN_OBS, context)

        # 1. Área: sin ella no hay nada que consultar, en ningún modo.
        fuente = self.parameterAsSource(parameters, self.AOI, context)
        ext = self.parameterAsExtent(
            parameters, self.EXTENSION, context,
            QgsCoordinateReferenceSystem('EPSG:4326'))
        if fuente is None and ext.isEmpty():
            return False, self.tr(
                '[!] Indique una «Capa de AOI» o una «Extensión». Sin área no '
                'hay nada que consultar.')

        # 2. Token: imprescindible para hablar con HyP3. Se lee del archivo
        #    aquí, en el diálogo, para fallar antes y con un motivo concreto.
        if modo in (1, 2):
            token, origen = _obtener_clave(auth_id, ruta_clave)
            if not token:
                return False, self.tr(
                    f'[!] Los modos de pedido y de colecta de datos necesitan el '
                    f'token de Earthdata Login: {origen}. Genérelo gratis en '
                    f'urs.earthdata.nasa.gov → Generate Token y guárdelo en '
                    f'una «Configuración de autenticación de QGIS» (más '
                    f'seguro) o en un archivo de texto. El modo de '
                    f'inventario no lo necesita.')
            if len(token) < 40:
                return False, self.tr(
                    f'[!] El token leído de {origen} tiene solo {len(token)} '
                    f'caracteres. Un token de Earthdata tiene varios '
                    f'cientos: probablemente el archivo guarda una parte, o '
                    f'su primera línea no es el token.')
            estado, cuando = _caducidad_clave(token)
            if estado == 'caducado':
                return False, self.tr(
                    f'[!] El token de {origen} {cuando}. Genere uno nuevo en '
                    f'urs.earthdata.nasa.gov → Generate Token y reemplace el '
                    f'contenido del archivo.')

        # 3. Carpeta: el pedido y la colecta de datos escriben a disco.
        if modo in (1, 2) and not carpeta:
            return False, self.tr(
                '[!] Los modos de pedido y de colecta de datos necesitan una «Carpeta '
                'de salida». Elíjala con el botón «…».')
        if carpeta:
            ok, motivo = self._carpeta_utilizable(carpeta)
            if not ok:
                return False, self.tr(f'[!] Carpeta de salida: {motivo}')

        # 4. Meses: las dos estaciones tienen que existir y no solaparse.
        seca = _parsear_meses(
            self.parameterAsString(parameters, self.MESES_SECA, context),
            ())
        lluvia = _parsear_meses(
            self.parameterAsString(parameters, self.MESES_LLUVIA, context),
            ())
        # Se valida en el modo de PEDIDO además del de colecta de datos: el pedido es
        # el que gasta créditos, y unas estaciones mal declaradas producirían
        # un gasto irreversible sobre una configuración que no puede dar
        # resultado. Descubrirlo al colectar sería tarde.
        if modo in (1, 2):
            if not seca or not lluvia:
                return False, self.tr(
                    '[!] La amplitud necesita las dos estaciones. Rellene '
                    '«Meses de estación seca» y «Meses de estación lluviosa» '
                    'con números de mes separados por coma.')
            comunes = sorted(set(seca) & set(lluvia))
            if comunes:
                return False, self.tr(
                    f'[!] Los meses {comunes} figuran en las DOS estaciones. '
                    f'Una diferencia de medianas con meses compartidos resta '
                    f'un dato de sí mismo y acerca la amplitud a cero.')

        # 5. Pedido: traza fija, confirmación y tope de créditos.
        if modo == 1:
            if traza <= 0:
                return False, self.tr(
                    '[!] Para pedir trabajos hay que fijar UNA traza en el '
                    'parámetro «Traza». Ejecute antes el inventario: mezclar '
                    'trazas en una serie temporal mide cambio de geometría de '
                    'observación, no de vegetación.')
            if not self.parameterAsBool(parameters, self.CONFIRMAR, context):
                return False, self.tr(
                    '[!] Marque «Confirmo el gasto de créditos». Enviar '
                    'trabajos a HyP3 consume créditos y no se puede deshacer; '
                    'el inventario le da la cifra exacta sin gastar nada.')
            # Viabilidad aritmética ANTES de gastar. Con menos gránulos que
            # el mínimo de observaciones, la amplitud sale toda NaN por
            # construcción: el gasto estaría perdido de antemano. Los dos
            # recuentos se calculan de verdad, no se estiman.
            n_s, n_l, aviso = self._viabilidad(
                parameters, context, traza, seca, lluvia, min_obs)
            if aviso:
                return False, aviso
        if modo == 2:
            manifiesto = (self.parameterAsFile(
                parameters, self.MANIFIESTO, context) or '').strip()
            if not manifiesto:
                return False, self.tr(
                    '[!] La colecta de datos necesita el «Manifiesto de '
                    'trabajos» que escribió el modo de pedido.')
            if not os.path.isfile(manifiesto):
                return False, self.tr(
                    f'[!] No existe el manifiesto «{manifiesto}».')
        return super().checkParameterValues(parameters, context)

    def _viabilidad(self, parameters, context, traza, seca, lluvia, min_obs):
        """(n_seca, n_lluvia, aviso) del pedido, consultado de verdad.

        Se hace en checkParameterValues, con una consulta de red, porque el
        error que evita cuesta créditos y es irreversible. Si la consulta
        falla NO se bloquea el diálogo: processAlgorithm lo volverá a
        intentar y ahí sí puede abortar con el motivo.
        """
        anio = self.parameterAsInt(parameters, self.ANIO, context)
        orbita = self.parameterAsEnum(parameters, self.ORBITA, context)
        pol_idx = self.parameterAsEnum(parameters, self.POLARIZACION, context)
        rect = self._rect_4326(parameters, context, None)
        if rect is None:
            return 0, 0, None
        bbox = [rect.xMinimum(), rect.yMinimum(),
                rect.xMaximum(), rect.yMaximum()]
        try:
            items = self._buscar_grd(bbox, anio, None,
                                     traza=traza, orbita=orbita)
        except (RuntimeError, ValueError):
            return 0, 0, None

        lote = [par for (_d, tz), sub in self._agrupar(items, orbita).items()
                if tz == traza for par in sub]
        if not lote:
            return 0, 0, self.tr(
                f'[!] La traza {traza} no tiene gránulos sobre el AOI en '
                f'{anio}. Ejecute el modo de inventario con «Traza» = 0 para '
                f'ver las que sí.')

        n_s, n_l = self._reparto(lote, seca, lluvia)
        if n_s < min_obs or n_l < min_obs:
            res_m = RESOLUCION_M[self.parameterAsEnum(
                parameters, self.RESOLUCION, context)]
            perdidos = len(lote) * CREDITOS_RTC[res_m]
            return n_s, n_l, self.tr(
                f'[!] La traza {traza} tiene {n_s} gránulo(s) de seca y '
                f'{n_l} de lluvia en {anio}, y «Mínimo de observaciones por '
                f'píxel» está en {min_obs}. La amplitud saldría ENTERA a NaN '
                f'por construcción, de modo que los {perdidos} créditos se '
                f'perderían. Baje el mínimo a {min(n_s, n_l)} o menos, elija '
                f'otro año, u otra traza.')

        # Polarización: un gránulo de polarización simple («1SSV», «1SSH») no
        # contiene la cruzada. Pedir VH sobre un año de polarización simple
        # gastaría créditos en productos que no sirven para lo pedido.
        quiere = {0: {'VH'}, 1: {'VV'}, 2: {'VH', 'VV'}}[pol_idx]
        con_pol = 0
        for ident, props in lote:
            disponibles = {str(x).upper()
                           for x in (props.get('sar:polarizations') or [])}
            if quiere <= disponibles:
                con_pol += 1
        if con_pol == 0:
            vistas = sorted({'+'.join(p.get('sar:polarizations') or ['?'])
                             for _i, p in lote})
            return n_s, n_l, self.tr(
                f'[!] Ninguno de los {len(lote)} gránulos de la traza '
                f'{traza} en {anio} trae la polarización pedida '
                f'({"+".join(sorted(quiere))}). Los gránulos disponibles son '
                f'{vistas}: un producto de polarización simple no contiene '
                f'la cruzada. Cambie «Polarización» o elija otro año.')
        if con_pol < len(lote):
            # No se bloquea: los que sí la traen pueden bastar. Se avisará
            # en la ejecución, donde el registro queda junto al resultado.
            _LOG.debug(f"[viabilidad] {len(lote) - con_pol} granulo(s) sin "
                       f"la polarizacion pedida")
        return n_s, n_l, None

    def _carpeta_utilizable(self, ruta):
        """(True, '') o (False, motivo). Igual criterio que el óptico."""
        try:
            if os.path.isfile(ruta):
                return False, (
                    f"«{ruta}» es un archivo, no una carpeta. Seleccione un "
                    f"directorio.")
            if not os.path.isdir(ruta):
                try:
                    os.makedirs(ruta, exist_ok=True)
                except OSError as e:
                    return False, f"no se pudo crear «{ruta}»: {e}"
            if not os.access(ruta, os.W_OK):
                return False, (
                    f"sin permisos de escritura en «{ruta}». Elija una "
                    f"carpeta de su perfil de usuario.")
            try:
                temporal = os.path.normcase(
                    os.path.abspath(QgsProcessingUtils.tempFolder()))
                elegida = os.path.normcase(os.path.abspath(ruta))
                if elegida.startswith(temporal):
                    return False, (
                        f"«{ruta}» está dentro de la carpeta temporal de "
                        f"Processing, que QGIS borra al cerrar la sesión. "
                        f"Los productos de HyP3 cuestan créditos: elija una "
                        f"carpeta permanente.")
            except (OSError, ValueError) as e:
                _LOG.debug(f"[temp_check] {e}")
            return True, ''
        except OSError as e:
            return False, f"error al comprobar «{ruta}»: {e}"

    # ------------------------------------------------------------- ejecución
    def processAlgorithm(self, parameters, context, feedback):
        modo = self.parameterAsEnum(parameters, self.MODO, context)
        # El token NUNCA viaja como parámetro de texto: el diálogo de
        # Processing vuelca todos los parámetros de entrada en el registro y
        # en el historial antes de ejecutar. Lo que viaja es un identificador
        # de configuración de autenticación (siete caracteres) o una ruta.
        token, origen_clave = _obtener_clave(
            self.parameterAsString(parameters, self.AUTH_EDL, context),
            self.parameterAsFile(parameters, self.CLAVE_EDL, context))
        if modo in (1, 2):
            if not token:
                raise QgsProcessingException(
                    f'No hay token de Earthdata: {origen_clave}')
            estado, cuando = _caducidad_clave(token)
            feedback.pushInfo(f"Token leído de {origen_clave}; {cuando}")
            if estado == 'por_caducar':
                feedback.pushWarning(
                    f"[!] El token {cuando}. Si el lote tarda más que eso, la "
                    f"colecta de datos fallará con 401 y habrá que renovarlo.")
        carpeta = (self.parameterAsFile(parameters, self.CARPETA, context)
                   or '').strip()
        anio = self.parameterAsInt(parameters, self.ANIO, context)
        traza = self.parameterAsInt(parameters, self.TRAZA, context)
        orbita = self.parameterAsEnum(parameters, self.ORBITA, context)
        res_m = RESOLUCION_M[
            self.parameterAsEnum(parameters, self.RESOLUCION, context)]
        pol_idx = self.parameterAsEnum(parameters, self.POLARIZACION, context)
        self._pendientes = []

        rect = self._rect_4326(parameters, context, feedback)
        if rect is None:
            raise QgsProcessingException(
                'No se pudo determinar el área de interés.')
        bbox = [rect.xMinimum(), rect.yMinimum(),
                rect.xMaximum(), rect.yMaximum()]
        feedback.pushInfo(
            f"AOI: {bbox[0]:.5f}, {bbox[1]:.5f} … {bbox[2]:.5f}, {bbox[3]:.5f}")

        if modo == 0:
            self._inventario(bbox, anio, traza, orbita, res_m, feedback,
                             token=token)
            return {self.SALIDA: carpeta or ''}

        if modo == 1:
            return self._pedir(bbox, anio, traza, orbita, res_m, pol_idx,
                               token, carpeta, parameters, context, feedback)

        return self._recoger(bbox, pol_idx, token, carpeta, parameters,
                             context, feedback)

    # --------------------------------------------------------- modo 0: ver
    def _buscar_grd(self, bbox, anio, feedback, traza=0, orbita=0):
        """Gránulos GRD-HD de ASF sobre el AOI. Devuelve [(nombre, props)].

        Se consulta ASF y no un catálogo STAC porque el nombre que entrega es
        el mismo que HyP3 acepta: buscar donde se va a pedir es lo que impide
        que el inventario y el pedido discrepen (ver la nota de API_ASF).
        """
        # Sin filtro «platform» a propósito. Se comprobó contra la API que
        # «SENTINEL-1», la lista explícita «SENTINEL-1A,SENTINEL-1C» y la
        # ausencia del parámetro devuelven exactamente lo mismo, porque
        # GRD_HD + IW ya es vocabulario propio de Sentinel-1. Omitirlo evita
        # depender de que el alias del grupo se mantenga al día cuando entre
        # en servicio un satélite nuevo: un alias rezagado dejaría fuera sus
        # escenas en silencio, que es justo lo que hay que evitar en un
        # inventario del que dependen créditos.
        consulta = {
            'processingLevel': 'GRD_HD',
            'beamMode': 'IW',
            'start': f'{anio}-01-01T00:00:00Z',
            'end': f'{anio}-12-31T23:59:59Z',
            'intersectsWith': _wkt_bbox(bbox),
            'output': 'jsonlite',
            'maxResults': TOPE_RESULTADOS,
        }
        if traza:
            consulta['relativeOrbit'] = int(traza)
        if orbita:
            consulta['flightDirection'] = ('ASCENDING', 'DESCENDING')[
                int(orbita) - 1]
        url = f"{API_ASF}?{urllib.parse.urlencode(consulta)}"
        respuesta = _peticion(url)
        registros = respuesta.get('results')
        if registros is None and isinstance(respuesta, list):
            registros = respuesta
        registros = registros or []
        if len(registros) >= TOPE_RESULTADOS and feedback is not None:
            feedback.pushWarning(
                f"[!] ASF devolvió {len(registros)} resultados, que es el "
                f"tope pedido: la lista puede estar truncada y el inventario "
                f"quedar corto. Acote el AOI o el año.")
        items = []
        for registro in registros:
            nombre, props = _normalizar_asf(registro)
            if nombre:
                items.append((nombre, props))
        return items

    @staticmethod
    def _agrupar(items, orbita):
        """{(direccion, traza): [(id, props)]}, filtrado por dirección."""
        quiere = (None, 'ascending', 'descending')[orbita]
        grupos = defaultdict(list)
        for ident, props in items:
            direccion = str(props.get('sat:orbit_state') or '?').lower()
            if quiere and direccion != quiere:
                continue
            grupos[(direccion,
                    props.get('sat:relative_orbit', '?'))].append(
                        (ident, props))
        return grupos

    def _inventario(self, bbox, anio, traza, orbita, res_m, feedback,
                    token=None):
        # token=None y no token='': Bandit marca B107 por el NOMBRE del
        # parámetro en cuanto el valor por omisión es una cadena, y el
        # escáner del portal reporta también las de severidad baja.
        feedback.pushInfo(
            f"\nInventario Sentinel-1 GRD-HD · {anio} · archivo de ASF")
        feedback.pushInfo(
            "Un gránulo = un trabajo RTC en HyP3. No se consume ningún "
            "crédito al inventariar.\n")

        items = self._buscar_grd(bbox, anio, feedback,
                                 traza=traza, orbita=orbita)
        if not items:
            feedback.pushWarning(
                f"[!] Sin gránulos Sentinel-1 sobre el AOI en {anio}. "
                f"Compruebe el año: el archivo empieza en octubre de 2014 y "
                f"los primeros años son escasos en Centroamérica.")
            return

        faltan = self._claves_ausentes(items)
        if faltan:
            feedback.pushWarning(
                f"[!] ASF no devolvió {', '.join(faltan)} en ningún "
                f"resultado. Las columnas correspondientes saldrán como «?» "
                f"y el desglose por traza no es fiable; puede que la API haya "
                f"cambiado de nombres de campo.")

        grupos = self._agrupar(items, orbita)
        if not grupos:
            feedback.pushWarning(
                f"[!] Hay {len(items)} gránulo(s), pero ninguno con la "
                f"dirección de órbita pedida ({ORBITAS[orbita]}).")
            return

        feedback.pushInfo(
            f"{'dirección':<12}{'traza':>7}{'gránulos':>10}{'seca':>7}"
            f"{'lluv':>7}{'créditos':>10}   polarizaciones")
        feedback.pushInfo('-' * 78)
        aptas = []
        for (direccion, tz), lote in sorted(
                grupos.items(), key=lambda kv: -len(kv[1])):
            if traza and tz != traza:
                continue
            seca, lluvia = self._reparto(lote)
            cred = len(lote) * CREDITOS_RTC[res_m]
            pols = sorted({'+'.join(p.get('sar:polarizations') or ['?'])
                           for _i, p in lote})
            feedback.pushInfo(
                f"{direccion:<12}{str(tz):>7}{len(lote):>10}{seca:>7}"
                f"{lluvia:>7}{cred:>10}   {', '.join(pols)}")
            aptas.append({'direccion': direccion, 'traza': tz,
                          'n': len(lote), 'seca': seca, 'lluvia': lluvia,
                          'creditos': cred})
        feedback.pushInfo('-' * 78)

        if not aptas:
            feedback.pushWarning(
                f"[!] La traza {traza} no aparece sobre el AOI en {anio}. "
                f"Ponga «Traza» en 0 para ver las que sí.")
            return

        aptas, empatadas = ordenar_trazas(aptas)

        # El SALDO, no la asignación. Darlos por lo mismo hacía que el
        # inventario recomendara un pedido de 1860 créditos a quien le
        # quedaban 1630: lo que se gasta sale del saldo, y la asignación
        # mensual solo dice cuánto se repone. Sin token no se puede
        # consultar, y entonces se dice que es una referencia y no un
        # saldo, en vez de dar a entender que hay 8000 disponibles.
        saldo, por_mes = (_saldo_creditos(token, feedback) if token
                          else (None, None))
        presupuesto = saldo
        if saldo is not None:
            feedback.pushInfo(
                f"\nSaldo en HyP3: {saldo:.0f} créditos"
                + (f" de {por_mes:.0f} al mes." if por_mes else "."))
        else:
            feedback.pushInfo(
                f"\nAsignación gratuita de HyP3 Basic: "
                f"{CREDITOS_LIBRES_MES} créditos por mes. Es la asignación, "
                f"NO su saldo: indique la configuración de autenticación "
                f"para que se consulte cuánto le queda de verdad antes de "
                f"elegir espaciamiento.")
        feedback.pushInfo(
            "\nOrdenadas por la estación MÁS DÉBIL, que es la que limita una "
            "diferencia de medianas:")
        for c in aptas[:6]:
            peor = min(c['seca'], c['lluvia'])
            feedback.pushInfo(
                f"  traza {c['traza']} {c['direccion']:<11} {c['n']:>3} "
                f"gránulos  {c['seca']:>2} seca / {c['lluvia']:>2} lluvia  → "
                f"{c['creditos']} créditos a {res_m} m  (estación más débil: "
                f"{peor})")

        mejor = aptas[0]

        # Si no alcanza, decirlo AQUÍ y decir para qué sí alcanza. El
        # precio por escena no es lineal —5, 15 y 60 créditos a 30, 20 y
        # 10 m— así que la alternativa no es evidente de cabeza, y el
        # usuario se enteraba al recibir un HTTP 400 del servidor después
        # de elegir traza y marcar la confirmación de gasto.
        if presupuesto is not None and mejor['creditos'] > presupuesto:
            opciones = opciones_asequibles(mejor['n'], presupuesto)
            if opciones:
                alternativas = '; '.join(f'{esp} m = {cst} créditos'
                                         for esp, cst in opciones)
                feedback.pushWarning(
                    f"[!] A {res_m} m NO le alcanza: {mejor['creditos']} "
                    f"créditos contra {presupuesto:.0f} de saldo. Los mismos "
                    f"{mejor['n']} gránulos sí caben a: {alternativas}. "
                    f"Cambie «Espaciamiento de píxel» antes de pedir.")
            else:
                feedback.pushWarning(
                    f"[!] Con {presupuesto:.0f} créditos de saldo no caben "
                    f"los {mejor['n']} gránulos ni al espaciamiento más "
                    f"barato ({mejor['n'] * CREDITOS_RTC[30]} créditos a "
                    f"30 m). Espere la asignación del mes que viene o acorte "
                    f"el periodo.")

        # El empate se dice. Entre trazas que ofrecen lo mismo, el orden de
        # la lista no las distingue, y la primera se presenta como
        # «siguiente paso»: callarlo hace pasar por recomendación lo que es
        # un desempate mecánico, y aquí se gastan créditos de verdad.
        if len(empatadas) > 1:
            lista = ', '.join(f"traza {a['traza']} {a['direccion']}"
                              for a in empatadas)
            juntas = sum(a['creditos'] for a in empatadas)
            feedback.pushInfo(
                f"\nEMPATE entre {len(empatadas)}: {lista}. Mismo número de "
                f"gránulos, mismo reparto estacional y mismo coste, así que "
                f"el orden de la lista NO las distingue.")
            feedback.pushInfo(
                "  Decídalo por geometría de mirada, que el inventario no "
                "puede juzgar. Sentinel-1 mira a la derecha del sentido de "
                "vuelo: una traza ascendente ilumina las laderas orientadas "
                "al oeste, y una descendente las orientadas al este. "
                "Prefiera la que ilumine la orientación dominante de su "
                "área. La corrección radiométrica del terreno atenúa el "
                "efecto, pero no recupera un píxel en sombra ni en solape. "
                "En terreno llano da igual.")
            # Solo se ofrece pedir varias si de verdad caben en el SALDO.
            # Con la asignación mensual se proponía un gasto que el
            # servidor iba a rechazar.
            if presupuesto is not None and juntas <= presupuesto:
                feedback.pushInfo(
                    f"  Las {len(empatadas)} juntas caben en su saldo: "
                    f"{juntas} de {presupuesto:.0f} créditos. Si la "
                    f"orientación dominante no está clara, pedir las dos y "
                    f"comparar cuesta menos que acertar por sorteo.")
            feedback.pushInfo(
                f"\nSiguiente paso: elija una de las empatadas, póngala en "
                f"«Traza» y «Dirección de órbita», marque la confirmación de "
                f"gasto y ejecute el modo de pedido. Cualquiera costaría "
                f"{mejor['creditos']} créditos.")
        else:
            feedback.pushInfo(
                f"\nSiguiente paso: ponga «Traza» = {mejor['traza']}, "
                f"«Dirección de órbita» = {mejor['direccion']}, marque la "
                f"confirmación de gasto y ejecute el modo de pedido. "
                f"Costaría {mejor['creditos']} créditos.")
        peor_mejor = min(mejor['seca'], mejor['lluvia'])
        if peor_mejor < 3:
            feedback.pushWarning(
                f"[!] Incluso la mejor traza llega solo a {peor_mejor} "
                f"adquisición(es) en su estación más débil. Una mediana "
                f"estacional sobre tan poco no es defendible: pruebe otro año "
                f"antes de gastar créditos.")

    @staticmethod
    def _claves_ausentes(items):
        """Propiedades esperadas que NINGÚN ítem trae."""
        vistas = set()
        for _i, props in items:
            vistas.update(props.keys())
        return [c for c in ('sat:orbit_state', 'sat:relative_orbit',
                            'sar:polarizations')
                if c not in vistas]

    def _reparto(self, lote, seca_meses=None, lluvia_meses=None):
        """(n_seca, n_lluvia) del lote según los meses de cada estación.

        El respaldo se decide con `is None` y NO con `or`: una tupla vacía es
        falsa, así que `or` convertiría «ninguna estación seca» —que es una
        petición explícita y debe contar 0— en el régimen por omisión, y
        devolvería un recuento que nadie pidió.
        """
        if seca_meses is None:
            seca_meses = (12, 1, 2, 3, 4)
        if lluvia_meses is None:
            lluvia_meses = (5, 6, 7, 8, 9, 10, 11)
        n_s = n_l = 0
        for ident, props in lote:
            mes = _mes_de(props, ident)
            if mes is None:
                continue
            if mes in seca_meses:
                n_s += 1
            elif mes in lluvia_meses:
                n_l += 1
        return n_s, n_l

    # ------------------------------------------------------ modo 1: pedir
    def _pedir(self, bbox, anio, traza, orbita, res_m, pol_idx, token,
               carpeta, parameters, context, feedback):
        seca = _parsear_meses(
            self.parameterAsString(parameters, self.MESES_SECA, context),
            (12, 1, 2, 3, 4))
        lluvia = _parsear_meses(
            self.parameterAsString(parameters, self.MESES_LLUVIA, context),
            (5, 6, 7, 8, 9, 10, 11))
        tope = self.parameterAsInt(parameters, self.TOPE_CREDITOS, context)
        speckle = self.parameterAsBool(
            parameters, self.FILTRO_SPECKLE, context)

        feedback.pushInfo(f"\nBuscando gránulos de {anio} en ASF…")
        items = self._buscar_grd(bbox, anio, feedback,
                                 traza=traza, orbita=orbita)
        grupos = self._agrupar(items, orbita)
        lote = []
        for (direccion, tz), sub in grupos.items():
            if tz == traza:
                lote.extend(sub)
                direccion_elegida = direccion
        if not lote:
            raise QgsProcessingException(
                f'La traza {traza} no tiene gránulos sobre el AOI en {anio}. '
                f'Ejecute el modo de inventario con «Traza» = 0.')

        # Solo los gránulos que caen en una de las dos estaciones y que traen
        # la polarización pedida: lo demás sería gastar créditos en productos
        # que la amplitud no puede usar.
        quiere = {0: {'VH'}, 1: {'VV'}, 2: {'VH', 'VV'}}[pol_idx]
        utiles, huerfanos, sin_pol, sin_nombre = [], [], [], []
        for ident, props in sorted(lote):
            mes = _mes_de(props, ident)
            if mes not in seca and mes not in lluvia:
                huerfanos.append((ident, mes))
                continue
            disponibles = {str(x).upper()
                           for x in (props.get('sar:polarizations') or [])}
            if disponibles and not quiere <= disponibles:
                sin_pol.append((ident, sorted(disponibles)))
                continue
            nombre, completo = _nombre_granulo(ident, props)
            if not completo:
                sin_nombre.append(nombre)
            utiles.append((nombre, props))
        if sin_pol:
            feedback.pushWarning(
                f"[!] {len(sin_pol)} gránulo(s) sin la polarización pedida "
                f"({'+'.join(sorted(quiere))}); no se piden. Un producto de "
                f"polarización simple no contiene la cruzada. Ejemplo: "
                f"{sin_pol[0][0][:38]}… trae {sin_pol[0][1]}.")
        if sin_nombre:
            feedback.pushWarning(
                f"[!] {len(sin_nombre)} gránulo(s) sin «s1:product_identifier» "
                f"en el catálogo: se usa el id del ítem, al que le falta el "
                f"identificador único de 4 hex del final. HyP3 los rechazará. "
                f"Ejemplo: {sin_nombre[0]}")
        if huerfanos:
            feedback.pushInfo(
                f"  {len(huerfanos)} gránulo(s) en meses sin estación "
                f"asignada: no se piden (ahorro de "
                f"{len(huerfanos) * CREDITOS_RTC[res_m]} créditos).")
        if not utiles:
            raise QgsProcessingException(
                'Ningún gránulo de esa traza cae en los meses declarados. '
                'Revise «Meses de estación seca» y «lluviosa».')

        n_s, n_l = self._reparto(utiles, seca, lluvia)
        costo = len(utiles) * CREDITOS_RTC[res_m]
        feedback.pushInfo(
            f"  traza {traza} {direccion_elegida}: {len(utiles)} gránulos "
            f"({n_s} seca, {n_l} lluvia) a {res_m} m = {costo} créditos")
        if costo > tope:
            raise QgsProcessingException(
                f'El pedido costaría {costo} créditos, por encima del tope de '
                f'{tope} que usted fijó. Suba «Tope de créditos por '
                f'ejecución» si es lo que quiere, o baje el espaciamiento de '
                f'píxel (30 m cuesta doce veces menos que 10 m).')

        # El saldo, antes de enviar. Es la comprobación que faltaba: el
        # tope de arriba es el límite que fija el usuario, no lo que le
        # queda en la cuenta, y HyP3 rechazaba el lote entero con un 400
        # cuando no alcanzaba.
        saldo, por_mes = _saldo_creditos(token, feedback)
        if saldo is not None:
            referencia = (f' de {por_mes:.0f} al mes' if por_mes
                          else f' (asignación: {CREDITOS_LIBRES_MES}/mes)')
            feedback.pushInfo(
                f"  Saldo en HyP3: {saldo:.0f} créditos{referencia}")
        if saldo is not None and costo > saldo:
            opciones = opciones_asequibles(len(utiles), saldo)
            if opciones:
                alternativas = '; '.join(
                    f'{esp} m = {cst} créditos' for esp, cst in opciones)
                salida = (f'Con ese saldo sí caben los mismos '
                          f'{len(utiles)} gránulos a: {alternativas}.')
            else:
                salida = (f'Con ese saldo no caben los {len(utiles)} '
                          f'gránulos ni al espaciamiento más barato '
                          f'({len(utiles) * CREDITOS_RTC[30]} créditos a '
                          f'30 m). Espere la asignación del mes que viene o '
                          f'reduzca el periodo.')
            raise QgsProcessingException(
                f'El pedido costaría {costo} créditos y en HyP3 le quedan '
                f'{saldo:.0f}: faltan {costo - saldo:.0f}. No se envió nada. '
                f'{salida}')
        if not n_s or not n_l:
            raise QgsProcessingException(
                f'La traza {traza} tiene {n_s} gránulo(s) de seca y {n_l} de '
                f'lluvia en {anio}: sin las dos estaciones no hay amplitud '
                f'que calcular. Elija otra traza u otro año.')
        if len(utiles) > MAX_TRABAJOS_POR_PETICION:
            feedback.pushInfo(
                f"  Se enviarán en tandas de {MAX_TRABAJOS_POR_PETICION}, "
                f"que es el máximo por petición.")

        etiqueta = f"rtc_{anio}_T{traza}_{res_m}m_{uuid.uuid4().hex[:6]}"
        # El primer gránulo, a la vista, antes de gastar: si el nombre está
        # mal el lote entero se rechaza, y verlo aquí cuesta una línea.
        feedback.pushInfo(f"  primer gránulo: {utiles[0][0]}")
        feedback.pushInfo(
            f"  parámetros: resolution={float(res_m)} "
            f"radiometry={RADIOMETRIA} scale={ESCALA} "
            f"speckle_filter={bool(speckle)}")
        enviados, fallidos = [], []
        for inicio in range(0, len(utiles), MAX_TRABAJOS_POR_PETICION):
            if feedback.isCanceled():
                break
            tanda = utiles[inicio:inicio + MAX_TRABAJOS_POR_PETICION]
            cuerpo = {'jobs': [{
                'name': etiqueta,
                'job_type': 'RTC_GAMMA',
                'job_parameters': {
                    # float y no int: el esquema declara «number» con enum
                    # [30.0, 20.0, 10.0], y un validador estricto puede
                    # distinguir 10 de 10.0.
                    'granules': [ident],
                    'resolution': float(res_m),
                    'radiometry': RADIOMETRIA,
                    'scale': ESCALA,
                    'speckle_filter': bool(speckle),
                },
            } for ident, _p in tanda]}
            try:
                respuesta = _peticion(
                    API_HYP3.rstrip('/') + '/jobs', payload=cuerpo,
                    token=token, espera=TIEMPO_ESPERA_ENVIO, reintentos=0)
            except RuntimeError as e:
                # Sin reintento: un POST que puede haber llegado no se repite,
                # porque repetirlo gastaría los créditos dos veces.
                fallidos.append(str(e))
                feedback.pushWarning(
                    f"[!] Falló el envío de una tanda de {len(tanda)} "
                    f"gránulos: {e}")
                feedback.pushWarning(
                    "    NO se reintenta: si la petición llegó al servidor, "
                    "repetirla gastaría los créditos dos veces. Compruebe en "
                    "search.asf.alaska.edu qué trabajos existen antes de "
                    "volver a enviar.")
                continue
            for trabajo in respuesta.get('jobs') or []:
                enviados.append({
                    'job_id': trabajo.get('job_id'),
                    'granule': (trabajo.get('job_parameters') or {}).get(
                        'granules', [None])[0],
                    'status_code': trabajo.get('status_code'),
                    'expiration_time': trabajo.get('expiration_time'),
                })
            feedback.setProgress(
                int(100.0 * (inicio + len(tanda)) / len(utiles)))

        if not enviados:
            raise QgsProcessingException(
                'No se envió ningún trabajo. '
                + (fallidos[0] if fallidos else ''))

        manifiesto = {
            'generado_por': f'buscar_sentinel2_cr {self.VERSION}',
            'autor': f'{AUTOR} <{AUTOR_EMAIL}>',
            'creado': datetime.now(timezone.utc).strftime(
                '%Y-%m-%dT%H:%M:%SZ'),
            'nombre_lote': etiqueta,
            'anio': anio,
            'traza': traza,
            'direccion': direccion_elegida,
            'resolucion_m': res_m,
            'radiometria': RADIOMETRIA,
            'escala': ESCALA,
            'filtro_speckle': bool(speckle),
            'polarizacion': POLARIZACIONES[pol_idx],
            'meses_seca': list(seca),
            'meses_lluvia': list(lluvia),
            'bbox_4326': list(bbox),
            'creditos_estimados': costo,
            'trabajos': enviados,
        }
        ruta = os.path.join(carpeta, f"{etiqueta}_manifiesto.json")
        try:
            with open(ruta, 'w', encoding='utf-8') as fh:
                json.dump(manifiesto, fh, ensure_ascii=False, indent=2)
        except OSError as e:
            # Los créditos ya se gastaron: perder el manifiesto es perderlos,
            # así que el identificador del lote se deja en el registro para
            # poder recuperar los trabajos por nombre desde la API.
            raise QgsProcessingException(
                f'Los trabajos SE ENVIARON ({len(enviados)}), pero no se pudo '
                f'escribir el manifiesto en «{ruta}»: {e}. Anote el nombre '
                f'del lote «{etiqueta}»: con él se pueden recuperar los '
                f'trabajos desde HyP3 sin volver a gastar créditos.') from e

        # La ruta COMPLETA, no el nombre: el paso siguiente consiste en
        # señalar este archivo en «Colecta de datos», y con solo el nombre hay que
        # adivinar en qué carpeta quedó. El mensaje de error de más arriba
        # ya daba la ruta entera, de modo que fallar informaba mejor que
        # salir bien.
        feedback.pushInfo(
            f"\n{len(enviados)} trabajo(s) enviado(s). Manifiesto → {ruta}")
        feedback.pushInfo(
            "Los productos tardan de minutos a horas según la cola. Vuelva "
            "con el modo «Colecta de datos» e indique este manifiesto; puede "
            "ejecutarlo tantas veces como quiera.")

        # El saldo DESPUÉS de gastar, consultado y no calculado: es el
        # número con el que se decide si cabe otro lote, y restar a mano
        # daría por supuesto que se cobró exactamente lo estimado.
        saldo_final, _pm = _saldo_creditos(token, feedback)
        if saldo_final is not None:
            feedback.pushInfo(
                f"Saldo en HyP3 tras el pedido: {saldo_final:.0f} créditos.")
        if fallidos:
            feedback.pushWarning(
                f"[!] {len(fallidos)} tanda(s) fallaron y no están en el "
                f"manifiesto.")
        return {self.SALIDA: carpeta}

    # -------------------------------------- modo 2: colecta de datos
    def _recoger(self, bbox, pol_idx, token, carpeta, parameters, context,
                 feedback):
        ruta_man = (self.parameterAsFile(
            parameters, self.MANIFIESTO, context) or '').strip()
        min_obs = self.parameterAsInt(parameters, self.MIN_OBS, context)
        try:
            with open(ruta_man, encoding='utf-8') as fh:
                man = json.load(fh)
        except (OSError, ValueError) as e:
            raise QgsProcessingException(
                f'No se pudo leer el manifiesto «{ruta_man}»: {e}') from e

        lote = str(man.get('nombre_lote') or '')
        traza = man.get('traza', '?')
        direccion = str(man.get('direccion') or '?')
        anio = man.get('anio', '?')
        res_m = man.get('resolucion_m', '?')
        seca = tuple(man.get('meses_seca') or (12, 1, 2, 3, 4))
        lluvia = tuple(man.get('meses_lluvia') or (5, 6, 7, 8, 9, 10, 11))
        feedback.pushInfo(
            f"\nLote {lote} · {anio} · traza {traza} {direccion} · {res_m} m")

        # Cómo se procesaron ESTOS productos, leído del manifiesto. Lo que
        # QGIS lista arriba son los parámetros del diálogo, y en esta modo
        # varios no se usan: el filtro de speckle, la radiometría y la
        # escala los fijó el pedido y viajan dentro del dato. Sin decirlo,
        # el registro muestra «FILTRO_SPECKLE: False» encima de unos
        # productos filtrados, y quien lo lea concluye lo contrario de lo
        # que tiene. El complemento ya evita esa clase de desajuste en las
        # huellas; aquí faltaba.
        filtro_man = man.get('filtro_speckle')
        feedback.pushInfo(
            f"Procesado del lote: radiometría "
            f"{man.get('radiometria', '?')} · escala "
            f"{man.get('escala', '?')} · filtro de speckle "
            f"{'sí' if filtro_man else 'no'}")
        pedido_ahora = self.parameterAsBool(
            parameters, self.FILTRO_SPECKLE, context)
        if filtro_man is not None and bool(pedido_ahora) != bool(filtro_man):
            feedback.pushWarning(
                f"[!] «Filtro de speckle» está en {pedido_ahora} en el "
                f"diálogo, pero este lote se pidió con {bool(filtro_man)} y "
                f"eso ya está en el dato. En la colecta de datos ese "
                f"parámetro no hace nada: para cambiarlo hay que volver a "
                f"pedir los productos.")

        # El recorte usa el AOI que se PIDIÓ, guardado en el manifiesto, y no
        # la extensión que haya en el diálogo ahora. Dos razones: la cologida
        # se repite varias veces hasta que terminan todos los trabajos, y debe
        # dar el mismo encuadre cada vez; y los recortes de un lote comparten
        # nombre, así que colectar dos veces con extensiones distintas dejaría
        # los primeros archivos en su sitio —se dan por descargados— y la
        # serie mezclaría dos encuadres sin avisar.
        bbox_man = man.get('bbox_4326')
        if (isinstance(bbox_man, (list, tuple)) and len(bbox_man) == 4):
            try:
                nuevo = [float(v) for v in bbox_man]
            except (TypeError, ValueError):
                nuevo = None
            if nuevo:
                if max(abs(a - b) for a, b in zip(nuevo, bbox)) > 1e-6:
                    feedback.pushInfo(
                        "  La extensión del diálogo difiere de la del "
                        "manifiesto; se usa la del manifiesto, que es el área "
                        "que se pidió. Para otro encuadre, recorte después.")
                bbox = nuevo
        else:
            feedback.pushWarning(
                "[!] El manifiesto no guarda el AOI (lo escribió una versión "
                "anterior del complemento): se usa la extensión del diálogo. "
                "Manténgala igual entre colectas del mismo lote.")
        feedback.pushInfo(
            f"  AOI de recorte: {bbox[0]:.5f}, {bbox[1]:.5f} … "
            f"{bbox[2]:.5f}, {bbox[3]:.5f}")

        if str(man.get('escala') or '') != ESCALA:
            feedback.pushWarning(
                f"[!] El manifiesto declara escala «{man.get('escala')}» y "
                f"este algoritmo supone «{ESCALA}». Los cocientes y los "
                f"promedios saldrían mal; revise con qué versión se pidió.")

        feedback.pushInfo("Consultando el estado en HyP3…")
        try:
            respuesta = _peticion(
                API_HYP3.rstrip('/') + '/jobs?'
                + urllib.parse.urlencode({'name': lote}),
                token=token)
        except RuntimeError as e:
            raise QgsProcessingException(
                f'No se pudo consultar el estado de los trabajos: {e}') from e

        por_estado = defaultdict(list)
        for trabajo in respuesta.get('jobs') or []:
            por_estado[str(trabajo.get('status_code') or '?')].append(trabajo)
        for estado, lst in sorted(por_estado.items()):
            feedback.pushInfo(f"  {estado:<12} {len(lst)}")

        listos = por_estado.get('SUCCEEDED') or []
        if not listos:
            pendientes = sum(len(v) for k, v in por_estado.items()
                             if k in ('PENDING', 'RUNNING'))
            feedback.pushWarning(
                f"[!] Ningún producto está listo todavía "
                f"({pendientes} en cola o en proceso). Vuelva a ejecutar este "
                f"mismo modo más tarde; no se gastan créditos al consultar.")
            return {self.SALIDA: carpeta}

        vencen = [t.get('expiration_time') for t in listos
                  if t.get('expiration_time')]
        if vencen:
            feedback.pushInfo(
                f"  Vencimiento más próximo declarado por HyP3: "
                f"{min(vencen)}. Después de esa fecha los productos dejan de "
                f"estar disponibles y habría que volver a pedirlos.")

        pols = {0: ('VH',), 1: ('VV',), 2: ('VH', 'VV')}[pol_idx]
        # La marca identifica el LOTE en el nombre de cada recorte: traza,
        # dirección y espaciamiento. Dos lotes en la misma carpeta dejan de
        # poder pisarse, que es lo que permite colectar varias trazas (o la
        # misma a dos resoluciones) sin separar carpetas a mano.
        letra = {'ascending': 'A', 'descending': 'D'}.get(direccion, 'X')
        marca = f"S1_T{traza}{letra}_{res_m}m"
        feedback.pushInfo(f"  recortes rotulados «{marca}_…»")
        # Aviso de volumen: HyP3 no publica las bandas sueltas, solo el ZIP
        # del producto entero. Se lee dentro del ZIP remoto y se recorta sin
        # guardarlo, pero un miembro comprimido no admite acceso aleatorio:
        # GDAL descomprime desde el principio del miembro, de modo que la
        # transferencia se parece al tamaño de la banda aunque el AOI sea
        # diminuto. Conviene decirlo antes, no a los veinte minutos.
        tam = 0
        for t in listos:
            for a in (t.get('files') or []):
                tam += int(a.get('size') or 0)
        if tam:
            # La comparación solo se escribe cuando hay algo con que
            # comparar: con res_m = 10 la frase anterior decía «a 10 m es
            # llevadero; a 10 m sería unas nueve veces más», que no
            # significa nada. Y a 10 m el aviso que hace falta es el
            # contrario.
            if int(res_m) <= 10:
                comparacion = ('Es el espaciamiento más fino, y el que más '
                               'transfiere: a 30 m serían unas nueve veces '
                               'menos.')
            else:
                comparacion = (f'A {res_m} m es llevadero; a 10 m sería '
                               f'unas nueve veces más.')
            feedback.pushInfo(
                f"  Los productos suman {tam / 1e9:.1f} GB. HyP3 solo "
                f"publica el ZIP completo, así que aunque se recorte sin "
                f"descargarlo entero, la transferencia es de ese orden. "
                f"{comparacion}")
        recortes = self._descargar_recortes(
            listos, pols, bbox, carpeta, feedback, marca=marca)
        if not recortes:
            feedback.pushWarning(
                "[!] No se pudo recortar ningún producto; no hay nada que "
                "promediar.")
            return {self.SALIDA: carpeta}

        for sufijo in sorted(recortes):
            if feedback.isCanceled():
                break
            self._amplitud(sufijo, recortes[sufijo], seca, lluvia, min_obs,
                           man, carpeta, context, feedback)

        if len(pols) == 2 and 'VH' in recortes and 'VV' in recortes:
            self._amplitud_rvi(recortes, seca, lluvia, min_obs, man, carpeta,
                               context, feedback)
        # El informe es un extra, y va al FINAL de una colecta que puede
        # haber tardado media hora. Si fallara, los rásteres ya están
        # escritos pero la ejecución se daría por fallida y las capas no
        # llegarían a cargarse: se perdería el producto por un accesorio.
        # De ahí que se capture todo y no solo lo previsible.
        try:
            self._informe_auditoria(man, lote, min_obs, carpeta, recortes,
                                    seca, lluvia, feedback)
        except Exception as e:                               # noqa: BLE001
            feedback.pushWarning(
                f'[!] No se pudo generar el informe de auditoría ({e}). '
                f'Los rásteres y sus metadatos están escritos y no les '
                f'afecta: el informe los resume, no los produce.')
        return {self.SALIDA: carpeta}

    def _descargar_recortes(self, trabajos, pols, bbox, carpeta, feedback,
                            marca='S1'):
        """{pol: [(ruta, mes, fecha)]} recortando cada COG al AOI.

        Los productos de HyP3 son COG con URL de S3 prefirmada que NO exige
        autenticación, así que se leen por /vsicurl/ y se recorta solo el AOI
        en lugar de bajar la escena completa.

        `marca` lleva la traza, la dirección y el espaciamiento, y va en el
        nombre de cada recorte. No es cosmético: sin ella dos lotes distintos
        recogidos en la misma carpeta comparten nombre, y como un archivo que
        ya existe se da por descargado, el segundo lote REUTILIZA los
        recortes del primero. Entre dos trazas eso significa calcular una
        amplitud con píxeles de la otra órbita —el error que todo este
        algoritmo intenta evitar— y el archivo de salida seguiría rotulado
        con la traza que se pidió. Entre dos resoluciones significa que un
        lote a 10 m se queda con los recortes de 30 m.
        """
        # La lista blanca de /vsicurl/, antes de nada. Si no permite .zip,
        # GDAL no abre NINGÚN producto —ni para listarlo ni para
        # recortarlo— y lo hace en silencio, sin petición ni error.
        clave_ext = 'CPL_VSIL_CURL_ALLOWED_EXTENSIONS'
        previo_ext = gdal.GetConfigOption(clave_ext, None)
        nueva_ext = lista_con_zip(previo_ext)
        if nueva_ext:
            gdal.SetConfigOption(clave_ext, nueva_ext)
            feedback.pushInfo(
                f"  {clave_ext} estaba en «{previo_ext}», que no permite "
                f"abrir .zip por /vsicurl/. Se añade .zip mientras dure la "
                f"colecta de datos y se deja como estaba al terminar.")
            self._avisos_informe.append(
                f'{clave_ext} estaba en «{previo_ext}» y no permite abrir '
                f'.zip por /vsicurl/. Se añadió .zip durante la colecta de datos y '
                f'se restauró al terminar; sin eso GDAL no abre ningún '
                f'producto, en silencio.')
        try:
            return self._descargar_recortes_int(
                trabajos, pols, bbox, carpeta, feedback, marca)
        finally:
            if nueva_ext:
                gdal.SetConfigOption(clave_ext, previo_ext)

    def _descargar_recortes_int(self, trabajos, pols, bbox, carpeta,
                                feedback, marca):
        """El cuerpo de _descargar_recortes, ya con .zip permitido."""
        salida = defaultdict(list)
        total = max(1, len(trabajos) * len(pols))
        hecho = 0
        for trabajo in trabajos:
            if feedback.isCanceled():
                break
            granulo = ''
            params = trabajo.get('job_parameters') or {}
            if params.get('granules'):
                granulo = str(params['granules'][0] or '')
            fecha = _fecha_de({}, granulo)
            mes = _mes_de({}, granulo)
            if mes is None:
                feedback.pushWarning(
                    f"[!] No se pudo leer la fecha de «{granulo[:40]}»; se "
                    f"omite porque sin mes no se le puede asignar estación.")
                continue
            archivos = trabajo.get('files') or []
            for pol in pols:
                hecho += 1
                feedback.setProgress(int(100.0 * hecho / total))
                sello = _sello_granulo(granulo) or fecha.replace('-', '')
                destino = os.path.join(
                    carpeta, f"{marca}_{sello}_{pol}.tif")
                # Se mira el disco ANTES de abrir el ZIP remoto: leer el
                # índice cuesta una petición y no vale la pena para algo que
                # ya está recortado.
                if os.path.exists(destino):
                    salida[pol].append((destino, mes, fecha))
                    continue
                fuente, motivo = self._ruta_banda(archivos, pol, feedback)
                if not fuente:
                    feedback.pushWarning(
                        f"[!] {granulo[:32]}… {pol}: {motivo} Se omite esta "
                        f"fecha.")
                    continue
                ok = self._recortar(fuente, destino, bbox, feedback)
                if ok:
                    salida[pol].append((destino, mes, fecha))
        for pol, lst in salida.items():
            feedback.pushInfo(f"  {pol}: {len(lst)} recorte(s) disponibles")
        return salida

    @staticmethod
    def _vsi_zip(url):
        """Ruta VSI de un ZIP remoto.

        Las llaves son obligatorias: la URL prefirmada de S3 lleva «?» y «&»,
        y sin delimitarla GDAL corta la ruta en el primer carácter especial y
        busca un archivo que no existe.
        """
        return '/vsizip/{/vsicurl/' + url + '}'

    @staticmethod
    def _diagnosticar_zip(url, espera=30):
        """Por qué no se pudo leer el índice de un ZIP remoto.

        GDAL no lo cuenta: VSIReadDirRecursive devuelve una lista vacía
        tanto si el enlace venció como si el servidor no admite peticiones
        de rango o devolvió una página de inicio de sesión. Sin esto, el
        aviso se queda en «salió vacío» y el usuario no tiene de dónde
        agarrar.

        NO se manda el token de Earthdata. Si la URL es prefirmada no hace
        falta —la credencial va en la cadena de consulta— y mandarlo
        filtraría el token a un host de Amazon, que además rechaza una
        petición que traiga las dos formas de autenticación a la vez. Lo
        que se busca es justamente saber si hace falta autenticación: un
        403 aquí lo responde.
        """
        seguro = sin_firma_url(url)

        # Lo primero, porque si es esto no hay nada que diagnosticar por
        # HTTP: GDAL ni siquiera llega a pedir. Un diagnóstico que empiece
        # por la red responde «el servidor va bien» y deja el fallo sin
        # explicar, que es justo lo que pasó.
        permitidas = gdal.GetConfigOption(
            'CPL_VSIL_CURL_ALLOWED_EXTENSIONS', None)
        if lista_con_zip(permitidas):
            return (f'{seguro}: CPL_VSIL_CURL_ALLOWED_EXTENSIONS está en '
                    f'«{permitidas}» y no incluye .zip, de modo que '
                    f'/vsicurl/ se niega a abrirlo SIN pedir nada al '
                    f'servidor y sin dar error. Es una opción de GDAL que '
                    f'se configura en QGIS: Configuración → Opciones → '
                    f'GDAL. El complemento añade .zip mientras colecta; si '
                    f've este mensaje, esa corrección no llegó a aplicarse.')
        try:
            pet = urllib.request.Request(_url_https(url))
            # Dos bytes: la pregunta es si el servidor admite rangos, no
            # qué hay dentro.
            pet.add_header('Range', 'bytes=0-1')
            # Esquema validado por _url_https arriba: B310 ya no aplica.
            with urllib.request.urlopen(pet, timeout=espera) as r:  # nosec B310
                codigo = getattr(r, 'status', None) or r.getcode()
                rangos = r.headers.get('Accept-Ranges') or '(no declarado)'
                tipo = r.headers.get('Content-Type') or '(sin tipo)'
                tamano = (r.headers.get('Content-Range')
                          or r.headers.get('Content-Length') or '(sin tamaño)')
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                return (f'{seguro}: HTTP {e.code}. El enlace venció o el '
                        f'objeto exige autenticación de Earthdata, que GDAL '
                        f'no le manda al leer por /vsicurl/. Vuelva a '
                        f'ejecutar la colecta de datos para pedir enlaces nuevos; si '
                        f'sigue igual, el producto ya no es público.')
            if e.code == 404:
                return (f'{seguro}: HTTP 404. El producto ya no está en el '
                        f'servidor; habría que volver a pedirlo a HyP3.')
            return f'{seguro}: HTTP {e.code}.'
        except urllib.error.URLError as e:
            return f'{seguro}: no se pudo conectar ({e.reason}).'
        except TimeoutError:
            return f'{seguro}: el servidor no respondió en {espera} s.'
        except ValueError as e:
            return f'{seguro}: URL rechazada ({e}).'

        if 'html' in tipo.lower():
            return (f'{seguro}: el servidor devolvió {tipo} en vez del ZIP, '
                    f'que es lo que pasa cuando la descarga redirige a una '
                    f'página de inicio de sesión de Earthdata.')
        if int(codigo or 0) == 200:
            return (f'{seguro}: respondió 200 y no 206, de modo que ignora '
                    f'las peticiones de rango (Accept-Ranges: {rangos}). '
                    f'GDAL necesita rangos para leer el índice de un ZIP sin '
                    f'bajárselo entero, así que no puede listarlo.')

        # HEAD aparte. Es la petición con la que GDAL averigua el tamaño
        # para saltar al índice, que está al final del ZIP; un servidor
        # puede contestar GET por rangos de maravilla y no contestar HEAD.
        # Sin probarla por separado, el diagnóstico decía «el ZIP responde,
        # así que el índice debería poder leerse» y se quedaba sin
        # explicación.
        estado_head = _estado_head(url, espera)
        total = _total_de_rango(tamano)
        if total and total >= 4 * 1024 ** 3:
            nota_tam = (f' Mide {total / 1024 ** 3:.2f} GiB, por encima de '
                        f'4 GiB, así que su índice es ZIP64.')
        elif total:
            nota_tam = (f' Mide {total / 1024 ** 3:.2f} GiB, por debajo de '
                        f'4 GiB: el índice NO es ZIP64 y eso queda '
                        f'descartado.')
        else:
            nota_tam = ''
        return (f'{seguro}: HTTP {codigo}, rangos {rangos}, {tamano}, '
                f'tipo {tipo}. HEAD: {estado_head}.{nota_tam} '
                f'{_probar_vsicurl(url)}')

    def _listar_zip(self, raiz, feedback):
        """(entradas, nota) del ZIP remoto, con respaldo si GDAL no puede.

        El índice de un ZIP está al FINAL del archivo, así que para leerlo
        sin bajarse el archivo entero GDAL necesita saber cuánto mide, y
        por omisión lo averigua con una petición HEAD. Hay servidores y
        redes de distribución que no contestan HEAD como él espera:
        entonces no sabe el tamaño, no puede saltar al final, y
        VSIReadDirRecursive devuelve una lista vacía SIN error, que es
        indistinguible de «el archivo está vacío».

        CPL_VSIL_CURL_USE_HEAD=NO le dice que averigüe el tamaño con un GET
        por rangos, que es justo lo que estos servidores sí contestan —el
        diagnóstico de esta misma clase lo confirmó: HTTP 206, rangos
        aceptados, tipo application/zip y 0.92 GiB de tamaño declarado—.

        Hay que limpiar la caché de /vsicurl entre intentos: GDAL recuerda
        lo que averiguó del archivo, y sin limpiarla el segundo intento
        reutiliza el estado fallido del primero y vuelve a salir vacío.
        """
        if not self._sin_head:
            entradas = gdal.ReadDirRecursive(raiz)
            if entradas:
                return entradas, ''
            fallo = gdal.GetLastErrorMsg() or ''
            gdal.VSICurlClearCache()
            anterior = gdal.GetConfigOption('CPL_VSIL_CURL_USE_HEAD', None)
            try:
                gdal.SetConfigOption('CPL_VSIL_CURL_USE_HEAD', 'NO')
                entradas = gdal.ReadDirRecursive(raiz)
            finally:
                if not entradas:
                    gdal.SetConfigOption('CPL_VSIL_CURL_USE_HEAD', anterior)
            if entradas:
                # Se deja puesto para el resto del lote: si hizo falta en
                # un producto, hará falta en los 30 restantes, y repetir
                # el intento fallido en cada uno cuesta una petición.
                self._sin_head = True
                feedback.pushInfo(
                    "  El servidor no responde a HEAD como GDAL espera; se "
                    "pasa a averiguar el tamaño por rangos "
                    "(CPL_VSIL_CURL_USE_HEAD=NO) para el resto del lote.")
                return entradas, 'sin-head'
            return [], fallo or gdal.GetLastErrorMsg() or ''

        entradas = gdal.ReadDirRecursive(raiz)
        return (entradas or []), ('' if entradas
                                  else (gdal.GetLastErrorMsg() or ''))

    def _ruta_banda(self, archivos, pol, feedback):
        """(ruta VSI, None) o (None, motivo) de esa polarización.

        Devuelve el MOTIVO en vez de informarlo por su cuenta. Antes
        devolvía None para todo —URL rechazada, índice ilegible, índice
        vacío, banda ausente— y quien llama añadía «no se localizó la banda
        VH dentro del producto», que es una conclusión que no se puede
        sostener: con el índice vacío no se sabe qué bandas trae el
        producto. El registro quedaba con dos avisos por producto que se
        contradecían, y el segundo señalaba la causa equivocada.

        HyP3 entrega UN solo archivo por trabajo: el ZIP del producto
        completo, con las bandas dentro. No publica los GeoTIFF sueltos, así
        que no hay nada que buscar por sufijo en el bloque `files`; hay que
        entrar en el archivo comprimido.

        El contenido se LISTA en vez de componer la ruta interna a partir del
        nombre del ZIP. Suponer «<base>/<base>_VH.tif» funcionaría hoy y
        fallaría el día que ASF cambie la estructura, con un mensaje que
        culparía al dato. Listar cuesta una petición de rango al índice del
        ZIP, que está al final del archivo.
        """
        for archivo in archivos:
            nombre = str(archivo.get('filename') or '')
            url = archivo.get('url') or ''
            if not url or not nombre.lower().endswith('.zip'):
                continue
            try:
                _url_https(url)
            except ValueError as e:
                return None, f'URL de producto rechazada: {e}'
            raiz = self._vsi_zip(url)
            try:
                dentro, _nota = self._listar_zip(raiz, feedback)
            except RuntimeError as e:
                # El mensaje de GDAL repite la URL, y la cadena de consulta
                # de una URL prefirmada es la credencial.
                return None, (f'{nombre}: no se pudo leer el índice: '
                              f'{str(e).replace(url, sin_firma_url(url))}')
            if not dentro:
                return None, (f'{nombre}: GDAL no pudo listar el contenido. '
                              + self._diagnosticar_zip(url))
            sufijo = f'_{pol.upper()}.TIF'
            for interno in dentro:
                if str(interno).upper().endswith(sufijo):
                    return raiz + '/' + str(interno), None
            tifs = sorted(os.path.basename(str(x)) for x in dentro
                          if str(x).upper().endswith('.TIF'))
            return None, (f'{nombre}: el producto se listó bien pero no trae '
                          f'ninguna banda terminada en {sufijo}. GeoTIFF '
                          f'presentes: {tifs if tifs else "ninguno"}')
        return None, 'el trabajo no declara ningún archivo .zip'

    def _recortar(self, fuente, destino, bbox, feedback):
        """Recorta al AOI la banda indicada. True si quedó algo utilizable.

        `fuente` llega ya como ruta VSI construida por _ruta_banda, que valida
        el esquema de la URL antes de componerla.
        """
        try:
            ds = gdal.Warp(
                destino, fuente,
                options=gdal.WarpOptions(
                    format='GTiff',
                    outputBounds=(bbox[0], bbox[1], bbox[2], bbox[3]),
                    outputBoundsSRS='EPSG:4326',
                    dstNodata=float('nan'),
                    resampleAlg='bilinear',
                    creationOptions=['COMPRESS=DEFLATE', 'PREDICTOR=3',
                                     'TILED=YES']))
        except RuntimeError as e:
            feedback.pushWarning(f"[!] gdal.Warp falló: {e}")
            return False
        if ds is None:
            feedback.pushWarning(
                f"[!] No se pudo recortar {os.path.basename(destino)}. Si el "
                f"producto venció, HyP3 ya no lo sirve y habría que pedirlo "
                f"de nuevo.")
            return False
        ds = None
        return True

    # ----------------------------------------------------------- amplitud
    def _leer_alineado(self, rutas, feedback):
        """(capas, geo, proj, ancho, alto, temporales) alineadas a la primera.

        Los productos de una misma traza vienen en el mismo huso UTM, pero el
        recorte puede diferir en un píxel según el redondeo; y nada garantiza
        el huso si el AOI cae en un borde. Sin este paso los arreglos tendrían
        tamaños incompatibles o, peor, se apilarían desalineados sin error.
        """
        carpeta_tmp = QgsProcessingUtils.tempFolder()
        temporales = []
        ds_ref = gdal.Open(rutas[0])
        if ds_ref is None:
            raise RuntimeError(f'No se pudo abrir {rutas[0]}')
        geo = ds_ref.GetGeoTransform()
        proj = ds_ref.GetProjection()
        ancho, alto = ds_ref.RasterXSize, ds_ref.RasterYSize
        ds_ref = None

        capas = []
        for ruta in rutas:
            ds = gdal.Open(ruta)
            if ds is None:
                feedback.pushWarning(
                    f"[_leer_alineado] No se pudo abrir "
                    f"{os.path.basename(ruta)}; omitido.")
                continue
            mismo = (ds.GetProjection() == proj
                     and ds.RasterXSize == ancho
                     and ds.RasterYSize == alto)
            if mismo:
                capas.append(self._banda_como_nan(ds, feedback))
                ds = None
                continue
            ds = None
            tmp = os.path.join(carpeta_tmp, f"s1_{uuid.uuid4().hex[:8]}.tif")
            alineado = gdal.Warp(
                tmp, ruta,
                options=gdal.WarpOptions(
                    format='GTiff', dstSRS=proj,
                    outputBounds=(geo[0], geo[3] + alto * geo[5],
                                  geo[0] + ancho * geo[1], geo[3]),
                    width=ancho, height=alto, resampleAlg='bilinear',
                    dstNodata=float('nan')))
            if alineado is None:
                feedback.pushWarning(
                    f"[_leer_alineado] No se pudo alinear "
                    f"{os.path.basename(ruta)}; omitido.")
                continue
            capas.append(self._banda_como_nan(alineado, feedback))
            alineado = None
            temporales.append(tmp)
        return capas, geo, proj, ancho, alto, temporales

    @staticmethod
    def _banda_como_nan(ds, feedback):
        """Banda 1 como float32 con el nodata convertido a NaN.

        La guía de producto de HyP3 no declara el valor de nodata, así que se
        lee del propio archivo. Si no lo declara, se trata el 0 como relleno:
        gamma0 en potencia es una potencia, y exactamente 0 no es una medida
        posible — un retrodispersado nulo no existe, ni en agua en calma.
        """
        banda = ds.GetRasterBand(1)
        arr = banda.ReadAsArray().astype(np.float32)
        nd = banda.GetNoDataValue()
        if nd is None:
            nd = NODATA_RESPALDO
            feedback.pushDebugInfo(
                "[_banda_como_nan] la banda no declara nodata; se trata el 0 "
                "como relleno")
        if np.isnan(nd):
            return arr
        return np.where(arr == np.float32(nd), np.nan, arr)

    def _amplitud(self, sufijo, entradas, seca, lluvia, min_obs, man,
                  carpeta, context, feedback):
        """Mediana de seca − mediana de lluvia, por píxel, en potencia."""
        secas = [r for r, m, _f in entradas if m in seca]
        lluvias = [r for r, m, _f in entradas if m in lluvia]
        fechas_s = sorted(f for _r, m, f in entradas if m in seca)
        fechas_l = sorted(f for _r, m, f in entradas if m in lluvia)
        feedback.pushInfo(
            f"\n  {sufijo}: {len(secas)} de seca, {len(lluvias)} de lluvia")
        if not secas or not lluvias:
            feedback.pushWarning(
                f"[!] {sufijo}: falta una estación entera; no se calcula la "
                f"amplitud. Espere a que terminen más trabajos o pida otro "
                f"año.")
            return

        temporales = []
        try:
            cap_s, geo, proj, ancho, alto, t1 = self._leer_alineado(
                secas, feedback)
            temporales.extend(t1)
            cap_l, _g, _p, _w, _h, t2 = self._leer_alineado(lluvias, feedback)
            temporales.extend(t2)
            if not cap_s or not cap_l:
                feedback.pushWarning(
                    f"[!] {sufijo}: no quedaron capas utilizables tras el "
                    f"alineado.")
                return

            with np.errstate(invalid='ignore'), warnings.catch_warnings():
                # np.errstate NO alcanza a «All-NaN slice encountered»: ese
                # aviso sale por warnings.warn y nanmedian lo emite una vez
                # por columna todo-NaN, llenando el registro de QGIS.
                warnings.filterwarnings(
                    'ignore', message='All-NaN slice encountered',
                    category=RuntimeWarning)
                warnings.filterwarnings(
                    'ignore', message='Mean of empty slice',
                    category=RuntimeWarning)
                pila_s = np.stack(cap_s).astype(np.float32)
                pila_l = np.stack(cap_l).astype(np.float32)
                n_s = np.isfinite(pila_s).sum(axis=0)
                n_l = np.isfinite(pila_l).sum(axis=0)
                med_s = np.nanmedian(pila_s, axis=0)
                med_l = np.nanmedian(pila_l, axis=0)
                amplitud = med_s - med_l
                suficiente = (n_s >= min_obs) & (n_l >= min_obs)
                descartados = int((~suficiente).sum())
                amplitud = np.where(suficiente, amplitud, np.nan)
                # Las medianas se enmascaran IGUAL que la amplitud. Si no,
                # un píxel con una sola observación tendría mediana pero no
                # amplitud, y la resta de los dos rásteres publicados
                # dejaría de coincidir con el ráster de amplitud justo
                # donde el dato es más flojo.
                med_s = np.where(suficiente, med_s, np.nan)
                med_l = np.where(suficiente, med_l, np.nan)

            feedback.pushInfo(
                f"    observaciones por píxel — seca: {int(n_s.min())}–"
                f"{int(n_s.max())} (mediana {int(np.median(n_s))}), lluvia: "
                f"{int(n_l.min())}–{int(n_l.max())} "
                f"(mediana {int(np.median(n_l))})")
            if descartados:
                feedback.pushWarning(
                    f"[!] {descartados} píxel(es) "
                    f"({100.0 * descartados / n_s.size:.1f} %) quedan a NaN "
                    f"por no alcanzar {min_obs} observaciones en alguna "
                    f"estación. En radar esto suele ser la máscara de sombra "
                    f"y solapamiento, que es sistemática: afecta siempre a "
                    f"los mismos píxeles y no se arregla con más fechas.")

            self._escribir(amplitud, n_s, n_l, sufijo, geo, proj, ancho, alto,
                           seca, lluvia, min_obs, len(secas), len(lluvias),
                           fechas_s, fechas_l, man, carpeta, context, feedback,
                           medianas=(med_s, med_l))
        except (RuntimeError, ValueError, OSError) as e:
            feedback.pushWarning(f"[_amplitud] {sufijo}: {e}")
        finally:
            for tmp in temporales:
                try:
                    if os.path.exists(tmp):
                        os.remove(tmp)
                except OSError as e2:
                    feedback.pushDebugInfo(f"[limpieza_s1] {e2}")

    def _amplitud_rvi(self, recortes, seca, lluvia, min_obs, man, carpeta,
                      context, feedback):
        """RVI dual-pol = 4*VH/(VV+VH), en potencia, y su amplitud.

        El cociente SE CALCULA EN POTENCIA. Con escala dB el resultado no
        sería un cociente de potencias sino una diferencia de logaritmos, que
        es otra cantidad con otra interpretación.
        """
        por_fecha = {}
        for pol in ('VH', 'VV'):
            for ruta, mes, fecha in recortes[pol]:
                por_fecha.setdefault(fecha, {})[pol] = (ruta, mes)
        pares = {f: d for f, d in por_fecha.items() if len(d) == 2}
        if not pares:
            feedback.pushWarning(
                "[!] RVI: ninguna fecha tiene VH y VV a la vez; no se "
                "calcula.")
            return
        feedback.pushInfo(
            f"\n  RVI: {len(pares)} fecha(s) con las dos polarizaciones")

        carpeta_tmp = QgsProcessingUtils.tempFolder()
        entradas, temporales = [], []
        try:
            for fecha in sorted(pares):
                ruta_vh, mes = pares[fecha]['VH']
                ruta_vv, _m = pares[fecha]['VV']
                capas, geo, proj, ancho, alto, tmps = self._leer_alineado(
                    [ruta_vh, ruta_vv], feedback)
                temporales.extend(tmps)
                if len(capas) != 2:
                    feedback.pushWarning(
                        f"[!] RVI {fecha}: no se pudieron alinear las dos "
                        f"polarizaciones; omitida.")
                    continue
                vh, vv = capas
                with np.errstate(invalid='ignore', divide='ignore'):
                    suma = vv + vh
                    rvi = np.where(suma > 0, 4.0 * vh / suma, np.nan)
                rvi = _acotar('RVI', rvi, feedback)
                tmp = os.path.join(
                    carpeta_tmp, f"rvi_{uuid.uuid4().hex[:8]}.tif")
                if not self._guardar_float(tmp, rvi, geo, proj, ancho, alto):
                    continue
                temporales.append(tmp)
                entradas.append((tmp, mes, fecha))
            if entradas:
                self._amplitud('RVI', entradas, seca, lluvia, min_obs, man,
                               carpeta, context, feedback)
        except (RuntimeError, ValueError, OSError) as e:
            feedback.pushWarning(f"[_amplitud_rvi] {e}")
        finally:
            for tmp in temporales:
                try:
                    if os.path.exists(tmp):
                        os.remove(tmp)
                except OSError as e2:
                    feedback.pushDebugInfo(f"[limpieza_rvi] {e2}")

    @staticmethod
    def _guardar_float(ruta, arr, geo, proj, ancho, alto):
        """GeoTIFF float32 de una banda con NaN como nodata."""
        driver = gdal.GetDriverByName('GTiff')
        ds = driver.Create(ruta, ancho, alto, 1, gdal.GDT_Float32,
                           options=['COMPRESS=DEFLATE', 'PREDICTOR=3',
                                    'TILED=YES'])
        if ds is None:
            return False
        ds.SetGeoTransform(geo)
        ds.SetProjection(proj)
        banda = ds.GetRasterBand(1)
        banda.WriteArray(arr.astype(np.float32))
        banda.SetNoDataValue(float('nan'))
        ds.FlushCache()
        ds = None
        return True

    def _informe_auditoria(self, man, lote, min_obs, carpeta, recortes,
                           seca, lluvia, feedback):
        """Escribe el informe HTML del lote.

        Reúne en un solo archivo lo que hace falta para repetir o revisar
        el cálculo: de dónde salió el dato, con qué parámetros se pidió,
        con cuáles se procesó aquí, qué fechas entraron en cada estación y
        qué salió. Esos datos estaban repartidos entre el manifiesto, los
        metadatos de cada GeoTIFF y el registro de QGIS —que no se guarda—,
        de modo que reconstruirlo seis meses después era trabajo manual.
        """
        from .core import informe_html

        fechas = {'seca': set(), 'lluvia': set()}
        for _pol, lst in (recortes or {}).items():
            for _ruta, mes, fecha in lst:
                if mes in seca:
                    fechas['seca'].add(fecha)
                elif mes in lluvia:
                    fechas['lluvia'].add(fecha)

        try:
            from qgis.core import Qgis
            version_qgis = Qgis.QGIS_VERSION
        except (ImportError, AttributeError):
            version_qgis = '?'

        datos = {
            'titulo': f'Informe de auditoría · {lote}',
            'generado': datetime.now(timezone.utc).strftime(
                '%Y-%m-%dT%H:%M:%SZ'),
            'generado_por': f'BuscadorSTAC {self.VERSION}',
            'avisos': list(self._avisos_informe),
            'lote': [
                ('Nombre del lote', lote),
                ('Año', man.get('anio', '?')),
                ('Traza', f"{man.get('traza', '?')} "
                          f"{man.get('direccion', '?')}"),
                ('Espaciamiento de píxel', f"{man.get('resolucion_m', '?')} m"),
                ('Radiometría', man.get('radiometria', '?')),
                ('Escala', man.get('escala', '?')),
                ('Filtro de speckle',
                 'sí' if man.get('filtro_speckle') else 'no'),
                ('Polarización pedida', man.get('polarizacion', '?')),
                ('AOI del pedido (EPSG:4326)',
                 ', '.join(f'{v:.5f}' for v in (man.get('bbox_4326') or []))),
                ('Pedido creado', man.get('creado', '?')),
            ],
            'parametros': [
                ('Meses de estación seca',
                 ','.join(str(m) for m in sorted(seca))),
                ('Meses de estación lluviosa',
                 ','.join(str(m) for m in sorted(lluvia))),
                ('Mínimo de observaciones por píxel y estación', min_obs),
                ('SRC de salida',
                 self._crs_salida or 'el nativo del producto'),
                ('Remuestreo al reproyectar',
                 'vecino más próximo' if self._crs_salida else '(no aplica)'),
                ('Carpeta', carpeta),
            ],
            'productos': list(self._prod_informe),
            'escenas': [
                [est, len(fechas[est]), ', '.join(sorted(fechas[est]))]
                for est in ('seca', 'lluvia') if fechas[est]
            ],
            'entorno': [
                ('QGIS', version_qgis),
                ('GDAL', gdal.VersionInfo('RELEASE_NAME') if gdal else '?'),
                ('Complemento', f'BuscadorSTAC {self.VERSION}'),
                ('Autor', f'{AUTOR} <{AUTOR_EMAIL}>'),
            ],
            'fuentes': [
                ('Sentinel-1 GRD',
                 'Copernicus Sentinel data (ESA), acceso abierto'),
                ('Productos RTC',
                 'ASF DAAC HyP3, procesados con software GAMMA'),
                ('Cita requerida por ASF',
                 'Los productos de HyP3 deben citarse; ver '
                 'hyp3-docs.asf.alaska.edu'),
                ('Estadístico',
                 'mediana por estación, en potencia; amplitud = seca − lluvia'),
                ('Advertencia',
                 'Serie válida solo DENTRO de una traza. La humedad del '
                 'suelo mueve la retrodispersión por sí sola.'),
            ],
        }
        ruta = os.path.join(carpeta, f'INFORME_{lote}.html')
        try:
            with open(ruta, 'w', encoding='utf-8') as fh:
                fh.write(informe_html(datos))
        except OSError as e:
            feedback.pushWarning(f'[!] No se pudo escribir el informe: {e}')
            return
        feedback.pushInfo(f"\ninforme de auditoría → {ruta}")

    def _reproyectar(self, ruta, feedback):
        """Reproyecta un GeoTIFF en su sitio al SRC de salida pedido.

        Se hace AL FINAL, sobre los productos, y no sobre los recortes:
        reproyectar cada fecha antes de la mediana resamplearía la
        retrodispersión 31 veces para calcular un estadístico que no
        depende de la rejilla.

        Vecino más próximo a propósito: conserva exactamente los valores
        medidos. Un bilineal inventaría promedios entre píxeles vecinos, y
        este producto existe para ser auditado.

        Los metadatos se vuelven a escribir después porque gdal.Warp no
        garantiza conservarlos, y perder la procedencia de un producto que
        se publica para auditoría sería peor que no reproyectar.
        """
        destino_srs = getattr(self, '_crs_salida', None)
        if not destino_srs:
            return ruta
        ds = gdal.Open(ruta)
        if ds is None:
            return ruta
        meta = dict(ds.GetMetadata() or {})
        descripciones = [ds.GetRasterBand(i + 1).GetDescription()
                         for i in range(ds.RasterCount)]
        ds = None
        tmp = ruta[:-4] + '_reproy.tif'
        try:
            salida = gdal.Warp(
                tmp, ruta,
                options=gdal.WarpOptions(
                    format='GTiff', dstSRS=destino_srs,
                    resampleAlg='near',
                    creationOptions=['COMPRESS=DEFLATE', 'TILED=YES']))
        except RuntimeError as e:
            feedback.pushWarning(
                f'[!] No se pudo reproyectar {os.path.basename(ruta)} a '
                f'{destino_srs}: {e}. Queda en su SRC nativo.')
            return ruta
        if salida is None:
            feedback.pushWarning(
                f'[!] No se pudo reproyectar {os.path.basename(ruta)} a '
                f'{destino_srs}. Queda en su SRC nativo.')
            return ruta
        salida = None
        try:
            os.replace(tmp, ruta)
        except OSError as e:
            feedback.pushWarning(f'[reproyectar] {e}')
            return ruta
        meta['SRC_REPROYECTADO_A'] = str(destino_srs)
        meta['REMUESTREO_REPROYECCION'] = (
            'vecino más próximo: conserva los valores medidos, a cambio de '
            'hasta medio píxel de desplazamiento geométrico')
        ds = gdal.Open(ruta, gdal.GA_Update)
        if ds is not None:
            ds.SetMetadata({k: str(v) for k, v in meta.items()})
            for i, desc in enumerate(descripciones):
                if desc:
                    ds.GetRasterBand(i + 1).SetDescription(desc)
            ds.FlushCache()
            ds = None
        return ruta

    @staticmethod
    def _meter_meta(ruta, meta, descripcion):
        """Metadatos y descripción de banda en un GeoTIFF ya escrito."""
        ds = gdal.Open(ruta, gdal.GA_Update)
        if ds is None:
            return False
        ds.GetRasterBand(1).SetDescription(descripcion)
        ds.SetMetadata({k: str(v) for k, v in meta.items()})
        ds.FlushCache()
        ds = None
        return True

    def _escribir(self, amplitud, n_s, n_l, sufijo, geo, proj, ancho, alto,
                  seca, lluvia, min_obs, n_esc_s, n_esc_l, fechas_s, fechas_l,
                  man, carpeta, context, feedback, medianas=None):
        """Escribe la amplitud y el recuento, y los deja cargados."""
        traza = man.get('traza', '?')
        direccion = str(man.get('direccion') or '?')
        # La traza va en el NOMBRE y no solo en los metadatos: mezclar trazas
        # es el error cardinal de este análisis, y así un apilado mezclado se
        # ve en el panel de capas en lugar de descubrirse en los resultados.
        letra = {'ascending': 'A', 'descending': 'D'}.get(direccion, 'X')
        # El espaciamiento también va en el nombre: la misma traza y año a 30
        # y a 10 m son dos productos que hay que poder comparar, no uno que
        # sobreescriba al otro.
        base = (f"AMPL_S1_{man.get('anio', '0000')}_T{traza}{letra}"
                f"_{man.get('resolucion_m', '00')}m_{sufijo}"
                f"_S{_codigo_meses(seca)}_L{_codigo_meses(lluvia)}")
        destino = os.path.join(carpeta, f"{base}.tif")
        if not self._guardar_float(destino, amplitud, geo, proj, ancho, alto):
            raise RuntimeError(f'No se pudo crear {destino}')

        # Los metadatos comunes se arman UNA vez y los comparten la
        # amplitud y las dos medianas: tres productos del mismo cálculo que
        # describieran su procedencia de tres maneras distintas serían
        # exactamente lo que estos metadatos existen para evitar.
        meta_comun = {
                'POLARIZACION': sufijo,
                'ESCALA': ESCALA,
                'RADIOMETRIA': RADIOMETRIA,
                'TRAZA': str(traza),
                'DIRECCION_ORBITA': direccion,
                'ANIO': str(man.get('anio', '')),
                'RESOLUCION_M': str(man.get('resolucion_m', '')),
                'FILTRO_SPECKLE': str(man.get('filtro_speckle', '')),
                'MESES_SECA': ','.join(str(m) for m in sorted(seca)),
                'MESES_LLUVIA': ','.join(str(m) for m in sorted(lluvia)),
                'FECHAS_SECA': ','.join(fechas_s),
                'FECHAS_LLUVIA': ','.join(fechas_l),
                'N_ESCENAS_SECA': str(n_esc_s),
                'N_ESCENAS_LLUVIA': str(n_esc_l),
                'MIN_OBS_POR_PIXEL': str(min_obs),
                'ESTADISTICO': 'mediana por estacion',
                'ADVERTENCIA_TRAZA': (
                    'Serie valida solo DENTRO de una traza: otra orbita '
                    'relativa observa con otro angulo de incidencia.'),
                'FUENTE': ('Copernicus Sentinel-1 (ESA); RTC por ASF DAAC '
                           'HyP3 con GAMMA'),
                'GENERADO_POR': f'BuscadorSTAC {self.VERSION}',
                'AUTOR': f'{AUTOR} <{AUTOR_EMAIL}>',
        }

        meta_amp = dict(meta_comun)
        meta_amp['SIGNO'] = 'seca menos lluvia'
        meta_amp['ESTADISTICO'] = 'diferencia de medianas por estacion'
        self._meter_meta(destino, meta_amp,
                         f'AMPL_{sufijo} seca-lluvia (gamma0 potencia)')
        self._reproyectar(destino, feedback)
        escritos = [('amplitud', destino)]

        # Las medianas por estación. La amplitud sola no dice sobre qué
        # nivel se mide: una diferencia de 0.001 no significa lo mismo
        # sobre un fondo de 0.005 que sobre uno de 0.05, y sin las
        # medianas no hay manera de saberlo desde el producto.
        if medianas is not None:
            nucleo = (f"S1_{man.get('anio', '0000')}_T{traza}{letra}"
                      f"_{man.get('resolucion_m', '00')}m_{sufijo}")
            for arr, etiqueta, nombre, meses in (
                    (medianas[0], 'seca',
                     f'MEDSECA_{nucleo}_S{_codigo_meses(seca)}', seca),
                    (medianas[1], 'lluvia',
                     f'MEDLLUV_{nucleo}_L{_codigo_meses(lluvia)}', lluvia)):
                ruta_m = os.path.join(carpeta, f'{nombre}.tif')
                if not self._guardar_float(ruta_m, arr, geo, proj, ancho,
                                           alto):
                    feedback.pushWarning(f'[!] No se pudo crear {ruta_m}')
                    continue
                meta_m = dict(meta_comun)
                meta_m['ESTACION'] = etiqueta
                meta_m['ESTADISTICO'] = f'mediana de la estacion {etiqueta}'
                meta_m['MESES'] = ','.join(str(m) for m in sorted(meses))
                meta_m['ENMASCARADO_COMO_LA_AMPLITUD'] = (
                    'sí: los píxeles sin MIN_OBS_POR_PIXEL en AMBAS '
                    'estaciones van a NaN, para que la resta de las dos '
                    'medianas coincida con el ráster de amplitud')
                self._meter_meta(
                    ruta_m, meta_m,
                    f'MED_{sufijo} {etiqueta} (gamma0 potencia)')
                self._reproyectar(ruta_m, feedback)
                escritos.append((f'mediana {etiqueta}', ruta_m))
                if np.isfinite(arr).any():
                    feedback.pushInfo(
                        f"  mediana {etiqueta} → {os.path.basename(ruta_m)}  "
                        f"[{np.nanmin(arr):.5f} … {np.nanmax(arr):.5f}], "
                        f"mediana {float(np.nanmedian(arr)):.5f}")
                    self._prod_informe.append([
                        f'mediana {etiqueta} {sufijo}',
                        os.path.basename(ruta_m),
                        f'{np.nanmin(arr):.5f}', f'{np.nanmax(arr):.5f}',
                        f'{float(np.nanmedian(arr)):.5f}',
                        f'{100.0 * np.isfinite(arr).mean():.1f} %'])

        validos = np.isfinite(amplitud)
        if validos.any():
            feedback.pushInfo(
                f"  amplitud → {os.path.basename(destino)}  "
                f"[{np.nanmin(amplitud):+.4f} … {np.nanmax(amplitud):+.4f}], "
                f"mediana {float(np.nanmedian(amplitud)):+.4f}, "
                f"{100.0 * validos.mean():.0f} % válidos")
            self._prod_informe.append([
                f'amplitud {sufijo}', os.path.basename(destino),
                f'{np.nanmin(amplitud):+.5f}', f'{np.nanmax(amplitud):+.5f}',
                f'{float(np.nanmedian(amplitud)):+.5f}',
                f'{100.0 * validos.mean():.1f} %'])
            feedback.pushInfo(
                "    Signo: seca − lluvia, en POTENCIA. En VH una caída "
                "grande hacia la seca indica pérdida de volumen dispersor "
                "—herbáceas que se secan—; valores cercanos a cero indican "
                "estructura que persiste, es decir leñosas.")
            feedback.pushInfo(
                "    Cuidado al interpretar: la humedad del suelo mueve esto "
                "por sí sola, así que un potrero desnudo también da amplitud "
                "grande. Cruce con la amplitud óptica antes de concluir.")
        else:
            max_s, max_l = int(n_s.max()), int(n_l.max())
            if max_s == 0 or max_l == 0:
                causa = (
                    f"ningún píxel tiene observación válida en "
                    f"{'seca' if max_s == 0 else 'lluvia'}: los recortes de "
                    f"esa estación salieron vacíos. Revise que el AOI caiga "
                    f"dentro de la huella de los gránulos.")
            elif max_s < min_obs or max_l < min_obs:
                causa = (
                    f"el mejor píxel llega a {max_s} observación(es) en seca "
                    f"y {max_l} en lluvia, por debajo del mínimo de "
                    f"{min_obs}. Baje «Mínimo de observaciones por píxel» a "
                    f"{min(max_s, max_l)} o espere a que terminen más "
                    f"trabajos.")
            else:
                causa = (
                    f"hay hasta {max_s} y {max_l} observaciones por píxel, "
                    f"pero ninguna coincide en el mismo píxel en ambas "
                    f"estaciones.")
            feedback.pushWarning(
                f"[!] {os.path.basename(destino)} sin píxeles válidos: "
                f"{causa}")

        ruta_nobs = os.path.join(carpeta, f"{base}_NOBS.tif")
        try:
            driver = gdal.GetDriverByName('GTiff')
            ds_n = driver.Create(ruta_nobs, ancho, alto, 2, gdal.GDT_Int16,
                                 options=['COMPRESS=DEFLATE', 'TILED=YES'])
            if ds_n is not None:
                ds_n.SetGeoTransform(geo)
                ds_n.SetProjection(proj)
                for idx, (arr, etiqueta) in enumerate(
                        ((n_s, 'n_obs_seca'), (n_l, 'n_obs_lluvia')), start=1):
                    b = ds_n.GetRasterBand(idx)
                    b.WriteArray(arr.astype(np.int16))
                    b.SetDescription(etiqueta)
                ds_n.SetMetadata({
                    'BANDA_1': 'observaciones validas en seca',
                    'BANDA_2': 'observaciones validas en lluvia',
                    'MIN_OBS_POR_PIXEL': str(min_obs),
                    'TRAZA': str(traza),
                })
                ds_n.FlushCache()
                ds_n = None
                self._reproyectar(ruta_nobs, feedback)
                feedback.pushInfo(
                    f"  recuento → {os.path.basename(ruta_nobs)} "
                    f"(banda 1 = seca, banda 2 = lluvia)")
        except (RuntimeError, OSError) as e:
            feedback.pushWarning(f"[_escribir] NOBS: {e}")

        # La capa se deja PENDIENTE y se carga en postProcessAlgorithm, no se
        # registra aquí con addLayerToLoadOnCompletion. Ese registro no la
        # mostraba —el archivo quedaba bien escrito en disco y la capa no
        # aparecía— y, lo que lo hacía indiagnosticable, no dejaba ni una
        # línea en el registro: no hay forma de distinguir «falló el cálculo»
        # de «falló la carga». Ver _cargar_pendientes.
        nombre = os.path.basename(destino)[:-4]
        pp = RealcePostProcessor(
            'whole',            # recorte local pequeño: estadística completa
            True,               # visible: es el producto, no un insumo
            1,                  # mean ± n·sigma
            2.0,
            1,                  # paleta divergente azul–rojo
            0, 6, None)
        _registrar_postproc(pp)
        self._pendientes.append((destino, nombre, pp))

    # ------------------------------------------------------------- utilidades
    def _rect_4326(self, parameters, context, feedback):
        """Envolvente del AOI en EPSG:4326, de la capa o de la extensión."""
        destino = QgsCoordinateReferenceSystem('EPSG:4326')
        fuente = self.parameterAsSource(parameters, self.AOI, context)
        if fuente is not None:
            rect = fuente.sourceExtent()
            src = fuente.sourceCrs()
            if src.isValid() and src != destino:
                try:
                    tr = QgsCoordinateTransform(
                        src, destino, QgsProject.instance())
                    rect = tr.transformBoundingBox(rect)
                except (RuntimeError, ValueError) as e:
                    if feedback is not None:
                        feedback.pushWarning(
                            f"[!] No se pudo reproyectar el AOI: {e}")
                    return None
            return rect if not rect.isEmpty() else None
        rect = self.parameterAsExtent(
            parameters, self.EXTENSION, context, destino)
        return rect if not rect.isEmpty() else None

    def postProcessAlgorithm(self, context, feedback):
        """Carga las amplitudes. Corre en el HILO PRINCIPAL.

        Es el único punto seguro para añadir capas al proyecto: hacerlo desde
        processAlgorithm sería tocar el proyecto desde un hilo secundario.
        """
        if self._pendientes:
            _cargar_pendientes(self._pendientes, context, feedback)
            self._pendientes = []
        return {}
