# -*- coding: utf-8 -*-
# SPDX-License-Identifier: GPL-2.0-or-later
"""Lógica pura del complemento: NI QGIS NI Qt.

Este módulo no importa nada de qgis ni de osgeo a propósito. Esa es su
única regla, y es lo que permite probar su contenido con pytest a secas,
sin QGIS instalado y sin imitarlo con clases falsas. Un QGIS imitado
introduce sus propios errores: un fallo del imitador se confunde con un
fallo del código, y eso ya costó un ciclo de depuración sobre código que
estaba bien.

Lo que vive aquí son fórmulas, parseo, agrupación temporal, estimaciones y
validaciones: cosas que no necesitan un proyecto abierto para tener
sentido. Lo que NO vive aquí es cualquier cosa que construya una capa, un
sumidero, un ráster o un diálogo.

Las funciones que reciben una fuente de entidades (_items_desde_capa,
_procedencia_huellas) la usan por pato: solo llaman a getFeatures(),
fields() y featureCount(). No importan QGIS para eso, y por eso se pueden
probar con un objeto cualquiera que ofrezca esos tres métodos.

Se vuelven a enganchar en el algoritmo como staticmethod, de modo que los
sitios de llamada siguen escribiéndose «self._foo(...)» y el traslado no
cambió ni una línea de ellos.

Autor    : Jorge Fallas (jfallas56@gmail.com)
Licencia : GPL v2 o posterior
Versión  : 1.0.3
"""

import datetime
import json
import math
import os
import re

import numpy as np


# ------------------------------------------------------------------------
# Constantes del dominio
# ------------------------------------------------------------------------
# Todas las bandas reflectivas de Landsat C2 L2 son de 30 m.
RES_LS = 30.0

# QA_PIXEL de Collection 2 es un campo de bits de 16 bits.
QA_BIT_RELLENO = 0          # 0 = con dato, 1 = relleno fuera de la escena

QA_BITS_NUBOSOS = (
    1,   # nube dilatada
    2,   # cirro (solo OLI/TIRS, L8-9)
    3,   # nube
    4,   # sombra de nube
)

# --------------------------------------------------------------------------
# Tasseled Cap: coeficientes por sensor
# --------------------------------------------------------------------------
# Orden de bandas común a todos los conjuntos, expresado en claves lógicas:
#   azul, verde, rojo, NIR, SWIR1, SWIR2
# que resuelven a B1-B5,B7 en TM/ETM+, B2-B7 en OLI y B2/B3/B4/B8/B11/B12 en
# MSI. Validados por ortonormalidad: ‖v‖ = 1.0000 y v·w = 0.0000 en los cuatro
# conjuntos (desviación máxima 1e-4), lo que descarta errores de transcripción.
#
# ADVERTENCIA METODOLÓGICA: cada conjunto se derivó para un tipo concreto de
# reflectancia y NO es intercambiable.
#   Crist (1985)      TM    -> factor de reflectancia (SUPERFICIE)  [coincide]
#   Huang et al. 2002 ETM+  -> at-satellite (TOA)                    [no coincide]
#   Zhai et al. 2022  OLI   -> reflectancia de SUPERFICIE            [coincide]
#   Shi & Xu 2019     MSI   -> at-sensor (TOA, L1C)                  [no coincide]
# Este script descarga productos de SUPERFICIE (Landsat C2 L2, Sentinel-2 L2A).
# TM y OLI ya usan conjuntos derivados sobre superficie; ETM+ y MSI siguen sin
# equivalente de adopción comparable y se marcan como desajuste en el registro.
# Para ETM+ es práctica extendida aplicar Crist, dado que las bandas de ETM+ y
# TM son casi idénticas; el archivo JSON de coeficientes permite hacerlo.
TC_ORDEN_BANDAS = (
    "blue (B02, 10 m)",
    "green (B03, 10 m)",
    "red (B04, 10 m)",
    "nir (B08, 10 m)",
    "swir16 (B11, 20 m)",
    "swir22 (B12, 20 m)",
)

# --------------------------------------------------------------------------
# Índices espectrales
# --------------------------------------------------------------------------
# (etiqueta, sufijo, bandas lógicas necesarias, solo_sentinel2)
# Las fórmulas son normalizadas o razones simples: no dependen de coeficientes
# calibrados por sensor, de modo que son directamente comparables entre
# Sentinel-2 y Landsat siempre que se calculen sobre reflectancia, no sobre DN.
INDICES_ESPECTRALES = [
    ("NDVI — vigor general (NIR, Rojo)", "NDVI",
     ("nir (B08, 10 m)", "red (B04, 10 m)"), False),
    ("SAVI — NDVI ajustado por suelo, L=0.5", "SAVI",
     ("nir (B08, 10 m)", "red (B04, 10 m)"), False),
    ("NDMI — humedad del dosel (NIR, SWIR1)", "NDMI",
     ("nir08 (B8A, 20 m)", "swir16 (B11, 20 m)"), False),
    ("NBR — estructura y quema (NIR, SWIR2)", "NBR",
     ("nir08 (B8A, 20 m)", "swir22 (B12, 20 m)"), False),
    ("MSI — estrés hídrico (SWIR1/NIR)", "MSI",
     ("swir16 (B11, 20 m)", "nir08 (B8A, 20 m)"), False),
    ("NDRE — clorofila borde rojo (solo Sentinel-2)", "NDRE",
     ("nir08 (B8A, 20 m)", "rededge1 (B05, 20 m)"), True),
    ("CIre — índice de clorofila borde rojo (solo Sentinel-2)", "CIre",
     ("rededge3 (B07, 20 m)", "rededge1 (B05, 20 m)"), True),
    ("Tasseled Cap — Wetness (humedad y sombra de dosel)", "TCW",
     TC_ORDEN_BANDAS, False),
    ("Tasseled Cap — Greenness", "TCG", TC_ORDEN_BANDAS, False),
    ("Tasseled Cap — Brightness", "TCB", TC_ORDEN_BANDAS, False),
]

