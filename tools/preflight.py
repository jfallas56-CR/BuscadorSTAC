#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# SPDX-License-Identifier: GPL-2.0-or-later
"""Comprueba el complemento ANTES de subirlo a plugins.qgis.org.

El portal rechaza por cosas que no se ven mirando el ZIP: un hallazgo
critico de Bandit bloquea la aprobacion aunque todo lo demas este bien, y
un «%» sin escapar en metadata.txt hace que configparser lea ese campo mal
sin lanzar ninguna excepcion. Esta herramienta corre esas mismas
comprobaciones en local, para fallar aqui en vez de en el formulario.

    python3 tools/preflight.py                  # empaqueta y comprueba
    python3 tools/preflight.py --zip ruta.zip   # comprueba un ZIP existente
    python3 tools/preflight.py --sin-red        # omite lo que necesita red
    python3 tools/preflight.py --json inf.json  # informe legible por maquina

Sale con codigo != 0 si algo bloquea la subida, de modo que sirve de puerta
en integracion continua.

Lo que NO puede ver: si el complemento FUNCIONA. Para eso hace falta QGIS
de verdad (tools/api_audit.py y el trabajo «qgis» de CI).

Autor    : Jorge Fallas (jfallas56@gmail.com)
Licencia : GPL v2 o posterior
Version  : 1.0.0
"""

from __future__ import annotations

import argparse
import ast
import configparser
import json
import os
import re
import subprocess  # nosec B404 - se invoca python -m con argumentos fijos
import sys
import tempfile
import zipfile

RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PAQUETE = 'BuscadorSTAC'
DIR_PAQUETE = os.path.join(RAIZ, PAQUETE)

# Limites del portal. El de tamano es conservador: el formulario acepta mas,
# pero un ZIP grande es sintoma de que se colo algo que no deberia ir.
MAX_MIB = 25.0
MAX_NOMBRE = 72
MAX_DESCRIPCION = 250

CAMPOS_OBLIGATORIOS = ('name', 'qgisMinimumVersion', 'description', 'version',
                       'author', 'email', 'about', 'repository', 'tracker')

# Lista blanca de lo que puede viajar dentro del ZIP.
PATRONES_PERMITIDOS = (
    r'\.py$', r'^metadata\.txt$', r'^icon\.png$', r'^LICENSE$',
    r'^README\.md$', r'^setup\.cfg$', r'\.ui$', r'\.qrc$',
    r'^i18n/.*\.qm$', r'^resources/.*\.(png|svg)$',
)
PATRONES_PROHIBIDOS = (
    r'__pycache__', r'\.pyc$', r'\.pyo$', r'\.DS_Store$', r'\.qgz$',
    r'\.qgs$', r'^\.git', r'^tests?/', r'\.zip$', r'\.swp$', r'~$',
)

# Rutas de la maquina de compilacion: si aparecen en el codigo entregado,
# el complemento lleva dentro algo del entorno de desarrollo.
RE_RUTAS_LOCALES = (
    re.compile(r'/home/[a-z_][a-z0-9_-]*/', re.I),
    re.compile(r'[A-Z]:\\Users\\[^\\\s"\']+', re.I),
    re.compile(r'/Users/[a-z_][a-z0-9_-]*/', re.I),
    re.compile(r'/mnt/user-data/'),
)

# Formas que delatan una credencial pegada en el codigo. Se busca el VALOR,
# no el nombre de la variable: Bandit B105 mira el identificador y por eso
# «ARCHIVO_TOKEN = ''» le parece sospechoso y un JWT de verdad no.
RE_CREDENCIALES = (
    # JWT: tres bloques base64url separados por puntos.
    (re.compile(r'\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.'
                r'[A-Za-z0-9_-]{10,}'), 'parece un JWT'),
    (re.compile(r'\bAKIA[0-9A-Z]{16}\b'), 'parece una clave de acceso AWS'),
    (re.compile(r'\bgh[pousr]_[A-Za-z0-9]{36,}'),
     'parece un token de GitHub'),
    (re.compile(r'\bEDL-[A-Za-z0-9]{20,}'),
     'parece un token de Earthdata Login'),
    (re.compile(r'(?i)\b(?:api[_-]?key|secret|passwd|password)\s*=\s*'
                r'[\'"][^\'"\s]{12,}[\'"]'),
     'asigna un literal largo a un nombre de credencial'),
)


# --------------------------------------------------------------- informe
class Informe:
    """Acumula resultados. «bloquea» separa lo que impide subir de lo demas."""

    def __init__(self, color=True):
        self.ok = 0
        self.bloqueantes = []
        self.avisos = []
        self.color = color
        self.detalles = []

    def _c(self, texto, codigo):
        return f"\033[{codigo}m{texto}\033[0m" if self.color else texto

    def seccion(self, titulo):
        print(f"\n{self._c(titulo, '1')}")

    def comprobar(self, etiqueta, bien, detalle='', bloquea=True):
        if bien:
            self.ok += 1
            print(f"  {self._c('pasa', '32')}  {etiqueta}")
        elif bloquea:
            self.bloqueantes.append((etiqueta, detalle))
            print(f"  {self._c('FALLA', '31')} {etiqueta}")
            if detalle:
                print(f"         {detalle}")
        else:
            self.avisos.append((etiqueta, detalle))
            print(f"  {self._c('aviso', '33')} {etiqueta}")
            if detalle:
                print(f"         {detalle}")
        self.detalles.append({'etiqueta': etiqueta, 'ok': bool(bien),
                              'bloquea': bool(bloquea) and not bien,
                              'detalle': detalle})
        return bool(bien)

    def nota(self, texto):
        print(f"         {texto}")


# ------------------------------------------------------------ utilidades
def _texto(ruta):
    """Lee un archivo como UTF-8. El encoding explicito NO es opcional:
    sin el, Python usa la codificacion del sistema y en Windows eso es
    cp1252, que destroza los acentos del codigo."""
    with open(ruta, encoding='utf-8', errors='replace') as fh:
        return fh.read()


def _archivos_py(carpeta):
    salida = []
    for raiz, dirs, archivos in os.walk(carpeta):
        dirs[:] = [d for d in dirs if d != '__pycache__']
        salida.extend(os.path.join(raiz, a) for a in sorted(archivos)
                      if a.endswith('.py'))
    return sorted(salida)


def _rel(ruta):
    return os.path.relpath(ruta, RAIZ)


# ---------------------------------------------------------- empaquetado
def construir_zip(destino=None):
    """Arma el ZIP con la lista blanca. Devuelve su ruta."""
    destino = destino or os.path.join(RAIZ, 'dist')
    os.makedirs(destino, exist_ok=True)
    cfg = leer_metadata_disco()
    version = (cfg or {}).get('version', '0.0.0')
    ruta = os.path.join(destino, f'{PAQUETE}-{version}.zip')
    incluidos = 0
    with zipfile.ZipFile(ruta, 'w', zipfile.ZIP_DEFLATED) as z:
        for raiz_dir, dirs, archivos in os.walk(DIR_PAQUETE):
            dirs[:] = [d for d in dirs if d != '__pycache__']
            for nombre in sorted(archivos):
                abs_ruta = os.path.join(raiz_dir, nombre)
                interno = os.path.relpath(abs_ruta, DIR_PAQUETE)
                interno_posix = interno.replace(os.sep, '/')
                if any(re.search(p, interno_posix)
                       for p in PATRONES_PROHIBIDOS):
                    continue
                if not any(re.search(p, interno_posix)
                           for p in PATRONES_PERMITIDOS):
                    continue
                z.write(abs_ruta, f'{PAQUETE}/{interno_posix}')
                incluidos += 1
    return ruta, incluidos


