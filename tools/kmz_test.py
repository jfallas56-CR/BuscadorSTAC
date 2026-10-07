#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# SPDX-License-Identifier: GPL-2.0-or-later
"""Exporta de verdad un ráster y un vector a KMZ, dentro de un QGIS real.

Es la prueba que faltaba. El smoke test comprueba el armazón --que los
algoritmos se registren y sus parámetros se construyan-- y las baterías
sin QGIS comprueban la lógica pura, pero NADA ejecutaba la exportación a
Google Earth de principio a fin. Un fallo en la tubería de renderizado o
en el controlador KMLSUPEROVERLAY solo aparece al correrla, y entonces
lo encuentra el usuario.

No pide nada por red ni necesita credenciales: el ráster se fabrica aquí
con GDAL, con los valores de un índice espectral (-1 a 1) porque es el
caso que importa --un índice sin renderizador aplicado sale gris
uniforme-- y la capa vectorial se construye en memoria.

    xvfb-run -a python3 tools/kmz_test.py

Autor    : Jorge Fallas (jfallas56@gmail.com)
Licencia : GPL v2 o posterior
Version  : 1.0.0
"""

from __future__ import annotations

import os
import re
import sys
import tempfile
import traceback
import zipfile

RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PAQUETE = 'BuscadorSTAC'
sys.path.insert(0, RAIZ)

fallos = []
hechas = 0


def comp(etiqueta, cond, detalle=''):
    global hechas
    hechas += 1
    if cond:
        print(f'  pasa  {etiqueta}')
    else:
        print(f'  FALLA {etiqueta}')
        if detalle:
            print(f'        {detalle}')
        fallos.append(etiqueta)
    return bool(cond)


def seccion(titulo):
    print(f'\n{titulo}')


def nota(texto):
    """Informa sin contar como comprobacion ni bloquear."""
    print(f'  nota  {texto}')


def _raster_sintetico(ruta, bandas=1, tipo=None, epsg=4326,
                      caja=None):
    """GeoTIFF pequeño sobre Guanacaste, con valores de índice (-1 a 1).

    epsg=32616 reproduce lo que descarga el propio complemento: el recorte
    de Sentinel-2 / Landsat NO pasa dstSRS a gdal.Warp, así que conserva el
    SRC nativo de la escena, que es UTM. El caso 4326 es el fácil y no
    representa lo que el usuario tiene en el proyecto.
    """
    import numpy as np
    from osgeo import gdal, osr

    tipo = gdal.GDT_Float32 if tipo is None else tipo
    cols, filas = 60, 40
    drv = gdal.GetDriverByName('GTiff')
    ds = drv.Create(ruta, cols, filas, bandas, tipo)
    if caja is not None:
        # (oeste, sur, este, norte) explicitos, para el caso de una capa
        # ancha a proposito.
        w, s, e, n = caja
        ds.SetGeoTransform([w, (e - w) / cols, 0.0, n, 0.0, -(n - s) / filas])
    elif epsg == 4326:
        # Un grado de lado, en el Pacífico norte de Costa Rica.
        ds.SetGeoTransform([-85.8, 1.0 / cols, 0.0, 10.6, 0.0, -1.0 / filas])
    else:
        # El mismo sitio, en UTM 16N y con píxel de 30 m.
        ds.SetGeoTransform([630000.0, 30.0, 0.0, 1172000.0, 0.0, -30.0])
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(epsg)
    ds.SetProjection(srs.ExportToWkt())
    for b in range(1, bandas + 1):
        y, x = np.mgrid[0:filas, 0:cols]
        if tipo == gdal.GDT_Float32:
            arr = (-1.0 + 2.0 * (x + y) / float(cols + filas)).astype('f4')
        else:
            arr = ((x + y) % 256).astype('u1')
        ds.GetRasterBand(b).WriteArray(arr)
    ds.FlushCache()
    ds = None
    return ruta


class _Recolector:
    """Guarda lo que el algoritmo informa, para poder enseñarlo al fallar.

    Sin esto el fallo llega como «devolvió False» y el motivo --que lo da
    reportError-- se pierde, que es justo lo que hace falta leer.
    """

    def __init__(self):
        self.info, self.errores = [], []


def _feedback(rec):
    from qgis.core import QgsProcessingFeedback

    class Fb(QgsProcessingFeedback):
        def pushInfo(self, texto):
            rec.info.append(str(texto))

        def pushWarning(self, texto):
            rec.info.append(f'AVISO {texto}')

        def reportError(self, texto, fatalError=False):
            rec.errores.append(str(texto))

    return Fb()


