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
Versión  : 1.9.0
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
    'blue',
    'green',
    'red',
    'nir',
    'swir16',
    'swir22',
)

# --------------------------------------------------------------------------
# Índices espectrales
# --------------------------------------------------------------------------
# (etiqueta, sufijo, bandas lógicas necesarias, solo_sentinel2)
# Las fórmulas son normalizadas o razones simples: no dependen de coeficientes
# calibrados por sensor, de modo que son directamente comparables entre
# Sentinel-2 y Landsat siempre que se calculen sobre reflectancia, no sobre DN.
#
# EVI2 es la excepcion y por eso lleva su procedencia, como el RVI dual-pol
# del algoritmo de radar: sus coeficientes SI estan calibrados.
#
#   EVI2 = 2.5 * (NIR - rojo) / (NIR + 2.4*rojo + 1)
#
#   Jiang, Huete, Didan y Miura (2008), «Development of a two-band enhanced
#   vegetation index without a blue band», Remote Sensing of Environment
#   112(10) 3833-3845, ecuacion 15. doi:10.1016/j.rse.2008.06.006
#
# Es la version de dos bandas del EVI de tres de Huete et al. (2002), cuya
# forma es G*(NIR - rojo)/(NIR + 6*rojo - 7.5*azul + 1). El 2.4 no es un
# numero libre: sale de 6 - 7.5/c con c = 2.08, la razon rojo/azul que los
# autores encuentran optima. ESA es la suposicion en la que se apoya, igual
# que el RVI dual-pol se apoya en sHH ~ sVV, y es el limite de la medida:
# donde la razon rojo/azul se aparte mucho de 2.08, EVI2 y EVI divergen.
#
# Se prefiere al EVI de tres bandas por dos razones, y la segunda es de este
# codigo:
#  1. El azul entra en el EVI con coeficiente 7.5, el mayor de la familia, y
#     es la banda mas ruidosa. Su papel era corregir el aerosol residual, que
#     en un producto de Nivel 2 ya viene corregido; lo que queda se amplifica
#     por 7.5. Los autores miden que, sobre pixeles de buena calidad y
#     corregidos, EVI y EVI2 difieren casi siempre menos de 0.02.
#  2. Este complemento NO trunca la reflectancia negativa --el desplazamiento
#     BOA de la linea base 04.00 es de -0.1 y se aplica a proposito--. Con el
#     azul negativo, el termino -7.5*azul del EVI se vuelve una suma positiva
#     grande y el indice colapsa hacia cero sobre agua, sombra profunda y
#     cicatrices de quema: un numero plausible y equivocado, que es la clase
#     de fallo que este codigo ya ha pagado varias veces. Sin banda azul, el
#     problema no existe.
#
# Los autores calibraron sobre MODIS, de modo que la relacion entre EVI2 y
# EVI puede variar con la respuesta espectral de cada sensor. Citelo como
# «EVI2», no como «EVI».
INDICES_ESPECTRALES = [
    ("NDVI — vigor general (NIR, Rojo)", "NDVI",
     ('nir', 'red'), False),
    ("SAVI — NDVI ajustado por suelo descubierto (L=0.5)", "SAVI",
     ('nir', 'red'), False),
    # EVI2 va aqui, junto al SAVI, porque atiende el otro extremo del mismo
    # problema: el SAVI corrige el lado del suelo descubierto y el EVI2 el
    # lado de la cubierta densa, donde el NDVI se satura.
    ("EVI2 — vigor sin saturarse en cubierta densa (NIR, Rojo)", "EVI2",
     ('nir', 'red'), False),
    ("NDMI — humedad de la cubierta (NIR, SWIR1)", "NDMI",
     ('nir08', 'swir16'), False),
    ("NBR — estructura y quema (NIR, SWIR2)", "NBR",
     ('nir08', 'swir22'), False),
    ("MSI — estrés hídrico (SWIR1/NIR)", "MSI",
     ('swir16', 'nir08'), False),
    ("NDRE — clorofila borde rojo (solo Sentinel-2)", "NDRE",
     ('nir08', 'rededge1'), True),
    ("CIre — índice de clorofila borde rojo (solo Sentinel-2)", "CIre",
     ('rededge3', 'rededge1'), True),
    # En el orden canonico de Kauth-Thomas --brillo, verdor, humedad-- que
    # es tambien el de los coeficientes de TC_COEFICIENTES (B, G, W).
    # Estaban al reves, de modo que quien cruzara la lista con la
    # bibliografia encontraba la primera componente en el ultimo sitio.
    #
    # Las tres llevan ahora su glosa: antes solo la tenia «Wetness», asi
    # que el unico termino explicado estaba junto a dos sin explicar.
    ("Tasseled Cap — Brillo (suelo desnudo y superficies claras)", "TCB",
     TC_ORDEN_BANDAS, False),
    ("Tasseled Cap — Verdor (densidad de la vegetación)", "TCG",
     TC_ORDEN_BANDAS, False),
    ("Tasseled Cap — Humedad (agua en la cubierta y el suelo, sombra)", "TCW",
     TC_ORDEN_BANDAS, False),
]

# Coeficientes publicados del EVI2 (Jiang et al. 2008, ec. 15). Con nombre
# y no incrustados en la fórmula porque son valores CALIBRADOS, igual que
# TC_COEFICIENTES: un número calibrado suelto en una expresión no se puede
# comprobar contra su fuente.
EVI2_G = 2.5     # ganancia
EVI2_C = 2.4     # = 6 - 7.5/c con c = 2.08, la razón rojo/azul óptima
EVI2_L = 1.0     # término de ajuste de suelo del denominador