def leer_metadata_disco():
    ruta = os.path.join(DIR_PAQUETE, 'metadata.txt')
    if not os.path.isfile(ruta):
        return None
    # Raw a proposito: sin interpolacion, de modo que un «%» literal en
    # cualquier campo no impide siquiera nombrar bien el ZIP.
    cfg = configparser.RawConfigParser()
    try:
        cfg.read(ruta, encoding='utf-8')
        return dict(cfg['general'])
    except (configparser.Error, KeyError):
        return None


# ------------------------------------------------------- comprobaciones
def comp_estructura(z, inf):
    inf.seccion('Estructura del ZIP')
    nombres = z.namelist()
    raices = {n.split('/')[0] for n in nombres if n.strip()}
    inf.comprobar(
        'una sola carpeta en la raiz, con el nombre del paquete',
        raices == {PAQUETE},
        f'raices encontradas: {sorted(raices)}; se esperaba solo "{PAQUETE}"')

    for obligatorio in ('__init__.py', 'metadata.txt', 'icon.png', 'LICENSE'):
        inf.comprobar(f'incluye {obligatorio}',
                      f'{PAQUETE}/{obligatorio}' in nombres)

    # classFactory se comprueba por AST: importar el paquete aqui exigiria
    # QGIS, y el portal tampoco lo importa para aceptarlo.
    try:
        fuente = z.read(f'{PAQUETE}/__init__.py').decode('utf-8')
        arbol = ast.parse(fuente)
        tiene = any(isinstance(n, ast.FunctionDef) and n.name == 'classFactory'
                    for n in arbol.body)
        inf.comprobar('__init__.py define classFactory(iface)', tiene,
                      'QGIS llama a classFactory para instanciar el '
                      'complemento; sin ella no carga')
    except (KeyError, SyntaxError, UnicodeDecodeError) as e:
        inf.comprobar('__init__.py se puede analizar', False, str(e))


def comp_tamano(ruta, z, inf):
    inf.seccion('Tamano')
    mib = os.path.getsize(ruta) / 1048576.0
    inf.comprobar(f'el ZIP pesa {mib:.2f} MiB (limite blando {MAX_MIB:.0f})',
                  mib <= MAX_MIB,
                  'un ZIP grande suele significar que se colo algo',
                  bloquea=False)
    descomprimido = sum(i.file_size for i in z.infolist()) / 1048576.0
    inf.nota(f'descomprimido: {descomprimido:.2f} MiB '
             f'en {len(z.namelist())} archivos')


def comp_contenido(z, inf):
    inf.seccion('Contenido entregado')
    nombres = [n for n in z.namelist() if not n.endswith('/')]
    intrusos = []
    for n in nombres:
        interno = n.split('/', 1)[1] if '/' in n else n
        if any(re.search(p, interno) for p in PATRONES_PROHIBIDOS):
            intrusos.append(n)
    inf.comprobar('sin __pycache__, .pyc, proyectos .qgz ni pruebas',
                  not intrusos, f'colados: {intrusos[:5]}')

    fuera = []
    for n in nombres:
        interno = n.split('/', 1)[1] if '/' in n else n
        if not any(re.search(p, interno) for p in PATRONES_PERMITIDOS):
            fuera.append(interno)
    inf.comprobar('todo archivo esta en la lista blanca', not fuera,
                  f'fuera de la lista: {fuera[:5]}', bloquea=False)

    # Credenciales y rutas locales: en el contenido ENTREGADO, que es lo que
    # se publica. Un token en el registro ya paso una vez.
    hallazgos_cred, hallazgos_ruta = [], []
    for n in nombres:
        if not n.endswith(('.py', '.txt', '.md', '.cfg')):
            continue
        try:
            texto = z.read(n).decode('utf-8', 'replace')
        except KeyError:
            continue
        for regex, motivo in RE_CREDENCIALES:
            m = regex.search(texto)
            if m:
                hallazgos_cred.append(f'{n}: {motivo}')
        for regex in RE_RUTAS_LOCALES:
            m = regex.search(texto)
            if m:
                hallazgos_ruta.append(f'{n}: {m.group(0)[:48]}')
    inf.comprobar('ninguna credencial pegada en el codigo',
                  not hallazgos_cred, '; '.join(hallazgos_cred[:3]))
    inf.comprobar('ninguna ruta de la maquina de desarrollo',
                  not hallazgos_ruta, '; '.join(hallazgos_ruta[:3]),
                  bloquea=False)


def comp_metadata(z, inf):
    inf.seccion('metadata.txt')
    crudo_bytes = z.read(f'{PAQUETE}/metadata.txt')
    crudo = crudo_bytes.decode('utf-8', 'replace')

    inf.comprobar('sin saltos de linea CRLF', b'\r\n' not in crudo_bytes,
                  'el portal los acepta, pero mezclarlos complica los diff',
                  bloquea=False)

    # Los valores se leen con RawConfigParser, SIN interpolacion: asi un
    # «%» mal escrito no deja el resto de las comprobaciones sin datos.
    # Un unico defecto producia antes siete mensajes, seis de ellos
    # enganosos («version «»», «tags (0)»), que es peor que no avisar.
    g = {}
    crudo_ok = True
    try:
        cfg_raw = configparser.RawConfigParser()
        cfg_raw.read_string(crudo)
        g = dict(cfg_raw['general'])
    except (configparser.Error, KeyError) as e:
        crudo_ok = False
        inf.comprobar('el archivo tiene una seccion [general] legible',
                      False, str(e))
    if crudo_ok:
        inf.comprobar('el archivo tiene una seccion [general] legible', True)

    # Aparte, el «%» literal. QGIS lee metadata.txt con interpolacion, de
    # modo que un «%» suelto rompe ese campo: o lanza, o lo sustituye por
    # algo distinto SIN avisar. Las dos formas se detectan aqui.
    sospechosos = [k for k, v in g.items()
                   if re.search(r'(?<!%)%(?![%(])', v or '')]
    interpola = True
    detalle_interp = ''
    try:
        configparser.ConfigParser().read_string(crudo)
    except configparser.InterpolationError as e:
        interpola = False
        detalle_interp = f'la interpolacion falla: {e}'
    except configparser.Error as e:
        interpola = False
        detalle_interp = str(e)
    inf.comprobar(
        'ningun «%» sin escapar (hay que escribir «%%»)',
        interpola and not sospechosos,
        (f'campos con «%» suelto: {sospechosos}. ' if sospechosos else '')
        + detalle_interp
        + ' QGIS lee este archivo con interpolacion: un «%» literal rompe '
          'ese campo en silencio.')

    faltan = [c for c in CAMPOS_OBLIGATORIOS if not (g.get(c.lower()) or '').strip()]
    inf.comprobar('estan todos los campos obligatorios', not faltan,
                  f'faltan: {faltan}')

    version = (g.get('version') or '').strip()
    inf.comprobar(f'version «{version}» con formato X.Y.Z',
                  bool(re.fullmatch(r'\d+\.\d+\.\d+', version)),
                  'el portal ordena las versiones; dos o cuatro numeros '
                  'rompen la comparacion')

    nombre = (g.get('name') or '')
    inf.comprobar(f'name cabe en {MAX_NOMBRE} caracteres ({len(nombre)})',
                  len(nombre) <= MAX_NOMBRE)
    desc = (g.get('description') or '')
    inf.comprobar(
        f'description cabe en {MAX_DESCRIPCION} caracteres ({len(desc)})',
        len(desc) <= MAX_DESCRIPCION)
    inf.comprobar('description sin saltos de linea',
                  '\n' not in desc.strip())
    inf.comprobar('description no acaba en espacio ni tabulador',
                  desc == desc.rstrip(), repr(desc[-6:]), bloquea=False)

    correo = (g.get('email') or '')
    inf.comprobar('email con forma de correo',
                  bool(re.fullmatch(r'[^@\s]+@[^@\s]+\.[a-z]{2,}', correo)))

    for bandera in ('experimental', 'deprecated'):
        valor = (g.get(bandera) or '').strip().lower()
        inf.comprobar(f'{bandera}=False', valor in ('false', 'no', '0'),
                      f'esta en «{valor}»: una version marcada '
                      f'{bandera} no se ofrece como estable',
                      bloquea=(bandera == 'deprecated'))

    changelog = (g.get('changelog') or '').lstrip()
    inf.comprobar('el changelog empieza por la version que se sube',
                  changelog.startswith(version),
                  f'empieza por «{changelog[:20]}», version «{version}»')

    etiquetas = [t.strip() for t in (g.get('tags') or '').split(',')
                 if t.strip()]
    inf.comprobar(f'tags no vacias ({len(etiquetas)})', bool(etiquetas))
    inf.comprobar('tags sin duplicados',
                  len(etiquetas) == len(set(t.lower() for t in etiquetas)),
                  bloquea=False)

    qmin = (g.get('qgisminimumversion') or '').strip()
    inf.comprobar(f'qgisMinimumVersion «{qmin}» con forma X.Y',
                  bool(re.fullmatch(r'\d+\.\d+', qmin)))

    # Si declara proveedor de Processing, debe haberlo de verdad.
    declara = (g.get('hasprocessingprovider') or '').strip().lower() == 'yes'
    hay = any('QgsProcessingProvider' in _texto(p)
              for p in _archivos_py(DIR_PAQUETE))
    inf.comprobar('hasProcessingProvider coincide con el codigo',
                  declara == hay,
                  f'metadata dice {declara}, en el codigo hay {hay}')
    return g