def _resumen(rec):
    partes = []
    if rec.errores:
        # De cada error, la ULTIMA linea. reportError entrega el traceback
        # entero en una sola cadena, y una anotacion de GitHub se corta en
        # el primer salto: quedaba «Traceback (most recent call last):» y
        # nada mas, que es justo la linea que no dice nada.
        ultimas = [e.strip().splitlines()[-1]
                   for e in rec.errores[-3:] if e.strip()]
        partes.append('reportError: ' + ' | '.join(ultimas))
    if rec.info:
        partes.append('ultimo pushInfo: ' + rec.info[-1])
    return ' ;; '.join(partes) or '(el algoritmo no informo nada)'


def _dentro_del_kmz(ruta):
    """(imagenes, kml_con_groundoverlay) del KMZ."""
    imagenes, con_overlay = [], False
    with zipfile.ZipFile(ruta) as z:
        for n in z.namelist():
            if n.lower().endswith(('.png', '.jpg', '.jpeg')):
                imagenes.append(n)
            elif n.lower().endswith('.kml'):
                if b'GroundOverlay' in z.read(n):
                    con_overlay = True
    return imagenes, con_overlay


def _imagen_con_contenido(kmz, imagenes, tmp):
    """¿Alguna imagen del KMZ tiene algo dibujado? (vistas, motivo).

    La comprobacion que importa. Un KMZ con su GroundOverlay y su PNG
    dentro, pero con el PNG transparente o de un color plano, se abre en
    Google Earth sin ningun error y sin que se vea nada --que es la forma
    exacta que tiene «no exporta el raster». Mirar solo si el archivo
    existe da por bueno justo ese caso.
    """
    import numpy as np
    from osgeo import gdal

    vistas, motivos = 0, []
    with zipfile.ZipFile(kmz) as z:
        for n in imagenes[:12]:
            destino = os.path.join(tmp, 'img_' + os.path.basename(n))
            with open(destino, 'wb') as fh:
                fh.write(z.read(n))
            ds = gdal.Open(destino)
            if ds is None:
                motivos.append(f'{n}: GDAL no la abre')
                continue
            vistas += 1
            bandas = [ds.GetRasterBand(i + 1).ReadAsArray()
                      for i in range(ds.RasterCount)]
            # Con canal alfa, lo primero es si hay algo opaco: todo a 0 es
            # una imagen invisible, no una imagen oscura.
            if ds.RasterCount == 4:
                if int(np.max(bandas[3])) == 0:
                    motivos.append(f'{n}: alfa todo a 0 (invisible)')
                    continue
                color = np.dstack(bandas[:3])[bandas[3] > 0]
            else:
                color = np.dstack(bandas)
            if color.size == 0:
                motivos.append(f'{n}: ningun pixel opaco')
                continue
            if int(np.max(color)) == int(np.min(color)):
                motivos.append(f'{n}: color plano ({int(np.max(color))})')
                continue
            return True, f'{n} tiene contenido'
    return False, (f'{vistas} imagen(es) revisadas; '
                   + '; '.join(motivos[:4]))


def _correr(alg, parametros, rec):
    """alg.run() devuelve (resultados, ok). Las excepciones NO se dejan
    escapar: el traceback es el dato que hace falta."""
    from qgis.core import QgsProcessingContext
    ctx = QgsProcessingContext()
    try:
        res, ok = alg.run(parametros, ctx, _feedback(rec))
        return res, ok, ''
    except Exception:                                        # noqa: BLE001
        return {}, False, traceback.format_exc()