# Rango físico admisible de cada índice. Fuera de él, el valor no es una
# medida sino un artefacto de un denominador cercano a cero (ver
# _acotar_indice). Los componentes Tasseled Cap no se acotan: son
# combinaciones lineales, no razones, y su rango depende del conjunto de
# coeficientes.
LIMITES_INDICE = {
    'NDVI': (-1.0, 1.0),
    'SAVI': (-1.5, 1.5),      # el factor 1.5 de la fórmula amplía la cota
    'NDMI': (-1.0, 1.0),
    'NBR':  (-1.0, 1.0),
    'NDRE': (-1.0, 1.0),
    'MSI':  (0.0, 10.0),      # razón: negativa es imposible, >10 es artefacto
    'CIre': (-1.0, 20.0),     # razón desplazada en -1
}


def _res_nativa(clave_logica, familia="s2"):
    """Resolución en metros del asset, según familia de sensor."""
    if familia == "ls":
        return RES_LS
    return RES_NATIVA.get(clave_logica, 10.0)


# Resolución nativa en metros por asset. Determina xRes/yRes de salida y evita
# que GDAL elija un tamaño de píxel arbitrario vía SuggestedWarpOutput.
RES_NATIVA = {
    "visual (RGB 8-bit)": 10.0,
    "blue (B02, 10 m)": 10.0,
    "green (B03, 10 m)": 10.0,
    "red (B04, 10 m)": 10.0,
    "rededge1 (B05, 20 m)": 20.0,
    "rededge2 (B06, 20 m)": 20.0,
    "rededge3 (B07, 20 m)": 20.0,
    "nir (B08, 10 m)": 10.0,
    "nir08 (B8A, 20 m)": 20.0,
    "swir16 (B11, 20 m)": 20.0,
    "swir22 (B12, 20 m)": 20.0,
    "scl (máscara de clases)": 20.0,
}


# ------------------------------------------------------------------------
# Funciones puras
# ------------------------------------------------------------------------

def _tamano_salida(bbox, ancho_px):
    """Ancho/alto en píxeles conservando la proporción real del AOI.

    La corrección por cos(latitud) evita miniaturas estiradas: a 10° N un
    grado de longitud mide ~98 % de un grado de latitud.
    """
    dx = abs(bbox[2] - bbox[0])
    dy = abs(bbox[3] - bbox[1])
    if dx <= 0 or dy <= 0:
        return int(ancho_px), int(ancho_px)
    lat_media = math.radians((bbox[1] + bbox[3]) / 2.0)
    dx_corr = dx * max(0.05, math.cos(lat_media))
    alto = int(round(ancho_px * (dy / dx_corr)))
    return int(ancho_px), max(16, min(alto, 4096))


def _destino_sumidero(crudo):
    """Texto del destino de un sumidero (cadena o definición de salida).

    Processing entrega el valor de un sumidero bien como cadena, bien
    como QgsProcessingOutputLayerDefinition, cuyo destino real está en
    «sink» como QgsProperty. Mirar solo str() del segundo no sirve.
    """
    if crudo is None:
        return ''
    sink = getattr(crudo, 'sink', None)
    if sink is not None:
        valor = getattr(sink, 'staticValue', None)
        if callable(valor):
            try:
                return str(valor() or '').strip()
            except (TypeError, RuntimeError, AttributeError):
                pass
    return str(crudo or '').strip()


def _salida_volatil(crudo):
    """True si el destino de las huellas no sobrevive a la sesión."""
    texto = _destino_sumidero(crudo).upper()
    if not texto:
        return False
    return ('TEMPORARY_OUTPUT' in texto
            or texto == 'MEMORY'
            or texto.startswith('MEMORY:'))


def _procedencia_huellas(fuente):
    """(catalogo, coleccion, n_entidades) de la capa de huellas revisada.

    Devuelve catalogo=None cuando la capa NO trae el campo «catalogo»:
    las huellas de una versión anterior del complemento no lo tienen. Eso
    no se rechaza —dejaría inservibles las capas ya guardadas— sino que
    se avisa al ejecutar, igual que un manifiesto de HyP3 sin bbox.

    Se lee una sola entidad: basta para la procedencia y evita recorrer
    la capa entera en el hilo principal durante la validación.
    """
    try:
        campos = fuente.fields()
        nombres = {campos.at(i).name() for i in range(campos.count())}
    except (AttributeError, RuntimeError, TypeError):
        return None, None, -1

    try:
        n = int(fuente.featureCount())
    except (AttributeError, TypeError, ValueError):
        n = -1

    cat = col = None
    vistas = 0
    try:
        for feat in fuente.getFeatures():
            vistas += 1
            if 'catalogo' in nombres:
                cat = str(feat['catalogo'] or '').strip() or None
            if 'coleccion' in nombres:
                col = str(feat['coleccion'] or '').strip() or None
            break
    except (RuntimeError, KeyError, TypeError):
        return cat, col, n
    # featureCount() puede no estar disponible en algunas fuentes; si no
    # lo está, al menos se sabe si había o no una primera entidad.
    if n < 0:
        n = vistas
    return cat, col, n


def _items_desde_capa(fuente, feedback):
    """Reconstruye items STAC mínimos desde la capa de huellas revisada."""
    items = []
    for feat in fuente.getFeatures():
        try:
            crudo = feat['assets']
            assets = json.loads(crudo) if crudo else {}
        except Exception as e:
            feedback.pushWarning(
                f"[_items_desde_capa] Campo «assets» ilegible en "
                f"{feat.id()}: {e}. Entidad omitida.")
            continue
        if not assets:
            feedback.pushWarning(
                f"[_items_desde_capa] Entidad {feat.id()} sin URL de "
                f"assets; omitida.")
            continue

        fecha = feat['fecha'] or ''
        hora = feat['hora_utc'] or '00:00:00'
        items.append({
            'id': feat['id'],
            'geometry': None,
            'properties': {
                'datetime': f"{fecha}T{hora}Z",
                'eo:cloud_cover': feat['nubes_pct'],
                'platform': feat['plataforma'],
                's2:mgrs_tile': feat['tile'],
                # Malla ya resuelta en la ejecución previa;
                # vale para ambos sensores sin reconstruirla.
                '_malla': feat['tile'],
                'proj:epsg': feat['epsg'],
            },
            'assets': {k: {'href': v} for k, v in assets.items()},
            '_nubes_aoi': feat['nubes_aoi'],
            '_datos_pct': feat['datos_pct'],
            '_thumb': feat['thumb'],
        })
    return items