def comp_icono(z, inf):
    inf.seccion('Icono')
    try:
        datos = z.read(f'{PAQUETE}/icon.png')
    except KeyError:
        inf.comprobar('icon.png presente', False)
        return
    inf.comprobar('icon.png es un PNG de verdad',
                  datos[:8] == b'\x89PNG\r\n\x1a\n',
                  'el formulario del portal rechaza un PNG con cabecera '
                  'rara aunque QGIS lo muestre')
    if datos[:8] == b'\x89PNG\r\n\x1a\n' and len(datos) > 24:
        ancho = int.from_bytes(datos[16:20], 'big')
        alto = int.from_bytes(datos[20:24], 'big')
        inf.comprobar(f'icono cuadrado ({ancho}x{alto})', ancho == alto,
                      bloquea=False)
        inf.comprobar('icono de al menos 24x24', min(ancho, alto) >= 24,
                      bloquea=False)


def comp_sintaxis(inf):
    inf.seccion('Sintaxis y estilo')
    archivos = _archivos_py(DIR_PAQUETE)
    r = subprocess.run(  # nosec B603 - argumentos fijos, sin shell
        [sys.executable, '-m', 'py_compile'] + archivos,
        capture_output=True, text=True)
    inf.comprobar(f'py_compile en los {len(archivos)} archivos',
                  r.returncode == 0,
                  (r.stderr.strip().splitlines() or [''])[-1])

    desnudos = []
    for p in archivos:
        for i, linea in enumerate(_texto(p).splitlines(), 1):
            if re.search(r'^\s*except\s*:', linea):
                desnudos.append(f'{_rel(p)}:{i}')
    inf.comprobar('cero «except:» desnudos', not desnudos,
                  f'{desnudos[:5]}')

    r = subprocess.run(  # nosec B603
        [sys.executable, '-m', 'flake8', '--max-line-length=100'] + archivos,
        capture_output=True, text=True)
    lineas = [x for x in r.stdout.splitlines() if x.strip()]
    inf.comprobar(f'flake8 sin hallazgos ({len(lineas)})', not lineas,
                  '; '.join(x.split(os.sep)[-1] for x in lineas[:4]),
                  bloquea=False)


def _cadena_devuelta(nodo):
    """El literal que devuelve un metodo de una linea, saltando comentarios.

    Se usa AST y no una expresion regular a proposito: entre «def name(self):»
    y su «return» puede haber comentarios o un docstring, y un patron que
    exija que vayan pegados falla en silencio -- da «no encontrado» y de ahi
    se concluye que el id esta roto cuando lo que esta roto es el patron.
    """
    for hijo in ast.walk(nodo):
        if isinstance(hijo, ast.Return) and isinstance(hijo.value, ast.Constant):
            if isinstance(hijo.value.value, str):
                return hijo.value.value
    return None


def comp_ids_algoritmos(inf):
    """Los ids del menu tienen que existir en el proveedor.

    Un algoritmo se registra en DOS sitios: loadAlgorithms() del proveedor y
    la lista de entradas de menu del complemento. Si solo esta en uno,
    aparece en la Caja de Herramientas y no en el menu -- y pasa
    desapercibido porque el algoritmo «si funciona». Cambiar el id() del
    proveedor rompe los ids del menu de golpe, que es lo que ocurrio al
    renombrar el paquete.
    """
    inf.seccion('Ids de algoritmo: proveedor frente a menu')
    prov = os.path.join(DIR_PAQUETE, 'buscar_sentinel2_provider.py')
    plug = os.path.join(DIR_PAQUETE, 'buscar_sentinel2_plugin.py')
    if not (os.path.isfile(prov) and os.path.isfile(plug)):
        inf.comprobar('existen el proveedor y el complemento principal',
                      False)
        return

    pid = None
    try:
        arbol = ast.parse(_texto(prov))
    except SyntaxError as e:
        inf.comprobar('el proveedor se analiza', False, str(e))
        return
    for n in ast.walk(arbol):
        if isinstance(n, ast.FunctionDef) and n.name == 'id':
            pid = _cadena_devuelta(n)
    if not inf.comprobar('el proveedor declara un id()', bool(pid)):
        return
    inf.nota(f'id del proveedor: {pid}')

    nombres = set()
    for archivo in sorted(os.listdir(DIR_PAQUETE)):
        if not archivo.endswith('.py'):
            continue
        try:
            arbol = ast.parse(_texto(os.path.join(DIR_PAQUETE, archivo)))
        except SyntaxError:
            continue
        for n in ast.walk(arbol):
            if not isinstance(n, ast.ClassDef):
                continue
            bases = {getattr(b, 'id', getattr(b, 'attr', ''))
                     for b in n.bases}
            if 'QgsProcessingAlgorithm' not in bases:
                continue
            for m in n.body:
                if isinstance(m, ast.FunctionDef) and m.name == 'name':
                    valor = _cadena_devuelta(m)
                    if valor:
                        nombres.add(valor)
    inf.comprobar(f'se encontraron {len(nombres)} algoritmo(s) con name()',
                  bool(nombres))
    esperados = {f'{pid}:{n}' for n in sorted(nombres)}

    ids_menu = set(re.findall(r"ALGORITMO\w*_ID\s*=\s*'([^']+)'",
                              _texto(plug)))
    inf.comprobar(f'el menu referencia {len(ids_menu)} id(s)',
                  bool(ids_menu))

    fantasmas = sorted(ids_menu - esperados)
    inf.comprobar('todo id del menu existe en el proveedor', not fantasmas,
                  f'no existen: {fantasmas}. Si acaba de cambiar el id() '
                  f'del proveedor o el name() de un algoritmo, actualice '
                  f'tambien las constantes ALGORITMO*_ID.')
    sin_menu = sorted(esperados - ids_menu)
    inf.comprobar('todo algoritmo tiene entrada de menu', not sin_menu,
                  f'sin entrada: {sin_menu}. Apareceran en la Caja de '
                  f'Herramientas pero no en el menu.', bloquea=False)