def _probar_raster(etiqueta, ruta_tif, tmp, estilo=None):
    from qgis.core import QgsRasterLayer
    from BuscadorSTAC.google_earth_algoritmo import (
        ExportarGoogleEarthAlgorithm)

    capa = QgsRasterLayer(ruta_tif, f'indice_{etiqueta}')
    if not comp(f'{etiqueta}: la capa sintetica carga', capa.isValid()):
        return
    print(f'        SRC de la capa: {capa.crs().authid()}')
    comp(f'{etiqueta}: la capa trae renderizador de serie',
         capa.renderer() is not None,
         'sin renderizador el algoritmo se detiene a proposito')
    if estilo is not None:
        estilo(capa)

    alg = ExportarGoogleEarthAlgorithm()
    alg.initAlgorithm()
    destino = os.path.join(tmp, f'salida_{etiqueta}.kmz')
    rec = _Recolector()
    res, ok, tb = _correr(alg, {
        'CAPA': capa,
        'NOMBRE': 'prueba',
        'PX_LADO': 512,
        'FORMATO': 0,
        'OPACIDAD': 100,
        'ABRIR': False,
        'SALIDA': destino,
    }, rec)

    if not comp(f'{etiqueta}: la exportacion termina sin error', ok,
                (tb.strip().splitlines()[-1] if tb else _resumen(rec))):
        if tb:
            print('        ' + tb.replace('\n', '\n        '))
        return
    if not comp(f'{etiqueta}: el KMZ existe', os.path.isfile(destino),
                f'no se escribio {destino}. {_resumen(rec)}'):
        return
    comp(f'{etiqueta}: el KMZ no esta vacio',
         os.path.getsize(destino) > 1024,
         f'{os.path.getsize(destino)} bytes')

    imagenes, overlay = _dentro_del_kmz(destino)
    if not comp(f'{etiqueta}: el KMZ lleva al menos una imagen',
                bool(imagenes),
                'un KMZ sin PNG/JPG se abre en Google Earth y no se ve '
                'nada; es el fallo que no da error'):
        return
    comp(f'{etiqueta}: el KML declara GroundOverlay', overlay,
         'sin GroundOverlay la imagen no se ancla al suelo')

    hay, motivo = _imagen_con_contenido(destino, imagenes, tmp)
    comp(f'{etiqueta}: la imagen del KMZ tiene contenido', hay, motivo)


def _probar_area_pedida(tmp):
    """«Área a exportar» tiene que mandar sobre la extensión de la capa.

    El caso real: un basemap WMTS de EOX declara extensión MUNDIAL, asi
    que exportar «toda la capa» repartio 2048 px entre 360 grados --19 568
    m por pixel, medido-- y en Google Earth parecia que la imagen no habia
    cargado. Aqui se comprueba con un raster normal que, pidiendo un
    cuadrante, el KMZ cubre ESE cuadrante y no la capa entera, y que el
    aviso de extension completa sale cuando toca.
    """
    from qgis.core import QgsRasterLayer, QgsRectangle
    from BuscadorSTAC.core import extension_sospechosa, metros_por_pixel
    from BuscadorSTAC.google_earth_algoritmo import (
        ExportarGoogleEarthAlgorithm)

    seccion('Raster: el area pedida manda sobre la extension de la capa')
    # Capa ancha a proposito: 20 grados de lado, para que dispare el aviso.
    tif = os.path.join(tmp, 'ancha.tif')
    _raster_sintetico(tif, bandas=3, epsg=4326,
                      caja=(-95.0, 0.0, -75.0, 20.0))
    capa = QgsRasterLayer(tif, 'ancha')
    if not comp('la capa ancha carga', capa.isValid()):
        return

    # 1. Sin area: tiene que avisar de que exporta la capa completa.
    alg = ExportarGoogleEarthAlgorithm()
    alg.initAlgorithm()
    rec = _Recolector()
    _r, ok, tb = _correr(alg, {
        'CAPA': capa, 'NOMBRE': 'ancha', 'PX_LADO': 512, 'FORMATO': 0,
        'OPACIDAD': 100, 'ABRIR': False,
        'SALIDA': os.path.join(tmp, 'ancha_entera.kmz'),
    }, rec)
    if not comp('sin area pedida la exportacion termina sin error', ok,
                (tb.strip().splitlines()[-1] if tb else _resumen(rec))):
        return
    todo = ' '.join(rec.info) + ' ' + ' '.join(rec.errores)
    comp('avisa de que esta exportando la capa completa',
         'extension COMPLETA' in todo or 'extensión COMPLETA' in todo,
         'sin el aviso, 19 km por pixel pasan desapercibidos: ' + todo[-300:])
    comp('informa de los metros por pixel',
         'Resoluci' in todo, todo[-300:])

    # 2. Con area: el KMZ tiene que cubrir ESO.
    alg2 = ExportarGoogleEarthAlgorithm()
    alg2.initAlgorithm()
    destino = os.path.join(tmp, 'ancha_recorte.kmz')
    rec2 = _Recolector()
    pedida = QgsRectangle(-85.0, 9.0, -84.0, 10.0)
    _r2, ok2, tb2 = _correr(alg2, {
        'CAPA': capa, 'NOMBRE': 'recorte', 'PX_LADO': 512, 'FORMATO': 0,
        'OPACIDAD': 100, 'ABRIR': False,
        'EXTENSION': '%f,%f,%f,%f [EPSG:4326]' % (
            pedida.xMinimum(), pedida.xMaximum(),
            pedida.yMinimum(), pedida.yMaximum()),
        'SALIDA': destino,
    }, rec2)
    if not comp('con area pedida la exportacion termina sin error', ok2,
                (tb2.strip().splitlines()[-1] if tb2
                 else _resumen(rec2))):
        return
    if not comp('el KMZ recortado existe', os.path.isfile(destino),
                destino):
        return

    caja = _caja_nivel0(destino)
    if not comp('se puede leer la caja del nivel 0 del KMZ', caja is not None,
                'sin ella no se puede saber que area cubre'):
        return
    n, s, e, w = caja
    cerca = (abs(w - pedida.xMinimum()) < 0.02
             and abs(e - pedida.xMaximum()) < 0.02
             and abs(s - pedida.yMinimum()) < 0.02
             and abs(n - pedida.yMaximum()) < 0.02)
    comp('el KMZ cubre el area pedida y no la capa entera', cerca,
         'pedido W=%.3f S=%.3f E=%.3f N=%.3f; KMZ W=%.3f S=%.3f E=%.3f '
         'N=%.3f' % (pedida.xMinimum(), pedida.yMinimum(),
                     pedida.xMaximum(), pedida.yMaximum(), w, s, e, n))
    comp('y el area pedida ya no dispara el aviso',
         not extension_sospechosa(w, s, e, n),
         'un grado de lado no es una extension sospechosa')
    mx, _my = metros_por_pixel(w, s, e, n, 512, 512)
    comp('la resolucion del recorte es util (< 500 m/px)',
         mx is not None and mx < 500, f'{mx} m/px')

    _probar_area_con_reproyeccion(tmp)


