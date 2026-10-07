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
Buscador Sentinel-2 / Landsat (STAC + COG) — punto de entrada del complemento
=============================================================================

QGIS importa este paquete y llama a classFactory(iface) para instanciar el
complemento. Es el único nombre que QGIS exige aquí; todo lo demás vive en
los módulos que este importa.

Autor  : Jorge Fallas (jfallas56@gmail.com)
Versión: 1.0.5
Licencia: GPL v2 o posterior
"""


def classFactory(iface):        # noqa: N802  (nombre exigido por QGIS)
    """Devuelve la instancia del complemento.

    La importación va DENTRO de la función a propósito: si se hiciera arriba,
    un fallo al importar qgis.core durante el arranque dejaría el paquete a
    medio cargar y el Administrador de Complementos mostraría un error sin
    decir de dónde viene. Aquí el traceback apunta al módulo real.
    """
    from .buscar_sentinel2_plugin import BuscarSentinel2Plugin
    return BuscarSentinel2Plugin(iface)
