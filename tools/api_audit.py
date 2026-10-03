#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# SPDX-License-Identifier: GPL-2.0-or-later
"""Comprueba cada simbolo de QGIS y Qt que usa el complemento.

El objetivo NO es saber si el complemento funciona —para eso hace falta
ejecutarlo— sino si cada API que toca sigue existiendo en ESTA version de
QGIS. Es la pregunta que importa cuando sale una version nueva, porque la
migracion de enums a Qt6 no fue uniforme: en una misma version de QGIS
conviven formas planas y anidadas, y se ha visto funcionar
QgsRasterMinMaxOrigin.StdDev mientras
QgsRasterMinMaxOrigin.WholeRaster fallaba por haberse movido a
QgsRasterMinMaxOrigin.Extent.WholeRaster.

Ese fallo es SILENCIOSO cuando ocurre dentro de un post-procesador: la
excepcion se captura, la capa se carga igual y simplemente no recibe el
estilo. Solo se detecta leyendo el registro completo. Esta herramienta lo
convierte en un fallo ruidoso, antes de entregar.

    python3 tools/api_audit.py              # auditar contra el QGIS que corre
    python3 tools/api_audit.py --listar     # solo imprimir lo que se miraria
    python3 tools/api_audit.py --json i.json

En Windows, con OSGeo4W:

    C:\\OSGeo4W\\bin\\python-qgis-ltr.bat tools\\api_audit.py

Sale con codigo != 0 si falta algo, de modo que sirve de puerta en CI.

Lo que NO puede ver: los metodos llamados sobre una instancia
(capa.triggerRepaint()), porque eso exige un objeto vivo. Eso lo cubre el
arranque del complemento en una imagen con QGIS. Lo que SI ve es cada
clase importada y cada constante con punto, que es donde se rompe de
verdad al cambiar de version.

Autor    : Jorge Fallas (jfallas56@gmail.com)
Licencia : GPL v2 o posterior
Version  : 1.0.0
"""

from __future__ import annotations

import argparse
import ast
import importlib
import json
import os
import sys
from collections import defaultdict

RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PAQUETE = 'BuscadorSTAC'
DIR_PAQUETE = os.path.join(RAIZ, PAQUETE)

# Raices que vale la pena auditar. «qgis.PyQt» es la capa propia de QGIS
# sobre Qt, asi que cubre Qt5 y Qt6 a la vez. «osgeo» entra porque la API
# de GDAL en Python tambien cambia entre versiones.
RAICES = ('qgis', 'osgeo')

# Nombres que NO son constantes aunque lo parezcan: son modulos o alias
# locales. Se excluyen para no inventar fallos.
NO_CONSTANTES = {'self', 'cls', 'np', 'os', 're', 'sys', 'json', 'math'}


def archivos_py():
    salida = []
    for raiz, dirs, archivos in os.walk(DIR_PAQUETE):
        dirs[:] = [d for d in dirs if d != '__pycache__']
        salida.extend(os.path.join(raiz, a) for a in sorted(archivos)
                      if a.endswith('.py'))
    return sorted(salida)


def _auditable(modulo):
    return modulo.split('.')[0] in RAICES


def recolectar(rutas):
    """(importaciones, atributos).

    importaciones: {modulo: {nombres importados}}
    atributos    : {Clase: {atributos con punto usados sobre ella}}

    Los atributos se recogen en cadena: «QgsWkbTypes.Type.Point» se guarda
    como «Type.Point» bajo «QgsWkbTypes», porque es exactamente la forma
    anidada que aparece y desaparece entre versiones.
    """
    importaciones = defaultdict(set)
    atributos = defaultdict(set)
    locales = set()

    for ruta in rutas:
        with open(ruta, encoding='utf-8') as fh:
            try:
                arbol = ast.parse(fh.read(), filename=ruta)
            except SyntaxError as e:
                print(f'  [!] {os.path.basename(ruta)}: {e}')
                continue

        traidos = set()
        for nodo in ast.walk(arbol):
            if isinstance(nodo, ast.ImportFrom) and nodo.module:
                if _auditable(nodo.module):
                    for alias in nodo.names:
                        if alias.name == '*':
                            continue
                        importaciones[nodo.module].add(alias.name)
                        traidos.add(alias.asname or alias.name)
            elif isinstance(nodo, ast.Import):
                for alias in nodo.names:
                    if _auditable(alias.name):
                        importaciones[alias.name] = importaciones[alias.name]
                        traidos.add(alias.asname or alias.name.split('.')[0])
        locales |= traidos

        # Cadenas de atributos cuya base es un nombre importado de QGIS.
        for nodo in ast.walk(arbol):
            if not isinstance(nodo, ast.Attribute):
                continue
            partes = []
            actual = nodo
            while isinstance(actual, ast.Attribute):
                partes.append(actual.attr)
                actual = actual.value
            if not isinstance(actual, ast.Name):
                continue
            base = actual.id
            if base not in traidos or base in NO_CONSTANTES:
                continue
            partes.reverse()
            # Solo las cadenas que parecen constantes o tipos anidados:
            # una llamada a metodo sobre la CLASE tambien vale, pero un
            # metodo sobre instancia no llega aqui (la base no esta
            # importada).
            atributos[base].add('.'.join(partes))

    return dict(importaciones), {k: v for k, v in atributos.items()}


