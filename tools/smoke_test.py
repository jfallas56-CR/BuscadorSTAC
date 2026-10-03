#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# SPDX-License-Identifier: GPL-2.0-or-later
"""Arranca el complemento en un QGIS de verdad y lo recorre.

Responde lo que ninguna prueba sin QGIS puede responder: ¿carga el
complemento, se registran los tres algoritmos, se construyen sus
parámetros y responde la validación? Un enum que se movió, una clase
retirada o un parámetro mal definido salen aquí.

    xvfb-run -a python3 tools/smoke_test.py

Necesita display porque instanciar cualquier cosa de Qt lo exige, incluso
sin mostrar nada. En Windows, con OSGeo4W:

    C:\\OSGeo4W\\bin\\python-qgis-ltr.bat tools\\smoke_test.py

Lo que NO hace: pedir nada por red ni ejecutar un algoritmo de principio
a fin. Una corrida real necesita credenciales y gastaría tiempo y, en el
caso de HyP3, créditos. Esto comprueba el armazón; el resultado numérico
lo comprueban las baterías sin QGIS.

Autor    : Jorge Fallas (jfallas56@gmail.com)
Licencia : GPL v2 o posterior
Version  : 1.0.0
"""

from __future__ import annotations

import os
import sys
import traceback

RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PAQUETE = 'BuscadorSTAC'

# El complemento se importa como paquete, así que la RAÍZ del repo tiene
# que estar en sys.path, no la carpeta del paquete.
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


