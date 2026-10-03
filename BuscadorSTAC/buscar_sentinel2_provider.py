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
Proveedor de Processing del complemento Buscador Sentinel-2 / Landsat
=====================================================================

Registra el algoritmo en la Caja de Herramientas de Processing bajo su propio
grupo, de modo que aparezca junto a los proveedores nativos de QGIS y quede
disponible para modelos y para qgis_process desde la línea de órdenes.

Autor  : Jorge Fallas (jfallas56@gmail.com)
Versión: 1.0.0
Licencia: GPL v2 o posterior
"""

import os

from qgis.PyQt.QtGui import QIcon
from qgis.core import QgsProcessingProvider

from .buscar_sentinel2_algoritmo import (
    AUTOR, AUTOR_EMAIL, BuscarSentinel2Algorithm)
from .esri_wayback_algoritmo import EsriWaybackAlgorithm
from .google_earth_algoritmo import ExportarGoogleEarthAlgorithm
from .sentinel1_hyp3_algoritmo import Sentinel1Hyp3Algorithm


class BuscarSentinel2Provider(QgsProcessingProvider):
    """Proveedor de los algoritmos del complemento.

    El identificador (`id()`) forma la primera mitad del nombre completo con
    que Processing referencia el algoritmo — «buscadorstac:buscarsentinel2stac».
    Cambiarlo rompe todo modelo guardado que lo invoque, así que se fija aquí
    una vez y no se toca entre versiones.
    """

    VERSION = '1.0.0'
    AUTOR = AUTOR
    AUTOR_EMAIL = AUTOR_EMAIL

    def loadAlgorithms(self):
        # Al añadir uno nuevo hay que registrarlo TAMBIÉN en la lista de
        # entradas de menú de buscar_sentinel2_plugin.py: si solo se hace
        # aquí, aparece en la Caja de Herramientas pero no en el menú, y la
        # inconsistencia pasa inadvertida porque el algoritmo sí funciona.
        self.addAlgorithm(BuscarSentinel2Algorithm())
        self.addAlgorithm(EsriWaybackAlgorithm())
        self.addAlgorithm(Sentinel1Hyp3Algorithm())
        self.addAlgorithm(ExportarGoogleEarthAlgorithm())

    def id(self):
        return 'buscadorstac'

    def name(self):
        return 'Sentinel-2 / Landsat (STAC)'

    def longName(self):
        return (f'Sentinel-2 / Landsat (STAC + COG) {self.VERSION} — '
                f'{self.AUTOR}')

    def icon(self):
        ruta = os.path.join(os.path.dirname(__file__), 'icon.png')
        return QIcon(ruta) if os.path.exists(ruta) else QgsProcessingProvider.icon(self)

    def versionInfo(self):
        return self.VERSION