def evi2(nir, rojo):
    """EVI2 de Jiang et al. (2008): vigor que no se satura en dosel denso.

    La fórmula vive aquí, y no junto a las otras siete del algoritmo,
    porque es la unica con coeficientes calibrados: aquí se puede probar
    sin QGIS contra los valores publicados. Ver la nota de procedencia
    sobre INDICES_ESPECTRALES.

    Acepta escalares o arreglos de numpy. El denominador no necesita
    guarda: con reflectancia por encima del suelo del desplazamiento BOA
    (-0.1) su mínimo es 0.66, de modo que no se acerca a cero. Esa es
    justamente la ventaja sobre un índice normalizado.
    """
    return EVI2_G * (nir - rojo) / (nir + EVI2_C * rojo + EVI2_L)


# Rango físico admisible de cada índice. Fuera de él, el valor no es una
# medida sino un artefacto de un denominador cercano a cero (ver
# _acotar_indice). Los componentes Tasseled Cap no se acotan: son
# combinaciones lineales, no razones, y su rango depende del conjunto de
# coeficientes.
LIMITES_INDICE = {
    'NDVI': (-1.0, 1.0),
    'SAVI': (-1.5, 1.5),      # el factor 1.5 de la fórmula amplía la cota
    # El 2.5 es la ganancia G, que es el supremo cuando NIR crece sin
    # limite con el rojo en su minimo. Medido por barrido sobre reflectancia
    # de -0.1 a 1.6 (nube brillante), el recorrido real es [-0.90, 1.80];
    # se deja la cota teorica para no recortar nunca un valor legitimo.
    # A diferencia de los indices normalizados, el denominador de EVI2
    # --NIR + 2.4*rojo + 1-- no puede acercarse a cero: con el suelo del
    # desplazamiento BOA (-0.1) su minimo es 0.66, asi que la patologia que
    # motivo _acotar_indice no se da aqui.
    'EVI2': (-1.0, 2.5),
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
    'visual': 10.0,
    'blue': 10.0,
    'green': 10.0,
    'red': 10.0,
    'rededge1': 20.0,
    'rededge2': 20.0,
    'rededge3': 20.0,
    'nir': 10.0,
    'nir08': 20.0,
    'swir16': 20.0,
    'swir22': 20.0,
    'scl': 20.0,
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
                f"recursos; omitida.")
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
    caso de cualquier estación seca que cruce el fin de año).
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


def _documento_html(titulo, partes, generado, generado_por, avisos=(),
                    pie=''):
    """Envoltorio común de los informes: cabecera, avisos, CSS y pie.

    Sale de informe_html() para que el informe de índices use el MISMO
    documento en vez de una copia que se va separando. El CSS vive en
    una sola cadena por la misma razón.

    Sin dependencias: ni plotly ni CDN ni fuentes remotas. Un informe de
    auditoría tiene que poder abrirse dentro de diez años y en una
    máquina sin red, que es justo cuando hace falta.
    """
    bloque_avisos = ''.join(
        f'<div class="aviso">{_esc(a)}</div>\n' for a in (avisos or ()))
    titulo = _esc(titulo or 'Informe')
    return (
        '<!DOCTYPE html>\n<html lang="es">\n<head>\n'
        '<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, '
        'initial-scale=1">\n'
        f'<title>{titulo}</title>\n<style>{_CSS_INFORME}</style>\n'
        '</head>\n<body>\n'
        f'<h1>{titulo}</h1>\n'
        f'<p class="sub">Generado {_esc(generado)} por '
        f'{_esc(generado_por)}</p>\n'
        f'{bloque_avisos}'
        + ''.join(partes) +
        f'<footer>{_esc(pie)}</footer>\n'
        '</body>\n</html>\n')


_PIE_AUDITORIA = (
    'Informe de auditoría: registra con qué datos y con qué parámetros se '
    'produjo cada archivo, para poder repetirlo o revisarlo más tarde.')


