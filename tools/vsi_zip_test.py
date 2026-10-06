#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
# -*- coding: utf-8 -*-
"""Comprueba que la ruta VSI con la que se lee un ZIP remoto funciona.

Existe por un fallo concreto: la recogida de Sentinel-1 RTC informó «el
ZIP se abrió pero salió vacío» en los 31 productos de un lote, y
`gdal.ReadDirRecursive` no dice por qué —devuelve una lista vacía tanto
si el enlace venció como si el servidor no admite rangos o la ruta VSI
está mal compuesta—. Antes de culpar al servidor de ASF hay que saber si
la ruta que compone el complemento sirve, y eso se puede comprobar sin
red y sin credenciales.

Lo que mide, de lo más simple a lo más parecido al caso real:

1. ZIP local, sintaxis llana `/vsizip/<ruta>`.
2. ZIP local, sintaxis con llaves `/vsizip/{<ruta>}` — la que usa el
   complemento, obligatoria cuando la ruta lleva «?» o «&».
3. Abrir el GeoTIFF de dentro por esa misma ruta.
4. ZIP con estructura ZIP64, que es la que toma un archivo de más de
   4 GiB. Se fabrica con `force_zip64` sobre un miembro diminuto, así que
   la prueba cuesta milisegundos en vez de 4 GiB.
5. ZIP servido por HTTP con soporte de rangos, leído por
   `/vsizip/{/vsicurl/...}`.
6. Lo mismo con una cadena de consulta tipo URL prefirmada, que es la
   razón de ser de las llaves.

    python3 tools/vsi_zip_test.py        (necesita osgeo.gdal; QGIS lo trae)

Autor    : Jorge Fallas (jfallas56@gmail.com)
Licencia : GPL v2 o posterior
Version  : 1.0.0
"""

from __future__ import annotations

import http.server
import os
import re
import struct
import sys
import tempfile
import threading
import zipfile

# La RAIZ del repositorio, no la carpeta tools: hace falta para importar
# BuscadorSTAC.core y probar el arreglo con la MISMA funcion que usa el
# complemento, en vez de con una copia que podria divergir.
RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
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


def _tif_minimo():
    """GeoTIFF diminuto, en memoria, sin depender de que GDAL escriba."""
    from osgeo import gdal
    tmp = os.path.join(tempfile.mkdtemp(prefix='vsi_tif_'), 'b_VH.tif')
    ds = gdal.GetDriverByName('GTiff').Create(tmp, 4, 4, 1, gdal.GDT_Byte)
    ds.SetGeoTransform([-85.0, 0.001, 0.0, 10.0, 0.0, -0.001])
    ds.GetRasterBand(1).Fill(7)
    ds.FlushCache()
    ds = None
    return tmp


_BASE = 'S1A_IW_20240221T234839_DVP_RTC10_G_gpufed_5D9B'


def _zip_con(tif, destino):
    """ZIP con el TIFF dentro, imitando la estructura de HyP3."""
    with zipfile.ZipFile(destino, 'w', zipfile.ZIP_DEFLATED,
                         allowZip64=True) as z:
        z.writestr(f'{_BASE}/{_BASE}_VH.tif', open(tif, 'rb').read())
        z.writestr(f'{_BASE}/{_BASE}.README.md.txt', 'prueba\n')
    return destino


def _zip64_de_verdad(tif, destino):
    """ZIP con indice ZIP64 real, forzado por numero de miembros.

    Un ZIP pasa a ZIP64 al rebasar 65535 miembros, 4 GiB de tamano o
    4 GiB de desplazamiento. Escribir 4 GiB en integracion continua no es
    razonable, asi que se fuerza por el numero de miembros: el archivo
    acaba con el End of Central Directory de ZIP64 y su localizador, que
    es la estructura que GDAL tiene que saber leer para listar un
    producto RTC a 10 m.

    No vale usar `force_zip64` sobre un miembro pequeno, que fue el primer
    intento: eso escribe el campo ZIP64 en el encabezado LOCAL del
    miembro y deja el indice central sin el, y el indice central es
    justamente lo que lee GDAL. La comprobacion habria pasado sin medir
    nada de ZIP64.

    Lo que NO cubre: los desplazamientos de 64 bits de cada miembro, que
    solo aparecen rebasando los 4 GiB de verdad. Se deja dicho para que
    la prueba no prometa mas de lo que mide.
    """
    with zipfile.ZipFile(destino, 'w', zipfile.ZIP_STORED,
                         allowZip64=True) as z:
        z.writestr(f'{_BASE}/{_BASE}_VH.tif', open(tif, 'rb').read())
        for i in range(65540):
            z.writestr(f'relleno/{i}.txt', b'')
    return destino