def _memoria_estimada(bbox4326, indices_sel, amplitud, n_fechas,
                      familia, estirar=False):
    """(GiB de pico, ancho_px, alto_px) del cálculo pedido.

    El modelo sigue lo que el código hace de verdad, no una regla
    aproximada. Ni `_calcular_indices` ni `_amplitud_fenologica` procesan
    por bloques: cada banda entra completa con `ReadAsArray`, de modo que
    el pico es el número de arreglos vivos a la vez por el tamaño en
    píxeles del recorte.

      _calcular_indices, por índice y fecha:
          len(bandas) arreglos + resultado + máscara de nube
        Se procesa un índice y una fecha a la vez, así que cuenta el
        índice más costoso, no la suma. Tasseled Cap pide seis bandas
        —tres de ellas de 10 m— y es por lejos el peor caso.

      _amplitud_fenologica, por índice:
          `pilas` guarda las dos estaciones completas (N arreglos) y
          `np.stack(...).astype(float32)` las vuelve a copiar (2N). A eso
          se suman n_seca y n_lluvia (int64: dos float32 cada uno),
          med_seca, med_lluvia, amplitud y la máscara `suficiente`
          ≈ 8 equivalentes.

    `familia` llega como argumento en vez de leerse de `self._familia`
    porque este método corre desde checkParameterValues, antes de que
    processAlgorithm asigne ese atributo.
    """
    lat_media = math.radians((bbox4326[1] + bbox4326[3]) / 2.0)
    ancho_m = abs(bbox4326[2] - bbox4326[0]) * 111320.0 * math.cos(lat_media)
    alto_m = abs(bbox4326[3] - bbox4326[1]) * 110570.0

    pico = 0.0
    ancho_px = alto_px = 1.0
    for i in indices_sel:
        bandas_log = INDICES_ESPECTRALES[i][2]
        # Todas las bandas del índice se remuestrean a la más fina.
        res = min(_res_nativa(b, familia) for b in bandas_log)
        w = max(1.0, ancho_m / res)
        h = max(1.0, alto_m / res)
        capas = len(bandas_log) + 2.0
        if amplitud:
            capas = max(capas, 2.0 * max(1, n_fechas) + 8.0)
        bytes_pico = w * h * 4.0 * capas
        if bytes_pico > pico:
            pico, ancho_px, alto_px = bytes_pico, w, h

    if not indices_sel:
        res = RES_LS if familia == 'ls' else 10.0
        ancho_px = max(1.0, ancho_m / res)
        alto_px = max(1.0, alto_m / res)
        if estirar:
            # _percentiles_vrt lee una banda entera, la convierte a
            # float32 y filtra los finitos: unos tres equivalentes vivos.
            pico = ancho_px * alto_px * 4.0 * 3.0
        else:
            # Componer bandas sin escalado va todo por Warp/Translate,
            # que escriben por bloques. Este código no acumula nada, así
            # que no hay techo que imponer.
            pico = 0.0

    return pico / (1024.0 ** 3), ancho_px, alto_px


def _agrupar_por_periodo(candidatos, estrategia, n_cand, feedback):
    """Agrupa por año o mes y devuelve las n_cand escenas de menor nube.

    Ordenar por eo:cloud_cover aquí es solo una preselección barata: el
    criterio definitivo es la nubosidad sobre el AOI, que se mide después
    leyendo SCL de estos candidatos.
    """
    grupos = {}
    for item in candidatos:
        fecha = str(item.get('properties', {}).get('datetime', ''))[:10]
        if len(fecha) < 7:
            continue
        clave = fecha[:4] if estrategia == 'anual' else fecha[:7]
        nubes = item.get('properties', {}).get('eo:cloud_cover')
        grupos.setdefault(clave, []).append(
            (float(nubes) if nubes is not None else 100.0, item))

    recortado = {}
    for clave, lista in grupos.items():
        lista.sort(key=lambda par: par[0])
        recortado[clave] = [par[1] for par in lista[:n_cand]]
    return recortado


def _periodos_sin_datos(grupos, f_ini, f_fin, estrategia, nubes,
                        feedback):
    """Informa qué años/meses del rango pedido quedaron sin candidatos."""
    try:
        ini = int(f_ini[:4])
        fin = int(f_fin[:4])
        if estrategia == 'anual':
            esperados = [str(a) for a in range(max(ini, 2017), fin + 1)]
        else:
            esperados = []
            for anio in range(max(ini, 2017), fin + 1):
                esperados.extend(f"{anio}-{m:02d}" for m in range(1, 13))
            esperados = [e for e in esperados if f_ini[:7] <= e <= f_fin[:7]]

        vacios = [e for e in esperados if e not in grupos]
        if vacios:
            feedback.pushWarning(
                f"[!] Sin ninguna escena en {len(vacios)} periodo(s): "
                f"{', '.join(vacios)}.")
            feedback.pushWarning(
                f"    Causa habitual: el filtro «Nubosidad máxima de la "
                f"ESCENA COMPLETA» ({nubes} %) se aplica en el servidor "
                f"sobre la teselá de 110 km y elimina el periodo antes de "
                f"evaluar su AOI. Súbalo a 90 % y vuelva a ejecutar. "
                f"Un mosaico anual sin nubes (p. ej. EOX s2cloudless) NO "
                f"prueba que exista una escena individual despejada: se "
                f"construye combinando píxeles claros de muchas fechas.")
    except Exception as e:
        feedback.pushDebugInfo(f"[periodos_sin_datos] {e}")