def _params_sin_ayuda(src, cuerpo):
    """Parametros anadidos sin setHelp(), recorriendo EN ORDEN DE CODIGO.

    ast.walk() recorre por NIVELES, no en orden de codigo: con el patron
    habitual «p = Parametro(...); p.setHelp(...); self.addParameter(p)»
    repetido con la misma variable, walk() entrega primero todas las
    asignaciones y despues todos los setHelp, de modo que cualquier p que
    reciba ayuda en algun sitio marca como buenos TODOS los usos de p. Esa
    es la razon de que esto se escriba a mano con iter_child_nodes.
    """
    estado, res = {}, []

    def nombre_de(call):
        txt = ast.get_source_segment(src, call) or ''
        m = re.search(r'self\.([A-Z_][A-Z_0-9]*)', txt)
        return m.group(1) if m else '(sin constante)'

    def visita(n):
        if (isinstance(n, ast.Assign) and len(n.targets) == 1
                and isinstance(n.targets[0], ast.Name)
                and isinstance(n.value, ast.Call)):
            nom = getattr(n.value.func, 'id',
                          getattr(n.value.func, 'attr', ''))
            if 'ProcessingParameter' in str(nom):
                estado[n.targets[0].id] = [nombre_de(n.value), False]
                return
        if isinstance(n, ast.Call):
            f = n.func
            if (getattr(f, 'attr', '') == 'setHelp'
                    and isinstance(f.value, ast.Name)
                    and f.value.id in estado):
                estado[f.value.id][1] = True
                return
            if getattr(f, 'attr', '') == 'addParameter':
                a = n.args[0] if n.args else None
                if isinstance(a, ast.Name) and a.id in estado:
                    nom, tiene = estado[a.id]
                    res.append((nom, tiene, n.lineno))
                elif isinstance(a, ast.Call):
                    # addParameter(Constructor(...)) en una sola expresion:
                    # no queda referencia a la que llamar setHelp.
                    res.append((nombre_de(a), False, n.lineno))
                elif isinstance(a, ast.Name):
                    res.append((a.id, False, n.lineno))
                return
        for h in ast.iter_child_nodes(n):
            visita(h)

    for st in cuerpo:
        visita(st)
    return res


# Palabras cuya forma SIN tilde no es otra palabra valida en espanol.
# Quedan fuera a proposito mas/mas, esta/esta, si/si, que/que, como/como y
# solo/solo: son ambiguas y marcarlas llenaria el informe de ruido.
_SIN_TILDE = {
    'resolucion': 'resolución', 'parametro': 'parámetro',
    'parametros': 'parámetros', 'ejecucion': 'ejecución',
    'minimo': 'mínimo', 'maximo': 'máximo', 'estacion': 'estación',
    'pixel': 'píxel', 'pixeles': 'píxeles', 'orbita': 'órbita',
    'angulo': 'ángulo', 'auditoria': 'auditoría',
    'descripcion': 'descripción', 'version': 'versión',
    'informacion': 'información', 'configuracion': 'configuración',
    'direccion': 'dirección', 'seleccion': 'selección',
    'polarizacion': 'polarización', 'radiometria': 'radiometría',
    'geometria': 'geometría', 'numero': 'número', 'analisis': 'análisis',
    'invalido': 'inválido', 'tambien': 'también', 'aqui': 'aquí',
    'asi': 'así', 'area': 'área', 'metodo': 'método',
    'mascara': 'máscara', 'raster': 'ráster', 'rasteres': 'rásteres',
    'dia': 'día', 'dias': 'días', 'seria': 'sería',
    'deberia': 'debería', 'podria': 'podría', 'habria': 'habría',
    'proximo': 'próximo', 'ultimo': 'último', 'unico': 'único',
    'estadistico': 'estadístico', 'grafico': 'gráfico',
    'limite': 'límite', 'credito': 'crédito', 'creditos': 'créditos',
    'imagenes': 'imágenes', 'atencion': 'atención', 'sesion': 'sesión',
    'tamano': 'tamaño', 'anios': 'años', 'granulo': 'gránulo',
    'granulos': 'gránulos', 'valida': 'válida', 'validos': 'válidos',
    'validas': 'válidas', 'despues': 'después', 'calculo': 'cálculo',
}
# Llamadas cuyo texto LEE el usuario.
_UI = {'tr', 'setHelp', 'pushInfo', 'pushWarning', 'QgsProcessingException',
       'setDescription'}


def _lit_de(nodo):
    """Constantes de texto de un argumento, SIN entrar en subindices.

    Un c['creditos'] dentro de una f-string es una clave de diccionario,
    no texto que lea nadie.
    """
    if isinstance(nodo, ast.Constant) and isinstance(nodo.value, str):
        return [nodo]
    if isinstance(nodo, ast.JoinedStr):
        return [v for v in nodo.values
                if isinstance(v, ast.Constant) and isinstance(v.value, str)]
    if isinstance(nodo, ast.BinOp):
        return _lit_de(nodo.left) + _lit_de(nodo.right)
    return []


def comp_tildes(inf):
    """El texto que ve el usuario lleva sus tildes.

    El complemento es de interfaz en espanol, y una etiqueta sin tilde se
    ve en el dialogo tal cual. Se reporto dos veces a mano --«Resolucion»,
    «Minimo», «Estacion», «Parametros de la ejecucion»-- antes de que
    existiera esta comprobacion.

    Solo mira texto de interfaz: etiquetas, ayudas, mensajes y las listas
    de opciones. No mira comentarios ni nombres de variables, que son
    codigo. Y respeta los limites de palabra: QA_PIXEL, raster:bands,
    [_mascara_nubes] y {version} son nombres tecnicos y no se tocan.
    """
    inf.seccion('Tildes en el texto de la interfaz')
    hallazgos = []
    revisadas = 0
    for archivo in sorted(os.listdir(DIR_PAQUETE)):
        if not archivo.endswith('.py'):
            continue
        try:
            arbol = ast.parse(_texto(os.path.join(DIR_PAQUETE, archivo)))
        except SyntaxError:
            continue
        textos = []
        for n in ast.walk(arbol):
            if isinstance(n, ast.Call):
                nom = getattr(n.func, 'id', getattr(n.func, 'attr', ''))
                if nom in _UI:
                    for arg in list(n.args) + [k.value for k in n.keywords]:
                        textos.extend((a.lineno, a.value) for a in _lit_de(arg))
            elif isinstance(n, ast.Assign) and isinstance(n.value, ast.List):
                if getattr(n.targets[0], 'id', '').isupper():
                    textos.extend(
                        (a.lineno, a.value) for a in ast.walk(n.value)
                        if isinstance(a, ast.Constant)
                        and isinstance(a.value, str))
        revisadas += len(textos)
        for linea, texto in textos:
            for m in re.finditer(r'[A-Za-zÁÉÍÓÚÜÑáéíóúüñ]+', texto):
                pal, ini, fin = m.group(0), m.start(), m.end()
                izq = texto[ini - 1] if ini else ' '
                der = texto[fin] if fin < len(texto) else ' '
                if izq in '_:{' or der in '_:}' or pal.isupper():
                    continue
                bien = _SIN_TILDE.get(pal.lower())
                if bien:
                    hallazgos.append(f'{archivo}:{linea} «{pal}» -> «{bien}»')

    if not inf.comprobar(f'se revisaron {revisadas} cadena(s) de interfaz',
                         revisadas > 0):
        return
    inf.comprobar('el texto de la interfaz lleva sus tildes', not hallazgos,
                  '; '.join(sorted(set(hallazgos))[:12]))