def auditar(importaciones, atributos):
    """Devuelve (faltan, revisados). Necesita QGIS importable."""
    faltan = []
    revisados = 0
    objetos = {}

    for modulo, nombres in sorted(importaciones.items()):
        try:
            mod = importlib.import_module(modulo)
        except ImportError as e:
            faltan.append((modulo, '', f'no se pudo importar el modulo: {e}'))
            continue
        for nombre in sorted(nombres):
            revisados += 1
            if hasattr(mod, nombre):
                objetos[nombre] = getattr(mod, nombre)
                continue
            # Un SUBMODULO no es atributo de su paquete hasta que alguien
            # lo importa: «import osgeo» deja hasattr(osgeo, 'gdal') en
            # False, y «from osgeo import gdal» es precisamente lo que
            # hace el complemento. Sin este segundo intento la auditoria
            # declaraba ausentes osgeo.gdal y osgeo.ogr en un QGIS que los
            # tiene -- un falso positivo que bloqueaba CI.
            try:
                sub = importlib.import_module(f'{modulo}.{nombre}')
            except ImportError:
                faltan.append((modulo, nombre, 'no existe en este QGIS'))
            else:
                objetos[nombre] = sub

    for base, cadenas in sorted(atributos.items()):
        if base not in objetos:
            continue
        for cadena in sorted(cadenas):
            revisados += 1
            actual = objetos[base]
            recorrido = base
            roto = None
            for parte in cadena.split('.'):
                recorrido += f'.{parte}'
                if not hasattr(actual, parte):
                    roto = recorrido
                    break
                actual = getattr(actual, parte)
            if roto:
                # Si la forma anidada falta, decir si existe la plana: es
                # la informacion que hace falta para arreglarlo.
                hoja = cadena.split('.')[-1]
                alt = (' (pero «%s.%s» si existe: el enum se movio)'
                       % (base, hoja)) if hasattr(objetos[base], hoja) else ''
                faltan.append((base, cadena, f'falta en {roto}{alt}'))

    return faltan, revisados


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--listar', action='store_true',
                    help='imprimir lo que se auditaria y salir')
    ap.add_argument('--json', dest='salida_json')
    args = ap.parse_args(argv)

    rutas = archivos_py()
    if not rutas:
        print(f'No hay .py en {DIR_PAQUETE}')
        return 2
    importaciones, atributos = recolectar(rutas)

    n_imp = sum(len(v) for v in importaciones.values())
    n_atr = sum(len(v) for v in atributos.values())
    print(f'{len(rutas)} archivo(s), {len(importaciones)} modulo(s), '
          f'{n_imp} simbolo(s) importado(s), {n_atr} atributo(s) con punto')

    if args.listar:
        for modulo, nombres in sorted(importaciones.items()):
            print(f'\n{modulo}')
            for nombre in sorted(nombres):
                print(f'    {nombre}')
        if atributos:
            print('\nAtributos con punto (donde se rompe al cambiar version):')
            for base, cadenas in sorted(atributos.items()):
                for cadena in sorted(cadenas):
                    print(f'    {base}.{cadena}')
        return 0

    try:
        from qgis.core import Qgis
        print(f'QGIS {Qgis.QGIS_VERSION}')
    except ImportError:
        print('\n[!] Aqui no hay QGIS, asi que no se puede auditar nada.')
        print('    Use --listar para ver que se comprobaria, o ejecute esto')
        print('    dentro de QGIS:')
        print('      - Windows: C:\\OSGeo4W\\bin\\python-qgis-ltr.bat '
              'tools\\api_audit.py')
        print('      - Linux  : python3 tools/api_audit.py')
        print('      - CI     : dentro de una imagen qgis/qgis')
        return 2

    faltan, revisados = auditar(importaciones, atributos)
    print(f'{revisados} simbolo(s) comprobado(s)')

    if faltan:
        print(f'\n{len(faltan)} PROBLEMA(S):')
        for base, nombre, motivo in faltan:
            donde = f'{base}.{nombre}' if nombre else base
            print(f'  FALTA  {donde}')
            print(f'         {motivo}')
    else:
        print('\nTodos los simbolos existen en este QGIS.')

    if args.salida_json:
        with open(args.salida_json, 'w', encoding='utf-8') as fh:
            json.dump({'revisados': revisados,
                       'faltan': [{'base': b, 'nombre': n, 'motivo': m}
                                  for b, n, m in faltan]},
                      fh, ensure_ascii=False, indent=2)

    return 1 if faltan else 0


if __name__ == '__main__':
    sys.exit(main())