def _reducir_por_periodo(items, estrategia, feedback):
    """Conserva por periodo la escena con menos nube DENTRO del AOI."""
    mejores = {}
    for item in items:
        fecha = str(item.get('properties', {}).get('datetime', ''))[:10]
        if len(fecha) < 7:
            continue
        clave = fecha[:4] if estrategia == 'anual' else fecha[:7]
        nubes_aoi = item.get('_nubes_aoi', -1.0)
        # Un -1 (SCL ilegible) se ordena al final, pero se conserva si es
        # lo único que hay en ese periodo.
        puntua = nubes_aoi if nubes_aoi is not None and nubes_aoi >= 0 else 999.0
        previo = mejores.get(clave)
        if previo is None or puntua < previo[0]:
            mejores[clave] = (puntua, item)

    feedback.pushInfo("\nSelección definitiva por periodo (nube sobre el AOI):")
    seleccion = []
    for clave in sorted(mejores):
        puntua, item = mejores[clave]
        seleccion.append(item)
        etiqueta = f"{puntua:5.1f} %" if puntua < 999 else "  n/d"
        escena = item.get('properties', {}).get('eo:cloud_cover') or 0.0
        feedback.pushInfo(
            f"  {clave}: {str(item.get('id', ''))[:40]:<40} "
            f"AOI {etiqueta}  (teselá {float(escena):.0f} %)")
    return seleccion

# ------------------------------------------------------- Landsat: calidad


def _mascaras_qa_pixel(arr):
    """Descompone QA_PIXEL de Landsat C2 en (sin_dato, nube).

    QA_PIXEL es un campo de bits de 16 bits, no una clasificación por
    valores: hay que consultar bit a bit. Interpretarlo como códigos de
    clase (al estilo SCL) daría resultados sin sentido.
    """
    arr = arr.astype(np.uint16)
    sin_dato = (arr & (1 << QA_BIT_RELLENO)) != 0
    nube = np.zeros(arr.shape, dtype=bool)
    for bit in QA_BITS_NUBOSOS:
        nube |= (arr & (1 << bit)) != 0
    return sin_dato, nube


def _acotar_indice(sufijo, valores, feedback):
    """Marca como NaN los valores fuera del rango físico del índice.

    Un índice normalizado (a-b)/(a+b) solo está acotado en [-1, 1] si a y
    b son positivos. El desplazamiento BOA de -0.1 permite reflectancias
    negativas a propósito (para no truncar superficies oscuras), así que
    sobre agua o sombra profunda el denominador puede acercarse a cero con
    signos opuestos: con NIR=0.030 y SWIR=-0.028 el NDMI sale 29.0, un
    valor FINITO que el filtro isfinite deja pasar y que contamina el
    rango, el realce y la mediana estacional.

    Los índices de razón (MSI, CIre) no tienen cota teórica superior; para
    ellos solo se descarta el signo imposible y valores absurdamente altos.
    """
    limites = LIMITES_INDICE.get(sufijo)
    if limites is None:
        return valores
    lo, hi = limites
    fuera = np.isfinite(valores) & ((valores < lo) | (valores > hi))
    n_fuera = int(fuera.sum())
    if n_fuera:
        total = int(np.isfinite(valores).sum()) or 1
        feedback.pushWarning(
            f"[!] {sufijo}: {n_fuera} píxel(es) ({100.0 * n_fuera / total:.2f} %) "
            f"fuera del rango físico [{lo}, {hi}] y marcados como sin dato. "
            f"Suele indicar reflectancia negativa sobre agua o sombra "
            f"profunda, donde el denominador del índice se acerca a cero.")
        valores = np.where(fuera, np.nan, valores)
    return valores

# ------------------------------ coeficientes Tasseled Cap intercambiables


def _validar_ortonormalidad(conjunto, nombre, feedback, tol=0.02):
    """Comprueba que B, G y W formen una rotación ortonormal.

    La transformación Tasseled Cap es una rotación rígida: cada vector debe
    tener norma 1 y ser ortogonal a los otros dos. Es la prueba que detecta
    un dígito mal copiado sin necesidad de volver al artículo.
    """
    try:
        vectores = {k: np.array(conjunto[k], dtype=float) for k in 'BGW'}
    except KeyError as e:
        feedback.pushWarning(
            f"[_validar_ortonormalidad] {nombre}: falta la componente {e}.")
        return False

    longitudes = {k: len(v) for k, v in vectores.items()}
    if len(set(longitudes.values())) != 1:
        feedback.pushWarning(
            f"[_validar_ortonormalidad] {nombre}: las componentes tienen "
            f"distinto número de coeficientes {longitudes}.")
        return False

    err_norma = max(abs(float(np.linalg.norm(v)) - 1.0)
                    for v in vectores.values())
    pares = (('B', 'G'), ('B', 'W'), ('G', 'W'))
    err_ortog = max(abs(float(vectores[a] @ vectores[b])) for a, b in pares)

    if err_norma < tol and err_ortog < tol:
        feedback.pushInfo(
            f"  {nombre}: ortonormalidad correcta "
            f"(máx|‖v‖-1| = {err_norma:.4f}, máx|v·w| = {err_ortog:.4f})")
        return True

    feedback.pushWarning(
        f"[!] {nombre}: los coeficientes NO forman una rotación ortonormal "
        f"(máx|‖v‖-1| = {err_norma:.4f}, máx|v·w| = {err_ortog:.4f}). "
        f"Revise la transcripción: lo habitual es un dígito cambiado o una "
        f"fila desplazada. Se usarán de todos modos, pero los componentes "
        f"no serán independientes entre sí.")
    return False