def comp_metodos_resueltos(inf):
    """Toda llamada self._foo() se resuelve en su PROPIA clase.

    Un «def» a nivel de modulo escrito EN MEDIO de una clase termina el
    cuerpo de la clase: los metodos que vienen despues dejan de serlo y
    quedan sueltos. El archivo compila, flake8 calla, pyflakes calla, las
    baterias sin QGIS no los tocan y el paquete se importa sin ruido.
    Falla al EJECUTAR, con AttributeError, y solo en la rama que llama al
    metodo perdido.

    Paso de verdad, y por eso existe esta comprobacion: al mover tres
    ayudantes a nivel de modulo dentro del cuerpo de
    Sentinel1Hyp3Algorithm, la clase perdio once metodos --_rect_4326
    entre ellos-- y flake8, bandit, 133 pruebas, las 69 comprobaciones de
    este guion y el smoke test en cuatro versiones de QGIS siguieron
    todas en verde. El usuario lo encontro al segundo de ejecutar.

    Solo se miran los nombres que empiezan por «_». Los heredados de
    QgsProcessingAlgorithm --parameterAsInt, addParameter, tr...-- no
    estan en el archivo y darian falsos positivos.
    """
    inf.seccion('Metodos que se resuelven en su clase')
    huerfanos = []
    clases = 0
    for archivo in sorted(os.listdir(DIR_PAQUETE)):
        if not archivo.endswith('.py'):
            continue
        try:
            arbol = ast.parse(_texto(os.path.join(DIR_PAQUETE, archivo)))
        except SyntaxError:
            continue
        for cls in [n for n in ast.walk(arbol)
                    if isinstance(n, ast.ClassDef)]:
            clases += 1
            definidos = set()
            for m in cls.body:
                if isinstance(m, ast.FunctionDef):
                    definidos.add(m.name)
                elif isinstance(m, ast.Assign):
                    # "_tamano_salida = staticmethod(_tamano_salida)" y
                    # las constantes de clase cuentan como definidas.
                    for t in m.targets:
                        if isinstance(t, ast.Name):
                            definidos.add(t.id)
            usados = []
            for n in ast.walk(cls):
                # self._foo = ... tambien define.
                if isinstance(n, ast.Assign):
                    for t in n.targets:
                        if (isinstance(t, ast.Attribute)
                                and isinstance(t.value, ast.Name)
                                and t.value.id == 'self'):
                            definidos.add(t.attr)
                # Solo las LLAMADAS: self._x sin llamar puede ser un
                # atributo que se crea en otro sitio.
                if (isinstance(n, ast.Call)
                        and isinstance(n.func, ast.Attribute)
                        and isinstance(n.func.value, ast.Name)
                        and n.func.value.id == 'self'
                        and n.func.attr.startswith('_')):
                    usados.append((n.func.attr, n.lineno))
            for nombre, linea in usados:
                if nombre not in definidos:
                    huerfanos.append(f'{archivo}:{linea} '
                                     f'{cls.name}.self.{nombre}()')

    if not inf.comprobar(f'se analizaron {clases} clase(s)', clases > 0):
        return
    inf.comprobar('toda llamada self._metodo() existe en su clase',
                  not huerfanos,
                  'no se resuelven: ' + '; '.join(sorted(set(huerfanos))))


def comp_interfaz_en_espanol(inf):
    """Ninguna palabra inglesa suelta en lo que lee el usuario.

    La interfaz de este complemento es en espanol. Se colaban terminos
    del ingles por la via de menor resistencia --el nombre del campo STAC,
    el del argumento de GDAL-- y acababan en rotulos de parametro y en el
    registro: «Bandas / assets individuales», «Cutline preparado»,
    «Filtro de speckle», «Browse for Layer...» (que ademas es el rotulo
    del QGIS en INGLES: en un QGIS en espanol ese texto no aparece, de
    modo que la instruccion mandaba a buscar algo inexistente).

    Se revisan solo las cadenas que llegan al usuario: tr(), setHelp(),
    pushInfo() y pushWarning(). Los identificadores de Python, los nombres
    de campo de la capa y las claves de las API quedan fuera a proposito:
    renombrar el campo «assets» rompe las capas de huellas ya guardadas.
    """
    inf.seccion('Interfaz en espanol')
    # Palabras que no deben aparecer como TEXTO. Si alguna hace falta como
    # identificador --«assets» es un campo de la capa-- se escribe entre
    # comillas angulares o inversas y esta comprobacion la respeta.
    prohibidas = re.compile(
        r'\b(assets?|cutline|speckle|footprints?|browse|layer|stretch|'
        r'download|folder|preview|thumbnail|overlay|red\s+edge)\b',
        re.IGNORECASE)
    # Lo protegido: entre «...», entre `...`, o pegado a un punto o guion
    # bajo (raster:bands, speckle_filter, gdal.Warp).
    protegido = re.compile(
        r'«[^»]*»|`[^`]*`|\w+[:._]\w+|<i>[^<]*</i>')

    hallazgos = []
    for archivo in sorted(os.listdir(DIR_PAQUETE)):
        if not archivo.endswith('.py'):
            continue
        try:
            arbol = ast.parse(_texto(os.path.join(DIR_PAQUETE, archivo)))
        except SyntaxError:
            continue
        for nodo in ast.walk(arbol):
            if not (isinstance(nodo, ast.Call)
                    and isinstance(nodo.func, ast.Attribute)
                    and nodo.func.attr in ('tr', 'setHelp', 'pushInfo',
                                           'pushWarning')):
                continue
            for hijo in ast.walk(nodo):
                if not (isinstance(hijo, ast.Constant)
                        and isinstance(hijo.value, str)):
                    continue
                limpio = protegido.sub(' ', hijo.value)
                for m in prohibidas.finditer(limpio):
                    linea = getattr(hijo, 'lineno', nodo.lineno)
                    hallazgos.append(
                        f'{archivo}:{linea} «{m.group()}»')
    inf.comprobar('la interfaz no deja palabras inglesas sueltas',
                  not hallazgos,
                  'traducir o marcar como identificador: '
                  + '; '.join(sorted(set(hallazgos))[:8]))


