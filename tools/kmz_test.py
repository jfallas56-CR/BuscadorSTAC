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


def _raster_sintetico(ruta, bandas=1, tipo=None, epsg=4326):
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
    if epsg == 4326:
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
        partes.append('reportError: ' + ' | '.join(rec.errores[-3:]))
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