def _escribir_plantilla_tc(carpeta, feedback):
    """Deja un JSON de ejemplo con el esquema exacto que espera el lector."""
    ruta = os.path.join(carpeta, 'tc_coeficientes_plantilla.json')
    if os.path.exists(ruta):
        return ruta
    plantilla = {
        "_comentario": (
            "Sustituya los valores por los del artículo que corresponda. "
            "El orden de los seis coeficientes es SIEMPRE: azul, verde, "
            "rojo, NIR, SWIR1, SWIR2. Indique en «tipo» si el conjunto se "
            "derivó sobre reflectancia de 'superficie' o 'TOA'. Grupos "
            "admitidos: TM (Landsat 4/5), ETM (Landsat 7), OLI (Landsat "
            "8/9), MSI (Sentinel-2). TM y OLI ya traen conjuntos de "
            "superficie; los pendientes son ETM y MSI."),
        "ETM": {
            "ref": "Sustituya por el conjunto de superficie que decida usar",
            "tipo": "superficie",
            "B": [0.0, 0.0, 0.0, 0.0, 0.0, 0.0],
            "G": [0.0, 0.0, 0.0, 0.0, 0.0, 0.0],
            "W": [0.0, 0.0, 0.0, 0.0, 0.0, 0.0],
        },
    }
    try:
        with open(ruta, 'w', encoding='utf-8') as fh:
            json.dump(plantilla, fh, ensure_ascii=False, indent=2)
        feedback.pushInfo(
            f"Plantilla de coeficientes escrita en: {ruta}\n"
            f"  Rellénela con la tabla del artículo y vuelva a ejecutar "
            f"indicándola en «Coeficientes Tasseled Cap alternativos».")
        return ruta
    except Exception as e:
        feedback.pushWarning(f"[_escribir_plantilla_tc] {e}")
        return None

# ---------------------------------------------------- amplitud fenológica


def _parsear_meses(texto):
    """Convierte '12,1,2' en [12, 1, 2]. Devuelve [] si algo no encaja."""
    meses = []
    for trozo in str(texto or '').replace(';', ',').split(','):
        trozo = trozo.strip()
        if not trozo:
            continue
        try:
            m = int(trozo)
        except ValueError:
            return []
        if not 1 <= m <= 12:
            return []
        meses.append(m)
    return meses


def _parsear_limites(texto):
    """Convierte 'mín,máx' en (float, float). None si está vacío o es inválido."""
    texto = str(texto or '').strip()
    if not texto:
        return None
    partes = [t.strip() for t in texto.replace(';', ',').split(',')]
    if len(partes) != 2:
        return None
    try:
        lo, hi = float(partes[0]), float(partes[1])
    except ValueError:
        return None
    if not hi > lo:
        return None
    return (lo, hi)


def _codigo_meses(meses):
    """Código compacto de una lista de meses para el nombre de archivo.

    Contiguo -> '12-04' (admite el salto de diciembre a enero, que es el
    caso normal de la estación seca en el Pacífico norte).
    No contiguo -> '01.03.07'.
    """
    unicos = sorted(set(int(m) for m in meses if 1 <= int(m) <= 12))
    if not unicos:
        return 'NA'
    if len(unicos) == 12:
        return '01-12'

    # Buscar un arranque tal que la secuencia sea contigua circularmente.
    conjunto = set(unicos)
    for inicio in unicos:
        if ((inicio - 2) % 12) + 1 in conjunto:
            continue                      # no es el principio de la racha
        secuencia = []
        actual = inicio
        while actual in conjunto:
            secuencia.append(actual)
            actual = (actual % 12) + 1
        if len(secuencia) == len(unicos):
            return f"{secuencia[0]:02d}-{secuencia[-1]:02d}"
        break
    return '.'.join(f"{m:02d}" for m in unicos)


# Costo en creditos de un trabajo RTC de HyP3 segun espaciamiento de pixel.
# Fuente: https://hyp3-docs.asf.alaska.edu/using/credits/
# Vive aqui, y no en el algoritmo, porque de estos numeros depende lo que
# se gasta y conviene poder probarlos sin QGIS.
CREDITOS_RTC = {30: 5, 20: 15, 10: 60}


def _esc(valor):
    """Texto seguro para HTML. Nunca se interpola nada sin pasar por aqui."""
    return (str('' if valor is None else valor)
            .replace('&', '&amp;').replace('<', '&lt;')
            .replace('>', '&gt;').replace('"', '&quot;'))


def _tabla_html(titulo, filas, nota=''):
    """Tabla clave/valor. `filas` es [(clave, valor)]."""
    if not filas:
        return ''
    cuerpo = '\n'.join(
        f'    <tr><th>{_esc(k)}</th><td>{_esc(v)}</td></tr>'
        for k, v in filas)
    pie = f'  <p class="nota">{_esc(nota)}</p>\n' if nota else ''
    return (f'<section>\n  <h2>{_esc(titulo)}</h2>\n'
            f'  <table>\n{cuerpo}\n  </table>\n{pie}</section>\n')


def _tabla_cols_html(titulo, encabezados, filas, nota=''):
    """Tabla de varias columnas. `filas` es [[celda, ...]]."""
    if not filas:
        return ''
    cab = ''.join(f'<th>{_esc(h)}</th>' for h in encabezados)
    cuerpo = '\n'.join(
        '    <tr>' + ''.join(f'<td>{_esc(c)}</td>' for c in fila) + '</tr>'
        for fila in filas)
    pie = f'  <p class="nota">{_esc(nota)}</p>\n' if nota else ''
    return (f'<section>\n  <h2>{_esc(titulo)}</h2>\n'
            f'  <table>\n    <tr>{cab}</tr>\n{cuerpo}\n  </table>\n'
            f'{pie}</section>\n')


