# -*- coding: utf-8 -*-
# SPDX-License-Identifier: GPL-2.0-or-later
"""
Exportar a Google Earth (KMZ) — algoritmo de Processing
=======================================================

Convierte la capa elegida —ráster o vectorial— a KMZ y, si se pide, la
abre en Google Earth.

QUÉ VERSIÓN DE GOOGLE EARTH
    KMZ es el formato que leen TODAS las versiones: Earth Pro de
    escritorio, Earth Web y las apps móviles. El archivo, por tanto, sirve
    en todas.

    Abrirlo automáticamente es otra cosa. Solo el Earth de escritorio se
    puede lanzar desde fuera, por la asociación de archivos del sistema.
    Earth Web y las apps móviles no exponen ninguna forma de que otra
    aplicación les entregue un archivo: la documentación de Google
    describe únicamente la importación manual («Importar archivo» /
    «Abrir archivo KML local»). Así que el algoritmo intenta abrirlo, y si
    no puede, dice dónde quedó el archivo y qué hacer. No hay manera de
    prometer más que eso sin prometer algo falso.

POR QUÉ SE RENDERIZA EL RÁSTER ANTES DE CONVERTIR
    KML no sabe de valores, solo de imágenes: un ráster viaja como
    GroundOverlay, es decir como PNG o JPEG pegado al terreno. Volcar un
    GeoTIFF de NDVI sin más daría una imagen gris ilegible, porque los
    valores van de −1 a 1 y nadie los ha mapeado a color.
    Aquí se renderiza la capa CON LA SIMBOLOGÍA QUE USTED VE en QGIS
    —paleta, realce, clases— y esa imagen es la que se empotra. Lo que ve
    en el lienzo es lo que verá en Google Earth.

POR QUÉ SE REPROYECTA A EPSG:4326
    KML está definido en coordenadas geográficas WGS84 y nada más. Sus
    recortes están en UTM 16N/17N, de modo que hay que reproyectar; no es
    una opción que convenga dejar al usuario.

LO QUE NO HACE
    No sube nada a ningún servidor ni necesita cuenta. El KMZ queda en su
    disco.

Autor    : Jorge Fallas (jfallas56@gmail.com)
Licencia : GPL v2 o posterior
Versión  : 1.0.1

Historial:
    1.0.1 (2026-10-07): El KMZ vectorial se aplana para que el documento
        viva en doc.kml. LIBKML escribia el nombre de la capa en
        UTF-8 crudo sin el bit 11 del ZIP, y con tildes el enlace
        interno no resolvia: el archivo abria vacio.
    1.0.0 (2026-10-02): Primera versión pública.
        El historial detallado del desarrollo previo a la publicación está en
        CHANGELOG.md del repositorio:
        https://github.com/jfallas56-CR/BuscadorSTAC
"""

import logging
import os
import re
import zipfile

from qgis.PyQt.QtCore import QCoreApplication, QUrl
from qgis.PyQt.QtGui import QDesktopServices

from qgis.core import (QgsCoordinateReferenceSystem,
                       QgsCoordinateTransform,
                       QgsFeature,
                       QgsField,
                       QgsFields,
                       QgsProcessingAlgorithm,
                       QgsProcessingException,
                       QgsProcessingParameterBoolean,
                       QgsProcessingParameterEnum,
                       QgsProcessingParameterField,
                       QgsProcessingParameterFileDestination,
                       QgsProcessingParameterMapLayer,
                       QgsProcessingParameterNumber,
                       QgsProcessingParameterString,
                       QgsProject,
                       QgsRasterFileWriter,
                       QgsRasterLayer,
                       QgsRasterPipe,
                       QgsRasterProjector,
                       QgsVectorFileWriter,
                       QgsVectorLayer,
                       QgsWkbTypes)

from .buscar_sentinel2_algoritmo import AUTOR, AUTOR_EMAIL, _STR
from .core import (TOPE_ENTIDADES_WEB, TOPE_VERTICES_WEB,
                   capa_a_promover, contar_kml, fecha_kml,
                   href_inseguro, insertar_opacidad_kml)

try:
    from osgeo import gdal, ogr
except ImportError:                                      # pragma: no cover
    gdal = ogr = None

_LOG = logging.getLogger('BuscadorSTAC')

# Lado máximo del super-overlay. Un KMZ enorme tarda en abrirse y Earth lo
# remuestrea de todos modos; 4096 es un compromiso razonable para un AOI.
PX_LADO_POR_OMISION = 2048

FORMATOS_IMAGEN = ['PNG (conserva transparencia)', 'JPEG (menos peso)']