def comp_claves_de_banda(inf):
    """Ninguna tabla indexada por la etiqueta que ve el usuario.

    Era la causa de un fallo real: ALIAS, SUFIJO, RES_NATIVA, los indices
    espectrales y las composiciones RGB estaban indexados por cadenas como
    «red (B04, 10 m)». Consecuencias, las dos vistas en produccion:

      - La etiqueta no se podia corregir sin romper las busquedas, asi que
        decia «B04, 10 m» tambien con Landsat, donde el rojo es B4 --B3 en
        TM y ETM+-- y mide 30 m. Y ese mismo texto acababa en el NOMBRE
        DEL ARCHIVO.
      - Al migrar a clave estable, las tablas que se quedaron atras no
        dieron error: la resolucion de assets cae a «usa la clave tal
        cual», no encuentra nada y el indice sencillamente no se calcula.

    Aqui se comprueban las dos mitades: que no quede ningun literal con la
    forma antigua, y que toda banda citada en COMPOSICIONES sea una clave
    de verdad.
    """
    inf.seccion('Claves de banda')
    forma_vieja = re.compile(r'^[a-z0-9]+ \(B[0-9]')
    viejos = []
    for archivo in sorted(os.listdir(DIR_PAQUETE)):
        if not archivo.endswith('.py'):
            continue
        try:
            arbol = ast.parse(_texto(os.path.join(DIR_PAQUETE, archivo)))
        except SyntaxError:
            continue
        for nodo in ast.walk(arbol):
            if (isinstance(nodo, ast.Constant)
                    and isinstance(nodo.value, str)
                    and forma_vieja.match(nodo.value)):
                viejos.append(f'{archivo}:{nodo.lineno} {nodo.value!r}')
    inf.comprobar(
        'ninguna tabla usa la etiqueta visible como clave', not viejos,
        'forma antigua «nombre (Bxx, N m)»: ' + '; '.join(viejos))

    # Y que las composiciones citen bandas que existan.
    # Se carga core.py SUELTO, por ruta: importar el paquete ejecutaria
    # __init__.py, que trae QGIS, y aqui no lo hay. core.py no importa
    # nada del proyecto, asi que se sostiene solo --esa es su regla.
    try:
        import importlib.util
        _spec = importlib.util.spec_from_file_location(
            '_core_preflight', os.path.join(DIR_PAQUETE, 'core.py'))
        _core = importlib.util.module_from_spec(_spec)
        _spec.loader.exec_module(_core)
    except Exception as e:                      # pragma: no cover
        inf.comprobar('core.py se puede cargar suelto, sin QGIS', False,
                      str(e))
        return
    validas = set(_core.CLAVES_BANDAS)
    ruta = os.path.join(DIR_PAQUETE, 'buscar_sentinel2_algoritmo.py')
    citadas, malas = 0, []
    try:
        arbol = ast.parse(_texto(ruta))
    except (SyntaxError, OSError) as e:
        inf.comprobar('se pudo leer el algoritmo de busqueda', False, str(e))
        return
    for nodo in ast.walk(arbol):
        if not (isinstance(nodo, ast.Assign)
                and any(isinstance(d, ast.Name) and d.id == 'COMPOSICIONES'
                        for d in nodo.targets)):
            continue
        for fila in getattr(nodo.value, 'elts', []):
            partes = getattr(fila, 'elts', [])
            if len(partes) < 3:
                continue
            for b in getattr(partes[2], 'elts', []):
                if isinstance(b, ast.Constant) and isinstance(b.value, str):
                    citadas += 1
                    if b.value not in validas:
                        malas.append(b.value)
    if not inf.comprobar(f'se leyeron {citadas} banda(s) de COMPOSICIONES',
                         citadas > 0):
        return
    inf.comprobar('toda banda de COMPOSICIONES es una clave real',
                  not malas, 'no existen: ' + ', '.join(sorted(set(malas))))


def comp_padres_de_campo(inf):
    """Ningun parametro de campo cuelga de un padre que pueda ser raster.

    Esto no degrada el complemento: TUMBA QGIS. Violacion de acceso, sin
    excepcion de Python que capturar, al CONSTRUIR el dialogo.

    En QgsProcessingFieldWidgetWrapper::setParentLayerWrapperValue, la
    rama de «una sola capa» hace

        QgsVectorLayer *layer = qobject_cast<QgsVectorLayer *>(
                                                 layers.at( 0 ) );
        ... context->takeResultLayer( layer->id() ) ...

    y si la capa del padre es un raster el qobject_cast da nullptr, de
    modo que layer->id() desreferencia nulo. La rama de «varias capas»,
    justo encima, si comprueba «vlayer && vlayer->isValid()», y los
    envoltorios hermanos tambien: es un olvido de QGIS, comprobado en
    release-3_44 y en master en octubre de 2026.

    Lo pagamos: el parametro CAMPO_FECHA de la exportacion a Google Earth
    colgaba de un QgsProcessingParameterMapLayer, que acepta las dos
    cosas. postInitialize corre al abrir el dialogo, asi que bastaba con
    que la capa activa del proyecto fuese un raster para que QGIS se
    cerrara antes de que el usuario pudiera elegir nada -- y abria bien
    si la activa era vectorial, lo que lo hacia parecer intermitente.

    Ni py_compile, ni flake8, ni bandit, ni las baterias sin QGIS, ni el
    smoke test lo ven: el smoke test registra los algoritmos, no abre sus
    dialogos.

    Seguro: QgsProcessingParameterFeatureSource y
    QgsProcessingParameterVectorLayer. Inseguro: MapLayer, y cualquier
    padre que no se encuentre en el mismo archivo.
    """
    inf.seccion('Padres de los parametros de campo')
    seguros = {'QgsProcessingParameterFeatureSource',
               'QgsProcessingParameterVectorLayer'}
    hijos_con_padre = {'QgsProcessingParameterField',
                       'QgsProcessingParameterExpression'}
    malos, revisados = [], 0
    for archivo in sorted(os.listdir(DIR_PAQUETE)):
        if not archivo.endswith('.py'):
            continue
        try:
            arbol = ast.parse(_texto(os.path.join(DIR_PAQUETE, archivo)))
        except SyntaxError:
            continue
        tipos, hijos = {}, []
        for nodo in ast.walk(arbol):
            if not isinstance(nodo, ast.Call):
                continue
            f = nodo.func
            nombre = (f.id if isinstance(f, ast.Name)
                      else getattr(f, 'attr', ''))
            if not nombre.startswith('QgsProcessingParameter'):
                continue
            clave = ''
            if nodo.args and isinstance(nodo.args[0], ast.Attribute):
                clave = nodo.args[0].attr
            if clave:
                tipos[clave] = nombre
            if nombre not in hijos_con_padre:
                continue
            for kw in nodo.keywords:
                if (kw.arg == 'parentLayerParameterName'
                        and isinstance(kw.value, ast.Attribute)):
                    hijos.append((clave, nombre, kw.value.attr,
                                  nodo.lineno))
        for clave, nombre, padre, linea in hijos:
            revisados += 1
            tipo_padre = tipos.get(padre)
            if tipo_padre not in seguros:
                malos.append(
                    f'{archivo}:{linea} {clave} ({nombre}) cuelga de '
                    f'{padre} ({tipo_padre or "declarado en otro archivo"})')

    inf.comprobar(
        f'los {revisados} parametro(s) con padre cuelgan de un padre '
        f'vectorial', not malos,
        'tumbaria QGIS al abrir el dialogo: ' + '; '.join(malos))