_CSS_INFORME = """
:root { color-scheme: light dark; }
body { font: 15px/1.55 system-ui, "Segoe UI", Roboto, sans-serif;
       margin: 0 auto; max-width: 60rem; padding: 2rem 1rem;
       background: #fff; color: #1a1a1a; }
h1 { font-size: 1.5rem; margin: 0 0 .25rem; }
h2 { font-size: 1.05rem; margin: 2rem 0 .5rem;
     border-bottom: 2px solid #d8dde3; padding-bottom: .25rem; }
.sub { color: #5a6570; margin: 0 0 1.5rem; }
table { border-collapse: collapse; width: 100%; }
th, td { text-align: left; padding: .4rem .6rem; border-bottom: 1px solid
         #e4e8ec; vertical-align: top; }
th { width: 16rem; font-weight: 600; color: #333; }
tr:first-child th { width: auto; }
td { font-variant-numeric: tabular-nums; }
.nota { color: #5a6570; font-size: .9rem; margin: .5rem 0 0; }
.aviso { background: #fff7e6; border-left: 4px solid #e0a800;
         padding: .6rem .9rem; margin: .6rem 0; }
code { font-family: ui-monospace, Consolas, monospace; font-size: .92em; }
footer { margin-top: 2.5rem; color: #5a6570; font-size: .85rem;
         border-top: 1px solid #e4e8ec; padding-top: .8rem; }
@media (prefers-color-scheme: dark) {
  body { background: #16191c; color: #e6e6e6; }
  h2 { border-bottom-color: #333a41; }
  th, td { border-bottom-color: #2a2f35; }
  th { color: #cfd6dd; }
  .sub, .nota, footer { color: #9aa4ae; }
  .aviso { background: #2a2410; border-left-color: #c89b1b; }
}
@media print { body { max-width: none; } h2 { page-break-after: avoid; } }
"""


def informe_html(datos):
    """Informe de auditoría de un lote, en HTML autocontenido.

    Sin dependencias: ni plotly ni CDN ni fuentes remotas. Un informe de
    auditoría tiene que poder abrirse dentro de diez años y en una
    máquina sin red, que es justo cuando hace falta.

    Vive en core.py --y no en el algoritmo-- porque es construccion de
    texto y se prueba sin QGIS.
    """
    partes = [
        _tabla_html('Lote', datos.get('lote') or []),
        _tabla_html('Parámetros de la ejecución', datos.get('parametros')
                    or [],
                    'Son los que gobiernan ESTE cálculo. Los que fijó el '
                    'pedido viajan dentro del dato y se listan arriba.'),
        _tabla_cols_html(
            'Productos escritos',
            ('Producto', 'Archivo', 'Mínimo', 'Máximo', 'Mediana',
             '% validos'),
            datos.get('productos') or [],
            'La amplitud es la resta de las dos medianas, píxel a píxel. '
            'Las tres llevan la misma máscara, de modo que la resta de los '
            'dos rásteres de mediana coincide con el de amplitud.'),
        _tabla_cols_html('Escenas usadas', ('Estación', 'N', 'Fechas'),
                         datos.get('escenas') or [],
                         'Una escena por fecha y traza. Mezclar trazas '
                         'invalidaría la serie: otra órbita relativa observa '
                         'con otro ángulo de incidencia.'),
        _tabla_html('Entorno', datos.get('entorno') or []),
        _tabla_html('Fuentes y licencias', datos.get('fuentes') or []),
    ]
    avisos = ''.join(
        f'<div class="aviso">{_esc(a)}</div>\n'
        for a in (datos.get('avisos') or []))
    titulo = _esc(datos.get('titulo') or 'Informe')
    return (
        '<!DOCTYPE html>\n<html lang="es">\n<head>\n'
        '<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, '
        'initial-scale=1">\n'
        f'<title>{titulo}</title>\n<style>{_CSS_INFORME}</style>\n'
        '</head>\n<body>\n'
        f'<h1>{titulo}</h1>\n'
        f'<p class="sub">Generado {_esc(datos.get("generado"))} por '
        f'{_esc(datos.get("generado_por"))}</p>\n'
        f'{avisos}'
        + ''.join(partes) +
        '<footer>Informe de auditoría: registra con qué datos y con qué '
        'parámetros se produjo cada archivo, para poder repetirlo o '
        'revisarlo más tarde.</footer>\n'
        '</body>\n</html>\n')


_RE_FECHA_KML = re.compile(
    r'^(\d{4})[-/](\d{2})[-/](\d{2})'
    r'(?:[T ](\d{2}):(\d{2})(?::(\d{2}))?(?:\.\d+)?'
    r'(Z|[+-]\d{2}:?\d{2})?)?$')


def fecha_kml(valor):
    """Fecha en la forma que acepta KML, o None si no se puede.

    Google Earth pone en su linea de tiempo las entidades que traen
    <TimeStamp>, y el controlador LIBKML lo escribe desde un campo
    llamado «timestamp». Pero solo lo entiende en ISO 8601: un
    «03/01/2025» se escribe tal cual en el KML y Google Earth lo ignora
    SIN decir nada, que es la peor forma de fallar --el archivo abre, las
    entidades se ven, y la linea de tiempo simplemente no aparece--. De
    ahi que se normalice aqui en vez de confiar en lo que traiga el
    campo.

    No se inventa zona horaria. KML da por supuesto UTC cuando no la hay,
    asi que anadir una «Z» que el dato no traia seria afirmar algo que no
    consta. Si venia, se conserva.

    Devuelve «AAAA-MM-DD» o «AAAA-MM-DDThh:mm:ss[zona]».
    """
    if valor is None:
        return None
    # Un date o datetime de Python ya sabe escribirse.
    if hasattr(valor, 'isoformat') and not isinstance(valor, str):
        try:
            return valor.isoformat()
        except (TypeError, ValueError):
            return None
    texto = str(valor).strip()
    if not texto:
        return None
    m = _RE_FECHA_KML.match(texto)
    if not m:
        return None
    anio, mes, dia, hh, mm, ss, zona = m.groups()
    # Que la fecha EXISTA: el patron acepta 2025-02-30 y KML no.
    try:
        datetime.date(int(anio), int(mes), int(dia))
    except ValueError:
        return None
    base = f'{anio}-{mes}-{dia}'
    if hh is None:
        return base
    if int(hh) > 23 or int(mm) > 59 or (ss is not None and int(ss) > 60):
        return None
    return f'{base}T{hh}:{mm}:{ss or "00"}{zona or ""}'