def _probar_area_con_reproyeccion(tmp):
    """Area pedida Y reproyeccion a la vez: la combinacion del caso real.

    El caso del usuario era una capa en EPSG:900913 --Mercator-- sobre la
    que hay que pedir un area. Las dos cosas estaban probadas por
    separado: la reproyeccion en el caso «utm16n», el area pedida en una
    capa que ya estaba en 4326. Juntas, no. Y es justo donde un error de
    sistema de coordenadas coloca la imagen en otro sitio sin que nada
    falle.
    """
    from qgis.core import QgsRasterLayer, QgsRectangle
    from BuscadorSTAC.google_earth_algoritmo import (
        ExportarGoogleEarthAlgorithm)

    seccion('Raster: area pedida SOBRE una capa que hay que reproyectar')
    # Capa en Mercator esferico que cubre de 89.8O a 71.9O y de 0 a 17.7N.
    tif = os.path.join(tmp, 'mercator.tif')
    _raster_sintetico(tif, bandas=3, epsg=3857,
                      caja=(-10000000.0, 0.0, -8000000.0, 2000000.0))
    capa = QgsRasterLayer(tif, 'mercator')
    if not comp('la capa en EPSG:3857 carga', capa.isValid()):
        return
    comp('y declara un SRC distinto de 4326',
         capa.crs().authid() != 'EPSG:4326', capa.crs().authid())

    pedida = QgsRectangle(-84.0, 9.7, -83.5, 10.2)      # Turrialba
    alg = ExportarGoogleEarthAlgorithm()
    alg.initAlgorithm()
    destino = os.path.join(tmp, 'mercator_recorte.kmz')
    rec = _Recolector()
    _r, ok, tb = _correr(alg, {
        'CAPA': capa, 'NOMBRE': 'turrialba', 'PX_LADO': 512, 'FORMATO': 0,
        'OPACIDAD': 100, 'ABRIR': False,
        'EXTENSION': '%f,%f,%f,%f [EPSG:4326]' % (
            pedida.xMinimum(), pedida.xMaximum(),
            pedida.yMinimum(), pedida.yMaximum()),
        'SALIDA': destino,
    }, rec)
    if not comp('la exportacion reproyectada y recortada termina sin error',
                ok, (tb.strip().splitlines()[-1] if tb else _resumen(rec))):
        return
    todo = ' '.join(rec.info)
    comp('la tuberia reproyecta', 'Reproyecci' in todo, todo[-200:])
    if not comp('el KMZ existe', os.path.isfile(destino), destino):
        return

    caja = _caja_nivel0(destino)
    if not comp('se lee la caja del nivel 0', caja is not None, ''):
        return
    n, s, e, w = caja
    comp('el KMZ cae sobre el area pedida, no sobre la capa entera',
         (abs(w - pedida.xMinimum()) < 0.02
          and abs(e - pedida.xMaximum()) < 0.02
          and abs(s - pedida.yMinimum()) < 0.02
          and abs(n - pedida.yMaximum()) < 0.02),
         'pedido W=%.3f S=%.3f E=%.3f N=%.3f; KMZ W=%.3f S=%.3f E=%.3f '
         'N=%.3f' % (pedida.xMinimum(), pedida.yMinimum(),
                     pedida.xMaximum(), pedida.yMaximum(), w, s, e, n))

    # Que la imagen tenga contenido: sin el proyector saldria transparente.
    with zipfile.ZipFile(destino) as z:
        pngs = [m for m in z.namelist() if m.endswith('.png')]
    comp('el KMZ reproyectado trae imagenes', bool(pngs),
         'sin PNG no hay nada que ver')
    hay, motivo = _imagen_con_contenido(destino, pngs, tmp)
    comp('y la imagen tiene contenido, no es transparente', hay, motivo)

    _probar_rechazo_de_capa_remota(tmp)