def comp_ayuda_parametros(inf):
    """Todo parametro de Processing necesita setHelp().

    Es el texto del panel de ayuda del dialogo: sin el, el usuario ve el
    nombre del parametro y nada mas. Lo pide la lista de verificacion del
    complemento y lo comprueba el smoke test dentro de QGIS -- pero ahi
    cuesta una vuelta entera de integracion continua y solo dice que algo
    falta. Comprobarlo aqui, sin QGIS, lo deja en la maquina de quien
    edita y nombra el parametro concreto.
    """
    inf.seccion('Ayuda de los parametros de Processing')
    total, faltantes = 0, []
    for archivo in sorted(os.listdir(DIR_PAQUETE)):
        if not archivo.endswith('.py'):
            continue
        ruta = os.path.join(DIR_PAQUETE, archivo)
        src = _texto(ruta)
        try:
            arbol = ast.parse(src)
        except SyntaxError:
            continue
        for n in ast.walk(arbol):
            if not isinstance(n, ast.ClassDef):
                continue
            inis = [m for m in n.body
                    if isinstance(m, ast.FunctionDef)
                    and m.name == 'initAlgorithm']
            if not inis:
                continue
            res = _params_sin_ayuda(src, inis[0].body)
            total += len(res)
            for nom, tiene, linea in res:
                if not tiene:
                    faltantes.append(f'{archivo}:{linea} {nom}')

    if not inf.comprobar('se encontro al menos un initAlgorithm con '
                         'parametros', total > 0):
        return
    inf.nota(f'{total} parametro(s) revisados')
    inf.comprobar('todo parametro lleva setHelp()', not faltantes,
                  'sin ayuda: ' + '; '.join(faltantes))


def comp_escaneo(inf):
    """El escaner del portal. Es la comprobacion que no se ve en el ZIP."""
    inf.seccion('Escaneo de seguridad (lo que corre plugins.qgis.org)')
    r = subprocess.run(  # nosec B603
        [sys.executable, '-m', 'bandit', '-q', '-r', DIR_PAQUETE,
         '-f', 'json'],
        capture_output=True, text=True)
    if r.returncode in (0, 1) and r.stdout.strip().startswith('{'):
        res = json.loads(r.stdout).get('results', [])
        graves = [x for x in res
                  if x.get('issue_severity') in ('HIGH', 'MEDIUM')]
        for x in graves[:5]:
            inf.nota(f"{x['issue_severity']:<7} {x['test_id']} "
                     f"{os.path.basename(x['filename'])}:{x['line_number']} "
                     f"{x['issue_text'][:56]}")
        inf.comprobar(f'bandit sin hallazgos bloqueantes '
                      f'({len(res)} en total)', not graves)
        # B110 y B310 son las dos que de verdad han bloqueado versiones.
        for codigo, texto in (('B110', 'try/except/pass'),
                              ('B310', 'urlopen sin validar el esquema')):
            cuantos = [x for x in res if x.get('test_id') == codigo]
            inf.comprobar(f'sin {codigo} ({texto})', not cuantos,
                          f'{len(cuantos)} caso(s)',
                          bloquea=False)
        # Un «# nosec» sin codigo silencia todo en esa linea.
        sueltos = []
        for p in _archivos_py(DIR_PAQUETE):
            for i, linea in enumerate(_texto(p).splitlines(), 1):
                if 'nosec' in linea and not re.search(r'nosec\s+B\d{3}', linea):
                    sueltos.append(f'{_rel(p)}:{i}')
        inf.comprobar('todo «# nosec» lleva su codigo B###', not sueltos,
                      f'{sueltos[:4]}')
        return
    inf.comprobar('bandit disponible', False,
                  'instale bandit: pip install bandit — es el escaneo exacto '
                  'que corre el portal')


def comp_version_coherente(g, inf):
    """La version repetida en muchos sitios: uno desfasado rompe la traza.

    La lista de archivos se DEDUCE recorriendo el paquete, no se fija a
    mano: una lista fija no se enteraria de un modulo nuevo, que es
    precisamente el caso en que se olvida subir la version. Un archivo que
    no declara version no es un fallo —__init__.py de un paquete sin
    cabecera, por ejemplo— pero el que la declara tiene que coincidir.
    """
    inf.seccion('Coherencia de version')
    version = (g.get('version') or '').strip()
    if not version:
        inf.comprobar('metadata declara una version', False)
        return

    re_doc = re.compile(r'^Versi\u00f3n\s*:\s*([0-9]+\.[0-9]+\.[0-9]+)',
                        re.M)
    re_const = re.compile(r"^\s*VERSION\s*=\s*'v?([0-9]+\.[0-9]+\.[0-9]+)'",
                          re.M)
    revisados = 0
    for ruta in _archivos_py(DIR_PAQUETE):
        nombre = os.path.basename(ruta)
        texto = _texto(ruta)
        # Solo la CABECERA del modulo: el historial de cambios menciona a
        # proposito versiones antiguas y no debe contar como desfase.
        cabeza = texto.split('Historial:')[0]
        for etiqueta, halladas in (
                ('Version: en la cabecera',
                 set(re_doc.findall(cabeza))),
                ("constante VERSION", set(re_const.findall(texto)))):
            if not halladas:
                continue
            revisados += 1
            malas = sorted(halladas - {version})
            inf.comprobar(f'{nombre}: {etiqueta} = {version}',
                          not malas,
                          f'declara {malas}, metadata dice {version}')
    inf.comprobar(f'se revisaron {revisados} declaracion(es) de version',
                  revisados > 0)

    # Hay DOS README con insignia de version: el del paquete, que viaja en
    # el ZIP, y el de la raiz del repositorio, que es la portada en GitHub.
    # Se comprueban los dos porque uno solo es exactamente como se
    # desincronizan.
    for etiqueta, ruta_readme in (
            ('README del paquete',
             os.path.join(DIR_PAQUETE, 'README.md')),
            ('README del repositorio', os.path.join(RAIZ, 'README.md'))):
        if not os.path.isfile(ruta_readme):
            continue
        texto = _texto(ruta_readme)
        inf.comprobar(f'{etiqueta}: insignia version-{version}',
                      f'version-{version}-' in texto)

    ruta_readme = os.path.join(DIR_PAQUETE, 'README.md')
    if os.path.isfile(ruta_readme):
        inf.comprobar(f'README del paquete: fila de historial para {version}',
                      f'| {version} |' in _texto(ruta_readme))


def comp_carga(ruta_zip, inf):
    """Extrae el ZIP e intenta cargarlo como lo hara QGIS.

    Sin QGIS instalado no se puede importar el paquete, asi que se
    comprueba lo que si se puede sin el: que cada modulo compile desde el
    ZIP extraido y que el arbol declare lo que QGIS va a buscar. El
    import de verdad lo hace el trabajo «qgis» de CI, dentro de una
    imagen con QGIS.
    """
    inf.seccion('Carga del paquete empaquetado')
    with tempfile.TemporaryDirectory() as tmp:
        with zipfile.ZipFile(ruta_zip) as z:
            z.extractall(tmp)
        dir_ext = os.path.join(tmp, PAQUETE)
        archivos = _archivos_py(dir_ext)
        r = subprocess.run(  # nosec B603
            [sys.executable, '-m', 'py_compile'] + archivos,
            capture_output=True, text=True)
        inf.comprobar('cada modulo compila desde el ZIP extraido',
                      r.returncode == 0,
                      (r.stderr.strip().splitlines() or [''])[-1])

        sonda = (
            'import sys, importlib.util;'
            f'sys.path.insert(0, r"{tmp}");'
            f'import {PAQUETE} as p;'
            'print(getattr(p, "__name__", "?"))'
        )
        r = subprocess.run(  # nosec B603
            [sys.executable, '-c', sonda], capture_output=True, text=True,
            timeout=120)
        if r.returncode == 0:
            inf.comprobar('el paquete extraido se importa', True)
            inf.nota('el __init__.py no arrastra QGIS al importarse, que es '
                     'justo lo que permite probar sin QGIS instalado')
        else:
            ultima = (r.stderr.strip().splitlines() or [''])[-1]
            # Que falte QGIS aqui no es un defecto del complemento: es que
            # esta maquina no lo tiene. Cualquier OTRO fallo de import si
            # romperia la carga en QGIS, y ese si bloquea.
            if 'qgis' in ultima.lower():
                inf.comprobar('el paquete extraido se importa (sin QGIS: '
                              'no concluyente)', True)
                inf.nota('no se pudo importar porque aqui no hay QGIS; '
                         'eso lo cubre el trabajo «qgis» de CI')
            else:
                inf.comprobar('el paquete extraido se importa', False,
                              ultima)

        # Los algoritmos que el proveedor registra deben existir.
        prov = os.path.join(dir_ext, 'buscar_sentinel2_provider.py')
        if os.path.isfile(prov):
            texto = _texto(prov)
            registrados = len(re.findall(r'addAlgorithm\(', texto))
            inf.comprobar(f'el proveedor registra {registrados} algoritmo(s)',
                          registrados > 0)
            inf.nota('el menu del complemento debe ofrecer los mismos; '
                     'un algoritmo nuevo hay que anadirlo en los DOS sitios')