def main():
    seccion('Entorno')
    from qgis.core import Qgis, QgsApplication
    print(f'  QGIS {Qgis.QGIS_VERSION}')

    # Sin GUI, pero con plataforma: los widgets necesitan display aunque no
    # se muestren. «offscreen» evita depender de un servidor X cuando lo
    # hay, y xvfb-run cubre el caso en que Qt lo exija de todos modos.
    os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
    app = QgsApplication([], False)
    app.initQgis()
    try:
        seccion('Importación')
        try:
            import importlib
            paquete = importlib.import_module(PAQUETE)
            comp('el paquete se importa', True)
        except Exception:                                    # noqa: BLE001
            comp('el paquete se importa', False, traceback.format_exc())
            return 1

        comp('define classFactory', hasattr(paquete, 'classFactory'))

        seccion('Módulos del complemento')
        modulos = {}
        for nombre in ('buscar_sentinel2_algoritmo',
                       'buscar_sentinel2_provider',
                       'buscar_sentinel2_plugin',
                       'esri_wayback_algoritmo',
                       'sentinel1_hyp3_algoritmo'):
            try:
                modulos[nombre] = importlib.import_module(
                    f'{PAQUETE}.{nombre}')
                comp(f'{nombre} se importa', True)
            except Exception:                                # noqa: BLE001
                comp(f'{nombre} se importa', False,
                     traceback.format_exc().strip().splitlines()[-1])

        seccion('Enums resueltos con respaldo')
        # Lo que el resolutor haya decidido en ESTA versión de QGIS. Un
        # valor igual al respaldo numérico significa que ninguna de las dos
        # formas existía: funciona, pero conviene saberlo.
        alg_mod = modulos.get('buscar_sentinel2_algoritmo')
        if alg_mod is not None:
            for nombre, respaldo in (('_MMO_WHOLE', 0), ('_MMO_STDDEV', 2),
                                     ('_CE_MINMAX', 1), ('_SHADER_INTERP', 0),
                                     ('_WKB_POLIGONO', 3),
                                     ('_TIPO_POLIGONO', None)):
                valor = getattr(alg_mod, nombre, '<ausente>')
                comp(f'{nombre} resuelto', valor != '<ausente>',
                     f'valor: {valor!r}')
                if valor != '<ausente>':
                    print(f'        = {valor!r}'
                          + ('  (igual al respaldo numérico: ninguna de las '
                             'dos formas existe en este QGIS)'
                             if respaldo is not None and valor == respaldo
                             else ''))

        seccion('Proveedor de Processing')
        prov_mod = modulos.get('buscar_sentinel2_provider')
        if prov_mod is None:
            return 1
        clases = [getattr(prov_mod, n) for n in dir(prov_mod)
                  if n.endswith('Provider')]
        clase_prov = None
        for c in clases:
            if isinstance(c, type) and hasattr(c, 'loadAlgorithms'):
                clase_prov = c
                break
        if not comp('hay una clase de proveedor', clase_prov is not None):
            return 1

        prov = clase_prov()
        prov.loadAlgorithms()
        algos = list(prov.algorithms())
        # El conteo se DEDUCE, no se fija: una lista fija obliga a editar
        # esta herramienta cada vez que se anade un algoritmo, y entonces
        # un fallo por expectativa desfasada no se distingue de una
        # regresion real. Eso ya paso aqui con el cuarto algoritmo.
        comp(f'el proveedor registra al menos 3 algoritmos '
             f'(registro {len(algos)})', len(algos) >= 3)
        nombres_vistos = [a.name() for a in algos]
        comp('ningun id de algoritmo duplicado',
             len(set(nombres_vistos)) == len(nombres_vistos),
             f'{nombres_vistos}')
        for a in algos:
            print(f'        {a.name()} — {a.displayName()}')

        seccion('Cada algoritmo: parámetros, ayuda y validación')
        from qgis.core import QgsProcessingContext
        for a in algos:
            nombre = a.name()
            try:
                inst = a.createInstance()
                comp(f'{nombre}: createInstance', inst is not None)
            except Exception:                                # noqa: BLE001
                comp(f'{nombre}: createInstance', False,
                     traceback.format_exc().strip().splitlines()[-1])
                continue

            try:
                inst.initAlgorithm({})
                params = inst.parameterDefinitions()
                comp(f'{nombre}: initAlgorithm define {len(params)} '
                     f'parámetro(s)', len(params) > 0)
                sin_ayuda = [p.name() for p in params
                             if not (p.help() or '').strip()]
                comp(f'{nombre}: todo parámetro lleva ayuda',
                     not sin_ayuda,
                     f'sin setHelp(): {sin_ayuda}')
            except Exception:                                # noqa: BLE001
                comp(f'{nombre}: initAlgorithm', False,
                     traceback.format_exc().strip().splitlines()[-1])
                continue

            try:
                ayuda = inst.shortHelpString() or ''
                comp(f'{nombre}: shortHelpString no vacía',
                     len(ayuda) > 200)
                comp(f'{nombre}: la ayuda nombra la versión',
                     inst.VERSION in ayuda,
                     f'VERSION={inst.VERSION}; sin ella el usuario no puede '
                     f'confirmar qué versión corre')
            except Exception:                                # noqa: BLE001
                comp(f'{nombre}: shortHelpString', False,
                     traceback.format_exc().strip().splitlines()[-1])

            # checkParameterValues con los valores por omisión: tiene que
            # RESPONDER, no reventar. Que rechace está bien —faltan datos—;
            # lo que no vale es una excepción, porque eso en el diálogo
            # aparece como un fallo sin explicación.
            try:
                ctx = QgsProcessingContext()
                vacios = {}
                for p in inst.parameterDefinitions():
                    vacios[p.name()] = p.defaultValue()
                res = inst.checkParameterValues(vacios, ctx)
                if isinstance(res, tuple):
                    bien, msj = res
                else:
                    bien, msj = bool(res), ''
                comp(f'{nombre}: checkParameterValues responde sin excepción',
                     True)
                print(f'        {"acepta" if bien else "rechaza"}'
                      + (f': {str(msj)[:90]}' if msj else ''))
            except Exception:                                # noqa: BLE001
                comp(f'{nombre}: checkParameterValues responde sin excepción',
                     False, traceback.format_exc().strip().splitlines()[-1])

        seccion('Registro en el registro de Processing')
        reg = QgsApplication.processingRegistry()
        try:
            ok_reg = reg.addProvider(clase_prov())
            comp('el proveedor se añade al registro de Processing', ok_reg)
        except Exception:                                    # noqa: BLE001
            comp('el proveedor se añade al registro de Processing', False,
                 traceback.format_exc().strip().splitlines()[-1])

        seccion('Menú del complemento')
        # Un algoritmo nuevo hay que añadirlo en DOS sitios: el proveedor y
        # la lista de entradas de menú. Si solo está en uno, aparece en la
        # caja de herramientas y no en el menú, y pasa desapercibido porque
        # el algoritmo «sí funciona».
        plug_mod = modulos.get('buscar_sentinel2_plugin')
        if plug_mod is not None:
            import inspect
            fuente = inspect.getsource(plug_mod)
            nombres_alg = [a.name() for a in algos]
            faltan = [n for n in nombres_alg if n not in fuente]
            comp('el menú menciona los tres algoritmos', not faltan,
                 f'no aparecen en buscar_sentinel2_plugin.py: {faltan}')
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
        print('Este script necesita un QGIS instalado. Ejecútelo dentro de '
              'una imagen qgis/qgis, o con python-qgis-ltr.bat en Windows.')
        sys.exit(2)