def _probar_rechazo_de_capa_remota(tmp):
    """Un mapa base remoto SIN area tiene que pararse antes de escribir.

    Avisar despues no sirvio: el algoritmo escribia 2,7 MiB, abria Google
    Earth y el usuario encontraba el resultado malo con la explicacion
    encima. Se usa una capa XYZ apuntando a un archivo local --no sale a
    la red, pero el proveedor es «wms», que es lo que decide.
    """
    from qgis.core import QgsProcessingContext, QgsRasterLayer
    from BuscadorSTAC.google_earth_algoritmo import (
        ExportarGoogleEarthAlgorithm)

    seccion('Raster: capa remota sin area pedida se rechaza')
    uri = ('type=xyz&url=file://' + tmp.replace('\\', '/')
           + '/teselas/%7Bz%7D/%7Bx%7D/%7By%7D.png&zmax=5&zmin=0')
    capa = QgsRasterLayer(uri, 'basemap', 'wms')
    proveedor = ''
    try:
        proveedor = capa.dataProvider().name()
    except Exception as e:
        nota(f'no se pudo leer el proveedor: {e}')
    if proveedor != 'wms':
        nota(f'capa XYZ local no disponible (proveedor={proveedor!r}); '
             f'el rechazo se comprueba igual en tests/test_core.py sobre '
             f'es_proveedor_remoto')
        return

    alg = ExportarGoogleEarthAlgorithm()
    alg.initAlgorithm()
    ctx = QgsProcessingContext()
    base = {'CAPA': capa, 'NOMBRE': 'x', 'PX_LADO': 2048, 'FORMATO': 0,
            'OPACIDAD': 100, 'ABRIR': False,
            'SALIDA': os.path.join(tmp, 'no_deberia_existir.kmz')}

    ok, msg = alg.checkParameterValues(dict(base), ctx)
    comp('sin area, la validacion RECHAZA la capa remota', not ok,
         f'dejo pasar: {msg!r}')
    comp('y el mensaje nombra el parametro que hay que rellenar',
         'área a exportar' in (msg or ''), repr(msg))

    conf = dict(base)
    conf['EXTENSION'] = '-84.0,-83.5,9.7,10.2 [EPSG:4326]'
    ok2, msg2 = alg.checkParameterValues(conf, ctx)
    comp('con area, la validacion ya no rechaza por ese motivo',
         ok2 or 'área a exportar' not in (msg2 or ''),
         f'siguio rechazando por el area: {msg2!r}')


def _caja_nivel0(kmz):
    """(N, S, E, W) del nivel 0 de un super-overlay, o None."""
    if not zipfile.is_zipfile(kmz):
        return None
    try:
        with zipfile.ZipFile(kmz) as z:
            l0 = [m for m in z.namelist()
                  if m.startswith('0/') and m.endswith('.kml')]
            if not l0:
                l0 = [m for m in z.namelist() if m.endswith('.kml')]
            if not l0:
                return None
            txt = z.read(sorted(l0)[0]).decode('utf-8', 'replace')
    except (zipfile.BadZipFile, OSError, KeyError):
        return None
    g = re.search(r'<LatLonBox>.*?<north>([-\d.]+)</north>.*?'
                  r'<south>([-\d.]+)</south>.*?<east>([-\d.]+)</east>.*?'
                  r'<west>([-\d.]+)</west>', txt, re.S)
    return tuple(float(x) for x in g.groups()) if g else None