def informe_html(datos):
    """Informe de auditoría de un lote, en HTML autocontenido.

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
    return _documento_html(
        datos.get('titulo') or 'Informe', partes,
        datos.get('generado'), datos.get('generado_por'),
        datos.get('avisos') or [], _PIE_AUDITORIA)


# --------------------------------------------------------------------------
# Informe de índices: dónde cae un valor DENTRO de la escena del usuario
# --------------------------------------------------------------------------
def estadisticas_indice(valores):
    """Distribución de un índice ya enmascarado: dónde cae cada valor.

    Es la respuesta a la pregunta que el usuario tiene de verdad frente a
    un ráster gris --«¿0.45 es alto o bajo aquí?»--, y la unica forma
    honesta de contestarla: con SU escena, no con una tabla ajena.

    Se calcula sobre el subconjunto FINITO y con np.percentile, no con
    las funciones nan*. No es un detalle de estilo: «All-NaN slice
    encountered» lo emite warnings.warn, que np.errstate NO silencia, y
    sobre un recorte enteramente nublado saldria una vez por llamada.
    Filtrando antes, el caso vacio se contesta sin warning y sin rama de
    excepcion.
    """
    arr = np.asarray(valores, dtype='float64').ravel()
    total = int(arr.size)
    finitos = arr[np.isfinite(arr)]
    n = int(finitos.size)
    if not n:
        return {'n': 0, 'total': total, 'pct': 0.0, 'min': None, 'p2': None,
                'p25': None, 'mediana': None, 'p75': None, 'p98': None,
                'max': None}
    p2, p25, p50, p75, p98 = (
        float(v) for v in np.percentile(finitos, [2, 25, 50, 75, 98]))
    return {'n': n, 'total': total,
            'pct': 100.0 * n / total if total else 0.0,
            'min': float(finitos.min()), 'p2': p2, 'p25': p25,
            'mediana': p50, 'p75': p75, 'p98': p98,
            'max': float(finitos.max())}


# Rangos ORIENTATIVOS de NDVI por tipo de cubierta.
#
# Tres decisiones, y las tres son por lo que esta tabla NO va a ser:
#
#  1. Pocas filas y anchas, no diez tajadas iguales. Una tabla de diez
#     rangos promete una resolucion que el indice no tiene.
#  2. La ambiguedad en SU PROPIA COLUMNA. Las tablas que circulan la
#     entierran en un «o» --«cobertura media-baja, vigor bajo O cobertura
#     baja, vigor alto»--, y quien buscaba una consulta lee la primera
#     rama y para. El NDVI confunde CANTIDAD de cubierta con ESTADO de la
#     cubierta, y eso tiene que estar en la misma linea que el numero.
#  3. Solo NDVI. Para los otros diez indices no circulan rangos
#     comparables, e inventarlos seria peor que no tenerlos.
#
# No se cita fuente porque no se tiene una verificada, y eso se dice en
# el informe en vez de atribuirlas a una autoridad que no se comprobo:
# este complemento acaba de retirar una cifra por ese mismo motivo.
RANGOS_NDVI = (
    ('Agua libre, sombra profunda', None, 0.0,
     'Nube no enmascarada, nieve'),
    ('Suelo desnudo, roca, arena, superficie construida', 0.0, 0.2,
     'Vegetación seca o senescente'),
    ('Herbácea rala, cultivo recién emergido', 0.2, 0.4,
     'Cubierta densa con vigor bajo'),
    ('Cultivo en desarrollo, pasto en crecimiento', 0.4, 0.6,
     'Cubierta rala con vigor alto'),
    ('Cultivo cerrado, pasto vigoroso, regeneración', 0.6, 0.8,
     'Cubierta media con vigor muy alto'),
    ('Dosel cerrado', 0.8, None,
     'Zona de saturación: el NDVI deja de resolver dentro de este tramo. '
     'Para separar ahí, use EVI2'),
)


# Qué mide cada índice y hacia dónde. Lo que SÍ está establecido para los
# once es la DIRECCIÓN de la respuesta; lo que no, los límites numéricos de
# clase. De ahí que esta tabla dé dirección y no tramos: no hay nada que
# inventar y no hay nada que envejezca.
#
# Está pensada para quien no es especialista en teledetección, que es la
# mayoría de quien abre el informe. La columna «Cuidado» es la importante:
# cada índice tiene una confusión caracteristica, y es la que convierte un
# mapa bonito en una conclusión equivocada.
#
# Clave = sufijo del archivo, de modo que la fila se encuentra desde el
# nombre del GeoTIFF que el usuario tiene delante.
GUIA_INDICES = {
    'NDVI': (
        'Cantidad de vegetación verde (contraste entre el infrarrojo '
        'cercano y el rojo)',
        'Mucha cubierta verde y activa',
        'Poca o ninguna vegetación verde',
        'Se satura en cubierta densa: pasado cierto punto, más biomasa ya '
        'no lo sube. Y no separa CUÁNTA cubierta hay de EN QUÉ ESTADO '
        'está, así que un mismo valor admite las dos lecturas.'),
    'SAVI': (
        'Lo mismo que el NDVI, descontando el suelo que se ve entre las '
        'plantas',
        'Mucha cubierta verde',
        'Poca vegetación',
        'Solo mejora al NDVI donde hay suelo descubierto a la vista. Con '
        'cubierta cerrada da prácticamente lo mismo, y se satura igual.'),
    'EVI2': (
        'Vigor de la vegetación, sin saturarse en cubierta densa',
        'Mucha biomasa verde; sigue respondiendo donde el NDVI ya no',
        'Poca vegetación',
        'Su recorrido NO es de −1 a 1: sobre vegetación va de 0 a ≈0.9. '
        'Sus coeficientes están calibrados sobre MODIS y suponen una razón '
        'rojo/azul de 2.08.'),
    'NDMI': (
        'Agua retenida en la cubierta (infrarrojo cercano frente al SWIR 1)',
        'Cubierta hidratada',
        'Cubierta seca, o poca cubierta',
        'Donde hay poca cubierta mide la humedad del SUELO, no de un dosel '
        'que no existe. Un valor bajo puede ser sequía o puede ser que no '
        'haya vegetación: no distingue los dos casos.'),
    'NBR': (
        'Estructura de la vegetación y material quemado (infrarrojo cercano '
        'frente al SWIR 2)',
        'Vegetación en pie',
        'Superficie quemada o suelo expuesto',
        'Una sola fecha NO mide severidad de quema. Para eso hace falta la '
        'diferencia entre antes y después del fuego (dNBR), que es otra '
        'cantidad y tiene sus propios umbrales publicados.'),
    'MSI': (
        'Estrés hídrico (razón SWIR 1 / infrarrojo cercano). Va al REVÉS de '
        'los demás',
        'Cubierta seca o estresada',
        'Cubierta hidratada',
        'Se invierte respecto a los demás índices: aquí un valor ALTO es '
        'mala señal. Es una razón sin cota superior, y sobre suelo '
        'descubierto da valores altos que no son el estrés de ninguna '
        'planta.'),
    'NDRE': (
        'Indicador relativo de clorofila, por el borde rojo',
        'Más clorofila que el resto de la escena',
        'Menos clorofila: senescencia o deficiencia',
        'NO es una medida de contenido de clorofila, solo un indicador '
        'para comparar dentro de la misma escena: los índices de banda '
        'ancha no dan un valor en mg g⁻¹, y los índices de banda estrecha '
        'hechos a propósito para eso explican alrededor de la mitad de la '
        'variación incluso en el cultivo con el que se calibraron. '
        'Solo Sentinel-2: Landsat no tiene borde rojo. Responde al ESTADO '
        'de la hoja más que a cuánta cubierta hay, de modo que para '
        'cartografía de cobertura dice menos que el NDMI o el NBR.'),
    'CIre': (
        'Indicador relativo de clorofila, por el borde rojo, como razón',
        'Más clorofila que el resto de la escena',
        'Menos clorofila',
        'NO es una medida de contenido de clorofila: vale para comparar '
        'dentro de la misma escena, no para dar una cifra. Solo '
        'Sentinel-2. Sin cota superior. Lleva el borde rojo 1 en el '
        'denominador, así que sobre cubierta escasa amplifica el ruido.'),
    'TCB': (
        'Brillo: suma ponderada de toda la reflectancia',
        'Suelo descubierto, arena, roca, superficie construida',
        'Agua, dosel denso, sombra',
        'Los coeficientes son propios de cada sensor, de modo que un valor '
        'NO es comparable entre Sentinel-2 y Landsat ni entre Landsat '
        'distintos. Dos de los cuatro conjuntos se derivaron sobre '
        'reflectancia TOA y aquí se aplican a reflectancia de superficie; '
        'el registro lo avisa en cada ejecución.'),
    'TCG': (
        'Verdor: el eje de la vegetación, independiente del brillo y de la '
        'humedad',
        'Vegetación',
        'Sin vegetación',
        'Como las otras dos componentes, sus coeficientes son propios de '
        'cada sensor: no compare el valor entre sensores distintos.'),
    'TCW': (
        'Humedad: contraste entre visible+NIR y los dos SWIR',
        'Agua en la cubierta y en el suelo, sombra',
        'Seco',
        'Responde también a la sombra que una superficie rugosa proyecta '
        'sobre sí misma, que puede leerse como humedad. Coeficientes '
        'propios de cada sensor, así que el valor no es comparable entre '
        'sensores distintos.'),
}

COLS_GUIA = ('Índice', 'Qué mide', 'Valor alto', 'Valor bajo', 'Cuidado')


def sufijos_presentes(filas):
    """Sufijos de índice que aparecen en la tabla de distribución.

    Se leen de las propias filas --primera celda-- y no de un parametro
    aparte, para que el informe no pueda hablar de un indice que no
    calculo. Un «AMPL_NDVI» cuenta como NDVI: la amplitud es del mismo
    indice y la guia que le sirve es la misma.
    """
    vistos = []
    for fila in filas or []:
        if not fila:
            continue
        nombre = str(fila[0])
        if nombre.startswith('AMPL_'):
            nombre = nombre[len('AMPL_'):]
        if nombre not in vistos:
            vistos.append(nombre)
    return vistos


def _filas_guia(sufijos):
    """Filas de la guía, en el orden canónico de INDICES_ESPECTRALES."""
    pedidos = set(sufijos or ())
    orden = [e[1] for e in INDICES_ESPECTRALES]
    return [[s] + list(GUIA_INDICES[s])
            for s in orden if s in pedidos and s in GUIA_INDICES]


# ==========================================================================
# dNBR: severidad de quema entre una fecha anterior y otra posterior
# ==========================================================================
# Los umbrales son los de Key y Benson (2006), «Landscape Assessment» en el
# manual FIREMON del USFS, adoptados entre otros por el EFFIS de Copernicus
# y por el MTBS.
#
# OJO CON LAS UNIDADES, que es la trampa de esta tabla: la literatura los
# publica multiplicados por 1000 --«100 a 270»-- porque trabaja con el
# indice escalado a enteros. Aqui el NBR se calcula en unidades de indice,
# de modo que los mismos umbrales son 0.100 a 0.270. Confundirlos no da
# error: da un mapa donde TODO sale «sin quemar», porque ningun dNBR real
# llega a 100 en unidades de indice.
#
# Y OJO CON A QUE SE APLICAN. Son para el dNBR de un incendio concreto:
# NBR de antes menos NBR de despues del MISMO fuego. NO valen para la
# amplitud estacional AMPL_NBR, que esta construida igual --una resta de
# dos NBR-- pero mide otra cosa: la diferencia entre estacion seca y
# lluviosa de una cubierta que no se quemo. Trasladar los umbrales de una
# a la otra es exactamente el error de autoridad falsa que este
# complemento viene corrigiendo.
CLASES_DNBR = (
    ('Sin quemar o rebrote', None, 0.100),
    ('Severidad baja', 0.100, 0.270),
    ('Severidad moderada-baja', 0.270, 0.440),
    ('Severidad moderada-alta', 0.440, 0.660),
    ('Severidad alta', 0.660, None),
)

# Condiciones bajo las que los umbrales de arriba significan algo. Se
# publican junto a la tabla en el informe, porque una tabla de umbrales
# sin sus condiciones es una invitacion a aplicarla donde no vale.
CONDICIONES_DNBR = (
    ('Mismo sensor en las dos fechas',
     'Un NBR de Sentinel-2 y otro de Landsat no se restan: las bandas no '
     'son las mismas y la diferencia arrastraria ese desajuste como si '
     'fuera severidad.'),
    ('Las dos escenas despejadas sobre el área quemada',
     'Una nube en cualquiera de las dos fechas deja NaN, y una nube sin '
     'enmascarar se parece a una quema: ambas bajan el NBR.'),
    ('La diferencia es pre menos post',
     'Así una quema da dNBR POSITIVO. Al revés, los umbrales publicados '
     'no aplican y el signo invierte la lectura.'),
    ('Umbrales derivados en otros ecosistemas',
     'Key y Benson los calibraron contra el índice de severidad de campo '
     '(CBI) en bosques de Norteamérica. En otra vegetación conservan el '
     'orden pero no necesariamente los cortes: valídelos con datos de '
     'campo antes de informar áreas por clase.'),
    ('Fuera del área quemada el dNBR no es cero',
     'Entre las dos fechas la vegetación sana también cambia —fenología, '
     'riego, cosecha—, y eso aparece como dNBR distinto de cero. Delimite '
     'el perímetro del incendio antes de contar áreas por clase; si no, '
     'la clase «severidad baja» recoge media escena.'),
    ('No se aplican a AMPL_NBR',
     'La amplitud estacional está construida igual pero mide otra cosa: '
     'la diferencia seca menos lluviosa de una cubierta que no se quemó.'),
)

EVALUACIONES_DNBR = ('inicial', 'extendida')
DIAS_ANIO = 365


def _a_fecha(texto):
    """'AAAA-MM-DD' a datetime.date, o None si no se puede."""
    try:
        return datetime.date.fromisoformat(str(texto)[:10])
    except (TypeError, ValueError):
        return None


def elegir_escenas_dnbr(fechas, fecha_incendio, evaluacion='inicial'):
    """Escoge la escena anterior y la posterior al fuego.

    Devuelve un dict con 'pre', 'post', 'dias' --los transcurridos entre
    el fuego y la escena posterior-- y 'motivo' cuando no se puede.

    La escena ANTERIOR es siempre la más próxima antes del fuego: cuanto
    más cerca, menos cambio ajeno al incendio se cuela en la resta.

    La POSTERIOR depende de la convención, y las dos miden cosas
    distintas, que es la razón de que el usuario elija:
      - «inicial»: la más próxima después. Recoge el carbón y la ceniza
        recién depositados.
      - «extendida»: la más cercana a un año después. Recoge la mortalidad
        diferida y lo que de verdad sobrevivió al primer rebrote. Es la
        que usa el MTBS para la mayoría de sus incendios.

    Una escena de la fecha EXACTA del incendio se descarta de las dos
    listas a proposito: el fuego pudo estar activo durante la toma, y
    entonces no es ni el antes ni el despues.
    """
    dia = _a_fecha(fecha_incendio)
    if dia is None:
        return {'pre': None, 'post': None, 'dias': None,
                'motivo': 'la fecha del incendio no se entiende; use '
                          'AAAA-MM-DD'}
    if evaluacion not in EVALUACIONES_DNBR:
        evaluacion = 'inicial'

    validas = sorted({f for f in (
        (_a_fecha(x), str(x)[:10]) for x in (fechas or []))
        if f[0] is not None}, key=lambda p: p[0])
    antes = [p for p in validas if p[0] < dia]
    despues = [p for p in validas if p[0] > dia]

    if not antes:
        return {'pre': None, 'post': None, 'dias': None,
                'motivo': f'no hay ninguna escena anterior al {dia}; '
                          f'amplíe el rango de fechas hacia atrás'}
    if not despues:
        return {'pre': None, 'post': None, 'dias': None,
                'motivo': f'no hay ninguna escena posterior al {dia}; '
                          f'amplíe el rango de fechas hacia adelante'}

    pre = antes[-1]
    if evaluacion == 'extendida':
        objetivo = dia + datetime.timedelta(days=DIAS_ANIO)
        post = min(despues, key=lambda p: abs((p[0] - objetivo).days))
    else:
        post = despues[0]

    return {'pre': pre[1], 'post': post[1],
            'dias': (post[0] - dia).days,
            'dias_pre': (dia - pre[0]).days,
            'evaluacion': evaluacion, 'motivo': ''}


def clasificar_dnbr(valores, area_pixel_m2=None):
    """Reparte un dNBR en las clases de severidad y cuenta cada una.

    Devuelve una lista de filas listas para la tabla del informe. El
    area se da en hectareas cuando se conoce el tamano del pixel; sin
    el, solo el recuento. Se cuenta sobre los pixeles FINITOS: los NaN
    son nube o falta de dato en alguna de las dos fechas, y meterlos en
    «sin quemar» inflaria justo la clase que ya recoge de todo.
    """
    arr = np.asarray(valores, dtype='float64').ravel()
    finitos = arr[np.isfinite(arr)]
    total = int(finitos.size)
    filas = []
    for nombre, lo, hi in CLASES_DNBR:
        if lo is None:
            masc = finitos < hi
        elif hi is None:
            masc = finitos >= lo
        else:
            masc = (finitos >= lo) & (finitos < hi)
        n = int(masc.sum())
        pct = (100.0 * n / total) if total else 0.0
        ha = ('' if area_pixel_m2 in (None, 0)
              else miles(n * float(area_pixel_m2) / 10000.0, 2))
        filas.append([nombre, _rango_dnbr_texto(lo, hi), miles(n),
                      f'{pct:.1f}', ha])
    return filas


def _rango_dnbr_texto(lo, hi):
    """El tramo, en unidades de índice y con el x1000 de la literatura."""
    if lo is None:
        return f'< {hi:.3f}  (< {hi * 1000:.0f})'
    if hi is None:
        return f'≥ {lo:.3f}  (≥ {lo * 1000:.0f})'
    return f'{lo:.3f} – {hi:.3f}  ({lo * 1000:.0f} – {hi * 1000:.0f})'


def _rango_texto(lo, hi):
    """«< 0.0», «0.0 – 0.2» o «> 0.8», con la convención del complemento."""
    if lo is None:
        return f'< {hi:.1f}'
    if hi is None:
        return f'> {lo:.1f}'
    return f'{lo:.1f} – {hi:.1f}'


def _filas_rangos_ndvi():
    return [(_rango_texto(lo, hi), cubierta, tambien or '—')
            for cubierta, lo, hi, tambien in RANGOS_NDVI]


COLS_DISTRIBUCION = ('Índice', 'Escena', 'Píxeles válidos', '% válidos',
                     'Mín', 'p2', 'p25', 'Mediana', 'p75', 'p98', 'Máx')


def _fila_estadisticas(indice, escena, est):
    """Una fila de la tabla de distribución, ya formateada.

    Tiene que medir exactamente len(COLS_DISTRIBUCION). Una fila corta no
    da error en ningun sitio: _tabla_cols_html la escribe igual y la
    tabla sale descuadrada en el informe, con los numeros bajo la columna
    de al lado. Hay una prueba que compara las dos longitudes, en los dos
    caminos --con datos y sin ellos--, porque el camino vacio es el que
    no se ve al revisar un informe normal.
    """
    if not est['n']:
        return ([indice, escena, '0', '0.0'] +
                ['—'] * (len(COLS_DISTRIBUCION) - 4))
    return [indice, escena, miles(est['n']), f"{est['pct']:.1f}",
            f"{est['min']:.3f}", f"{est['p2']:.3f}", f"{est['p25']:.3f}",
            f"{est['mediana']:.3f}", f"{est['p75']:.3f}",
            f"{est['p98']:.3f}", f"{est['max']:.3f}"]


def informe_indices_html(datos):
    """Informe de los índices escritos: su distribución, escena por escena.

    Vive en core.py por la misma razón que informe_html: es construcción
    de texto y se prueba sin QGIS. Comparte con él el documento y el CSS.
    """
    filas = datos.get('distribucion') or []
    # Solo se habla de los indices que el usuario PIDIO. El informe
    # sacaba la tabla de tramos de NDVI siempre, de modo que quien
    # calculaba solo NDMI recibia tramos de un indice que no tenia, al
    # lado de sus numeros: justo la lectura equivocada que esa tabla
    # existe para evitar.
    presentes = sufijos_presentes(filas)
    partes = [
        _tabla_cols_html(
            'Distribución de cada índice', COLS_DISTRIBUCION, filas,
            'Calculado sobre los píxeles válidos de SU recorte, '
            'descontados nube, sombra de nube y sin dato. Aquí está la '
            'respuesta a «¿0.45 es alto o bajo?»: alto o bajo respecto a '
            'esta escena, que es la única comparación que no depende de '
            'una tabla ajena. Los percentiles 2 y 98 son los mismos que '
            'usa el realce de la simbología, así que explican el color '
            'que ve en el lienzo.'),
        _tabla_html(
            'dNBR — escenas usadas', datos.get('dnbr_escenas') or [],
            'La resta es ANTES menos DESPUÉS, de modo que una quema sale '
            'POSITIVA: es el signo con el que están publicados los '
            'umbrales.'),
        _tabla_cols_html(
            'dNBR — severidad de quema',
            ('Clase', 'dNBR (unidades de índice, y ×1000 como lo publica '
             'la literatura)', 'Píxeles', '% de los válidos', 'Hectáreas'),
            datos.get('dnbr_clases') or [],
            'Clases de Key y Benson (2006), «Landscape Assessment» del '
            'manual FIREMON del USFS, adoptadas entre otros por el EFFIS de '
            'Copernicus y por el MTBS. Las áreas valen SOLO dentro del '
            'perímetro del incendio: delimítelo antes de informarlas. Lea '
            'las condiciones de abajo antes de usar estos números.'),
        _tabla_cols_html(
            'dNBR — condiciones bajo las que estos umbrales significan algo',
            ('Condición', 'Por qué'),
            [[t, d] for t, d in CONDICIONES_DNBR]
            if datos.get('dnbr_clases') else [],
            'Una tabla de umbrales sin sus condiciones es una invitación a '
            'aplicarla donde no vale.'),
        _tabla_cols_html(
            'Cómo se lee cada índice', COLS_GUIA, _filas_guia(presentes),
            'Esta tabla dice qué mide cada índice y en qué dirección '
            'responde, para quien no trabaja en teledetección a diario. '
            'Da direcciones y no tramos numéricos porque la dirección de '
            'la respuesta sí está establecida y los límites de clase no lo '
            'están: dónde empieza una clase lo decide su área de estudio. '
            'Fíjese sobre todo en la columna «Cuidado»: cada índice tiene '
            'una confusión característica, y es esa confusión la que '
            'convierte un mapa convincente en una conclusión equivocada.'),
        _tabla_cols_html(
            'Rangos orientativos de NDVI',
            ('NDVI', 'Cubierta típica', 'Pero también puede ser'),
            _filas_rangos_ndvi() if 'NDVI' in presentes else [],
            'Estos tramos son ORIENTATIVOS y nada más: no son una '
            'clasificación publicada ni se derivaron de su área. La '
            'tercera columna explica por qué no pueden serlo: el NDVI '
            'confunde la cantidad de cubierta con su estado, de modo que '
            'un mismo valor admite dos lecturas. Los límites se mueven '
            'además con el sensor (la banda roja de Landsat no es la de '
            'Sentinel-2), con la corrección atmosférica, con la fenología '
            'y con la geometría de iluminación. Para decidir clases, '
            'guíese por la tabla de arriba —que sí es de su escena— y por '
            'verdad de campo. Solo se dan para NDVI: para los demás '
            'índices no circulan rangos comparables, e inventarlos sería '
            'peor que no tenerlos.'),
        _tabla_html('Parámetros de la ejecución', datos.get('parametros')
                    or []),
        _tabla_html('Entorno', datos.get('entorno') or []),
        _tabla_html('Fuentes y licencias', datos.get('fuentes') or []),
    ]
    return _documento_html(
        datos.get('titulo') or 'Índices espectrales', partes,
        datos.get('generado'), datos.get('generado_por'),
        datos.get('avisos') or [],
        'Dónde cae cada valor dentro de su propia escena. Un índice no '
        'es una clase: la correspondencia la decide el área de estudio y '
        'la verdad de campo.')


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


# Grados a metros: radio medio de la Tierra. Para un aviso de orden de
# magnitud no hace falta el elipsoide.
_M_POR_GRADO_LAT = 110574.0
_M_POR_GRADO_LON = 111320.0

# A partir de cuantos grados de lado conviene avisar de que la extension
# es la de la capa entera y seguramente no es lo que se queria. Diez
# grados son unos 1100 km: ya no es un area de trabajo, es un continente.
GRADOS_EXTENSION_SOSPECHOSA = 10.0


def metros_por_pixel(oeste, sur, este, norte, cols, filas):
    """(m/px en x, m/px en y) de una imagen en grados, en su centro.

    Es el numero que dice si una exportacion sirve de algo ANTES de
    abrirla: un basemap WMTS declara extension mundial, y 2048 px
    repartidos en 360 grados dan ~19 km por pixel. El sitio que el
    usuario queria ver ocupa entonces menos de un pixel.

    En x se corrige por el coseno de la latitud central, porque un grado
    de longitud no mide lo mismo en el ecuador que en Finlandia.
    """
    try:
        cols = int(cols)
        filas = int(filas)
    except (TypeError, ValueError):
        return None, None
    if cols <= 0 or filas <= 0:
        return None, None
    ancho = float(este) - float(oeste)
    alto = float(norte) - float(sur)
    if ancho <= 0 or alto <= 0:
        return None, None
    lat_centro = (float(norte) + float(sur)) / 2.0
    cos_lat = math.cos(math.radians(max(-89.9, min(89.9, lat_centro))))
    mx = ancho * _M_POR_GRADO_LON * cos_lat / cols
    my = alto * _M_POR_GRADO_LAT / filas
    return mx, my


def extension_sospechosa(oeste, sur, este, norte):
    """¿La extension es tan grande que seguramente no es la pedida?"""
    try:
        ancho = abs(float(este) - float(oeste))
        alto = abs(float(norte) - float(sur))
    except (TypeError, ValueError):
        return False
    return max(ancho, alto) > GRADOS_EXTENSION_SOSPECHOSA


# Proveedores cuyo extent() es el del SERVICIO, no el de los datos que el
# usuario esta mirando. Un WMTS mundial declara el mundo aunque en el
# lienzo se vea una finca.
PROVEEDORES_REMOTOS = ('wms', 'wcs', 'xyz', 'arcgismapserver',
                       'arcgisrestserver')


def es_proveedor_remoto(nombre):
    """¿Es un proveedor de teselas/mapa remoto servido por la red?"""
    return str(nombre or '').strip().lower() in PROVEEDORES_REMOTOS


# Espacio fino e indivisible: el separador de millar de la tipografia
# espanola. Indivisible para que «19 568» no se parta al final de linea.
ESPACIO_FINO = '\u202f'


def miles(valor, decimales=0):
    """Numero en la convencion UNICA del complemento: espacio fino para
    el millar, PUNTO para el decimal. La coma no se usa ni como decimal
    ni como separador de millar.

    Punto decimal, aunque la convencion del espanol sea la coma, porque
    estos numeros se copian: a un campo de extension o de SRC de QGIS, a
    la consola de Python, a una hoja de calculo. Un «-83,68» se pega mal
    en todos ellos, y falla callado --se parte en dos valores o se lee
    como otro numero--. La norma cientifica (ISO 80000-1, NIST SP 811)
    admite punto o coma como marca decimal, asi que elegir el punto no es
    un anglicismo; lo que la norma NO admite es la coma como separador de
    millar, y de eso venia el defecto que esta funcion existe para cerrar.

    Y nunca una coma de millar --el «{:,}» de Python-- porque «10,000»
    leido con la convencion del espanol es DIEZ. Esa cifra aparecia en el
    aviso del tope de entidades de Google Earth Web: quien la leyera como
    diez concluia que su capa de 500 entidades ya pasaba un tope del que
    estaba lejisimos. Un error de mil veces que invierte el consejo.

    Se formatea APARTE de la frase a proposito. Antes el numero se metia
    en la frase y luego se hacia .replace(',', ' ') sobre TODA la cadena:
    el separador de millar quedaba bien y las comas de la prosa
    desaparecian. El usuario leyo «su extension declarada es la del
    servicio no la de lo que usted ve», y el error llego hasta el porque
    nada lo podia detectar: la frase era gramaticalmente plausible.

    La comprobacion «comp_convencion_numerica» de tools/preflight.py
    vigila que no reaparezca una coma por ningun lado.
    """
    try:
        texto = '{:,.{d}f}'.format(float(valor), d=int(decimales))
    except (TypeError, ValueError):
        return ''
    # El punto decimal que pone Python ya es el que queremos; solo hay que
    # cambiar su coma de millar por el espacio fino.
    return texto.replace(',', ESPACIO_FINO)


# La convencion, declarada al usuario. Vive aqui y no copiada en cada
# shortHelpString porque cuatro copias de la misma frase se separan: basto
# que una dijera «coma decimal» mientras el codigo ya ponia punto para que
# la ayuda mintiera. Cada algoritmo la envuelve en su propio marcado
# --unos usan <b>..</b><br>, otro <p>-- pero la frase es una sola.
NOTA_NUMEROS = (
    'Punto (.) para los decimales y espacio para los millares: '
    f'«1{ESPACIO_FINO}234.56». Así los valores del registro se pegan sin '
    'retoques en un campo de extensión o de SRC de QGIS, en la consola de '
    'Python o en una hoja de cálculo. La coma no se usa ni como decimal '
    'ni como separador de millar, '
    'porque «10,000» leído con la convención del español es diez.'
)


# ==========================================================================
# Bandas: una sola tabla, con clave estable
# ==========================================================================
# Antes habia cuatro diccionarios --ALIAS_ASSETS, ALIAS_LS, CLAVES_BANDAS y
# SUFIJO_BANDA-- y los cuatro estaban indexados POR LA ETIQUETA QUE VE EL
# USUARIO. Eso tenia dos consecuencias, y la segunda es seria:
#
#   1. La etiqueta no se podia corregir sin romper las busquedas, asi que
#      decia «red (B04, 10 m)» tambien con Landsat, donde el rojo es B4 (B3
#      en TM y ETM+) y mide 30 m.
#   2. SUFIJO_BANDA convertia esa misma etiqueta en el NOMBRE DEL ARCHIVO.
#      Un rojo de Landsat salia como «..._B04.tif», afirmando un numero de
#      banda de Sentinel-2 sobre datos que no lo son. Y como «nir» y
#      «nir08» apuntan los dos al unico infrarrojo cercano de Landsat,
#      marcar ambos escribia «_B08.tif» y «_B8A.tif» CON LOS MISMOS
#      PIXELES, como si fueran dos bandas distintas.
#
# Aqui la clave es interna y estable, y lo que cambia por sensor --alias
# del asset, numero de banda, resolucion y sufijo de archivo-- vive junto.
#
# El numero de banda de Landsat NO se pone en la etiqueta a proposito: la
# coleccion landsat-c2-l2 cubre de L4 a L9, y la numeracion cambia dentro
# de esa familia (en TM y ETM+ el rojo es B3; en OLI es B4). Lo unico
# cierto para todas es el nombre comun del STAC y los 30 m, asi que es eso
# lo que se dice.

# clave -> (etiqueta corta, alias S2, banda S2, res S2, alias LS, res LS)
_BANDAS = (
    ('visual',   'visual (RGB 8 bits)',
     ['visual', 'TCI', 'tci', 'visual-10m'], 'TCI', 10,  [],          None),
    ('blue',     'azul',
     ['blue', 'B02'],      'B02', 10,  ['blue'],     30),
    ('green',    'verde',
     ['green', 'B03'],     'B03', 10,  ['green'],    30),
    ('red',      'rojo',
     ['red', 'B04'],       'B04', 10,  ['red'],      30),
    ('rededge1', 'borde rojo 1',
     ['rededge1', 'B05'],  'B05', 20,  [],           None),
    ('rededge2', 'borde rojo 2',
     ['rededge2', 'B06'],  'B06', 20,  [],           None),
    ('rededge3', 'borde rojo 3',
     ['rededge3', 'B07'],  'B07', 20,  [],           None),
    ('nir',      'NIR',
     ['nir', 'B08'],       'B08', 10,  ['nir08'],    30),
    ('nir08',    'NIR estrecho',
     ['nir08', 'B8A'],     'B8A', 20,  ['nir08'],    30),
    ('swir16',   'SWIR 1',
     ['swir16', 'B11'],    'B11', 20,  ['swir16'],   30),
    ('swir22',   'SWIR 2',
     ['swir22', 'B12'],    'B12', 20,  ['swir22'],   30),
    ('scl',      'm\u00e1scara de clases',
     ['scl', 'SCL'],       'SCL', 20,  ['qa_pixel'], 30),
)

CLAVES_BANDAS = tuple(b[0] for b in _BANDAS)
ALIAS_S2 = {b[0]: list(b[2]) for b in _BANDAS}
ALIAS_LS = {b[0]: list(b[5]) for b in _BANDAS}
SIN_EQUIVALENTE_LS = tuple(k for k in CLAVES_BANDAS if not ALIAS_LS[k])


def etiqueta_banda(clave):
    """Texto del desplegable: dice lo que vale en CADA sensor.

    «rojo — S2 B04 10 m · Landsat red 30 m». Sin numero de banda para
    Landsat: cambia entre TM/ETM+ y OLI (ver el comentario de _BANDAS).
    """
    for k, nombre, _a2, b2, r2, als, rls in _BANDAS:
        if k != clave:
            continue
        s2 = 'S2 {} {} m'.format(b2, r2) if b2 else 'S2 no'
        ls = ('Landsat {} {} m'.format(als[0], rls) if als
              else 'Landsat no')
        return '{} — {} · {}'.format(nombre, s2, ls)
    return str(clave)


ETIQUETAS_BANDAS = tuple(etiqueta_banda(k) for k in CLAVES_BANDAS)


def alias_banda(clave, familia='s2'):
    """Nombres de asset a probar para esta banda en este sensor."""
    tabla = ALIAS_LS if familia == 'ls' else ALIAS_S2
    return list(tabla.get(clave, [] if familia == 'ls' else [clave]))


def sufijo_banda(clave, familia='s2'):
    """Sufijo del nombre de archivo, con el numero REAL del sensor.

    Sentinel-2 conserva su numero («B04»), que es estable en toda la
    familia. Landsat usa el nombre comun en mayusculas («RED», «NIR08»),
    porque su numeracion cambia entre TM/ETM+ y OLI y poner «B04» sobre
    una escena de Landsat seria afirmar algo falso.
    """
    for k, _n, _a2, b2, _r2, als, _rls in _BANDAS:
        if k != clave:
            continue
        if familia == 'ls':
            return als[0].upper().replace('QA_PIXEL', 'QA') if als else 'X'
        return b2 or 'X'
    return 'X'


def bandas_duplicadas(claves, familia='s2'):
    """Claves distintas que en ESTE sensor resuelven al mismo asset.

    Devuelve [(asset, [claves...]), ...] solo para los que colisionan.
    En Landsat «nir» y «nir08» son los dos la unica banda de infrarrojo
    cercano, asi que pedir las dos escribiria el mismo raster dos veces
    con nombres que sugieren longitudes de onda distintas.
    """
    porasset = {}
    for c in claves:
        alias = alias_banda(c, familia)
        if not alias:
            continue
        porasset.setdefault(alias[0], []).append(c)
    return [(a, cs) for a, cs in sorted(porasset.items()) if len(cs) > 1]


def composiciones_vetadas(composiciones, sin_equivalente=None):
    """(vetadas, disponibles) de una lista de composiciones, para Landsat.

    `composiciones` son tuplas (etiqueta, sufijo, bandas). Devuelve las que
    citan alguna banda que Landsat no tiene, con las bandas que les
    faltan, y las etiquetas de las que si se pueden usar.

    Existe porque el mensaje de rechazo nombraba SEIS composiciones «que
    si tienen equivalente» cuando las ocho funcionan: la lista estaba
    escrita a mano y se quedo atras. Un mensaje de error que miente es
    peor que no tenerlo, porque manda al usuario a buscar un problema que
    no existe. Calculado desde los datos no puede envejecer.

    Hoy ninguna composicion cita borde rojo ni TCI, asi que `vetadas`
    sale vacia siempre. Se conserva como guarda: si alguna vez se anade
    una de borde rojo --el README llego a anunciar una-- el rechazo sale
    solo, y con el texto correcto.
    """
    if sin_equivalente is None:
        sin_equivalente = SIN_EQUIVALENTE_LS
    faltantes = set(sin_equivalente)
    vetadas, disponibles = [], []
    for fila in composiciones or ():
        etiqueta = fila[0]
        bandas = fila[2] if len(fila) > 2 else ()
        faltan = [b for b in (bandas or ()) if b in faltantes]
        if faltan:
            vetadas.append((etiqueta, faltan))
        else:
            disponibles.append(etiqueta)
    return vetadas, disponibles