class ExportarGoogleEarthAlgorithm(QgsProcessingAlgorithm):
    """Capa ráster o vectorial a KMZ, y abrirlo en Google Earth."""

    VERSION = 'v1.0.1'

    CAPA = 'CAPA'
    CAMPO_FECHA = 'CAMPO_FECHA'
    NOMBRE = 'NOMBRE'
    PX_LADO = 'PX_LADO'
    FORMATO = 'FORMATO'
    OPACIDAD = 'OPACIDAD'
    ABRIR = 'ABRIR'
    SALIDA = 'SALIDA'

    def __init__(self):
        super().__init__()
        self._ruta_kmz = ''
        self._abrir = False

    # ---------------------------------------------------------- identidad
    def name(self):
        return 'exportargoogleearth'

    def displayName(self):
        return self.tr('Exportar a Google Earth (KMZ)')

    def group(self):
        return self.tr('Teledetección')

    def groupId(self):
        return 'teledeteccion'

    def tr(self, cadena):
        return QCoreApplication.translate('ExportarGoogleEarthAlgorithm',
                                          cadena)

    def createInstance(self):
        return ExportarGoogleEarthAlgorithm()

    def shortHelpString(self):
        return self.tr(
            f"<b>Exportar a Google Earth (KMZ)</b> — {self.VERSION}<br><br>"
            "Convierte la capa elegida a KMZ y, si lo pide, la abre en "
            "Google Earth. Sirve para enseñar un resultado a alguien que no "
            "tiene QGIS, o para verlo sobre el relieve y las fotos "
            "oblicuas de Earth.<br><br>"
            "<b>Qué versión de Google Earth</b><br>"
            "El KMZ lo leen <i>todas</i>: Earth Pro de escritorio, Earth "
            "Web y las apps móviles. Abrirlo solo se puede automatizar en "
            "el de <b>escritorio</b>, mediante la asociación de archivos "
            "del sistema. Earth Web y las apps no permiten que otra "
            "aplicación les pase un archivo —la documentación de Google "
            "describe únicamente la importación manual—, así que para esas "
            "versiones el algoritmo le deja el archivo y le dice dónde "
            "está; usted lo importa con «Importar archivo» o «Abrir "
            "archivo KML local».<br><br>"
            "<b>Ráster</b><br>"
            "KML no guarda valores, solo imágenes pegadas al terreno. Por "
            "eso la capa se renderiza primero <b>con la simbología que "
            "usted ve en QGIS</b> —paleta, realce, clases— y esa imagen es "
            "la que se empotra: lo del lienzo es lo que verá en Earth. "
            "Exportar un NDVI sin renderizar daría una imagen gris, porque "
            "sus valores van de −1 a 1 y nadie los ha llevado a color.<br>"
            "Se genera un <i>super-overlay</i>: la imagen se parte en "
            "teselas con niveles de detalle, de modo que Earth carga solo "
            "lo que hace falta al acercarse.<br><br>"
            "<b>Vectorial</b><br>"
            "Se escribe con el controlador LIBKML, que conserva los "
            "atributos: en Earth se ven al pulsar cada elemento. Es la vía "
            "para llevarse la capa de huellas con su nubosidad y sus "
            "fechas.<br><br>"
            "<b>Línea de tiempo</b><br>"
            "Si indica un «Campo de fecha», cada entidad sale con su "
            "<code>&lt;TimeStamp&gt;</code> y Google Earth muestra el "
            "control deslizante de tiempo: una serie de huellas se recorre "
            "o se acota a un intervalo, en vez de verse toda encimada. En "
            "la capa de huellas de este complemento el campo es "
            "<code>fecha</code>.<br>"
            "Las fechas se normalizan a ISO 8601 antes de escribir, porque "
            "Earth ignora en silencio cualquier otra forma: el archivo "
            "abre, las entidades se ven, y la línea de tiempo no aparece. "
            "Se informa cuántas se entendieron y cuántas no.<br><br>"
            "<b>Reproyección</b><br>"
            "KML solo existe en coordenadas geográficas WGS84 "
            "(EPSG:4326), así que la capa se reproyecta siempre. Sus "
            "recortes están en UTM 16N/17N; no hay que hacer nada, pero "
            "conviene saber que el KMZ no está en el SRC del original.<br>"
            "<br>"
            "<b>Límites de Earth Web</b><br>"
            f"Por encima de {TOPE_ENTIDADES_WEB:,} entidades o "
            f"{TOPE_VERTICES_WEB:,} vértices, Earth Web importa el archivo "
            "como capa de datos de solo lectura en lugar de como elementos "
            "editables. Se ve igual; cambia lo que puede hacer con él. El "
            "algoritmo avisa si su capa pasa de ahí.<br><br>"
            "<b>Privacidad</b><br>"
            "No se sube nada a ningún servidor y no hace falta cuenta de "
            "Google. El KMZ queda en su disco."
            "<hr>"
            f"<b>Autor:</b> {AUTOR} &lt;{AUTOR_EMAIL}&gt;<br>"
            f"<b>Versión:</b> {self.VERSION} · <b>Licencia:</b> GPL v2 o "
            f"posterior"
        )

    # -------------------------------------------------------- parámetros
    def initAlgorithm(self, config=None):
        p = QgsProcessingParameterMapLayer(
            self.CAPA, self.tr('Capa a exportar (ráster o vectorial)'))
        p.setHelp(self.tr(
            'Acepta las dos cosas. Un ráster se renderiza con su '
            'simbología actual; una capa vectorial se escribe con sus '
            'atributos.'))
        self.addParameter(p)

        p = QgsProcessingParameterField(
            self.CAMPO_FECHA,
            self.tr('Campo de fecha (pone la capa en la línea de tiempo)'),
            parentLayerParameterName=self.CAPA, optional=True)
        p.setHelp(self.tr(
            'Solo para capas vectoriales. Si indica un campo con fechas, '
            'cada entidad sale con su <code>&lt;TimeStamp&gt;</code> y '
            'Google Earth muestra el control deslizante de tiempo: se puede '
            'recorrer la serie, o acotarla a un intervalo, en vez de ver '
            'todas las huellas encimadas.<br><br>'
            'En la capa de huellas de este complemento el campo es '
            '<code>fecha</code>.<br><br>'
            'Las fechas se normalizan a ISO 8601 antes de escribir, porque '
            'Google Earth ignora en silencio cualquier otra forma: el '
            'archivo abre, las entidades se ven y la línea de tiempo '
            'sencillamente no aparece. Se admiten <code>AAAA-MM-DD</code>, '
            '<code>AAAA-MM-DD hh:mm:ss</code>, ISO con zona horaria y los '
            'campos de tipo fecha. Un <code>03/01/2025</code> se rechaza a '
            'propósito: no se puede saber si es 3 de enero o 1 de marzo, y '
            'adivinarlo desplazaría la serie entera.'))
        self.addParameter(p)

        p = QgsProcessingParameterString(
            self.NOMBRE, self.tr('Nombre dentro de Google Earth (opcional)'),
            defaultValue='', optional=True)
        p.setHelp(self.tr(
            'Lo que aparecerá en el panel «Lugares» de Google Earth. '
            'Vacío: se usa el nombre de la capa.'))
        self.addParameter(p)

        p = QgsProcessingParameterNumber(
            self.PX_LADO, self.tr('Ráster: lado máximo en píxeles'),
            type=QgsProcessingParameterNumber.Integer,
            defaultValue=PX_LADO_POR_OMISION, minValue=256, maxValue=16384)
        p.setHelp(self.tr(
            'Resolución de la imagen que se empotra. Más píxeles es más '
            'detalle y más peso; Earth remuestrea de todos modos al '
            'alejarse. 2048 va bien para un AOI de unos pocos kilómetros; '
            'suba a 4096 si va a acercarse mucho.'))
        self.addParameter(p)

        p = QgsProcessingParameterEnum(
            self.FORMATO, self.tr('Ráster: formato de la imagen'),
            options=FORMATOS_IMAGEN, defaultValue=0)
        p.setHelp(self.tr(
            'PNG conserva la transparencia, que es lo que deja ver el '
            'relieve de Earth por los huecos sin dato (nube enmascarada, '
            'fuera del recorte). JPEG pesa menos pero rellena esos huecos '
            'de negro.'))
        self.addParameter(p)

        p = QgsProcessingParameterNumber(
            self.OPACIDAD, self.tr('Ráster: opacidad (%)'),
            type=QgsProcessingParameterNumber.Integer,
            defaultValue=100, minValue=10, maxValue=100)
        p.setHelp(self.tr(
            'Por debajo de 100 se transparenta la capa sobre la imagen de '
            'Earth, útil para comprobar que el recorte cae donde debe.'))
        self.addParameter(p)

        p = QgsProcessingParameterBoolean(
            self.ABRIR, self.tr('Abrir en Google Earth al terminar'),
            defaultValue=True)
        p.setHelp(self.tr(
            'Solo funciona con el Google Earth de ESCRITORIO, por la '
            'asociación de archivos del sistema. Si no está instalado, o '
            'si usa Earth Web, el algoritmo le dirá la ruta del KMZ para '
            'que lo importe a mano.'))
        self.addParameter(p)

        p = QgsProcessingParameterFileDestination(
            self.SALIDA, self.tr('Archivo KMZ de salida'),
            fileFilter='KMZ (*.kmz *.KMZ)')
        p.setHelp(self.tr(
            'KMZ es KML comprimido con sus imágenes dentro: un solo '
            'archivo que se puede enviar por correo.'))
        self.addParameter(p)

    # ------------------------------------------------------- validación
    def checkParameterValues(self, parameters, context):
        if gdal is None:
            return False, self.tr(
                '[!] Falta osgeo.gdal, que viene con QGIS. Sin él no se '
                'puede generar el KMZ de un ráster.')

        capa = self.parameterAsLayer(parameters, self.CAPA, context)
        if capa is None:
            return False, self.tr(
                '[!] Elija una capa en «Capa a exportar (ráster o '
                'vectorial)».')
        if not capa.isValid():
            return False, self.tr(
                '[!] La capa «{n}» no es válida: QGIS no puede leer su '
                'fuente. Compruebe que el archivo sigue en su sitio.'
            ).format(n=capa.name())

        # Se distingue el tipo con isinstance y NO con capa.type(), porque
        # ese enum es de los que se movieron en Qt6: QgsMapLayer.RasterLayer
        # en QGIS 3 pasa a Qgis.LayerType.Raster en QGIS 4. La clase es
        # estable en las dos.
        if isinstance(capa, QgsRasterLayer):
            if gdal.GetDriverByName('KMLSUPEROVERLAY') is None:
                return False, self.tr(
                    '[!] Su GDAL no trae el controlador KMLSUPEROVERLAY, '
                    'necesario para empotrar un ráster en KMZ. Compruébelo '
                    'con «gdalinfo --formats | grep -i kml». Una capa '
                    'vectorial sí se puede exportar.')
        elif isinstance(capa, QgsVectorLayer):
            if capa.featureCount() == 0:
                return False, self.tr(
                    '[!] La capa «{n}» no tiene ninguna entidad que '
                    'exportar.').format(n=capa.name())
        else:
            return False, self.tr(
                '[!] Solo se pueden exportar capas ráster o vectoriales. '
                '«{n}» es de otro tipo.').format(n=capa.name())

        if not capa.crs().isValid():
            return False, self.tr(
                '[!] La capa «{n}» no declara SRC, así que no se puede '
                'reproyectar a WGS84, que es el único sistema que admite '
                'KML. Asígnele su SRC real antes de exportar.'
            ).format(n=capa.name())

        return super().checkParameterValues(parameters, context)

    # -------------------------------------------------------- ejecución
    def processAlgorithm(self, parameters, context, feedback):
        capa = self.parameterAsLayer(parameters, self.CAPA, context)
        destino = self.parameterAsFileOutput(parameters, self.SALIDA, context)
        rotulo = (self.parameterAsString(parameters, self.NOMBRE, context)
                  or '').strip() or capa.name()
        self._abrir = self.parameterAsBool(parameters, self.ABRIR, context)

        if not destino.lower().endswith('.kmz'):
            destino += '.kmz'
        carpeta = os.path.dirname(destino)
        if carpeta and not os.path.isdir(carpeta):
            raise QgsProcessingException(
                f'La carpeta de salida no existe: {carpeta}')

        feedback.pushInfo(f'Exportar a Google Earth {self.VERSION}')
        feedback.pushInfo(f'Capa    : {capa.name()}')
        feedback.pushInfo(f'SRC     : {capa.crs().authid()} -> EPSG:4326 '
                          f'(KML solo existe en WGS84)')
        feedback.pushInfo(f'Salida  : {destino}')

        es_raster = isinstance(capa, QgsRasterLayer)
        if es_raster:
            self._exportar_raster(capa, destino, rotulo, parameters, context,
                                  feedback)
        else:
            destino = self._exportar_vector(
                capa, destino, rotulo, context, feedback,
                campo_fecha=self.parameterAsString(
                    parameters, self.CAMPO_FECHA, context))

        if not os.path.exists(destino):
            raise QgsProcessingException(
                'La conversión terminó sin error pero no se escribió '
                f'ningún archivo en {destino}. Revise el registro.')

        # Solo el camino vectorial. Un KMZ de raster lo escribe
        # KMLSUPEROVERLAY, cuyos miembros son rutas de teselas en
        # ASCII: nunca tuvo este problema, y uno de una sola tesela
        # tiene la misma forma que el de LIBKML —doc.kml con solo un
        # NetworkLink y un unico .kml— asi que conviene no acercarse.
        if not es_raster:
            self._aplanar_kmz(destino, feedback)

        mib = os.path.getsize(destino) / 1048576.0
        feedback.pushInfo(f'\nKMZ escrito: {destino}  ({mib:.2f} MiB)')
        self._avisar_contenido(destino, feedback)
        self._ruta_kmz = destino
        return {self.SALIDA: destino}

    # --- ráster ---------------------------------------------------------
    def _exportar_raster(self, capa, destino, rotulo, parameters, context,
                         feedback):
        """Renderiza con la simbología de la capa y hace el super-overlay."""
        px_lado = self.parameterAsInt(parameters, self.PX_LADO, context)
        idx_fmt = self.parameterAsEnum(parameters, self.FORMATO, context)
        opacidad = self.parameterAsInt(parameters, self.OPACIDAD, context)
        formato = 'PNG' if idx_fmt == 0 else 'JPEG'

        crs4326 = QgsCoordinateReferenceSystem('EPSG:4326')
        ext = capa.extent()
        if capa.crs() != crs4326:
            transformador = QgsCoordinateTransform(
                capa.crs(), crs4326,
                context.project() or QgsProject.instance())
            ext = transformador.transformBoundingBox(ext)

        # Lado mayor al máximo pedido, conservando la proporción.
        if ext.width() <= 0 or ext.height() <= 0:
            raise QgsProcessingException(
                'La extensión de la capa es degenerada tras reproyectar a '
                'EPSG:4326; no hay nada que dibujar.')
        if ext.width() >= ext.height():
            cols = int(px_lado)
            filas = max(1, int(round(px_lado * ext.height() / ext.width())))
        else:
            filas = int(px_lado)
            cols = max(1, int(round(px_lado * ext.width() / ext.height())))
        feedback.pushInfo(f'Imagen  : {cols} x {filas} px, {formato}')

        tmp_tif = destino[:-4] + '_render.tif'
        # El renderizador de la capa va DENTRO de la tubería: es lo que
        # convierte los valores en color. Sin él saldría la banda cruda, y
        # un índice de −1 a 1 se vería gris uniforme.
        renderizador = capa.renderer()
        if renderizador is None:
            raise QgsProcessingException(
                f'La capa «{capa.name()}» no tiene renderizador, así que no '
                f'hay simbología que aplicar. Ábrala en QGIS y dele un '
                f'estilo antes de exportar.')

        tuberia = QgsRasterPipe()
        if not tuberia.set(capa.dataProvider().clone()):
            raise QgsProcessingException(
                'No se pudo preparar el proveedor de datos de la capa.')
        if not tuberia.set(renderizador.clone()):
            raise QgsProcessingException(
                'No se pudo aplicar el renderizador de la capa a la '
                'tubería de exportación.')

        # Sin esto, un ráster que no esté ya en WGS84 sale COMPLETAMENTE
        # transparente y sin ningún error: el escritor recorre la extensión
        # de destino en grados y se la pide al proveedor, que está en otro
        # sistema, de modo que cada bloque cae fuera de la fuente y
        # devuelve nodata. El KMZ resultante es válido, lleva su
        # GroundOverlay y sus imágenes, y en Google Earth no se ve nada.
        #
        # Importa justo en el caso normal: el recorte de Sentinel-2 y
        # Landsat de este mismo complemento conserva el UTM de la escena,
        # así que lo que el usuario acaba de descargar es precisamente lo
        # que no se exportaba.
        if capa.crs() != crs4326:
            proyector = QgsRasterProjector()
            try:
                proyector.setCrs(capa.crs(), crs4326,
                                 context.transformContext())
            except TypeError:
                # Firma anterior, sin contexto de transformación.
                proyector.setCrs(capa.crs(), crs4326)
            # set() y no insert(): la tubería coloca cada interfaz según su
            # papel, y el del proyector va después del renderizador. Fijar
            # el índice a mano se rompe si ese orden cambia de versión.
            if not tuberia.set(proyector):
                raise QgsProcessingException(
                    f'No se pudo añadir la reproyección de '
                    f'{capa.crs().authid()} a EPSG:4326 a la tubería de '
                    f'exportación. Reproyecte la capa a EPSG:4326 con '
                    f'«Combar (reproyectar)» y vuelva a exportar.')
            feedback.pushInfo(
                f'Reproyección: {capa.crs().authid()} -> EPSG:4326 '
                f'dentro de la tubería')

        escritor = QgsRasterFileWriter(tmp_tif)
        escritor.setOutputFormat('GTiff')
        resultado = escritor.writeRaster(tuberia, cols, filas, ext, crs4326,
                                         context.transformContext())
        # writeRaster devuelve 0 (NoError) al ir bien; el enum vive en
        # QgsRasterFileWriter y su nombre cambia entre versiones, así que
        # se compara con 0, que es estable.
        if int(resultado) != 0 or not os.path.exists(tmp_tif):
            raise QgsProcessingException(
                f'La renderización del ráster falló (código {resultado}). '
                f'Si la capa es muy grande, baje «lado máximo en píxeles».')

        self._comprobar_visible(tmp_tif, capa, feedback)

        try:
            self._superoverlay(tmp_tif, destino, rotulo, formato, opacidad,
                               feedback)
        finally:
            for sufijo in ('', '.aux.xml'):
                try:
                    if os.path.exists(tmp_tif + sufijo):
                        os.remove(tmp_tif + sufijo)
                except OSError as e:
                    feedback.pushDebugInfo(f'[limpieza] {tmp_tif}{sufijo}: {e}')

    @staticmethod
    def _comprobar_visible(tmp_tif, capa, feedback):
        """Para si la imagen renderizada quedó entera transparente.

        Ese es el fallo que no avisa: el KMZ sale válido, con su
        GroundOverlay y sus imágenes, y en Google Earth no se ve nada. Sin
        esta comprobación el usuario no tiene de dónde agarrar —no hay
        error que buscar—, así que vale más detenerse aquí y decir por qué.

        Se lee el canal alfa con estadísticas aproximadas y no con el
        arreglo completo: de un 4096x4096 son unos 16 MiB por banda, y la
        única pregunta es si hay algún píxel opaco.
        """
        if gdal is None:
            return
        ds = gdal.Open(tmp_tif)
        if ds is None or ds.RasterCount < 4:
            return
        try:
            maximo = ds.GetRasterBand(4).ComputeStatistics(True)[1]
        except RuntimeError as e:
            # Sin estadísticas no se puede concluir; no es motivo para
            # abortar una exportación que quizá esté bien.
            feedback.pushDebugInfo(f'[visible] no se pudo medir el alfa: {e}')
            return
        finally:
            ds = None
        if maximo and maximo > 0:
            return
        raise QgsProcessingException(
            f'La imagen renderizada salió entera transparente, así que el '
            f'KMZ se abriría en Google Earth sin que se viera nada. La capa '
            f'«{capa.name()}» está en {capa.crs().authid()}. Comprueba que '
            f'tenga datos en su extensión y que su simbología no sea toda '
            f'transparente; si acaba de reproyectarla, vuelva a cargarla '
            f'antes de exportar.')

    def _superoverlay(self, origen, destino, rotulo, formato, opacidad,
                      feedback):
        """gdal_translate a KMLSUPEROVERLAY, pasando solo opciones reales.

        Las opciones de creación del controlador cambian entre versiones de
        GDAL. Pasar una que no existe aborta la conversión con un error que
        parece del dato, así que se consulta lo que el controlador declara
        y se pasa solo eso.
        """
        driver = gdal.GetDriverByName('KMLSUPEROVERLAY')
        declaradas = driver.GetMetadataItem(
            gdal.DMD_CREATIONOPTIONLIST) or ''
        # La opacidad NO es opción de creación de este controlador: se
        # aplica después, editando el KML dentro del KMZ.
        opciones = []
        for clave, valor in (('NAME', rotulo), ('FORMAT', formato)):
            if not valor:
                continue
            if f'{clave}=' in declaradas or f"name='{clave}'" in declaradas \
                    or f'"{clave}"' in declaradas:
                opciones.append(f'{clave}={valor}')
            else:
                feedback.pushDebugInfo(
                    f'[kmz] su GDAL no declara la opción {clave}; se omite')
        feedback.pushInfo(f'Opciones KMLSUPEROVERLAY: '
                          f'{opciones or "(ninguna admitida)"}')

        gdal.UseExceptions()
        try:
            salida = gdal.Translate(destino, origen,
                                    format='KMLSUPEROVERLAY',
                                    creationOptions=opciones)
        except RuntimeError as e:
            raise QgsProcessingException(
                f'GDAL no pudo escribir el KMZ: {e}')
        if salida is None:
            raise QgsProcessingException(
                'GDAL no pudo escribir el KMZ y no dio motivo. Pruebe con '
                'un «lado máximo en píxeles» menor.')
        salida = None

        if opacidad < 100:
            self._aplicar_opacidad(destino, opacidad, feedback)

    @staticmethod
    def _aplicar_opacidad(kmz, opacidad, feedback):
        """Reescribe el KMZ con <color> en sus GroundOverlay.

        Aquí solo va la entrada/salida del ZIP: el orden de los canales y
        la aritmética del alfa están en core.insertar_opacidad_kml, que se
        prueba sin QGIS. Equivocar ese orden no se vería hasta abrir
        Google Earth.
        """
        try:
            with zipfile.ZipFile(kmz, 'r') as z:
                contenido = {n: z.read(n) for n in z.namelist()}
        except (zipfile.BadZipFile, OSError, KeyError) as e:
            feedback.pushWarning(
                f'[!] No se pudo aplicar la opacidad (el KMZ sí está '
                f'escrito): {e}')
            return

        tocados = 0
        for nombre in list(contenido):
            if not nombre.lower().endswith('.kml'):
                continue
            try:
                texto = contenido[nombre].decode('utf-8')
            except UnicodeDecodeError:
                continue
            texto, n = insertar_opacidad_kml(texto, opacidad)
            if n:
                contenido[nombre] = texto.encode('utf-8')
                tocados += n

        if not tocados:
            feedback.pushWarning(
                '[!] No se encontró ningún GroundOverlay al que aplicar la '
                'opacidad; el KMZ queda opaco.')
            return
        try:
            with zipfile.ZipFile(kmz, 'w', zipfile.ZIP_DEFLATED) as z:
                for nombre, datos in contenido.items():
                    z.writestr(nombre, datos)
            feedback.pushInfo(f'Opacidad {opacidad} % aplicada a {tocados} '
                              f'superposición(es).')
        except OSError as e:
            raise QgsProcessingException(
                f'El KMZ se corrompió al reescribirlo con la opacidad: {e}')

    # --- vectorial ------------------------------------------------------
    def _con_timestamp(self, capa, campo, feedback):
        """Copia en memoria con un campo «timestamp» en ISO 8601.

        LIBKML escribe <TimeStamp> desde un campo llamado así, y eso es
        lo que enciende el control de tiempo de Google Earth.

        Se NORMALIZA en una copia en vez de apuntar el controlador al
        campo original con LIBKML_TIMESTAMP_FIELD, que sería más barato,
        porque Google Earth ignora en silencio cualquier forma que no sea
        ISO 8601: el KMZ abriría, las entidades se verían, y la línea de
        tiempo no aparecería sin que nada lo explique. Pasar por aquí
        permite además CONTAR cuántas fechas se entendieron y decirlo.

        Si la capa ya traía un campo «timestamp», la copia lo sustituye:
        es el nombre que el controlador interpreta, y dejar los dos haría
        que el resultado dependiera del orden de los campos.
        """
        idx = capa.fields().indexOf(campo)
        if idx < 0:
            feedback.pushWarning(
                f'[!] La capa no tiene el campo «{campo}»; se exporta sin '
                f'línea de tiempo.')
            return capa, 0, 0

        campos = QgsFields()
        for f in capa.fields():
            if f.name() != 'timestamp':
                campos.append(f)
        campos.append(QgsField('timestamp', _STR))

        tipo = QgsWkbTypes.displayString(capa.wkbType()) or 'Polygon'
        mem = QgsVectorLayer(f'{tipo}?crs={capa.crs().authid()}',
                             capa.name(), 'memory')
        if not mem.isValid():
            feedback.pushWarning(
                '[!] No se pudo preparar la capa temporal para la línea de '
                'tiempo; se exporta sin ella.')
            return capa, 0, 0
        mem.dataProvider().addAttributes(list(campos))
        mem.updateFields()

        # La selección se resuelve AQUÍ: la copia no la hereda, y dejar
        # que el escritor la aplicase después exportaría la capa entera.
        origen = (capa.getSelectedFeatures() if capa.selectedFeatureCount()
                  else capa.getFeatures())
        nombres = [c.name() for c in mem.fields()]
        nuevas, con_fecha, sin_fecha = [], 0, 0
        for f in origen:
            valor = f[campo]
            # QDate/QDateTime a objeto de Python, y NO con Qt.ISODate: en
            # Qt6 ese enum vive en Qt.DateFormat.ISODate y la forma plana
            # desaparece. Lo cazó api_audit antes de que llegara a nadie.
            # toPyDateTime()/toPyDate() existen en PyQt5 y PyQt6, y lo que
            # devuelven ya lo entiende fecha_kml por su isoformat().
            for _conv in ('toPyDateTime', 'toPyDate'):
                _f = getattr(valor, _conv, None)
                if callable(_f):
                    valor = _f()
                    break
            iso = fecha_kml(valor)
            if iso:
                con_fecha += 1
            else:
                sin_fecha += 1
            nf = QgsFeature(mem.fields())
            nf.setGeometry(f.geometry())
            nf.setAttributes([iso if n == 'timestamp' else f[n]
                              for n in nombres])
            nuevas.append(nf)
        mem.dataProvider().addFeatures(nuevas)
        mem.updateExtents()
        return mem, con_fecha, sin_fecha

    def _exportar_vector(self, capa, destino, rotulo, context, feedback,
                         campo_fecha=''):
        """LIBKML si está; si no, KML y archivo .kml. Devuelve la ruta real.

        Solo LIBKML escribe KMZ. El controlador KML antiguo escribe .kml a
        secas, así que si LIBKML falta no se puede cumplir la extensión
        pedida: se degrada a .kml y se DICE, en vez de escribir un .kmz que
        por dentro no es un ZIP y que Google Earth rechazaría con un error
        sobre el archivo, no sobre el controlador.
        """
        feedback.pushInfo(f'Entidades: {capa.featureCount()}')

        usa_copia = False
        if campo_fecha:
            capa, con_fecha, sin_fecha = self._con_timestamp(
                capa, campo_fecha, feedback)
            usa_copia = bool(con_fecha or sin_fecha)
            if usa_copia:
                feedback.pushInfo(
                    f'Línea de tiempo: {con_fecha} entidad(es) con fecha '
                    f'utilizable en «{campo_fecha}».')
            if sin_fecha:
                feedback.pushWarning(
                    f'[!] {sin_fecha} entidad(es) sin fecha interpretable en '
                    f'«{campo_fecha}»: salen sin <TimeStamp> y Google Earth '
                    f'las muestra siempre, fuera del control de tiempo. Se '
                    f'admite ISO 8601 (AAAA-MM-DD); una fecha como '
                    f'03/01/2025 se rechaza porque no se puede saber si es '
                    f'3 de enero o 1 de marzo.')
            if con_fecha == 0 and sin_fecha:
                feedback.pushWarning(
                    f'[!] NINGUNA fecha de «{campo_fecha}» se pudo '
                    f'interpretar, así que no habrá línea de tiempo.')

        hay_libkml = bool(ogr and ogr.GetDriverByName('LIBKML'))
        if hay_libkml:
            controlador = 'LIBKML'
        else:
            controlador = 'KML'
            destino = destino[:-4] + '.kml'
            feedback.pushWarning(
                '[!] Su GDAL no trae el controlador LIBKML, que es el único '
                'que escribe KMZ. Se escribe KML sin comprimir: '
                f'{os.path.basename(destino)}. Google Earth lo abre igual; '
                'solo pierde el empaquetado en un archivo único.')
        feedback.pushInfo(f'Controlador: {controlador}')

        opciones = QgsVectorFileWriter.SaveVectorOptions()
        opciones.driverName = controlador
        opciones.fileEncoding = 'UTF-8'
        opciones.ct = QgsCoordinateTransform(
            capa.crs(), QgsCoordinateReferenceSystem('EPSG:4326'),
            context.project() or QgsProject.instance())
        opciones.layerName = rotulo
        if capa.selectedFeatureCount() and not usa_copia:
            feedback.pushInfo(
                f'Se exportan solo las {capa.selectedFeatureCount()} '
                f'entidades seleccionadas.')
            opciones.onlySelectedFeatures = True

        res = QgsVectorFileWriter.writeAsVectorFormatV3(
            capa, destino, context.transformContext(), opciones)
        # writeAsVectorFormatV3 devuelve (código, mensaje) o una tupla más
        # larga según la versión; el código 0 es «sin error» en todas.
        codigo = res[0] if isinstance(res, (tuple, list)) else res
        mensaje = ''
        if isinstance(res, (tuple, list)) and len(res) > 1:
            mensaje = str(res[1] or '')
        if int(codigo) != 0:
            raise QgsProcessingException(
                f'La escritura del KMZ falló (código {codigo}): {mensaje}')
        return destino

    # --- aplanado del KMZ -----------------------------------------------
    def _aplanar_kmz(self, kmz, feedback):
        """Sube el KML de la capa a `doc.kml` y quita el <NetworkLink>.

        El porqué está en core.capa_a_promover. Aquí solo se reescribe el
        archivo, y si algo falla se deja el KMZ como estaba: un KMZ con el
        enlace es lo que había hasta ahora, y es mejor que ninguno.
        """
        if not zipfile.is_zipfile(kmz):
            return                      # KML sin comprimir: no aplica
        try:
            with zipfile.ZipFile(kmz) as z:
                nombres = z.namelist()
                if 'doc.kml' not in nombres:
                    return
                doc = z.read('doc.kml').decode('utf-8', 'replace')
                capa = capa_a_promover(nombres, doc)
                if not capa:
                    self._avisar_hrefs(doc, nombres, z, feedback)
                    return
                contenido = z.read(capa)
        except (zipfile.BadZipFile, OSError, KeyError) as e:
            feedback.pushDebugInfo(f'[kmz] no se pudo aplanar: {e}')
            return

        temporal = kmz + '.aplanado'
        try:
            with zipfile.ZipFile(temporal, 'w',
                                 zipfile.ZIP_DEFLATED) as z:
                z.writestr('doc.kml', contenido)
            os.replace(temporal, kmz)
        except (OSError, zipfile.BadZipFile) as e:
            feedback.pushWarning(
                f'[!] El KMZ quedó con el enlace interno de LIBKML porque '
                f'no se pudo reescribir: {e}. Si Google Earth lo abre y no '
                f'muestra nada, renombre la capa SIN TILDES NI EÑES y '
                f'vuelva a exportar.')
            try:
                if os.path.exists(temporal):
                    os.remove(temporal)
            except OSError as e2:
                feedback.pushDebugInfo(f'[kmz] temporal: {e2}')
            return

        feedback.pushInfo(
            'KMZ aplanado: el documento va en doc.kml, sin el enlace '
            'interno de LIBKML.')

    def _avisar_hrefs(self, doc, nombres, z, feedback):
        """Si queda algún <href> relativo impronunciable, decirlo."""
        textos = [doc]
        for n in nombres:
            if n != 'doc.kml' and n.lower().endswith('.kml'):
                try:
                    textos.append(z.read(n).decode('utf-8', 'replace'))
                except (OSError, KeyError) as e:
                    feedback.pushDebugInfo(f'[kmz] {n}: {e}')
        for texto in textos:
            for href in re.findall(r'<href>\s*(.*?)\s*</href>', texto,
                                   re.S):
                if '://' in href:
                    continue            # enlace externo, no es asunto nuestro
                malos = href_inseguro(href)
                if malos:
                    feedback.pushWarning(
                        f'[!] El KMZ lleva un enlace interno con '
                        f'caracteres que no valen en una URI ({malos}): '
                        f'«{href}». Google Earth puede abrir el archivo y '
                        f'no mostrar nada. Renombre la capa sin esos '
                        f'caracteres —las tildes y la eñe son las que '
                        f'rompen— y vuelva a exportar.')
                    return

    # --- avisos sobre el contenido --------------------------------------
    def _avisar_contenido(self, kmz, feedback):
        """Cuenta entidades y vértices, y avisa de los topes de Earth Web."""
        texto = ''
        try:
            if zipfile.is_zipfile(kmz):
                with zipfile.ZipFile(kmz) as z:
                    kmls = [n for n in z.namelist()
                            if n.lower().endswith('.kml')]
                    for n in kmls[:4]:
                        texto += z.read(n).decode('utf-8', 'replace')
            else:
                # Camino de degradación: KML sin comprimir, cuando falta
                # LIBKML. Tratarlo como ZIP daría BadZipFile y se perdería
                # el aviso de los topes.
                with open(kmz, encoding='utf-8', errors='replace') as fh:
                    texto = fh.read(4 * 1024 * 1024)
        except (zipfile.BadZipFile, OSError) as e:
            feedback.pushDebugInfo(f'[kmz] no se pudo inspeccionar: {e}')
            return

        cuenta = contar_kml(texto)
        if cuenta['superposiciones']:
            feedback.pushInfo(
                f"Contiene {cuenta['superposiciones']} superposición(es) de "
                f"terreno (imagen pegada al relieve).")
        if cuenta['marcadores']:
            feedback.pushInfo(f"Contiene {cuenta['marcadores']} "
                              f"marcador(es).")
            if cuenta['marcadores'] > TOPE_ENTIDADES_WEB:
                feedback.pushWarning(
                    f"[!] {cuenta['marcadores']:,} entidades pasan del tope "
                    f"de {TOPE_ENTIDADES_WEB:,} de Google Earth Web: allí el "
                    f"archivo entrará como capa de datos de SOLO LECTURA en "
                    f"vez de como elementos editables. Se ve igual; cambia "
                    f"lo que puede hacer con él. El Earth de escritorio no "
                    f"tiene ese tope.")
        if cuenta['vertices'] > TOPE_VERTICES_WEB:
            feedback.pushWarning(
                f"[!] La geometría lleva {cuenta['vertices']:,} vértices, "
                f"por encima del tope de {TOPE_VERTICES_WEB:,} de Earth "
                f"Web. Si lo va a usar allí, simplifique la capa antes "
                f"(Vectorial -> Herramientas de geometría -> Simplificar).")

    # --- apertura, en el hilo principal ---------------------------------
    def postProcessAlgorithm(self, context, feedback):
        """Se ejecuta en el HILO PRINCIPAL tras terminar el algoritmo.

        Es el único punto seguro para pedir al sistema que abra un archivo:
        processAlgorithm() corre en un hilo de trabajo y llamar allí a
        QDesktopServices sería tocar la interfaz desde fuera del hilo de la
        GUI. Es el mismo remedio que usa la hoja de contactos.
        """
        if not (self._abrir and self._ruta_kmz):
            if self._ruta_kmz:
                self._decir_donde(feedback, abierto=False, intentado=False)
            return {}
        if not os.path.exists(self._ruta_kmz):
            feedback.pushWarning(
                f'[!] El KMZ ya no está en {self._ruta_kmz}')
            return {}

        abierto = False
        try:
            abierto = bool(QDesktopServices.openUrl(
                QUrl.fromLocalFile(self._ruta_kmz)))
        except Exception as e:                               # noqa: BLE001
            # Ejecución sin entorno gráfico (qgis_process, servidor).
            _LOG.debug('[abrir_kmz] %s', e)
            feedback.pushDebugInfo(f'[abrir_kmz] {e}')
        self._decir_donde(feedback, abierto=abierto, intentado=True)
        return {}

    def _decir_donde(self, feedback, abierto, intentado):
        """Mensaje final. Honesto sobre qué versión se puede abrir sola."""
        if abierto:
            feedback.pushInfo(
                f'\nEntregado al Google Earth de escritorio: '
                f'{self._ruta_kmz}')
            feedback.pushInfo(
                'Si en vez de Earth se abrió otro programa, es la '
                'asociación de archivos .kmz del sistema; cámbiela y vuelva '
                'a ejecutar.')
            return
        if intentado:
            feedback.pushWarning(
                '[!] El sistema no abrió el archivo. Lo normal es que '
                'Google Earth de escritorio no esté instalado, o que .kmz '
                'no esté asociado a él.')
        feedback.pushInfo(f'\nEl KMZ está en: {self._ruta_kmz}')
        feedback.pushInfo(
            'Para verlo en Google Earth:\n'
            '  - Escritorio (Earth Pro): Archivo -> Abrir, o doble clic.\n'
            '  - Earth Web (earth.google.com): menu -> Proyectos -> Abrir '
            '-> «Importar archivo KML», y elija este archivo. Earth Web no '
            'se puede abrir desde otra aplicacion, de modo que este paso '
            'es manual necesariamente.\n'
            '  - Movil: comparta el archivo a la app de Google Earth.')