def _probar_vector(tmp):
    from qgis.core import (QgsFeature, QgsGeometry, QgsPointXY,
                           QgsVectorLayer)
    from BuscadorSTAC.google_earth_algoritmo import (
        ExportarGoogleEarthAlgorithm)

    # Imita la capa de huellas: campo «fecha» con los diez primeros del
    # datetime de STAC, y una entidad con fecha ilegible a proposito.
    capa = QgsVectorLayer(
        'Polygon?crs=EPSG:4326&field=nombre:string&field=fecha:string',
        'poligono', 'memory')
    for nombre, fecha in (('AOI-1', '2025-01-03'), ('AOI-2', '2025-07-14'),
                          ('AOI-3', 'sin fecha')):
        f = QgsFeature(capa.fields())
        f.setGeometry(QgsGeometry.fromPolygonXY([[
            QgsPointXY(-85.8, 10.6), QgsPointXY(-85.7, 10.6),
            QgsPointXY(-85.7, 10.5), QgsPointXY(-85.8, 10.5),
            QgsPointXY(-85.8, 10.6)]]))
        f.setAttribute('nombre', nombre)
        f.setAttribute('fecha', fecha)
        capa.dataProvider().addFeatures([f])
    comp('vector: la capa en memoria tiene tres entidades',
         capa.featureCount() == 3)

    alg = ExportarGoogleEarthAlgorithm()
    alg.initAlgorithm()
    destino = os.path.join(tmp, 'salida_vector.kmz')
    rec = _Recolector()
    _res, ok, tb = _correr(alg, {
        'CAPA': capa, 'NOMBRE': 'prueba', 'PX_LADO': 512, 'FORMATO': 0,
        'OPACIDAD': 100, 'ABRIR': False, 'SALIDA': destino,
    }, rec)
    comp('vector: la exportacion termina sin error', ok,
         (tb.strip().splitlines()[-1] if tb else _resumen(rec)))

    seccion('Vectorial con linea de tiempo')
    alg2 = ExportarGoogleEarthAlgorithm()
    alg2.initAlgorithm()
    destino2 = os.path.join(tmp, 'salida_tiempo.kmz')
    rec2 = _Recolector()
    _res2, ok2, tb2 = _correr(alg2, {
        'CAPA': capa, 'CAMPO_FECHA': 'fecha', 'NOMBRE': 'prueba',
        'PX_LADO': 512, 'FORMATO': 0, 'OPACIDAD': 100, 'ABRIR': False,
        'SALIDA': destino2,
    }, rec2)
    if not comp('con CAMPO_FECHA la exportacion termina sin error', ok2,
                (tb2.strip().splitlines()[-1] if tb2 else _resumen(rec2))):
        return

    # Lo que importa no es que el archivo exista, sino que Google Earth
    # vaya a encontrar el <TimeStamp>: sin el, abre igual y la linea de
    # tiempo no aparece, sin un solo error.
    real = destino2 if os.path.exists(destino2) else destino2[:-4] + '.kml'
    if not comp('el archivo con tiempo existe', os.path.isfile(real), real):
        return
    if real.lower().endswith('.kmz'):
        with zipfile.ZipFile(real) as z:
            crudo = b''.join(z.read(n) for n in z.namelist()
                             if n.lower().endswith('.kml'))
    else:
        crudo = open(real, 'rb').read()
    texto = crudo.decode('utf-8', 'replace')
    comp('el KML declara TimeStamp', 'TimeStamp' in texto,
         'sin el, Google Earth no muestra el control de tiempo')
    comp('y trae las fechas normalizadas',
         '2025-01-03' in texto and '2025-07-14' in texto,
         'las dos fechas validas tienen que estar en el <when>')
    comp('la entidad sin fecha legible no inventa un <when>',
         texto.count('<when>') == 2,
         f'se esperaban 2 <when> y hay {texto.count("<when>")}')
    avisos = ' '.join(rec2.info) + ' '.join(rec2.errores)
    comp('se informa cuantas fechas se entendieron',
         '2' in avisos or 'fecha' in avisos.lower(), avisos[-200:])

    _probar_nombre_con_tildes(capa, tmp)