def _tiene_zip64(ruta):
    """¿El archivo trae de verdad el indice ZIP64?

    Se exigen las DOS firmas: el End of Central Directory de ZIP64 y su
    localizador. Con una sola, un archivo que llevase esos cuatro bytes
    por casualidad dentro de un miembro comprimido daria un falso si.
    """
    with open(ruta, 'rb') as fh:
        crudo = fh.read()
    return (struct.pack('<I', 0x06064b50) in crudo
            and struct.pack('<I', 0x07064b50) in crudo)


def _zip_grande(tif, destino, mib=8):
    """ZIP de varios MiB, con relleno incompresible.

    El primer ZIP de esta prueba medía 253 bytes, de modo que entraba
    entero en la primera lectura de GDAL y el indice quedaba a mano sin
    saltar a ningun sitio. Esa prueba pasaba sin ejercitar lo unico que
    importa en un ZIP remoto: que para leer el indice --que esta al
    FINAL-- GDAL tiene que averiguar el tamaño del archivo y saltar
    hasta alli. El producto real mide 0.92 GiB; con 8 MiB ya no cabe en
    la primera lectura y el salto se ejercita de verdad.
    """
    with zipfile.ZipFile(destino, 'w', zipfile.ZIP_STORED) as z:
        z.writestr(f'{_BASE}/relleno.bin', os.urandom(mib * 1024 * 1024))
        z.writestr(f'{_BASE}/{_BASE}_VH.tif', open(tif, 'rb').read())
    return destino


class _ServidorConRangos:
    """Sirve UN archivo admitiendo Range, que es lo que /vsicurl/ exige.

    SimpleHTTPRequestHandler NO implementa Range: contesta 200 con el
    cuerpo entero. Montar la prueba sobre el servidor de serie mediria
    otra cosa --y daria por bueno justo el caso que falla en produccion
    si el servidor remoto ignorase los rangos.

    `sin_head=True` rechaza las peticiones HEAD con 405, imitando al
    servidor que no las contesta como GDAL espera. Es el caso que se
    sospecha en produccion: GET por rangos impecable --206, Accept-Ranges,
    application/zip-- y aun asi un listado vacio, porque el tamaño se
    averigua con HEAD.
    """

    def __init__(self, ruta, sin_head=False):
        datos = open(ruta, 'rb').read()
        rechazar_head = sin_head

        class Manejador(http.server.BaseHTTPRequestHandler):
            protocol_version = 'HTTP/1.1'

            def log_message(self, *a):
                pass

            def _cabeceras(self, largo, codigo=200, extra=None):
                self.send_response(codigo)
                self.send_header('Content-Length', str(largo))
                self.send_header('Content-Type', 'application/zip')
                self.send_header('Accept-Ranges', 'bytes')
                for k, v in (extra or {}).items():
                    self.send_header(k, v)
                self.end_headers()

            def do_HEAD(self):
                if rechazar_head:
                    self.send_response(405)
                    self.send_header('Content-Length', '0')
                    self.end_headers()
                    return
                self._cabeceras(len(datos))

            def do_GET(self):
                pedido = self.headers.get('Range') or ''
                m = re.match(r'bytes=(\d*)-(\d*)', pedido)
                if not m:
                    self._cabeceras(len(datos))
                    self.wfile.write(datos)
                    return
                ini = int(m.group(1)) if m.group(1) else 0
                fin = (int(m.group(2)) if m.group(2) else len(datos) - 1)
                fin = min(fin, len(datos) - 1)
                trozo = datos[ini:fin + 1]
                self._cabeceras(
                    len(trozo), 206,
                    {'Content-Range': f'bytes {ini}-{fin}/{len(datos)}'})
                self.wfile.write(trozo)

        self.srv = http.server.ThreadingHTTPServer(('127.0.0.1', 0),
                                                   Manejador)
        self.puerto = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def cerrar(self):
        self.srv.shutdown()
        self.srv.server_close()


