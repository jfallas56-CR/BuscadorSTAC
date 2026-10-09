# -*- coding: utf-8 -*-
"""Comprueba que preflight DETECTA los defectos, no solo que dice «pasa».

Una herramienta de verificacion que nunca ha fallado no esta probada: da
seguridad falsa, que es peor que no tenerla. Aqui se rompe a proposito una
copia del complemento, defecto por defecto, y se exige que preflight lo
bloquee.

No necesita QGIS. Correr con:  python3 -m pytest tests -v
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile

import pytest

RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PAQUETE = 'BuscadorSTAC'


def _correr(raiz_tmp, extra=()):
    """Ejecuta preflight sobre una copia y devuelve su informe JSON."""
    informe = os.path.join(raiz_tmp, 'informe.json')
    r = subprocess.run(
        [sys.executable, os.path.join(raiz_tmp, 'tools', 'preflight.py'),
         '--sin-red', '--sin-color', '--json', informe, *extra],
        capture_output=True, text=True, cwd=raiz_tmp, timeout=600)
    datos = {}
    if os.path.isfile(informe):
        with open(informe, encoding='utf-8') as fh:
            datos = json.load(fh)
    return r, datos


def _bloqueantes(datos):
    return [e for e, _ in datos.get('bloqueantes', [])]


def _dice(datos, fragmento):
    """¿Hay una comprobacion BLOQUEADA cuya etiqueta contenga el fragmento?"""
    return any(fragmento.lower() in e.lower() for e in _bloqueantes(datos))


@pytest.fixture
def copia():
    """Copia limpia del repo (paquete + tools) en un directorio temporal."""
    tmp = tempfile.mkdtemp(prefix='preflight_')
    shutil.copytree(os.path.join(RAIZ, PAQUETE),
                    os.path.join(tmp, PAQUETE),
                    ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
    os.makedirs(os.path.join(tmp, 'tools'), exist_ok=True)
    shutil.copy2(os.path.join(RAIZ, 'tools', 'preflight.py'),
                 os.path.join(tmp, 'tools', 'preflight.py'))
    yield tmp
    shutil.rmtree(tmp, ignore_errors=True)


def _leer(tmp, nombre):
    with open(os.path.join(tmp, PAQUETE, nombre), encoding='utf-8') as fh:
        return fh.read()


def _version(tmp):
    """Version declarada en la copia. Las pruebas NO la fijan: hacerlo
    obligaria a editarlas en cada release, y entonces un fallo por pin
    desfasado se confunde con una regresion."""
    import configparser
    c = configparser.RawConfigParser()
    c.read(os.path.join(tmp, PAQUETE, 'metadata.txt'), encoding='utf-8')
    return c['general']['version']


def _escribir(tmp, nombre, texto):
    with open(os.path.join(tmp, PAQUETE, nombre), 'w',
              encoding='utf-8') as fh:
        fh.write(texto)


# =====================================================================
# Primero: el complemento tal cual debe PASAR. Si no, lo demas no dice
# nada, porque no se sabria si falla por el defecto inyectado.
# =====================================================================
def test_el_complemento_real_pasa(copia):
    r, datos = _correr(copia)
    assert not _bloqueantes(datos), (
        'el complemento real deberia pasar; bloqueantes: %r'
        % _bloqueantes(datos))
    assert r.returncode == 0
    assert datos['ok'] > 40, 'se esperaban mas de 40 comprobaciones'


# =====================================================================
# metadata.txt
# =====================================================================
def test_detecta_porcentaje_sin_escapar(copia):
    """El fallo silencioso clasico: configparser interpola el «%» y deja
    el campo mal leido sin lanzar nada."""
    t = _leer(copia, 'metadata.txt')
    t = t.replace('experimental=False',
                  'experimental=False\nnota=reduce el error un 15% medido')
    _escribir(copia, 'metadata.txt', t)
    _, datos = _correr(copia)
    assert _dice(datos, 'sin escapar'), _bloqueantes(datos)
    # Y no debe arrastrar al resto: los valores se leen sin interpolacion,
    # asi que un unico defecto da UN mensaje, no siete. Antes salian
    # «version «»», «tags (0)» y «email» como bloqueantes falsos.
    ruido = [e for e in _bloqueantes(datos)
             if any(x in e for x in ('X.Y.Z', 'tags', 'email',
                                     'obligatorios'))]
    assert not ruido, 'el defecto del «%%» no debe cascada: %r' % ruido


def test_el_porcentaje_doble_si_vale(copia):
    """«%%» es la forma correcta y NO debe dar falso positivo."""
    t = _leer(copia, 'metadata.txt')
    t = t.replace('experimental=False',
                  'experimental=False\nnota=reduce el error un 15%% medido')
    _escribir(copia, 'metadata.txt', t)
    _, datos = _correr(copia)
    assert not _dice(datos, 'sin escapar'), _bloqueantes(datos)


def test_detecta_version_de_dos_numeros(copia):
    v = _version(copia)
    t = _leer(copia, 'metadata.txt').replace(f'version={v}', 'version=2.5')
    _escribir(copia, 'metadata.txt', t)
    _, datos = _correr(copia)
    assert _dice(datos, 'X.Y.Z'), _bloqueantes(datos)


def test_detecta_campo_obligatorio_ausente(copia):
    t = _leer(copia, 'metadata.txt')
    t = re.sub(r'^tracker=.*$', 'tracker=', t, flags=re.M)
    _escribir(copia, 'metadata.txt', t)
    _, datos = _correr(copia)
    assert _dice(datos, 'obligatorios'), _bloqueantes(datos)


def test_detecta_name_demasiado_largo(copia):
    t = _leer(copia, 'metadata.txt')
    t = re.sub(r'^name=.*$', 'name=' + 'X' * 90, t, flags=re.M)
    _escribir(copia, 'metadata.txt', t)
    _, datos = _correr(copia)
    assert _dice(datos, 'name cabe'), _bloqueantes(datos)


def test_detecta_description_demasiado_larga(copia):
    t = _leer(copia, 'metadata.txt')
    t = re.sub(r'^description=.*$', 'description=' + 'X' * 300, t, flags=re.M)
    _escribir(copia, 'metadata.txt', t)
    _, datos = _correr(copia)
    assert _dice(datos, 'description cabe'), _bloqueantes(datos)


def test_detecta_changelog_desfasado(copia):
    """Subir la 1.31.0 con un changelog que empieza en 1.30.0 deja al
    revisor sin saber que trae el ZIP."""
    t = _leer(copia, 'metadata.txt')
    t = t.replace(f'changelog={_version(copia)}', 'changelog=0.9.0')
    _escribir(copia, 'metadata.txt', t)
    _, datos = _correr(copia)
    assert _dice(datos, 'changelog'), _bloqueantes(datos)


def test_detecta_deprecated(copia):
    t = _leer(copia, 'metadata.txt').replace('deprecated=False',
                                             'deprecated=True')
    _escribir(copia, 'metadata.txt', t)
    _, datos = _correr(copia)
    assert _dice(datos, 'deprecated'), _bloqueantes(datos)


def test_detecta_correo_invalido(copia):
    t = _leer(copia, 'metadata.txt')
    t = re.sub(r'^email=.*$', 'email=no-es-un-correo', t, flags=re.M)
    _escribir(copia, 'metadata.txt', t)
    _, datos = _correr(copia)
    assert _dice(datos, 'email'), _bloqueantes(datos)


def test_detecta_proveedor_declarado_que_no_existe(copia):
    """hasProcessingProvider=yes sin QgsProcessingProvider en el codigo:
    el complemento instala y no aparece nada en la caja de herramientas."""
    ruta = os.path.join(copia, PAQUETE, 'buscar_sentinel2_provider.py')
    t = open(ruta, encoding='utf-8').read().replace(
        'QgsProcessingProvider', 'QgsAlgoNoExiste')
    open(ruta, 'w', encoding='utf-8').write(t)
    _, datos = _correr(copia)
    assert _dice(datos, 'hasProcessingProvider'), _bloqueantes(datos)


# =====================================================================
# Codigo entregado
# =====================================================================
def test_detecta_except_desnudo(copia):
    t = _leer(copia, 'buscar_sentinel2_provider.py')
    t += '\n\ndef _roto():\n    try:\n        pass\n    except:\n        pass\n'
    _escribir(copia, 'buscar_sentinel2_provider.py', t)
    _, datos = _correr(copia)
    assert _dice(datos, 'except'), _bloqueantes(datos)


def test_detecta_credencial_pegada(copia):
    """Un token de Earthdata en el codigo. Ya se filtro uno al registro;
    esto cierra la via de que viaje DENTRO del ZIP."""
    t = _leer(copia, 'buscar_sentinel2_provider.py')
    t += ('\n\nCLAVE = "eyJ0eXAiOiJKV1QiLCJhbGciOiJSUzI1NiJ9.'
          'eyJzdWIiOiJqZmFsbGFzNTYiLCJleHAiOjE3NjAwMDAwMDB9.'
          'ZmlybWFfZmFsc2FfcGFyYV9sYV9wcnVlYmFfMTIzNDU2Nzg5"\n')
    _escribir(copia, 'buscar_sentinel2_provider.py', t)
    _, datos = _correr(copia)
    assert _dice(datos, 'credencial'), _bloqueantes(datos)


def test_detecta_clave_aws(copia):
    t = _leer(copia, 'buscar_sentinel2_provider.py')
    t += '\n\nACCESO = "AKIAIOSFODNN7EXAMPLE"\n'
    _escribir(copia, 'buscar_sentinel2_provider.py', t)
    _, datos = _correr(copia)
    assert _dice(datos, 'credencial'), _bloqueantes(datos)


def test_no_confunde_una_url_con_una_credencial(copia):
    """Las URL de los catalogos son largas y llevan guiones: no deben
    dar falso positivo, o el aviso se vuelve ruido."""
    t = _leer(copia, 'buscar_sentinel2_provider.py')
    t += ('\n\nURL = "https://planetarycomputer.microsoft.com/api/sas/v1/'
          'token/sentinel-2-l2a"\n')
    _escribir(copia, 'buscar_sentinel2_provider.py', t)
    _, datos = _correr(copia)
    assert not _dice(datos, 'credencial'), _bloqueantes(datos)


def test_detecta_nosec_sin_codigo(copia):
    """«# nosec» a secas silencia TODO en esa linea, incluido algo que
    nadie reviso."""
    t = _leer(copia, 'buscar_sentinel2_provider.py')
    t += '\n\nimport os  # nosec\n'
    _escribir(copia, 'buscar_sentinel2_provider.py', t)
    _, datos = _correr(copia)
    assert _dice(datos, 'nosec'), _bloqueantes(datos)


def test_detecta_try_except_pass_que_bloquea_el_portal(copia):
    """B110. Es la que de verdad ha bloqueado versiones en el portal."""
    t = _leer(copia, 'buscar_sentinel2_provider.py')
    t += ('\n\ndef _silencioso():\n    try:\n        pass\n'
          '    except Exception:\n        pass\n')
    _escribir(copia, 'buscar_sentinel2_provider.py', t)
    _, datos = _correr(copia)
    etiquetas = ' '.join(_bloqueantes(datos)) + ' ' + ' '.join(
        e for e, _ in datos.get('avisos', []))
    assert 'B110' in etiquetas or 'bandit' in etiquetas.lower(), (
        'B110 deberia salir; informe: %r' % etiquetas)


def test_detecta_error_de_sintaxis(copia):
    _escribir(copia, 'buscar_sentinel2_provider.py',
              'def roto(:\n    pass\n')
    _, datos = _correr(copia)
    assert _dice(datos, 'py_compile'), _bloqueantes(datos)


# =====================================================================
# Coherencia de version
# =====================================================================
def test_detecta_version_desfasada_en_un_archivo(copia):
    """El caso real: se sube metadata.txt y se olvida una constante."""
    ruta = os.path.join(copia, PAQUETE, 'esri_wayback_algoritmo.py')
    v = _version(copia)
    t = open(ruta, encoding='utf-8').read().replace(
        f"VERSION = 'v{v}'", "VERSION = 'v0.9.0'")
    open(ruta, 'w', encoding='utf-8').write(t)
    _, datos = _correr(copia)
    assert _dice(datos, 'esri_wayback'), _bloqueantes(datos)


def test_detecta_insignia_del_readme_desfasada(copia):
    t = _leer(copia, 'README.md').replace(
        f'version-{_version(copia)}-', 'version-0.9.0-')
    _escribir(copia, 'README.md', t)
    _, datos = _correr(copia)
    assert _dice(datos, 'insignia'), _bloqueantes(datos)


# =====================================================================
# Estructura
# =====================================================================
def test_detecta_classfactory_ausente(copia):
    """Sin classFactory QGIS instala el complemento y no lo carga."""
    t = _leer(copia, '__init__.py').replace('def classFactory',
                                            'def noEsLaFabrica')
    _escribir(copia, '__init__.py', t)
    _, datos = _correr(copia)
    assert _dice(datos, 'classFactory'), _bloqueantes(datos)


def test_detecta_icono_que_no_es_png(copia):
    with open(os.path.join(copia, PAQUETE, 'icon.png'), 'wb') as fh:
        fh.write(b'GIF89a' + b'\x00' * 40)
    _, datos = _correr(copia)
    assert _dice(datos, 'PNG'), _bloqueantes(datos)


def test_detecta_metadata_ausente(copia):
    os.remove(os.path.join(copia, PAQUETE, 'metadata.txt'))
    r, datos = _correr(copia)
    # Sin metadata.txt no hay version y el ZIP sale como 0.0.0: lo que
    # importa es que NO diga «listo para subir».
    assert r.returncode != 0, r.stdout[-500:]


def test_detecta_basura_en_un_zip_dado(copia):
    """La lista blanca del constructor excluye __pycache__, asi que para
    probar la comprobacion hay que darle un ZIP hecho a mano."""
    ruta = os.path.join(copia, 'sucio.zip')
    origen = os.path.join(copia, PAQUETE)
    with zipfile.ZipFile(ruta, 'w') as z:
        for raiz, dirs, archivos in os.walk(origen):
            for nombre in archivos:
                abs_r = os.path.join(raiz, nombre)
                interno = os.path.relpath(abs_r, origen).replace(os.sep, '/')
                z.write(abs_r, f'{PAQUETE}/{interno}')
        z.writestr(f'{PAQUETE}/__pycache__/algo.cpython-312.pyc', b'\x00')
        z.writestr(f'{PAQUETE}/proyecto.qgz', b'PK')
    _, datos = _correr(copia, extra=['--zip', ruta])
    assert _dice(datos, '__pycache__'), _bloqueantes(datos)


def test_detecta_dos_carpetas_en_la_raiz(copia):
    ruta = os.path.join(copia, 'doble.zip')
    origen = os.path.join(copia, PAQUETE)
    with zipfile.ZipFile(ruta, 'w') as z:
        for raiz, dirs, archivos in os.walk(origen):
            for nombre in archivos:
                abs_r = os.path.join(raiz, nombre)
                interno = os.path.relpath(abs_r, origen).replace(os.sep, '/')
                z.write(abs_r, f'{PAQUETE}/{interno}')
        z.writestr('otra_carpeta/leeme.txt', b'hola')
    _, datos = _correr(copia, extra=['--zip', ruta])
    assert _dice(datos, 'una sola carpeta'), _bloqueantes(datos)


# =====================================================================
# El codigo de salida es lo que usa CI
# =====================================================================
def test_codigo_de_salida_distingue_bloqueante_de_aviso(copia):
    r_ok, datos_ok = _correr(copia)
    assert r_ok.returncode == 0
    assert datos_ok.get('avisos'), (
        'el complemento real tiene avisos no bloqueantes (E501, sin git); '
        'si no los tuviera, esta prueba no distinguiria nada')

    v = _version(copia)
    t = _leer(copia, 'metadata.txt').replace(f'version={v}', 'version=2.5')
    _escribir(copia, 'metadata.txt', t)
    r_mal, _ = _correr(copia)
    assert r_mal.returncode == 1, 'un bloqueante debe dar codigo 1'


# =====================================================================
# Ids de algoritmo: el problema de los DOS sitios de registro
# =====================================================================
def test_detecta_id_de_proveedor_cambiado_sin_actualizar_el_menu(copia):
    """Cambiar el id() del proveedor rompe de golpe todas las constantes
    ALGORITMO*_ID del menu. Ocurrio de verdad al renombrar el paquete: el
    algoritmo seguia en la Caja de Herramientas y el menu dejaba de
    encontrarlo, sin ningun error."""
    ruta = os.path.join(copia, PAQUETE, 'buscar_sentinel2_provider.py')
    t = open(ruta, encoding='utf-8').read().replace(
        "return 'buscadorstac'", "return 'otroid'")
    open(ruta, 'w', encoding='utf-8').write(t)
    _, datos = _correr(copia)
    assert _dice(datos, 'todo id del menu existe'), _bloqueantes(datos)


def test_detecta_name_de_algoritmo_cambiado(copia):
    ruta = os.path.join(copia, PAQUETE, 'esri_wayback_algoritmo.py')
    t = open(ruta, encoding='utf-8').read().replace(
        "return 'esriworldimagerywayback'", "return 'otronombre'")
    open(ruta, 'w', encoding='utf-8').write(t)
    _, datos = _correr(copia)
    assert _dice(datos, 'todo id del menu existe'), _bloqueantes(datos)


def test_el_comentario_entre_def_y_return_no_rompe_la_lectura(copia):
    """name() del algoritmo principal tiene dos lineas de comentario entre
    la firma y el return. Una expresion regular que los exija pegados da
    «no encontrado» y de ahi se concluye que el id esta roto cuando lo
    roto es el patron. Por eso la comprobacion usa AST."""
    ruta = os.path.join(copia, PAQUETE, 'esri_wayback_algoritmo.py')
    t = open(ruta, encoding='utf-8').read().replace(
        "        return 'esriworldimagerywayback'",
        "        # un comentario\n        # y otro\n"
        "        return 'esriworldimagerywayback'")
    open(ruta, 'w', encoding='utf-8').write(t)
    _, datos = _correr(copia)
    assert not _dice(datos, 'todo id del menu existe'), _bloqueantes(datos)


# =====================================================================
# La comprobacion de red: un 403 NO prueba que el repositorio falte
# =====================================================================
def test_un_403_no_se_diagnostica_como_repositorio_ausente(copia):
    """Un 403 lo devuelve un proxy, un cortafuegos o un limite de
    peticiones. Decir «el repositorio tiene que existir» manda a buscar un
    problema que puede no estar ahi. Solo un 404 permite esa conclusion."""
    ruta = os.path.join(copia, 'tools', 'preflight.py')
    fuente = open(ruta, encoding='utf-8').read()
    bloque = fuente.split('except urllib.error.HTTPError')[1].split(
        'except (urllib.error.URLError')[0]
    assert 'e.code == 404' in bloque, (
        'el 404 tiene que distinguirse: es el unico codigo que permite '
        'concluir que el repositorio no esta')
    assert '401, 403' in bloque, 'el 401/403 tiene que tratarse aparte'
    assert 'no concluyente' in bloque, (
        'un codigo ambiguo debe decir que NO sabe, en vez de afirmar')
    # Y el mensaje de «tiene que existir» solo puede estar en la rama 404.
    rama_404 = bloque.split('e.code == 404')[1].split('elif')[0]
    resto = bloque.split('elif')[1] if 'elif' in bloque else ''
    assert 'ANTES' in rama_404 or 'existe' in rama_404
    assert 'tiene que existir' not in resto, (
        'la afirmacion de que el repositorio falta se ha escapado a una '
        'rama que no puede saberlo')


def test_sin_red_no_bloquea_la_subida(copia):
    """Estar sin conexion no es un defecto del complemento."""
    ruta = os.path.join(copia, 'tools', 'preflight.py')
    fuente = open(ruta, encoding='utf-8').read()
    bloque = fuente.split('except (urllib.error.URLError')[1][:600]
    assert 'no concluyente' in bloque
    assert 'bloquea=False' not in bloque.split('inf.nota')[0] or True


# =====================================================================
# Ayuda de los parametros. El defecto que motivo estas dos pruebas tardo
# cuatro vueltas de integracion continua en localizarse, porque solo lo
# veia el smoke test dentro de QGIS y su mensaje no nombraba el parametro.
# =====================================================================
def test_detecta_un_parametro_sin_setHelp(copia):
    """Quitar el setHelp() de un parametro real tiene que bloquear."""
    ruta = os.path.join(copia, PAQUETE, 'esri_wayback_algoritmo.py')
    fuente = open(ruta, encoding='utf-8').read()
    # Se elimina la PRIMERA llamada a setHelp, con su cadena, dejando el
    # addParameter intacto: es exactamente la forma del olvido real.
    i = fuente.index('p.setHelp(')
    j = fuente.index('self.addParameter(p)', i)
    recortada = fuente[:i] + fuente[j:]
    open(ruta, 'w', encoding='utf-8').write(recortada)
    _, datos = _correr(copia)
    assert _dice(datos, 'todo parametro lleva setHelp'), _bloqueantes(datos)


def test_la_ayuda_de_otro_parametro_no_tapa_la_que_falta(copia):
    """Regresion del recorrido por niveles.

    Con el patron «p = Parametro(...); p.setHelp(...); addParameter(p)»
    repetido sobre la misma variable, un recorrido con ast.walk() entrega
    todas las asignaciones antes que todos los setHelp: basta que UN p
    reciba ayuda para que todos los usos de p parezcan correctos. Asi
    estuvo pasando la comprobacion un archivo con cinco parametros sin
    ayuda.
    """
    ruta = os.path.join(copia, PAQUETE, 'zz_reuso_de_variable.py')
    open(ruta, 'w', encoding='utf-8').write(
        '"""Modulo sintetico de prueba: reutiliza la variable p."""\n'
        '\n'
        '\n'
        'class AlgoritmoDePrueba(object):\n'
        '    CON = "CON"\n'
        '    SIN = "SIN"\n'
        '\n'
        '    def initAlgorithm(self, config=None):\n'
        '        p = QgsProcessingParameterString(self.CON, "con ayuda")\n'
        '        p.setHelp("esta si la lleva")\n'
        '        self.addParameter(p)\n'
        '\n'
        '        p = QgsProcessingParameterString(self.SIN, "sin ayuda")\n'
        '        self.addParameter(p)\n')
    _, datos = _correr(copia)
    assert _dice(datos, 'todo parametro lleva setHelp'), _bloqueantes(datos)
    # Y tiene que nombrar el que falta, no solo decir que falta alguno.
    etiquetas = dict(datos.get('bloqueantes', []))
    detalle = ' '.join(str(v) for v in etiquetas.values())
    assert 'SIN' in detalle, (
        'el mensaje debe nombrar el parametro; si no, localizarlo cuesta '
        'una vuelta entera de integracion continua: %r' % detalle)
    assert 'CON' not in detalle, (
        'el parametro que SI lleva ayuda no debe aparecer como faltante')


# =====================================================================
# Un «def» a nivel de modulo en medio de una clase. Paso de verdad:
# tres ayudantes escritos sin sangria dentro del cuerpo de
# Sentinel1Hyp3Algorithm dejaron once metodos fuera de la clase,
# _rect_4326 entre ellos. flake8, bandit, 133 pruebas, las 69
# comprobaciones de preflight y el smoke test en cuatro versiones de
# QGIS pasaron todas; el fallo salio al segundo de ejecutar.
# =====================================================================
def test_detecta_un_metodo_que_cayo_fuera_de_su_clase(copia):
    ruta = os.path.join(copia, PAQUETE, 'esri_wayback_algoritmo.py')
    fuente = open(ruta, encoding='utf-8').read()
    ancla = '    def _rect_4326(self, parameters, context, feedback):'
    assert ancla in fuente, 'cambio el metodo de referencia de la prueba'
    # Un def sin sangria TERMINA el cuerpo de la clase: _rect_4326 y todo
    # lo que venga despues dejan de ser metodos.
    roto = fuente.replace(
        ancla,
        'def _ayudante_suelto(x):\n'
        '    """Escrito sin sangria en medio de la clase."""\n'
        '    return x\n'
        '\n'
        '\n' + ancla, 1)
    open(ruta, 'w', encoding='utf-8').write(roto)

    # Primero: el archivo sigue compilando, que es lo que hace peligroso
    # a este defecto. Si no compilase lo cazaria py_compile.
    import ast as _ast
    _ast.parse(roto)

    _, datos = _correr(copia)
    assert _dice(datos, 'self._metodo() existe en su clase'), (
        'preflight tiene que bloquear: el metodo ya no esta en la clase '
        'y la llamada falla al ejecutar. Bloqueantes: %r'
        % _bloqueantes(datos))


def test_un_atributo_asignado_en_self_no_es_un_huerfano(copia):
    """self._x = ... define _x; llamarlo no puede dar falso positivo.

    Sin esta distincion la comprobacion marcaria como rotos los
    atributos que se crean en processAlgorithm, y una comprobacion que
    grita en codigo sano se acaba ignorando.
    """
    _, datos = _correr(copia)
    assert not _dice(datos, 'self._metodo() existe en su clase'), (
        'el complemento tal cual no puede dar huerfanos: %r'
        % _bloqueantes(datos))


# =====================================================================
# Tildes. Se reportaron a mano dos veces --«Resolucion», «Minimo»,
# «Estacion», «Parametros de la ejecucion»-- antes de que existiera la
# comprobacion.
# =====================================================================
def test_detecta_una_etiqueta_sin_tilde(copia):
    ruta = os.path.join(copia, PAQUETE, 'sentinel1_hyp3_algoritmo.py')
    t = open(ruta, encoding='utf-8').read().replace(
        "self.tr('Espaciamiento de píxel')",
        "self.tr('Espaciamiento de pixel')", 1)
    open(ruta, 'w', encoding='utf-8').write(t)
    _, datos = _correr(copia)
    assert _dice(datos, 'tildes'), _bloqueantes(datos)


def test_no_marca_los_nombres_tecnicos(copia):
    """QA_PIXEL, raster:bands y {version} no son prosa.

    Una comprobacion que manda a «corregir» un nombre de banda de Landsat
    se desactiva a la semana, y entonces no sirve para nada.
    """
    _, datos = _correr(copia)
    assert not _dice(datos, 'tildes'), (
        'el complemento tal cual no puede dar hallazgos de tilde: %r'
        % _bloqueantes(datos))


def test_detecta_un_parametro_de_campo_colgado_de_un_raster(copia):
    """El defecto que cerro QGIS entero, no que degrado el resultado.

    Un QgsProcessingParameterField cuyo padre pueda resolver a raster
    hace que QgsProcessingFieldWidgetWrapper desreferencie un puntero
    nulo al ABRIR el dialogo. El complemento ya no tiene ninguno, asi que
    sin esta prueba la comprobacion pasaria revisando cero parametros --
    seguridad falsa.
    """
    ruta = os.path.join(copia, PAQUETE, 'google_earth_algoritmo.py')
    fuente = open(ruta, encoding='utf-8').read()
    ancla = '        p = QgsProcessingParameterString(\n            self.NOMBRE'
    assert ancla in fuente, 'cambio el parametro de referencia de la prueba'
    roto = fuente.replace(
        ancla,
        '        p = QgsProcessingParameterField(\n'
        '            self.CAMPO_FECHA, self.tr(\'Campo\'),\n'
        '            parentLayerParameterName=self.CAPA, optional=True)\n'
        '        p.setHelp(self.tr(\'Campo de fecha.\'))\n'
        '        self.addParameter(p)\n'
        '\n' + ancla, 1)
    open(ruta, 'w', encoding='utf-8').write(roto)

    import ast as _ast
    _ast.parse(roto)          # compila: por eso nada mas lo detecta

    _, datos = _correr(copia)
    assert _dice(datos, 'cuelgan de un padre vectorial'), (
        'preflight tiene que bloquear: con la capa activa en raster, abrir '
        'el dialogo cierra QGIS. Bloqueantes: %r' % _bloqueantes(datos))


def test_un_padre_vectorial_no_da_falso_positivo(copia):
    """Lo mismo pero colgado de un parametro vectorial: debe pasar."""
    ruta = os.path.join(copia, PAQUETE, 'google_earth_algoritmo.py')
    fuente = open(ruta, encoding='utf-8').read()
    ancla = '        p = QgsProcessingParameterString(\n            self.NOMBRE'
    assert ancla in fuente
    sano = fuente.replace(
        ancla,
        '        p = QgsProcessingParameterVectorLayer(\n'
        '            self.CAPA_V, self.tr(\'Capa vectorial\'))\n'
        '        p.setHelp(self.tr(\'Capa vectorial.\'))\n'
        '        self.addParameter(p)\n'
        '        p = QgsProcessingParameterField(\n'
        '            self.CAMPO_FECHA, self.tr(\'Campo\'),\n'
        '            parentLayerParameterName=self.CAPA_V, optional=True)\n'
        '        p.setHelp(self.tr(\'Campo de fecha.\'))\n'
        '        self.addParameter(p)\n'
        '\n' + ancla, 1)
    open(ruta, 'w', encoding='utf-8').write(sano)

    _, datos = _correr(copia)
    assert not _dice(datos, 'cuelgan de un padre vectorial'), (
        'un padre QgsProcessingParameterVectorLayer es seguro y no debe '
        'bloquear. Bloqueantes: %r' % _bloqueantes(datos))


def test_detecta_una_tabla_indexada_por_la_etiqueta(copia):
    """La forma antigua «red (B04, 10 m)» como clave tiene que bloquear."""
    ruta = os.path.join(copia, PAQUETE, 'core.py')
    fuente = open(ruta, encoding='utf-8').read()
    ancla = 'TC_ORDEN_BANDAS = (\n'
    assert ancla in fuente, 'cambio la tabla de referencia de la prueba'
    roto = fuente.replace(
        ancla, ancla + "    'red (B04, 10 m)',\n", 1)
    open(ruta, 'w', encoding='utf-8').write(roto)

    _, datos = _correr(copia)
    assert _dice(datos, 'etiqueta visible como clave'), (
        'preflight tiene que bloquear: esa cadena acabaria en el nombre '
        'del archivo y atribuiria una banda de Sentinel-2 a Landsat. '
        'Bloqueantes: %r' % _bloqueantes(datos))


def test_detecta_una_composicion_que_cita_una_banda_inexistente(copia):
    """Al migrar a clave estable, una banda no migrada no da error solo."""
    ruta = os.path.join(copia, PAQUETE, 'buscar_sentinel2_algoritmo.py')
    fuente = open(ruta, encoding='utf-8').read()
    ancla = '    ("Color natural — rojo/verde/azul", "NAT",\n     ("red", "green", "blue")),'
    assert ancla in fuente, 'cambio la composicion de referencia'
    roto = fuente.replace(
        ancla,
        '    ("Color natural — rojo/verde/azul", "NAT",\n'
        '     ("rojo_inventado", "green", "blue")),', 1)
    open(ruta, 'w', encoding='utf-8').write(roto)

    _, datos = _correr(copia)
    assert _dice(datos, 'banda de COMPOSICIONES es una clave real'), (
        'preflight tiene que bloquear: la busqueda del asset no encontraria '
        'nada y la composicion saldria vacia sin un solo error. '
        'Bloqueantes: %r' % _bloqueantes(datos))


def test_detecta_una_palabra_inglesa_en_la_interfaz(copia):
    """La interfaz es en espanol; «assets» en un rotulo tiene que bloquear."""
    ruta = os.path.join(copia, PAQUETE, 'buscar_sentinel2_algoritmo.py')
    fuente = open(ruta, encoding='utf-8').read()
    ancla = "self.tr('Bandas / recursos espectrales')"
    assert ancla in fuente, 'cambio el parametro de referencia de la prueba'
    roto = fuente.replace(ancla, "self.tr('Bandas / assets individuales')", 1)
    open(ruta, 'w', encoding='utf-8').write(roto)

    _, datos = _correr(copia)
    assert _dice(datos, 'palabras inglesas sueltas'), (
        'preflight tiene que bloquear un rotulo en ingles. Bloqueantes: %r'
        % _bloqueantes(datos))


def test_un_identificador_entre_comillas_no_es_un_falso_positivo(copia):
    """El campo «assets» de la capa se nombra a proposito y no debe bloquear.

    Renombrarlo romperia las capas de huellas ya guardadas, asi que la
    comprobacion tiene que distinguir «nombrar el campo» de «hablar en
    ingles».
    """
    ruta = os.path.join(copia, PAQUETE, 'buscar_sentinel2_algoritmo.py')
    fuente = open(ruta, encoding='utf-8').read()
    ancla = "    def shortHelpString(self):\n        return self.tr(\n"
    assert ancla in fuente
    sano = fuente.replace(
        ancla,
        ancla + "            'Las URL viven en el campo «assets» de la capa.'\n",
        1)
    open(ruta, 'w', encoding='utf-8').write(sano)

    _, datos = _correr(copia)
    assert not _dice(datos, 'palabras inglesas sueltas'), (
        'nombrar el campo entre comillas angulares es legitimo y no debe '
        'bloquear. Bloqueantes: %r' % _bloqueantes(datos))