def _probar_nombre_con_tildes(capa, tmp):
    """El caso que esta prueba NO cubria: un nombre de capa en espanol.

    Hasta ahora todas las llamadas pasaban NOMBRE='prueba', ASCII, y por
    eso CI daba verde mientras el complemento producia un KMZ que ni
    Google Earth ni el propio GDAL podian leer. El nombre es el de la capa
    real con la que fallo: «Bufer Oval 357x179 m [Union]», con tildes.
    """
    from BuscadorSTAC.core import href_inseguro
    from BuscadorSTAC.google_earth_algoritmo import (
        ExportarGoogleEarthAlgorithm)

    seccion('Vectorial con tildes en el nombre de la capa')
    rotulo = 'Búfer Oval 357x179 m [Unión]'
    alg = ExportarGoogleEarthAlgorithm()
    alg.initAlgorithm()
    destino = os.path.join(tmp, 'salida_tildes.kmz')
    rec = _Recolector()
    _res, ok, tb = _correr(alg, {
        'CAPA': capa, 'NOMBRE': rotulo, 'PX_LADO': 512, 'FORMATO': 0,
        'OPACIDAD': 100, 'ABRIR': False, 'SALIDA': destino,
    }, rec)
    if not comp('con tildes la exportacion termina sin error', ok,
                (tb.strip().splitlines()[-1] if tb else _resumen(rec))):
        return
    if not comp('el archivo con tildes existe', os.path.isfile(destino),
                destino):
        return
    if not zipfile.is_zipfile(destino):
        comp('el KMZ con tildes es un ZIP', False,
             'LIBKML falta y se degrado a .kml; el resto no aplica')
        return

    with zipfile.ZipFile(destino) as z:
        nombres = z.namelist()
        doc = z.read('doc.kml').decode('utf-8', 'replace') \
            if 'doc.kml' in nombres else ''

    # 1. Ningun miembro con nombre no ASCII. LIBKML los escribe en UTF-8
    #    crudo con el bit 11 del ZIP en cero, asi que la norma obliga a
    #    leerlos como CP437 y el nombre deja de coincidir con el href.
    no_ascii = [n for n in nombres if any(ord(c) > 126 for c in n)]
    comp('ningun miembro del ZIP lleva caracteres no ASCII',
         not no_ascii, f'miembros problematicos: {no_ascii}')

    # 2. Sin NetworkLink: el documento tiene que estar en doc.kml.
    comp('doc.kml no delega en un <NetworkLink>',
         '<NetworkLink' not in doc,
         'con el enlace, ogr.Open devuelve None sobre este mismo archivo')
    comp('doc.kml trae los <Placemark> el mismo',
         doc.count('<Placemark') == 3,
         f'se esperaban 3 y hay {doc.count("<Placemark")}')

    # 3. Ningun href interno impronunciable, haya o no NetworkLink.
    malos = []
    for href in re.findall(r'<href>\s*(.*?)\s*</href>', doc, re.S):
        if '://' not in href and href_inseguro(href):
            malos.append(href)
    comp('ningun <href> relativo con caracteres invalidos en URI',
         not malos, f'hrefs: {malos}')

    # 4. Y las tildes siguen donde el usuario las ve.
    comp('el nombre con tildes se conserva para mostrarlo',
         rotulo in doc, 'el <name> del documento perdio las tildes')

    # 5. La prueba de verdad: que GDAL lo lea. Es el lector mas parecido
    #    al de Google Earth que hay disponible sin Google Earth.
    try:
        from osgeo import ogr as _ogr
    except ImportError as e:
        comp('GDAL disponible para releer el KMZ', False, str(e))
        return
    ds = _ogr.Open(destino)
    if ds is None:
        comp('GDAL abre el KMZ con tildes', False,
             'ogr.Open devolvio None: es el fallo que reporto el usuario')
    else:
        cap = ds.GetLayerCount()
        ent = ds.GetLayer(0).GetFeatureCount() if cap else 0
        comp('GDAL lee una capa con sus tres entidades',
             cap == 1 and ent == 3, f'{cap} capa(s), {ent} entidad(es)')
    ds = None

    _control_negativo(rotulo, tmp)


def _control_negativo(rotulo, tmp):
    """Comprobar que la comprobacion anterior comprueba algo.

    Una prueba que nunca ha fallado da seguridad falsa. Esta escribe el
    MISMO nombre de capa con LIBKML y SIN aplanar, y mira si GDAL puede
    leerlo. Si no puede, la prueba de arriba discrimina de verdad.

    Si algun dia si puede, no es un fallo: significa que GDAL arreglo el
    bit 11, y entonces el aplanado deja de hacer falta. Eso se informa,
    no se bloquea — romper CI de alguien porque una dependencia mejoro
    seria absurdo.
    """
    from osgeo import ogr as _ogr
    from osgeo import osr as _osr

    crudo = os.path.join(tmp, 'control_sin_aplanar.kmz')
    try:
        drv = _ogr.GetDriverByName('LIBKML')
        if drv is None:
            nota('control negativo omitido: este GDAL no trae LIBKML')
            return
        srs = _osr.SpatialReference()
        srs.ImportFromEPSG(4326)
        if hasattr(srs, 'SetAxisMappingStrategy'):
            srs.SetAxisMappingStrategy(_osr.OAMS_TRADITIONAL_GIS_ORDER)
        ds = drv.CreateDataSource(crudo)
        lyr = ds.CreateLayer(rotulo, srs, _ogr.wkbPoint)
        f = _ogr.Feature(lyr.GetLayerDefn())
        f.SetGeometry(_ogr.CreateGeometryFromWkt('POINT(-85.8 10.6)'))
        lyr.CreateFeature(f)
        f = None
        ds = None
    except Exception as e:
        # Amplio a proposito: esto es un control, no la prueba. Si no se
        # puede escribir, se dice y se sigue; nunca debe tumbar la corrida.
        nota(f'control negativo no se pudo escribir: {e}')
        return

    d = _ogr.Open(crudo)
    capas = d.GetLayerCount() if d is not None else -1
    if capas < 1:
        comp('el control negativo confirma que la prueba discrimina',
             True, '')
        nota(f'sin aplanar, ogr.Open da {"None" if capas < 0 else "0 capas"}'
             f' sobre «{rotulo}»: el aplanado es lo que arregla el KMZ')
    else:
        nota('ATENCION: este GDAL ya lee el KMZ de LIBKML con tildes sin '
             'aplanarlo. El bit 11 del ZIP parece corregido; el aplanado '
             'sigue siendo correcto pero ya no seria imprescindible. '
             'Conviene revisar si se puede simplificar.')