def comp_repo(g, inf, sin_red=False):
    inf.seccion('Repositorio')
    url = (g.get('repository') or '').strip()
    inf.comprobar('metadata declara repository', bool(url))
    if not url:
        return
    inf.comprobar('repository es https', url.lower().startswith('https://'))

    hay_git = os.path.isdir(os.path.join(RAIZ, '.git'))
    inf.comprobar('hay un repositorio git local', hay_git,
                  'el portal exige que el repositorio enlazado exista y '
                  'este al dia ANTES de subir el ZIP', bloquea=False)
    if hay_git:
        r = subprocess.run(  # nosec B603, B607
            ['git', '-C', RAIZ, 'status', '--porcelain'],
            capture_output=True, text=True)
        inf.comprobar('el arbol de trabajo esta confirmado',
                      not r.stdout.strip(),
                      f'{len(r.stdout.strip().splitlines())} archivo(s) sin '
                      f'confirmar', bloquea=False)

    if sin_red:
        inf.nota('--sin-red: no se comprueba si la URL responde')
        return
    try:
        import urllib.error
        import urllib.request
        peticion = urllib.request.Request(
            url, headers={'User-Agent': 'preflight/1.0'}, method='HEAD')
        with urllib.request.urlopen(peticion, timeout=20) as resp:  # nosec B310
            codigo = resp.status
        inf.comprobar(f'{url} responde ({codigo})', 200 <= codigo < 400,
                      bloquea=False)
    except urllib.error.HTTPError as e:
        # El codigo HTTP importa, y mucho. Decir «el repositorio tiene que
        # existir» ante cualquier error manda a buscar un problema que
        # puede no estar ahi: un 403 lo devuelve un proxy corporativo, un
        # cortafuegos o un limite de peticiones, y entonces la comprobacion
        # NO sabe si el repositorio existe. Un diagnostico seguro y
        # equivocado cuesta mas que no diagnosticar.
        if e.code == 404:
            inf.comprobar(f'{url} responde', False,
                          'HTTP 404: no existe o es privado. El portal exige '
                          'que el repositorio enlazado sea accesible ANTES '
                          'de subir el ZIP.', bloquea=False)
        elif e.code in (401, 403):
            inf.comprobar(f'{url} responde (no concluyente)', True)
            inf.nota(f'HTTP {e.code}: la peticion no llego a GitHub o fue '
                     f'rechazada por un proxy, un cortafuegos o un limite de '
                     f'peticiones. Esta comprobacion NO puede decir si el '
                     f'repositorio existe; abra la URL en el navegador.')
        elif e.code == 429:
            inf.comprobar(f'{url} responde (no concluyente)', True)
            inf.nota('HTTP 429: limite de peticiones. Reintente mas tarde.')
        else:
            inf.comprobar(f'{url} responde', False,
                          f'HTTP {e.code}', bloquea=False)
    except (urllib.error.URLError, OSError, ValueError) as e:
        # Sin red tampoco se sabe nada del repositorio: no es un fallo suyo.
        inf.comprobar(f'{url} responde (no concluyente)', True)
        inf.nota(f'no se pudo consultar: {e}. Si esta sin conexion, use '
                 f'--sin-red y compruebe la URL a mano.')


# -------------------------------------------------------------- programa
def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--zip', dest='zip_existente',
                    help='comprobar este ZIP en vez de construir uno')
    ap.add_argument('--sin-red', action='store_true',
                    help='omitir las comprobaciones que necesitan red')
    ap.add_argument('--json', dest='salida_json',
                    help='escribir el informe en este archivo')
    ap.add_argument('--sin-color', action='store_true')
    args = ap.parse_args(argv)

    color = not args.sin_color and sys.stdout.isatty()
    inf = Informe(color=color)

    if not os.path.isdir(DIR_PAQUETE):
        print(f'No existe el paquete {DIR_PAQUETE}')
        return 2

    if args.zip_existente:
        ruta_zip = args.zip_existente
        if not os.path.isfile(ruta_zip):
            print(f'No existe el ZIP {ruta_zip}')
            return 2
        print(f'Comprobando {ruta_zip}')
    else:
        ruta_zip, n = construir_zip()
        print(f'Construido {_rel(ruta_zip)} con {n} archivo(s)')

    with zipfile.ZipFile(ruta_zip) as z:
        comp_estructura(z, inf)
        comp_tamano(ruta_zip, z, inf)
        comp_contenido(z, inf)
        g = comp_metadata(z, inf)
        comp_icono(z, inf)
    comp_sintaxis(inf)
    comp_ids_algoritmos(inf)
    comp_tildes(inf)
    comp_metodos_resueltos(inf)
    comp_interfaz_en_espanol(inf)
    comp_claves_de_banda(inf)
    comp_padres_de_campo(inf)
    comp_ayuda_parametros(inf)
    comp_escaneo(inf)
    comp_version_coherente(g, inf)
    comp_carga(ruta_zip, inf)
    comp_repo(g, inf, sin_red=args.sin_red)

    print('\n' + '=' * 70)
    if inf.bloqueantes:
        print(inf._c(f'NO SUBIR TODAVIA — {len(inf.bloqueantes)} '
                     f'comprobacion(es) bloqueante(s)', '31'))
        for etiqueta, detalle in inf.bloqueantes:
            print(f'  - {etiqueta}')
            if detalle:
                print(f'      {detalle}')
    else:
        print(inf._c(f'LISTO PARA SUBIR — {inf.ok} comprobaciones pasadas',
                     '32'))
    if inf.avisos:
        print(f'\n{len(inf.avisos)} cosa(s) que conviene mirar:')
        for etiqueta, detalle in inf.avisos:
            print(f'  - {etiqueta}')
    if not inf.bloqueantes:
        print(f'\n  Suba {ruta_zip}')
        print('  en https://plugins.qgis.org/plugins/add/')

    if args.salida_json:
        with open(args.salida_json, 'w', encoding='utf-8') as fh:
            json.dump({'zip': ruta_zip, 'ok': inf.ok,
                       'bloqueantes': inf.bloqueantes,
                       'avisos': inf.avisos,
                       'comprobaciones': inf.detalles}, fh,
                      ensure_ascii=False, indent=2)
        print(f'  Informe JSON: {args.salida_json}')

    return 1 if inf.bloqueantes else 0


if __name__ == '__main__':
    sys.exit(main())