def _listar(raiz):
    from osgeo import gdal
    try:
        return gdal.ReadDirRecursive(raiz) or [], ''
    except RuntimeError as e:
        return [], str(e)


def _hay_vh(entradas):
    return any(str(x).upper().endswith('_VH.TIF') for x in entradas)


def main():
    try:
        from osgeo import gdal
    except ImportError as e:
        print(f'Necesita osgeo.gdal (lo trae QGIS): {e}')
        return 2
    print(f'  GDAL {gdal.VersionInfo("RELEASE_NAME")}')
    gdal.UseExceptions()

    tmp = tempfile.mkdtemp(prefix='vsi_zip_')
    tif = _tif_minimo()
    z_llano = _zip_con(tif, os.path.join(tmp, 'p.zip'))
    z_64 = _zip64_de_verdad(tif, os.path.join(tmp, 'p64.zip'))

    seccion('ZIP local')
    ent, err = _listar('/vsizip/' + z_llano)
    comp('sintaxis llana /vsizip/<ruta> lista el contenido',
         _hay_vh(ent), f'{ent} {err}')

    raiz_llaves = '/vsizip/{' + z_llano + '}'
    ent, err = _listar(raiz_llaves)
    comp('sintaxis con llaves /vsizip/{<ruta>} lista el contenido',
         _hay_vh(ent),
         f'es la que usa el complemento; si falla, la ruta VSI es el '
         f'fallo y no el servidor. entradas={ent} {err}')

    if _hay_vh(ent):
        interno = [x for x in ent
                   if str(x).upper().endswith('_VH.TIF')][0]
        ruta = raiz_llaves + '/' + str(interno)
        try:
            ds = gdal.Open(ruta)
        except RuntimeError as e:
            ds = None
            err = str(e)
        comp('el GeoTIFF de dentro se abre por esa ruta', ds is not None,
             f'{ruta} -> {err}')
        if ds is not None:
            comp('y trae los datos esperados',
                 ds.RasterXSize == 4 and ds.RasterYSize == 4)
            ds = None

    seccion('ZIP con indice ZIP64 (el de un archivo de mas de 4 GiB)')
    comp('el ZIP de prueba lleva de verdad el indice ZIP64',
         _tiene_zip64(z_64),
         'si no, la prueba no esta midiendo lo que dice medir')
    comp('y el ZIP normal NO lo lleva', not _tiene_zip64(z_llano),
         'el detector tiene que distinguir los dos casos; si dice si a '
         'los dos no esta detectando nada')
    ent, err = _listar('/vsizip/{' + z_64 + '}')
    comp('GDAL lista un indice ZIP64', _hay_vh(ent),
         f'un producto RTC a 10 m ronda los 4 GiB. entradas={ent} {err}')

    seccion('ZIP por HTTP con rangos, como lo lee la recogida')
    srv = _ServidorConRangos(z_llano)
    try:
        url = f'http://127.0.0.1:{srv.puerto}/p.zip'
        ent, err = _listar('/vsizip/{/vsicurl/' + url + '}')
        comp('/vsizip/{/vsicurl/...} lista un ZIP remoto', _hay_vh(ent),
             f'entradas={ent} {err}')

        # La cadena de consulta es la razon de ser de las llaves: sin
        # ellas GDAL corta la ruta en el «?».
        url_firmada = (f'http://127.0.0.1:{srv.puerto}/p.zip'
                       f'?X-Amz-Algorithm=AWS4-HMAC-SHA256'
                       f'&X-Amz-Signature=0123456789abcdef')
        ent, err = _listar('/vsizip/{/vsicurl/' + url_firmada + '}')
        comp('y tambien con cadena de consulta tipo URL prefirmada',
             _hay_vh(ent), f'entradas={ent} {err}')
    finally:
        srv.cerrar()

    seccion('ZIP remoto de varios MiB: el indice ya no cabe en la 1a lectura')
    grande = _zip_grande(tif, os.path.join(tmp, 'grande.zip'))
    print(f'        tamaño: {os.path.getsize(grande) / 1048576:.1f} MiB')
    srv = _ServidorConRangos(grande)
    try:
        gdal.VSICurlClearCache()
        ent, err = _listar(
            '/vsizip/{/vsicurl/' + f'http://127.0.0.1:{srv.puerto}/g.zip}}')
        comp('GDAL salta al final del archivo y lee el indice',
             _hay_vh(ent), f'entradas={len(ent)} {err}')
    finally:
        srv.cerrar()

    seccion('Lista blanca de extensiones de /vsicurl/ (la causa real)')
    # Reproduce la configuracion encontrada en el QGIS del usuario:
    # CPL_VSIL_CURL_ALLOWED_EXTENSIONS=.tif,.TIF,.tiff,.jp2. Con eso
    # /vsicurl/ se niega a abrir un .zip sin pedir nada al servidor y sin
    # dar error, que es por lo que el fallo era mudo.
    srv = _ServidorConRangos(grande)
    try:
        url_g = f'http://127.0.0.1:{srv.puerto}/g.zip'
        previo = gdal.GetConfigOption('CPL_VSIL_CURL_ALLOWED_EXTENSIONS',
                                      None)
        try:
            gdal.SetConfigOption('CPL_VSIL_CURL_ALLOWED_EXTENSIONS',
                                 '.tif,.TIF,.tiff,.jp2')
            gdal.VSICurlClearCache()
            fh = gdal.VSIFOpenL('/vsicurl/' + url_g, 'rb')
            if fh is not None:
                gdal.VSIFCloseL(fh)
            comp('con la lista sin .zip, /vsicurl/ NO abre el archivo',
                 fh is None,
                 'si lo abre, esta version de GDAL no aplica la lista y '
                 'esta prueba no reproduce el fallo del usuario')
            gdal.VSICurlClearCache()
            ent, err = _listar('/vsizip/{/vsicurl/' + url_g + '}')
            comp('y el indice sale vacio, que era el sintoma',
                 not _hay_vh(ent), f'entradas={len(ent)} {err}')

            # Y ahora el arreglo del complemento.
            from BuscadorSTAC.core import lista_con_zip
            gdal.SetConfigOption('CPL_VSIL_CURL_ALLOWED_EXTENSIONS',
                                 lista_con_zip('.tif,.TIF,.tiff,.jp2'))
            gdal.VSICurlClearCache()
            ent2, err2 = _listar('/vsizip/{/vsicurl/' + url_g + '}')
            comp('anadiendo .zip a la lista, el indice se lee',
                 _hay_vh(ent2), f'entradas={len(ent2)} {err2}')
        finally:
            gdal.SetConfigOption('CPL_VSIL_CURL_ALLOWED_EXTENSIONS', previo)
    finally:
        srv.cerrar()

    seccion('Servidor que NO contesta HEAD (el caso sospechoso)')
    srv = _ServidorConRangos(grande, sin_head=True)
    try:
        gdal.VSICurlClearCache()
        ent, _err = _listar(
            '/vsizip/{/vsicurl/' + f'http://127.0.0.1:{srv.puerto}/g.zip}}')
        sin_respaldo = _hay_vh(ent)
        print(f'        sin respaldo: '
              f'{"lista" if sin_respaldo else "NO lista"} el contenido')

        # El respaldo que aplica el complemento.
        gdal.VSICurlClearCache()
        previo = gdal.GetConfigOption('CPL_VSIL_CURL_USE_HEAD', None)
        try:
            gdal.SetConfigOption('CPL_VSIL_CURL_USE_HEAD', 'NO')
            ent2, err2 = _listar(
                '/vsizip/{/vsicurl/'
                + f'http://127.0.0.1:{srv.puerto}/g.zip}}')
        finally:
            gdal.SetConfigOption('CPL_VSIL_CURL_USE_HEAD', previo)
        comp('con CPL_VSIL_CURL_USE_HEAD=NO si se lista', _hay_vh(ent2),
             f'es el respaldo que aplica el complemento; si tampoco lista, '
             f'la causa es otra. entradas={len(ent2)} {err2}')
        if sin_respaldo:
            print('        NOTA: este GDAL lista igual sin el respaldo, de '
                  'modo que aqui no se reproduce el fallo de produccion.')
    finally:
        srv.cerrar()

    print('\n' + '=' * 60)
    if fallos:
        print(f'FALLOS: {len(fallos)} de {hechas} comprobaciones')
        for f in fallos:
            print(f'  - {f}')
        return 1
    print(f'OK — {hechas} comprobaciones')
    return 0


if __name__ == '__main__':
    sys.exit(main())