def _pseudocolor(capa):
    """Pseudocolor, que es como se ve un indice en QGIS de verdad."""
    from qgis.core import (QgsColorRampShader, QgsRasterShader,
                           QgsSingleBandPseudoColorRenderer)
    from qgis.PyQt.QtGui import QColor

    rampa = QgsColorRampShader()
    tipo = getattr(getattr(QgsColorRampShader, 'Type', QgsColorRampShader),
                   'Interpolated', 0)
    rampa.setColorRampType(tipo)
    rampa.setColorRampItemList([
        QgsColorRampShader.ColorRampItem(-1.0, QColor(165, 0, 38), '-1'),
        QgsColorRampShader.ColorRampItem(0.0, QColor(255, 255, 191), '0'),
        QgsColorRampShader.ColorRampItem(1.0, QColor(0, 104, 55), '1')])
    sombreador = QgsRasterShader()
    sombreador.setRasterShaderFunction(rampa)
    capa.setRenderer(QgsSingleBandPseudoColorRenderer(
        capa.dataProvider(), 1, sombreador))


def main():
    seccion('Entorno')
    from qgis.core import Qgis, QgsApplication
    print(f'  QGIS {Qgis.QGIS_VERSION}')
    try:
        from osgeo import gdal
        print(f'  GDAL {gdal.VersionInfo("RELEASE_NAME")}')
        drv = gdal.GetDriverByName('KMLSUPEROVERLAY')
        comp('GDAL trae el controlador KMLSUPEROVERLAY', drv is not None,
             'sin el no hay KMZ con raster posible')
        if drv is not None:
            opciones = drv.GetMetadataItem(gdal.DMD_CREATIONOPTIONLIST) or ''
            print(f'        opciones declaradas: {opciones[:200]}')
    except ImportError as e:
        comp('osgeo.gdal disponible', False, str(e))
        return 1

    os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
    app = QgsApplication([], False)
    app.initQgis()
    tmp = tempfile.mkdtemp(prefix='kmz_')
    try:
        seccion('Raster monobanda de indice, con el renderizador de serie')
        tif = _raster_sintetico(os.path.join(tmp, 'indice.tif'))
        _probar_raster('monobanda', tif, tmp)

        seccion('Raster monobanda con pseudocolor')
        _probar_raster('pseudocolor', tif, tmp, estilo=_pseudocolor)

        seccion('Raster de tres bandas (composicion RGB)')
        from osgeo import gdal as _g
        tif3 = _raster_sintetico(os.path.join(tmp, 'rgb.tif'), bandas=3,
                                 tipo=_g.GDT_Byte)
        _probar_raster('rgb', tif3, tmp)

        seccion('Raster en UTM: lo que descarga el propio complemento')
        # El recorte de Sentinel-2 / Landsat no pasa dstSRS, de modo que
        # queda en el UTM de la escena. Si el caso 4326 pasa y este no, el
        # SRC es la causa y no el controlador.
        tif_utm = _raster_sintetico(os.path.join(tmp, 'utm.tif'),
                                    epsg=32616)
        _probar_raster('utm16n', tif_utm, tmp)

        _probar_area_pedida(tmp)

        seccion('Vectorial, para contraste')
        _probar_vector(tmp)
    finally:
        app.exitQgis()

    print('\n' + '=' * 60)
    if fallos:
        print(f'FALLOS: {len(fallos)} de {hechas} comprobaciones')
        for f in fallos:
            print(f'  - {f}')
        return 1
    print(f'OK — {hechas} comprobaciones')
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except ImportError as e:
        print(f'No se pudo cargar QGIS: {e}')
        print('Necesita un QGIS instalado: una imagen qgis/qgis, o '
              'python-qgis-ltr.bat en Windows.')
        sys.exit(2)
