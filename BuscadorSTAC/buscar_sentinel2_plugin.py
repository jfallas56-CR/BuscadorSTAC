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
Complemento QGIS — Buscador Sentinel-2 / Landsat (STAC + COG)
============================================================

Clase principal del complemento. Su única responsabilidad es el ciclo de vida:
registrar el proveedor de Processing al cargar, añadir las entradas de menú, y
deshacer ambas cosas al descargar. La lógica de teledetección vive entera en
buscar_sentinel2_algoritmo.py.

Autor  : Jorge Fallas (jfallas56@gmail.com)
Versión: 1.0.0
Licencia: GPL v2 o posterior
"""

import logging
import os

from qgis.PyQt.QtCore import QCoreApplication, QLocale, QSettings, QTranslator
from qgis.PyQt.QtGui import QIcon
from qgis.PyQt.QtWidgets import QAction, QMessageBox
from qgis.core import QgsApplication

from .buscar_sentinel2_provider import BuscarSentinel2Provider

_LOG = logging.getLogger('BuscadorSTAC')

# Nombre completo del algoritmo: id del proveedor + name() del algoritmo.
ALGORITMO_ID = 'buscadorstac:buscarsentinel2stac'
ALGORITMO_ESRI_ID = 'buscadorstac:esriworldimagerywayback'
ALGORITMO_S1_ID = 'buscadorstac:sentinel1rtchyp3'
ALGORITMO_GE_ID = 'buscadorstac:exportargoogleearth'


class BuscarSentinel2Plugin:
    """Ciclo de vida del complemento."""

    MENU = '&Sentinel-2 / Landsat (STAC)'

    def __init__(self, iface):
        self.iface = iface
        self.provider = None
        self.acciones = []
        self._traductor = None
        self._carpeta = os.path.dirname(__file__)
        self._cargar_traduccion()

    # ------------------------------------------------------------- traducción
    def _cargar_traduccion(self):
        """Carga i18n/*.qm si existe para el idioma en uso.

        QGIS guarda el idioma en QSettings; si el usuario no lo fijó se usa el
        del sistema. Sin archivo .qm para ese idioma no se hace nada y las
        cadenas quedan en español, que es el idioma fuente.
        """
        try:
            ajuste = QSettings().value('locale/userLocale', '')
            codigo = (str(ajuste) or QLocale.system().name())[:2]
            ruta = os.path.join(
                self._carpeta, 'i18n', f'buscar_sentinel2_{codigo}.qm')
            if os.path.exists(ruta):
                self._traductor = QTranslator()
                if self._traductor.load(ruta):
                    QCoreApplication.installTranslator(self._traductor)
                else:
                    self._traductor = None
        except Exception as e:
            # Un fallo de traducción no debe impedir que cargue el complemento.
            _LOG.debug(f"[traduccion] {e}")

    def tr(self, cadena):
        return QCoreApplication.translate('BuscarSentinel2Plugin', cadena)

    # ------------------------------------------------------------ ciclo vida
    def initGui(self):
        """Se llama al habilitar el complemento, en el hilo principal."""
        self.provider = BuscarSentinel2Provider()
        QgsApplication.processingRegistry().addProvider(self.provider)

        icono = self._icono()

        # El algoritmo YA es accesible desde la Caja de Herramientas por el
        # proveedor; esta entrada de menú es un atajo. Si en el futuro se añade
        # un segundo algoritmo, hay que registrarlo en loadAlgorithms() del
        # proveedor Y añadir aquí su entrada: de lo contrario aparece en la
        # caja de herramientas pero no en el menú, y la inconsistencia pasa
        # inadvertida porque el algoritmo «sí funciona».
        self._anadir_accion(
            icono,
            # El rótulo nombra el sensor, como los demás del menú: así la
            # lista se lee por fuente y no hace falta abrir una entrada
            # para saber de qué satélite es. El verbo se conserva en la
            # ayuda emergente, que es donde cabe.
            self.tr('Sentinel-2 / Landsat…'),
            self._abrir_algoritmo,
            self.tr('Abre el diálogo de búsqueda, previsualización y '
                    'descarga de Sentinel-2 / Landsat'))
        self._anadir_accion(
            icono,
            self.tr('Esri World Imagery / Wayback…'),
            self._abrir_esri,
            self.tr('Imágenes de alta resolución, actuales e históricas, '
                    'sobre el área de interés'))
        self._anadir_accion(
            icono,
            self.tr('Sentinel-1 RTC (ASF HyP3)…'),
            self._abrir_s1,
            self.tr('Radar: inventario por traza, pedido de productos RTC '
                    'y amplitud estacional de retrodispersión'))
        self._anadir_accion(
            icono,
            self.tr('Exportar a Google Earth (KMZ)…'),
            self._abrir_ge,
            self.tr('Convierte la capa elegida a KMZ, con su simbología, y '
                    'la abre en Google Earth de escritorio'))
        self._anadir_accion(
            icono,
            self.tr('Acerca de…'),
            self._acerca_de,
            self.tr('Versión, autoría y licencia'))

    def unload(self):
        """Se llama al deshabilitar o actualizar el complemento.

        Hay que retirar TODO lo añadido: el proveedor, las acciones de menú y
        el traductor. Dejar el proveedor registrado tras una recarga produce
        dos entradas del mismo algoritmo en la Caja de Herramientas.
        """
        if self.provider is not None:
            try:
                QgsApplication.processingRegistry().removeProvider(self.provider)
            except Exception as e:
                _LOG.debug(f"[unload_provider] {e}")
            self.provider = None

        for accion in self.acciones:
            try:
                self.iface.removePluginMenu(self.MENU, accion)
                self.iface.removeToolBarIcon(accion)
            except Exception as e:
                _LOG.debug(f"[unload_menu] {e}")
        self.acciones = []

        if self._traductor is not None:
            try:
                QCoreApplication.removeTranslator(self._traductor)
            except Exception as e:
                _LOG.debug(f"[unload_traductor] {e}")
            self._traductor = None

    # --------------------------------------------------------------- internos
    def _icono(self):
        ruta = os.path.join(self._carpeta, 'icon.png')
        return QIcon(ruta) if os.path.exists(ruta) else QIcon()

    def _anadir_accion(self, icono, texto, destino, ayuda):
        accion = QAction(icono, texto, self.iface.mainWindow())
        accion.triggered.connect(destino)
        accion.setStatusTip(ayuda)
        accion.setWhatsThis(ayuda)
        self.iface.addPluginToMenu(self.MENU, accion)
        self.acciones.append(accion)
        return accion

    def _abrir_algoritmo(self):
        """Diálogo del buscador Sentinel-2 / Landsat."""
        self._abrir_dialogo(ALGORITMO_ID)

    def _abrir_esri(self):
        """Diálogo del algoritmo de Esri World Imagery / Wayback."""
        self._abrir_dialogo(ALGORITMO_ESRI_ID)

    def _abrir_s1(self):
        """Diálogo del algoritmo de Sentinel-1 RTC vía ASF HyP3."""
        self._abrir_dialogo(ALGORITMO_S1_ID)

    def _abrir_ge(self):
        """Diálogo del exportador a Google Earth (KMZ)."""
        self._abrir_dialogo(ALGORITMO_GE_ID)

    def _abrir_dialogo(self, alg_id=ALGORITMO_ID):
        """Abre el diálogo estándar de Processing para el algoritmo.

        La importación de `processing` va aquí y no arriba: el módulo pertenece
        al complemento Processing de QGIS, que puede no estar habilitado. Si
        falta, conviene decirlo con un mensaje claro en vez de que falle la
        carga entera de este complemento.
        """
        try:
            from processing import execAlgorithmDialog
        except ImportError as e:
            QMessageBox.warning(
                self.iface.mainWindow(),
                self.tr('Processing no disponible'),
                self.tr('Este complemento necesita el complemento «Processing» '
                        'de QGIS, que no está habilitado.\n\nActívelo en '
                        'Complementos → Administrar e instalar complementos → '
                        'Instalados → Processing.\n\nDetalle: {0}').format(e))
            return
        execAlgorithmDialog(alg_id, {})

    def _acerca_de(self):
        prov = BuscarSentinel2Provider()
        QMessageBox.information(
            self.iface.mainWindow(),
            self.tr('Buscador Sentinel-2 / Landsat (STAC + COG)'),
            self.tr(
                '<b>Buscador Sentinel-2 / Landsat (STAC + COG)</b><br>'
                'Versión {version}<br><br>'
                'Tres algoritmos de Processing:<br>'
                '<b>1.</b> Sentinel-2 y Landsat por STAC — búsqueda, '
                'previsualización, descarga recortada al AOI, índices '
                'espectrales y amplitud fenológica.<br>'
                '<b>2.</b> Esri World Imagery / Wayback — imágenes de alta '
                'resolución, actuales e históricas.<br>'
                '<b>3.</b> Sentinel-1 RTC vía ASF HyP3 — radar: inventario '
                'por traza y amplitud estacional de retrodispersión, que no '
                'se pierde por nube.<br><br>'
                '<b>Autor:</b> {autor} &lt;{correo}&gt;<br>'
                '<b>Licencia:</b> GPL v2 o posterior<br><br>'
                '<b>Datos:</b> Copernicus Sentinel-1 y Sentinel-2 (ESA) y '
                'Landsat Collection 2 (USGS/NASA), de acceso abierto; '
                'productos RTC generados por ASF DAAC HyP3 con software '
                'GAMMA. Las imágenes de Esri NO son dato abierto: se rigen '
                'por el Esri Master License Agreement. Cite la fuente de las '
                'imágenes en cualquier producto derivado.<br><br>'
                'La ayuda completa de cada parámetro está en el panel de '
                'ayuda del propio diálogo de cada algoritmo.'
            ).format(version=prov.VERSION, autor=prov.AUTOR,
                     correo=prov.AUTOR_EMAIL))