def lista_con_zip(valor):
    """La lista blanca de extensiones de /vsicurl/, con «.zip» incluido.

    CPL_VSIL_CURL_ALLOWED_EXTENSIONS limita que extensiones puede abrir
    /vsicurl/. Con «.tif,.TIF,.tiff,.jp2» puesto --que es lo que trae
    alguna instalacion de QGIS en Windows-- un .zip sencillamente NO se
    abre: VSIFOpenL devuelve NULL sin lanzar error, sin hacer ninguna
    peticion HTTP y sin un solo mensaje de depuracion. De ahi que el
    fallo fuera mudo, y de ahi que el resto del complemento siguiera
    funcionando: los COG de Sentinel-2 y Landsat son .tif.

    Se AÑADE .zip en vez de vaciar la lista. La restriccion la puso el
    usuario o su instalacion, y vaciarla abriria /vsicurl/ a todo lo
    demás mientras durase la colecta de datos.

    Devuelve None cuando no hay nada que cambiar: lista vacia o sin
    definir ya significa «todo permitido», y si .zip ya esta, tampoco.
    """
    texto = str(valor or '').strip()
    if not texto:
        return None
    partes = [p.strip() for p in texto.split(',') if p.strip()]
    if any(p.lower() == '.zip' for p in partes):
        return None
    return ','.join(partes + ['.zip'])


def opciones_asequibles(n_granulos, saldo):
    """[(espaciamiento, costo)] que caben en el saldo, de mas fino a menos.

    Decir «no le alcanza» sin decir para qué SÍ alcanza deja al usuario
    calculandolo a mano, y el precio por escena no es lineal: 5, 15 y 60
    créditos a 30, 20 y 10 m. Un saldo de 1630 no cubre 31 gránulos a
    10 m (1860) y sí los cubre a 20 m (465), que no es evidente de
    cabeza.
    """
    if saldo is None:
        return []
    opciones = []
    for espaciado in sorted(CREDITOS_RTC):
        costo = int(n_granulos) * CREDITOS_RTC[espaciado]
        if costo <= saldo:
            opciones.append((espaciado, costo))
    return opciones


def ordenar_trazas(candidatas):
    """(ordenadas, empatadas) de las trazas aptas para una diferencia estacional.

    Cada candidata es un dict con 'direccion', 'traza', 'n', 'seca',
    'lluvia' y 'creditos'.

    Se ordena por la estación MÁS DÉBIL, que es la que limita una
    diferencia de medianas, y el desempate es explícito y numérico: más
    gránulos, menos créditos y número de traza. Antes se ordenaba una
    tupla que empezaba por la estación débil, de modo que a igualdad de
    estación lo decidía el elemento siguiente, el NOMBRE de la dirección:
    «descending» va después de «ascending» y con reverse=True salía
    primero. La primera de la lista se presenta como «siguiente paso»,
    así que el alfabeto acababa recomendando dónde gastar créditos.

    `empatadas` son las que coinciden con la primera en lo que de verdad
    decide —estación débil, número de gránulos y coste—. Entre ellas el
    inventario no distingue, y conviene decirlo antes de gastar: la
    elección real es de geometría de mirada, que estos datos no contienen.
    """
    filas = sorted(
        candidatas,
        key=lambda c: (-min(int(c['seca']), int(c['lluvia'])),
                       -int(c['n']), int(c['creditos']), int(c['traza'])))
    if not filas:
        return [], []

    def _clave(c):
        return (min(int(c['seca']), int(c['lluvia'])),
                int(c['n']), int(c['creditos']))

    primera = _clave(filas[0])
    return filas, [c for c in filas if _clave(c) == primera]


def sin_firma_url(url):
    """URL sin su cadena de consulta, apta para el registro.

    En una URL prefirmada la cadena de consulta ES la credencial: quien la
    tenga puede descargar el objeto sin ninguna otra cosa. Por eso no puede
    ir al registro de QGIS, que el usuario copia y pega en un informe de
    error sin pensarlo. Se conserva el host y la ruta, que es lo que hace
    falta para saber de donde venia.
    """
    texto = str(url or '')
    corte = texto.find('?')
    if corte < 0:
        return texto
    return texto[:corte] + '?<firma oculta>'


def sin_prefijo_bearer(valor):
    """Token desnudo: quita un «Bearer » inicial si viene pegado.

    La ayuda del propio algoritmo dice que la cabecera va con valor
    «Bearer <token>», asi que copiar esa forma entera al archivo de token o
    a la variable de entorno es el error natural. Y entonces la peticion
    sale con «Authorization: Bearer Bearer eyJ...», el servidor responde
    401 y el mensaje culpa al token, que estaba bien: el usuario acaba
    generando otro para nada.

    Vive aqui, y no en cada via de lectura, porque las tres --
    configuracion de autenticacion, archivo y entorno-- tienen que
    devolver lo mismo. Tenerlo en una sola y no en las otras es
    exactamente como aparecio la incoherencia.
    """
    texto = str(valor or '').strip()
    while True:
        bajo = texto.lower()
        # Solo el prefijo y nada detras: es ausencia de token, no un token
        # llamado «Bearer». Devolverlo haria salir «Bearer Bearer» y el 401
        # volveria a culpar al token en vez de decir que falta.
        if bajo == 'bearer':
            return ''
        # El espacio es lo que distingue el prefijo de un token que
        # empiece por esas letras: recortar siete caracteres a ciegas
        # convertiria «bearertoken123» en basura.
        if bajo[:7] != 'bearer ':
            return texto
        texto = texto[7:].strip()


# --------------------------------------------------------------------------
# KML / KMZ: lo que no necesita QGIS
# --------------------------------------------------------------------------
# Topes de importacion de Google Earth Web, publicados por Google. Por
# encima de ellos el archivo entra como capa de datos de SOLO LECTURA en
# vez de como entidades editables. No impide verlo; cambia lo que se puede
# hacer con el.
TOPE_ENTIDADES_WEB = 10000
TOPE_VERTICES_WEB = 250000


