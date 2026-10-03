#!/usr/bin/env python3
# -*- coding: utf-8 -*-
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


def _raster_sintetico(ruta, bandas=1, tipo=None):
    """GeoTIFF pequeño sobre Guanacaste, con valores de índice (-1 a 1)."""
    import numpy as np
    from osgeo import gdal, osr

    tipo = gdal.GDT_Float32 if tipo is None else tipo
    cols, filas = 60, 40
    drv = gdal.GetDriverByName('GTiff')
    ds = drv.Create(ruta, cols, filas, bandas, tipo)
    # Un grado de lado, en el Pacífico norte de Costa Rica.
    ds.SetGeoTransform([-85.8, 1.0 / cols, 0.0, 10.6, 0.0, -1.0 / filas])
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(4326)
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
    comp(f'{etiqueta}: el KMZ lleva al menos una imagen', bool(imagenes),
         'un KMZ sin PNG/JPG se abre en Google Earth y no se ve nada; '
         'es el fallo que no da error')
    comp(f'{etiqueta}: el KML declara GroundOverlay', overlay,
         'sin GroundOverlay la imagen no se ancla al suelo')


def _probar_vector(tmp):
    from qgis.core import (QgsFeature, QgsGeometry, QgsPointXY,
                           QgsVectorLayer)
    from BuscadorSTAC.google_earth_algoritmo import (
        ExportarGoogleEarthAlgorithm)

    capa = QgsVectorLayer(
        'Polygon?crs=EPSG:4326&field=nombre:string', 'poligono', 'memory')
    f = QgsFeature(capa.fields())
    f.setGeometry(QgsGeometry.fromPolygonXY([[
        QgsPointXY(-85.8, 10.6), QgsPointXY(-85.7, 10.6),
        QgsPointXY(-85.7, 10.5), QgsPointXY(-85.8, 10.5),
        QgsPointXY(-85.8, 10.6)]]))
    f.setAttribute('nombre', 'AOI')
    capa.dataProvider().addFeatures([f])
    comp('vector: la capa en memoria tiene una entidad',
         capa.featureCount() == 1)

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