def alfa_kml(opacidad_pct):
    """Color KML para una opacidad en por ciento: 'aabbggrr'.

    El canal alfa va PRIMERO en KML, no al final como en CSS o HTML, y los
    componentes van en orden inverso (azul, verde, rojo). Equivocar el
    orden con un blanco es inocuo —ffffff es simetrico— pero con cualquier
    otro color daria un tinte en vez de transparencia, y el error no se ve
    hasta abrir Google Earth.
    """
    pct = max(0, min(100, int(round(float(opacidad_pct)))))
    alfa = int(round(pct * 255 / 100.0))
    return '%02x%02x%02x%02x' % (alfa, 255, 255, 255)


def insertar_opacidad_kml(texto, opacidad_pct):
    """(texto, n_tocados). Mete <color> en cada <GroundOverlay> sin color.

    KMLSUPEROVERLAY no expone la opacidad como opcion de creacion, asi que
    se edita el KML ya escrito. Un <GroundOverlay> que ya trae <color> se
    deja intacto: duplicar la etiqueta deja un KML invalido.
    """
    if '<GroundOverlay>' not in texto:
        return texto, 0
    etiqueta = '<color>%s</color>' % alfa_kml(opacidad_pct)
    partes = texto.split('<GroundOverlay>')
    salida = [partes[0]]
    tocados = 0
    for trozo in partes[1:]:
        # ¿Este overlay ya tiene color, antes de que cierre?
        fin = trozo.find('</GroundOverlay>')
        cabeza = trozo if fin < 0 else trozo[:fin]
        if '<color>' in cabeza:
            salida.append('<GroundOverlay>' + trozo)
        else:
            salida.append('<GroundOverlay>' + etiqueta + trozo)
            tocados += 1
    return ''.join(salida), tocados


def contar_kml(texto):
    """{'marcadores', 'superposiciones', 'vertices'} de un KML.

    Los vertices se cuentan sobre el contenido de <coordinates>, no sobre
    las comas de todo el documento: un KML lleva comas en los nombres y en
    las descripciones, y contarlas todas inflaba la cifra y disparaba un
    aviso de tope que no correspondia.
    """
    cuenta = {'marcadores': texto.count('<Placemark'),
              'superposiciones': texto.count('<GroundOverlay'),
              'vertices': 0}
    pos = 0
    total = 0
    while True:
        i = texto.find('<coordinates', pos)
        if i < 0:
            break
        j = texto.find('>', i)
        k = texto.find('</coordinates>', j if j >= 0 else i)
        if j < 0 or k < 0:
            break
        bloque = texto[j + 1:k]
        total += len([t for t in bloque.replace('\n', ' ').split() if t])
        pos = k + 1
    cuenta['vertices'] = total
    return cuenta


# Un <href> de KML es una referencia URI, y RFC 3986 no admite estos
# caracteres sin porcentaje-codificar. Los que importan en la practica,
# porque salen de un nombre de capa de QGIS, son los NO ASCII: ver
# capa_a_promover para por que esos son los que rompen de verdad.
_HREF_PROHIBIDOS = ' "<>\\^`{|}[]'


def href_inseguro(href):
    """Caracteres de `href` que no valen en una URI, en orden y sin repetir.

    Devuelve '' si el href es seguro. Sirve para no escribir dentro de un
    KMZ un enlace que Google Earth no va a poder resolver.
    """
    malos = []
    for car in str(href or ''):
        if car in _HREF_PROHIBIDOS or ord(car) > 126 or ord(car) < 32:
            if car not in malos:
                malos.append(car)
    return ''.join(malos)


def capa_a_promover(nombres, doc_texto):
    """Nombre del KML de capa que debe pasar a ser el `doc.kml`, o None.

    LIBKML no escribe el documento en `doc.kml`: escribe ahi un
    <NetworkLink> con un <href> hacia `layers/<nombre de la capa>.kml`, y
    mete el <Document>, su <name> y los <Placemark> en ese segundo
    archivo. El nombre de la capa pasa TAL CUAL al href y al nombre del
    miembro del ZIP, y ahi esta el fallo: LIBKML escribe ese nombre en
    UTF-8 crudo pero DEJA EN CERO el bit 11 de las banderas del ZIP, el
    que declara «este nombre esta en UTF-8». Sin ese bit, la norma del
    formato obliga a leer el nombre como CP437, asi que un lector
    conforme ve «layers/BÃºfer.kml» mientras el href pide
    «layers/Búfer.kml». El enlace no resuelve, y el KMZ abre vacio sin un
    solo mensaje.

    Medido con GDAL 3.8.4: con el nombre «Búfer» el propio `ogr.Open`
    devuelve None sobre el KMZ que LIBKML acaba de escribir. Con
    «Bufer Oval 357» (espacios) y con «Buferes[Union]» (corchetes) lo lee
    sin problema. O sea: lo que rompe son las tildes y la enie, no los
    espacios ni los corchetes — y en espanol eso es casi cualquier nombre
    de capa.

    Aplanar el archivo —que el documento viva directamente en `doc.kml`—
    deja un unico miembro, de nombre ASCII, y sin ningun href que
    resolver. Es ademas la forma canonica de un KMZ de una sola capa, y
    para una capa de nombre ASCII, que ya funcionaba, no cambia nada.

    Solo se aplana el caso que LIBKML produce para una capa, y se
    comprueba antes: `doc.kml` sin contenido propio, exactamente un KML de
    capa y ningun otro miembro que pudiera depender de la ruta relativa
    (una imagen de icono, por ejemplo) y que al mover el documento se
    quedaria sin resolver.
    """
    nombres = list(nombres)
    if 'doc.kml' not in nombres:
        return None
    if '<Placemark' in doc_texto or '<GroundOverlay' in doc_texto:
        return None          # doc.kml ya tiene contenido propio
    capas = [n for n in nombres
             if n != 'doc.kml' and n.lower().endswith('.kml')]
    if len(capas) != 1:
        return None          # varias capas: el NetworkLink hace falta
    otros = [n for n in nombres
             if n != 'doc.kml' and n not in capas and not n.endswith('/')]
    if otros:
        return None          # hay recursos con ruta relativa; no tocar
    if not capas[0].startswith('layers/'):
        return None          # no es la forma que escribe LIBKML
    return capas[0]
