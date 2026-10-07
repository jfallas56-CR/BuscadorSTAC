# -*- coding: utf-8 -*-
"""Lógica pura del complemento, probada SIN QGIS y sin imitarlo.

Este archivo es la prueba de que la extracción sirvió de algo: importa
BuscadorSTAC.core directamente, sin stub, sin clases falsas de Qt y
sin variables de entorno. Si alguien añade un «from qgis.core import …»
a core.py, la primera prueba falla y dice por qué.

    python3 -m pytest tests/test_core.py -v

Las funciones que reciben una fuente de entidades o un «feedback» lo usan
por pato: basta un objeto con los métodos que llaman. Eso no es una
concesión a las pruebas, es lo que mantiene core.py libre de QGIS.
"""

from __future__ import annotations

import ast
import io
import json
import math
import os
import sys

import numpy as np
import pytest

RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, RAIZ)

from BuscadorSTAC import core                            # noqa: E402


class Feedback:
    """Lo mínimo que piden las funciones de core: avisar e informar."""

    def __init__(self):
        self.info, self.avisos, self.debug = [], [], []

    def pushInfo(self, t):
        self.info.append(str(t))

    def pushWarning(self, t):
        self.avisos.append(str(t))

    def pushDebugInfo(self, t):
        self.debug.append(str(t))

    def isCanceled(self):
        return False

    def texto(self):
        return '\n'.join(self.info + self.avisos)


# =====================================================================
# La regla del módulo
# =====================================================================
def test_core_no_importa_qgis_ni_qt_ni_gdal():
    """Es la única regla de core.py, y la que hace posible este archivo."""
    ruta = os.path.join(RAIZ, 'BuscadorSTAC', 'core.py')
    arbol = ast.parse(io.open(ruta, encoding='utf-8').read())
    modulos = []
    for n in ast.walk(arbol):
        if isinstance(n, ast.Import):
            modulos += [a.name for a in n.names]
        elif isinstance(n, ast.ImportFrom) and n.module:
            modulos.append(n.module)
    prohibidos = [m for m in modulos
                  if m.split('.')[0] in ('qgis', 'osgeo', 'PyQt5', 'PyQt6')]
    assert not prohibidos, (
        'core.py importa %r. Eso obliga a tener QGIS para probar cualquier '
        'cosa de aquí y devuelve el proyecto al stub. Si la función nueva '
        'necesita QGIS, su sitio es buscar_sentinel2_algoritmo.py.'
        % prohibidos)


def test_core_se_importa_sin_qgis_en_sys_modules():
    assert 'qgis' not in sys.modules and 'qgis.core' not in sys.modules, (
        'algo metió QGIS en sys.modules: estas pruebas ya no demuestran que '
        'core.py funcione sin él')


# =====================================================================
# _parsear_meses / _parsear_limites / _codigo_meses
# =====================================================================
def test_parsear_meses():
    assert core._parsear_meses('12,1,2') == [12, 1, 2]
    assert core._parsear_meses(' 5 , 6 ') == [5, 6]
    assert core._parsear_meses('') == []
    assert core._parsear_meses(None) == []


@pytest.mark.parametrize('malo', ['0,1', '1,13', 'ene,feb', '-3', '1.5,2'])
def test_parsear_meses_rechaza_lo_imposible(malo):
    """Un mes 0 o 13 tiene que dar [] y no colarse como índice: un mes
    fuera de rango falsea la amplitud estacional sin ningún error."""
    assert core._parsear_meses(malo) == [], malo


def test_parsear_meses_tolera_separadores_y_huecos():
    """El «;» y una coma de más se admiten a propósito: son el error de
    tecleo normal y rechazarlos no protege de nada."""
    assert core._parsear_meses('1;2') == [1, 2]
    assert core._parsear_meses('1,,2') == [1, 2]
    assert core._parsear_meses('1, 2,') == [1, 2]


def test_parsear_limites():
    assert core._parsear_limites('-0.1,0.5') == (-0.1, 0.5)
    assert core._parsear_limites(' 0 , 1 ') == (0.0, 1.0)
    assert core._parsear_limites('') is None
    assert core._parsear_limites(None) is None


@pytest.mark.parametrize('malo', ['0.5,-0.1', 'a,b', '1', '1,2,3', '2,2'])
def test_parsear_limites_rechaza_lo_invalido(malo):
    """Orden invertido e iguales incluidos: hi>lo es estricto, porque un
    rango de ancho cero daría una división por cero en el realce."""
    assert core._parsear_limites(malo) is None, malo


def test_parsear_limites_admite_punto_y_coma():
    assert core._parsear_limites('-0.1;0.5') == (-0.1, 0.5)


def test_codigo_meses():
    # Meses contiguos se colapsan a un rango; no contiguos se separan con
    # puntos. El salto diciembre->enero cuenta como contiguo, que es la
    # estación seca del Pacífico norte.
    assert core._codigo_meses([1, 2, 3]) == '01-03'
    assert core._codigo_meses([12, 1]) == '12-01'
    assert core._codigo_meses([12, 1, 2, 3, 4]) == '12-04'
    assert core._codigo_meses([1, 3, 7]) == '01.03.07'
    assert core._codigo_meses(list(range(1, 13))) == '01-12'
    assert core._codigo_meses([]) == 'NA'
    # Dos listas distintas no pueden dar el mismo código: el código va en
    # el NOMBRE del archivo y una colisión sobreescribiría una serie.
    vistos = {}
    for meses in ([1, 2], [12], [1, 22], [11, 2], [1, 12], [11, 12]):
        c = core._codigo_meses(meses)
        assert c not in vistos or vistos[c] == meses, (
            'colisión de código %r entre %r y %r' % (c, vistos[c], meses))
        vistos[c] = meses


# =====================================================================
# _tamano_salida
# =====================================================================
def test_tamano_salida_conserva_la_proporcion():
    # 1 grado de lon por 1 de lat cerca del ecuador: la celda es casi
    # cuadrada en metros, así que el alto debe parecerse al ancho.
    ancho, alto = core._tamano_salida([-84.0, 9.0, -83.0, 10.0], 1000)
    assert ancho == 1000
    assert 900 < alto < 1100, (ancho, alto)


def test_tamano_salida_aoi_alargado():
    # Cuatro veces más ancho que alto -> el alto sale ~1/4 del ancho.
    ancho, alto = core._tamano_salida([-86.0, 9.0, -82.0, 10.0], 800)
    assert ancho == 800
    assert 150 < alto < 260, (ancho, alto)


def test_tamano_salida_nunca_devuelve_cero():
    """Un AOI degenerado no puede dar 0 píxeles: GDAL falla con un
    mensaje que no apunta al AOI."""
    for bbox in ([-84.0, 9.0, -84.0, 9.0], [-84.0, 9.0, -83.99999, 9.00001]):
        ancho, alto = core._tamano_salida(bbox, 500)
        assert ancho >= 1 and alto >= 1, (bbox, ancho, alto)


# =====================================================================
# _salida_volatil / _destino_sumidero
# =====================================================================
class _Prop:
    def __init__(self, v):
        self._v = v

    def staticValue(self):
        return self._v


class _DefSalida:
    def __init__(self, v):
        self.sink = _Prop(v)


@pytest.mark.parametrize('crudo', [
    'TEMPORARY_OUTPUT', 'memory:huellas', 'MEMORY',
    _DefSalida('TEMPORARY_OUTPUT'), _DefSalida('memory:x'),
])
def test_salida_volatil_detecta_lo_temporal(crudo):
    assert core._salida_volatil(crudo) is True


@pytest.mark.parametrize('crudo', [
    '/home/jorge/huellas.gpkg', r'C:\Users\jfall\huellas.gpkg',
    '/datos/memoria_tecnica/huellas.gpkg',      # «memoria» en la ruta
    "ogr:dbname='/x/y.gpkg'", None, '',
    _DefSalida('/home/jorge/h.gpkg'),
])
def test_salida_volatil_sin_falsos_positivos(crudo):
    """Un falso positivo entrena al usuario a ignorar el aviso."""
    assert core._salida_volatil(crudo) is False


# =====================================================================
# _procedencia_huellas / _items_desde_capa  (uso por pato)
# =====================================================================
class _Campo:
    def __init__(self, n):
        self._n = n

    def name(self):
        return self._n


class _Campos:
    def __init__(self, nombres):
        self._c = [_Campo(x) for x in nombres]

    def count(self):
        return len(self._c)

    def at(self, i):
        return self._c[i]


class _Rasgo:
    def __init__(self, d, fid=1):
        self._d, self._fid = d, fid

    def __getitem__(self, k):
        if k not in self._d:
            raise KeyError(k)
        return self._d[k]

    def id(self):
        return self._fid


class _Fuente:
    def __init__(self, nombres, filas):
        self._n, self._f = nombres, filas

    def fields(self):
        return _Campos(self._n)

    def featureCount(self):
        return len(self._f)

    def getFeatures(self):
        return iter([_Rasgo(f, i) for i, f in enumerate(self._f)])


CAMPOS = ['id', 'fecha', 'hora_utc', 'nubes_pct', 'nubes_aoi', 'datos_pct',
          'plataforma', 'tile', 'epsg', 'thumb', 'thumb_url', 'url_visual',
          'assets', 'coleccion', 'catalogo']
EARTH = 'https://earth-search.aws.element84.com/v1'


def _fila(**kw):
    base = {k: '' for k in CAMPOS}
    base.update({'nubes_pct': 10.0, 'nubes_aoi': 5.0, 'datos_pct': 100.0,
                 'epsg': 32616, 'coleccion': 'sentinel-2-l2a',
                 'catalogo': EARTH, 'fecha': '2025-01-03',
                 'hora_utc': '11:31:22', 'id': 'ESC1',
                 'assets': json.dumps({'B04': 'https://x/b04.tif'})})
    base.update(kw)
    return base


def test_procedencia_lee_endpoint_y_coleccion():
    cat, col, n = core._procedencia_huellas(_Fuente(CAMPOS, [_fila()]))
    assert (cat, col, n) == (EARTH, 'sentinel-2-l2a', 1)


def test_procedencia_de_una_capa_anterior_sin_el_campo():
    """Una capa de antes de la 1.31.0 no trae «catalogo»: debe dar None
    para que se avise, no reventar con KeyError."""
    viejos = [c for c in CAMPOS if c != 'catalogo']
    fila = {k: v for k, v in _fila().items() if k != 'catalogo'}
    cat, col, n = core._procedencia_huellas(_Fuente(viejos, [fila]))
    assert cat is None and col == 'sentinel-2-l2a' and n == 1


def test_procedencia_lee_una_sola_entidad():
    """Recorrer la capa entera en la validación bloquea el diálogo."""
    leidas = {'n': 0}

    class Contadora(_Fuente):
        def getFeatures(self):
            def gen():
                for i, f in enumerate(self._f):
                    leidas['n'] += 1
                    yield _Rasgo(f, i)
            return gen()

    core._procedencia_huellas(Contadora(CAMPOS, [_fila() for _ in range(300)]))
    assert leidas['n'] == 1


def test_items_desde_capa_reconstruye_el_item():
    fb = Feedback()
    items = core._items_desde_capa(_Fuente(CAMPOS, [_fila()]), fb)
    assert len(items) == 1
    it = items[0]
    assert it['id'] == 'ESC1'
    assert it['properties']['datetime'] == '2025-01-03T11:31:22Z'
    assert it['assets']['B04']['href'] == 'https://x/b04.tif'
    assert it['_nubes_aoi'] == 5.0


def test_items_desde_capa_omite_las_filas_sin_assets():
    """Sin URL no hay nada que descargar; la fila se omite CON aviso, no
    en silencio."""
    fb = Feedback()
    filas = [_fila(id='BUENA'), _fila(id='SIN', assets=''),
             _fila(id='ROTA', assets='{esto no es json')]
    items = core._items_desde_capa(_Fuente(CAMPOS, filas), fb)
    assert [i['id'] for i in items] == ['BUENA']
    assert len(fb.avisos) == 2, fb.avisos


# =====================================================================
# _mascaras_qa_pixel  (Landsat Collection 2)
# =====================================================================
def test_mascaras_qa_pixel():
    # bit 0 = relleno; los bits nubosos los define QA_BITS_NUBOSOS.
    arr = np.array([[0, 1]], dtype=np.uint16)
    sin_dato, nube = core._mascaras_qa_pixel(arr)
    assert sin_dato[0, 1] and not sin_dato[0, 0]

    for bit in core.QA_BITS_NUBOSOS:
        arr = np.array([[1 << bit]], dtype=np.uint16)
        _, nube = core._mascaras_qa_pixel(arr)
        assert nube[0, 0], f'el bit {bit} debería marcar nube'

    # Un píxel limpio no es ni relleno ni nube.
    libre = 0
    for bit in (0,) + tuple(core.QA_BITS_NUBOSOS):
        libre &= ~(1 << bit)
    sin_dato, nube = core._mascaras_qa_pixel(
        np.array([[libre]], dtype=np.uint16))
    assert not sin_dato[0, 0] and not nube[0, 0]


# =====================================================================
# _acotar_indice
# =====================================================================
def test_acotar_indice_marca_lo_imposible():
    """Un NDVI de 7 no es una medida: es un denominador cerca de cero."""
    fb = Feedback()
    v = np.array([-0.5, 0.0, 0.9, 7.0, -3.0])
    out = core._acotar_indice('NDVI', v.copy(), fb)
    assert np.isnan(out[3]) and np.isnan(out[4])
    assert not np.isnan(out[:3]).any()
    assert fb.avisos, 'tiene que decir cuántos descartó'
    assert '2' in fb.texto()


def test_acotar_indice_no_toca_lo_valido():
    fb = Feedback()
    v = np.array([-1.0, 0.0, 1.0])
    out = core._acotar_indice('NDVI', v.copy(), fb)
    assert np.allclose(out, v)
    assert not fb.avisos


def test_acotar_indice_respeta_el_rango_propio_de_cada_indice():
    """MSI va de 0 a 10, no de -1 a 1: aplicar el rango del NDVI
    descartaría medidas buenas."""
    fb = Feedback()
    out = core._acotar_indice('MSI', np.array([0.5, 3.0, 9.0]), fb)
    assert not np.isnan(out).any(), out


def test_acotar_indice_sufijo_desconocido_no_descarta_nada():
    fb = Feedback()
    v = np.array([-50.0, 0.0, 50.0])
    out = core._acotar_indice('NOEXISTE', v.copy(), fb)
    assert np.allclose(out, v)


# =====================================================================
# _validar_ortonormalidad  (Tasseled Cap)
# =====================================================================
def _ortonormal():
    """Tres vectores ortonormales. Las claves son B, G y W —no TCB/TCG/TCW—
    porque es lo que lee la función; con otras avisa de componente que
    falta, que es su primera comprobación."""
    n = len(core.TC_ORDEN_BANDAS)
    b, g, w = np.zeros(n), np.zeros(n), np.zeros(n)
    b[0] = 1.0
    g[1] = 1.0
    w[2] = 1.0
    return {'B': list(b), 'G': list(g), 'W': list(w)}


def test_validar_ortonormalidad_exige_las_componentes_B_G_W():
    fb = Feedback()
    assert core._validar_ortonormalidad({'TCB': [1.0]}, 'x', fb) is False
    assert any('falta la componente' in a for a in fb.avisos), fb.avisos


def test_validar_ortonormalidad_detecta_longitudes_distintas():
    """Una fila desplazada al transcribir deja un vector más corto."""
    fb = Feedback()
    c = _ortonormal()
    c['G'] = c['G'][:-1]
    assert core._validar_ortonormalidad(c, 'x', fb) is False
    assert any('distinto número' in a for a in fb.avisos), fb.avisos


def test_validar_ortonormalidad_acepta_una_rotacion_valida():
    fb = Feedback()
    assert core._validar_ortonormalidad(_ortonormal(), 'prueba', fb) is True
    assert not fb.avisos, fb.avisos


def test_validar_ortonormalidad_detecta_vectores_no_unitarios():
    fb = Feedback()
    c = _ortonormal()
    c['B'] = [2.0 if i == 0 else 0.0
              for i in range(len(core.TC_ORDEN_BANDAS))]
    core._validar_ortonormalidad(c, 'prueba', fb)
    assert fb.avisos, 'un vector de norma 2 no es una rotación'


def test_validar_ortonormalidad_detecta_vectores_no_perpendiculares():
    fb = Feedback()
    c = _ortonormal()
    c['G'] = list(np.array(c['B']))          # G = B -> producto escalar 1
    core._validar_ortonormalidad(c, 'prueba', fb)
    assert fb.avisos, 'dos ejes paralelos no son una rotación'


# =====================================================================
# _memoria_estimada
# =====================================================================
def test_memoria_estimada_crece_con_el_area():
    chico = core._memoria_estimada([-84.1, 9.9, -84.0, 10.0], [0], False, 1,
                                   's2', False)
    grande = core._memoria_estimada([-86.0, 8.0, -82.0, 11.5], [0], False, 1,
                                    's2', False)
    assert grande[0] > chico[0] * 50, (chico, grande)


def test_memoria_estimada_sin_indices_y_sin_estirar_es_cero():
    """No es un descuido: componer bandas sin escalado va por
    Warp/Translate, que escriben por bloques y no acumulan nada. Imponer
    un techo ahí rechazaría ejecuciones que caben de sobra."""
    gib, ancho, alto = core._memoria_estimada(
        [-86.5, 7.5, -82.0, 11.5], [], False, 1, 's2', False)
    assert gib == 0.0
    assert ancho > 1000 and alto > 1000

    # Con «estirar» sí acumula: _percentiles_vrt lee la banda entera.
    gib_est, _, _ = core._memoria_estimada(
        [-86.5, 7.5, -82.0, 11.5], [], False, 1, 's2', True)
    assert gib_est > 0.0


def test_memoria_estimada_el_aoi_por_omision_es_enorme():
    """La extensión por omisión abarca Costa Rica entera: con índices y
    amplitud pide decenas de GiB. Es el caso que cerraba QGIS sin
    mensaje, así que la estimación tiene que verlo."""
    gib, ancho, alto = core._memoria_estimada(
        [-86.5, 7.5, -82.0, 11.5], [0, 1, 2], True, 12, 's2', False)
    assert gib > 10, gib
    assert ancho > 1000 and alto > 1000


def test_memoria_estimada_landsat_pide_menos_que_sentinel():
    """30 m frente a 10 m: nueve veces menos píxeles para la misma área."""
    bbox = [-85.0, 9.0, -84.0, 10.0]
    s2 = core._memoria_estimada(bbox, [0], False, 1, 's2', False)
    ls = core._memoria_estimada(bbox, [0], False, 1, 'ls', False)
    assert ls[0] < s2[0], (s2, ls)


def test_memoria_estimada_devuelve_numeros_usables():
    gib, ancho, alto = core._memoria_estimada(
        [-85.0, 9.0, -84.0, 10.0], [0], False, 3, 's2', False)
    assert gib > 0 and math.isfinite(gib)
    # Son float a propósito: el llamador los redondea para el mensaje.
    assert ancho >= 1.0 and alto >= 1.0
    assert math.isfinite(ancho) and math.isfinite(alto)


# =====================================================================
# Agrupación y reducción temporal
# =====================================================================
def _item(fecha, nube_aoi=None, nube=10.0, ident=None):
    it = {'id': ident or fecha,
          'properties': {'datetime': f'{fecha}T10:00:00Z',
                         'eo:cloud_cover': nube}}
    if nube_aoi is not None:
        it['_nubes_aoi'] = nube_aoi
    return it


def test_agrupar_por_ano_toma_las_menos_nubosas():
    fb = Feedback()
    cands = [_item('2023-01-01', nube=80), _item('2023-06-01', nube=5),
             _item('2024-02-01', nube=60), _item('2024-07-01', nube=10)]
    grupos = core._agrupar_por_periodo(cands, 'anual', 1, fb)
    assert set(grupos) == {'2023', '2024'}
    assert [i['properties']['eo:cloud_cover']
            for g in grupos.values() for i in g] == [5, 10]


def test_agrupar_por_mes_separa_el_mismo_mes_de_anos_distintos():
    """Agrupar solo por número de mes mezclaría 2023-01 con 2024-01 y la
    serie perdería un año."""
    fb = Feedback()
    cands = [_item('2023-01-05'), _item('2024-01-05')]
    grupos = core._agrupar_por_periodo(cands, 'mes', 2, fb)
    assert len(grupos) == 2, grupos.keys()


def test_reducir_por_periodo_prefiere_la_nube_en_el_aoi():
    """Lo que importa es la nube DENTRO del AOI, no la de la tesela de
    110 km: elegir por la de escena descarta escenas despejadas sobre el
    área."""
    fb = Feedback()
    items = [_item('2023-03-01', nube_aoi=2.0, nube=90.0, ident='BUENA'),
             _item('2023-03-20', nube_aoi=80.0, nube=5.0, ident='MALA')]
    salida = core._reducir_por_periodo(items, 'mes', fb)
    assert [i['id'] for i in salida] == ['BUENA']


def test_reducir_por_periodo_sin_nube_de_aoi_es_determinista():
    """Si NINGUNA escena del periodo trae nube de AOI, todas puntúan 999 y
    se conserva la primera. No cae a la nube de escena: el criterio de
    esta función es la nube sobre el AOI, y mezclar los dos criterios
    daría una selección que depende de qué dato falte. Lo que importa
    aquí es que el resultado sea estable, no arbitrario entre corridas."""
    fb = Feedback()
    items = [_item('2023-03-01', nube=90.0, ident='A'),
             _item('2023-03-20', nube=5.0, ident='B')]
    primera = core._reducir_por_periodo(items, 'mes', fb)
    segunda = core._reducir_por_periodo(items, 'mes', Feedback())
    assert [i['id'] for i in primera] == ['A']
    assert [i['id'] for i in primera] == [i['id'] for i in segunda]
    assert 'n/d' in fb.texto(), 'debe quedar claro que no hubo dato de AOI'


def test_reducir_por_periodo_conserva_la_unica_aunque_no_tenga_aoi():
    fb = Feedback()
    salida = core._reducir_por_periodo(
        [_item('2023-03-01', nube=90.0, ident='SOLA')], 'mes', fb)
    assert [i['id'] for i in salida] == ['SOLA']


def test_periodos_sin_datos_informa_los_huecos():
    fb = Feedback()
    grupos = {'2023': [_item('2023-05-01')]}
    core._periodos_sin_datos(grupos, '2022-01-01', '2024-12-31', 'anual',
                             50, fb)
    texto = fb.texto()
    assert '2022' in texto and '2024' in texto, texto


# =====================================================================
# _res_nativa
# =====================================================================
def test_res_nativa_distingue_sensor():
    assert core._res_nativa('red (B04, 10 m)', 's2') == 10.0
    assert core._res_nativa('red (B04, 10 m)', 'ls') == core.RES_LS
    assert core._res_nativa('clave inventada', 'ls') == core.RES_LS


# =====================================================================
# _escribir_plantilla_tc
# =====================================================================
def test_escribir_plantilla_tc_produce_json_releible(tmp_path):
    """La plantilla tiene que poder leerse con el propio lector: si el
    esquema de ejemplo no encaja, manda al usuario a un error suyo."""
    fb = Feedback()
    ruta = core._escribir_plantilla_tc(str(tmp_path), fb)
    assert ruta and os.path.isfile(ruta)
    with io.open(ruta, encoding='utf-8') as fh:
        datos = json.load(fh)
    assert isinstance(datos, dict) and datos
    for conjunto in datos.values():
        if isinstance(conjunto, dict) and 'TCB' in conjunto:
            assert len(conjunto['TCB']) == len(core.TC_ORDEN_BANDAS)


# =====================================================================
# KML / KMZ
# =====================================================================
def test_alfa_kml_orden_de_canales():
    """El alfa va PRIMERO en KML (aabbggrr), no al final como en CSS. Con
    blanco el error es invisible porque ffffff es simetrico, asi que se
    comprueba la POSICION del alfa, que es lo que importa."""
    assert core.alfa_kml(100) == 'ffffffff'
    assert core.alfa_kml(0) == '00ffffff'
    assert core.alfa_kml(50)[:2] == '80'      # 128 -> 0x80, al PRINCIPIO
    assert core.alfa_kml(50)[2:] == 'ffffff'
    # 8 digitos hexadecimales, siempre.
    for pct in (0, 1, 33, 50, 99, 100):
        v = core.alfa_kml(pct)
        assert len(v) == 8 and int(v, 16) >= 0, (pct, v)


def test_alfa_kml_acota_y_redondea():
    assert core.alfa_kml(-20) == core.alfa_kml(0)
    assert core.alfa_kml(500) == core.alfa_kml(100)
    assert core.alfa_kml(100.0) == 'ffffffff'


KML_OVERLAY = (
    '<?xml version="1.0"?><kml><Document>'
    '<GroundOverlay><name>a</name><Icon><href>t.png</href></Icon>'
    '</GroundOverlay>'
    '<GroundOverlay><name>b</name></GroundOverlay>'
    '</Document></kml>')


def test_insertar_opacidad_toca_cada_overlay():
    salida, n = core.insertar_opacidad_kml(KML_OVERLAY, 50)
    assert n == 2
    assert salida.count('<color>80ffffff</color>') == 2
    # El color debe ir DENTRO del GroundOverlay, pegado a su apertura.
    assert '<GroundOverlay><color>80ffffff</color>' in salida


def test_insertar_opacidad_no_duplica_un_color_existente():
    """Dos <color> en el mismo overlay dejan un KML invalido."""
    ya = ('<kml><GroundOverlay><color>ccffffff</color><name>a</name>'
          '</GroundOverlay></kml>')
    salida, n = core.insertar_opacidad_kml(ya, 50)
    assert n == 0
    assert salida == ya


def test_insertar_opacidad_mezcla_con_y_sin_color():
    mezcla = ('<kml><GroundOverlay><color>ccffffff</color></GroundOverlay>'
              '<GroundOverlay><name>b</name></GroundOverlay></kml>')
    salida, n = core.insertar_opacidad_kml(mezcla, 25)
    assert n == 1, 'solo el que no tenia color'
    assert salida.count('<color>') == 2
    assert 'ccffffff' in salida, 'el color existente se conserva'


def test_insertar_opacidad_sin_overlays_no_cambia_nada():
    sin = '<kml><Document><Placemark><name>x</name></Placemark></Document></kml>'
    salida, n = core.insertar_opacidad_kml(sin, 50)
    assert n == 0 and salida == sin


def test_contar_kml_cuenta_vertices_solo_en_coordinates():
    """Contar todas las comas del documento inflaba la cifra: un KML lleva
    comas en los nombres y las descripciones, y eso disparaba un aviso de
    tope que no correspondia."""
    kml = (
        '<kml><Document>'
        '<Placemark><name>Finca, lote 3</name>'
        '<description>Nubes 5%, datos 100%</description>'
        '<Polygon><outerBoundaryIs><LinearRing><coordinates>'
        '-84.1,9.9,0 -84.0,9.9,0 -84.0,10.0,0 -84.1,9.9,0'
        '</coordinates></LinearRing></outerBoundaryIs></Polygon>'
        '</Placemark>'
        '</Document></kml>')
    c = core.contar_kml(kml)
    assert c['marcadores'] == 1
    assert c['superposiciones'] == 0
    assert c['vertices'] == 4, c


def test_contar_kml_varios_bloques_de_coordenadas():
    kml = ('<kml>'
           '<Placemark><Point><coordinates>-84,9,0</coordinates></Point>'
           '</Placemark>'
           '<Placemark><LineString><coordinates>\n'
           '  -84,9,0\n  -83,10,0\n</coordinates></LineString></Placemark>'
           '</kml>')
    c = core.contar_kml(kml)
    assert c['marcadores'] == 2
    assert c['vertices'] == 3, c


def test_contar_kml_tolera_atributos_en_la_etiqueta():
    kml = ('<kml><Placemark><Point>'
           '<coordinates xmlns="x">-84,9,0 -83,10,0</coordinates>'
           '</Point></Placemark></kml>')
    assert core.contar_kml(kml)['vertices'] == 2


def test_contar_kml_no_revienta_con_etiquetas_truncadas():
    """Un KMZ leido a medias (se limita la lectura a unos MiB) deja la
    ultima etiqueta cortada."""
    for malo in ('<kml><coordinates>-84,9,0',
                 '<kml><coordinates',
                 '<kml></kml>', ''):
        c = core.contar_kml(malo)
        assert c['vertices'] >= 0


def test_topes_web_son_los_publicados_por_google():
    assert core.TOPE_ENTIDADES_WEB == 10000
    assert core.TOPE_VERTICES_WEB == 250000


# =====================================================================
# sin_prefijo_bearer. La ayuda del algoritmo dice que la cabecera vale
# «Bearer <token>», de modo que copiar esa forma completa al archivo de
# token es el error natural -- y daba un 401 cuyo mensaje culpaba al
# token.
# =====================================================================
def test_quita_el_prefijo_bearer():
    assert core.sin_prefijo_bearer('Bearer eyJabc') == 'eyJabc'


def test_el_prefijo_bearer_no_distingue_mayusculas():
    for crudo in ('bearer eyJabc', 'BEARER eyJabc', 'BeArEr eyJabc'):
        assert core.sin_prefijo_bearer(crudo) == 'eyJabc', crudo


def test_un_token_sin_prefijo_queda_igual():
    assert core.sin_prefijo_bearer('eyJabc') == 'eyJabc'


def test_no_muerde_un_token_que_empieza_por_bearer():
    """«bearertoken» no lleva prefijo: sin el espacio no hay que cortar.

    Recortar siete caracteres a ciegas convertiria un token valido en
    basura, y el 401 resultante seria indistinguible del que se quiere
    arreglar.
    """
    assert core.sin_prefijo_bearer('bearertoken123') == 'bearertoken123'
    assert core.sin_prefijo_bearer('Bearereyjabc') == 'Bearereyjabc'


def test_quita_el_prefijo_repetido():
    assert core.sin_prefijo_bearer('Bearer Bearer eyJabc') == 'eyJabc'


def test_tolera_vacio_y_none():
    assert core.sin_prefijo_bearer(None) == ''
    assert core.sin_prefijo_bearer('') == ''
    assert core.sin_prefijo_bearer('   ') == ''
    assert core.sin_prefijo_bearer('Bearer ') == ''


def test_recorta_espacios_y_salto_de_linea_del_archivo():
    """La via del archivo pasaba antes por .strip(); no se pierde eso."""
    assert core.sin_prefijo_bearer('  Bearer eyJabc\n') == 'eyJabc'
    assert core.sin_prefijo_bearer('eyJabc\n') == 'eyJabc'


# =====================================================================
# sin_firma_url. En una URL prefirmada la cadena de consulta ES la
# credencial, y el registro de QGIS se copia y se pega en informes de
# error sin pensarlo.
# =====================================================================
def test_quita_la_firma_de_una_url_prefirmada():
    url = ('https://hyp3-contentbucket.s3.us-west-2.amazonaws.com/x/p.zip'
           '?X-Amz-Algorithm=AWS4-HMAC-SHA256'
           '&X-Amz-Signature=deadbeefcafe1234')
    salida = core.sin_firma_url(url)
    assert 'X-Amz-Signature' not in salida
    assert 'deadbeefcafe1234' not in salida
    # Pero sigue diciendo de donde venia, que es para lo que se registra.
    assert salida.startswith(
        'https://hyp3-contentbucket.s3.us-west-2.amazonaws.com/x/p.zip')
    assert salida.endswith('?<firma oculta>')


def test_una_url_sin_consulta_queda_igual():
    url = 'https://example.org/a/b.zip'
    assert core.sin_firma_url(url) == url


def test_sin_firma_url_tolera_vacio():
    assert core.sin_firma_url(None) == ''
    assert core.sin_firma_url('') == ''


def test_no_deja_pasar_la_firma_cuando_hay_varios_signos():
    """Se corta en el PRIMER «?»: lo de despues es todo consulta."""
    url = 'https://h/x.zip?a=1&b=?raro&X-Amz-Signature=secreto'
    salida = core.sin_firma_url(url)
    assert 'secreto' not in salida
    assert salida == 'https://h/x.zip?<firma oculta>'


# =====================================================================
# ordenar_trazas. El caso real: dos trazas idénticas en todo, y el
# algoritmo presentaba una como «siguiente paso» porque «descending» va
# después de «ascending» en el alfabeto. Son 1860 créditos.
# =====================================================================
def _traza(direccion, traza, n, seca, lluvia, creditos=1860):
    return {'direccion': direccion, 'traza': traza, 'n': n,
            'seca': seca, 'lluvia': lluvia, 'creditos': creditos}


def test_detecta_el_empate_entre_dos_trazas_identicas():
    """El caso del registro: 165 ascending y 157 descending, iguales."""
    filas, empatadas = core.ordenar_trazas([
        _traza('ascending', 165, 31, 13, 18),
        _traza('descending', 157, 31, 13, 18)])
    assert len(filas) == 2
    assert len(empatadas) == 2, (
        'ofrecen lo mismo: hay que decir que empatan en vez de recomendar '
        'una')


def test_el_desempate_no_lo_decide_el_nombre_de_la_direccion():
    """El desempate es por número de traza, no por alfabeto.

    Hay que elegir los valores con cuidado: en el caso del registro
    —ascending 165 contra descending 157— la ordenación vieja y la nueva
    dan las DOS la traza 157, una por alfabeto y la otra por número, así
    que ese caso no distingue nada y no sirve de prueba.

    Con la ascendente de número MENOR el resultado se separa: la vieja
    ordenaba la tupla (débil, dirección, …) con reverse=True, de modo
    que «descending» ganaba y devolvía la 200; la nueva devuelve la 100.
    """
    a = _traza('ascending', 100, 31, 13, 18)
    d = _traza('descending', 200, 31, 13, 18)
    filas, empatadas = core.ordenar_trazas([a, d])
    assert filas[0]['traza'] == 100, (
        'a igualdad manda el número de traza; si sale la 200 es que el '
        'nombre de la dirección sigue decidiendo')
    assert len(empatadas) == 2, 'y siguen siendo un empate'
    # Y el orden de entrada tampoco decide.
    assert core.ordenar_trazas([d, a])[0][0]['traza'] == 100


def test_gana_la_estacion_mas_debil_mas_alta():
    filas, empatadas = core.ordenar_trazas([
        _traza('ascending', 1, 30, 5, 25),      # débil = 5
        _traza('descending', 2, 30, 14, 16)])   # débil = 14
    assert filas[0]['traza'] == 2
    assert len(empatadas) == 1, 'no empatan: una es claramente mejor'


def test_a_igual_estacion_debil_gana_la_de_mas_granulos():
    filas, _ = core.ordenar_trazas([
        _traza('ascending', 1, 26, 13, 13),
        _traza('descending', 2, 31, 13, 18)])
    assert filas[0]['traza'] == 2
    assert filas[0]['n'] == 31


def test_a_igual_estacion_y_granulos_gana_la_mas_barata():
    filas, empatadas = core.ordenar_trazas([
        _traza('ascending', 1, 31, 13, 18, creditos=1860),
        _traza('descending', 2, 31, 13, 18, creditos=155)])
    assert filas[0]['creditos'] == 155
    assert len(empatadas) == 1, 'el coste las distingue, no empatan'


def test_ordenar_trazas_con_lista_vacia():
    assert core.ordenar_trazas([]) == ([], [])


def test_tres_empatadas_se_reportan_las_tres():
    filas, empatadas = core.ordenar_trazas([
        _traza('ascending', 10, 31, 13, 18),
        _traza('descending', 20, 31, 13, 18),
        _traza('ascending', 30, 31, 13, 18),
        _traza('descending', 40, 20, 4, 16)])
    assert len(filas) == 4
    assert [c['traza'] for c in empatadas] == [10, 20, 30]


# =====================================================================
# opciones_asequibles. El caso real: HyP3 rechazó un lote de 1860
# créditos con un HTTP 400 porque quedaban 1630, después de que el
# inventario lo hubiera recomendado dando la asignación mensual (8000)
# por saldo.
# =====================================================================
def test_los_precios_rtc_son_los_publicados_por_asf():
    """5, 15 y 60 créditos a 30, 20 y 10 m. De esto depende el gasto."""
    assert core.CREDITOS_RTC == {30: 5, 20: 15, 10: 60}


def test_con_1630_creditos_no_cabe_10m_pero_si_20m():
    """El caso del registro: 31 gránulos, saldo de 1630."""
    opciones = core.opciones_asequibles(31, 1630)
    assert [e for e, _c in opciones] == [20, 30], (
        f'31 x 60 = 1860 no cabe en 1630; 20 y 30 m si: {opciones}')
    assert dict(opciones) == {20: 465, 30: 155}


def test_con_saldo_de_sobra_caben_las_tres():
    opciones = core.opciones_asequibles(31, 8000)
    assert sorted(e for e, _c in opciones) == [10, 20, 30]
    assert dict(opciones)[10] == 1860


def test_sin_saldo_para_nada_devuelve_lista_vacia():
    assert core.opciones_asequibles(31, 100) == []


def test_el_limite_es_inclusivo():
    """Un pedido que cuesta exactamente el saldo SI cabe.

    HyP3 rechaza cuando el costo EXCEDE el saldo, no cuando lo iguala;
    dejar fuera el caso exacto esconderia una opcion valida.
    """
    assert (30, 155) in core.opciones_asequibles(31, 155)
    assert core.opciones_asequibles(31, 154) == []


def test_saldo_desconocido_no_inventa_opciones():
    """Sin saldo legible no se puede afirmar que algo quepa."""
    assert core.opciones_asequibles(31, None) == []


# =====================================================================
# lista_con_zip. La causa real del fallo de la recogida: QGIS traía
# CPL_VSIL_CURL_ALLOWED_EXTENSIONS=.tif,.TIF,.tiff,.jp2, de modo que
# /vsicurl/ se negaba a abrir los .zip de HyP3 sin pedir nada al
# servidor y sin dar error. Los COG .tif seguían funcionando, que es
# por lo que solo fallaba este algoritmo.
# =====================================================================
def test_anade_zip_a_la_lista_real_de_qgis():
    assert core.lista_con_zip('.tif,.TIF,.tiff,.jp2') == \
        '.tif,.TIF,.tiff,.jp2,.zip'


def test_no_toca_nada_si_la_lista_esta_vacia():
    """Vacía o sin definir ya significa «todo permitido»."""
    assert core.lista_con_zip(None) is None
    assert core.lista_con_zip('') is None
    assert core.lista_con_zip('   ') is None


def test_no_duplica_zip_si_ya_estaba():
    assert core.lista_con_zip('.tif,.zip') is None
    assert core.lista_con_zip('.ZIP,.tif') is None, 'sin distinguir mayúsculas'


def test_conserva_la_restriccion_del_usuario():
    """Se AÑADE, no se vacía: quitar la lista abriría /vsicurl/ a todo."""
    salida = core.lista_con_zip('.tif,.jp2')
    assert salida.startswith('.tif,.jp2'), salida
    assert salida.endswith(',.zip'), salida


def test_tolera_espacios_sueltos():
    assert core.lista_con_zip(' .tif , .jp2 ') == '.tif,.jp2,.zip'


# =====================================================================
# informe_html. Un informe de auditoría tiene que poder abrirse dentro
# de diez años y en una máquina sin red: sin CDN, sin fuentes remotas y
# sin plotly.
# =====================================================================
def _datos_minimos(**extra):
    base = {'titulo': 'Lote de prueba', 'generado': '2026-10-06T00:00:00Z',
            'generado_por': 'BuscadorSTAC v1.0.0'}
    base.update(extra)
    return base


def test_el_informe_no_trae_recursos_remotos():
    h = core.informe_html(_datos_minimos(
        lote=[('Traza', '157 descending')]))
    for patron in ('http://', 'https://', '<script', 'cdn'):
        assert patron not in h.lower(), patron


def test_el_informe_escapa_el_html_de_los_datos():
    """Un nombre de archivo con «<» no puede inyectar etiquetas."""
    h = core.informe_html(_datos_minimos(
        lote=[('Archivo', '<img src=x onerror=alert(1)>')]))
    assert '<img' not in h
    assert '&lt;img' in h


def test_el_informe_escapa_tambien_el_titulo():
    h = core.informe_html({'titulo': '<script>alert(1)</script>'})
    assert '<script>alert(1)' not in h
    assert '&lt;script&gt;' in h


def test_las_secciones_vacias_no_se_dibujan():
    """Una tabla sin filas no debe dejar un encabezado huérfano."""
    h = core.informe_html(_datos_minimos())
    assert 'Productos escritos' not in h
    assert 'Escenas usadas' not in h


def test_el_informe_incluye_los_avisos():
    h = core.informe_html(_datos_minimos(
        avisos=['CPL_VSIL_CURL_ALLOWED_EXTENSIONS no permitia .zip']))
    assert 'CPL_VSIL_CURL_ALLOWED_EXTENSIONS' in h
    assert 'class="aviso"' in h


def test_el_informe_lleva_los_valores_que_se_le_pasan():
    h = core.informe_html(_datos_minimos(
        productos=[['amplitud VH', 'AMPL.tif', '-0.03', '+0.13', '-0.001',
                    '100 %']],
        escenas=[['seca', 13, '2025-01-03']]))
    for v in ('amplitud VH', 'AMPL.tif', '+0.13', 'seca', '2025-01-03'):
        assert v in h, v


def test_el_informe_es_html_completo():
    h = core.informe_html(_datos_minimos())
    assert h.startswith('<!DOCTYPE html>')
    assert h.rstrip().endswith('</html>')
    assert 'lang="es"' in h


# =====================================================================
# fecha_kml. Google Earth solo pone en su línea de tiempo lo que venga
# en ISO 8601; cualquier otra forma la ignora SIN avisar, que es el peor
# fallo posible: el KMZ abre, las entidades se ven, y la línea de tiempo
# no aparece sin que nada lo explique.
# =====================================================================
def test_acepta_la_fecha_de_la_capa_de_huellas():
    """El campo «fecha» guarda los diez primeros del datetime STAC."""
    assert core.fecha_kml('2025-01-03') == '2025-01-03'


def test_acepta_iso_con_hora_y_zona():
    assert core.fecha_kml('2025-01-03T11:31:17Z') == '2025-01-03T11:31:17Z'
    assert (core.fecha_kml('2025-01-03T11:31:17+02:00')
            == '2025-01-03T11:31:17+02:00')


def test_el_espacio_de_qgis_se_vuelve_T():
    """QGIS muestra «2025-01-03 11:31:17»; KML exige la T."""
    assert core.fecha_kml('2025-01-03 11:31:17') == '2025-01-03T11:31:17'


def test_no_inventa_zona_horaria():
    """KML da por supuesto UTC si no hay zona; añadir «Z» afirmaría algo
    que el dato no dice."""
    assert not core.fecha_kml('2025-01-03 11:31:17').endswith('Z')


def test_descarta_los_fraccionarios_de_segundo():
    assert core.fecha_kml('2025-01-03T11:31:17.123456Z') == \
        '2025-01-03T11:31:17Z'


def test_rechaza_una_fecha_que_no_existe():
    """El patrón acepta 2025-02-30; el calendario no."""
    assert core.fecha_kml('2025-02-30') is None
    assert core.fecha_kml('2025-13-01') is None


def test_rechaza_el_formato_ambiguo():
    """03/01/2025 puede ser 3 de enero o 1 de marzo.

    Adivinar desplazaría la serie entera, y en silencio: es mejor no
    poner <TimeStamp> en esa entidad y decir cuántas quedaron fuera.
    """
    assert core.fecha_kml('03/01/2025') is None


def test_acepta_la_barra_cuando_el_orden_es_inequivoco():
    """2025/01/03 empieza por el año: no hay nada que adivinar."""
    assert core.fecha_kml('2025/01/03') == '2025-01-03'


def test_rechaza_horas_imposibles():
    assert core.fecha_kml('2025-01-03T25:00:00') is None
    assert core.fecha_kml('2025-01-03T10:75:00') is None


def test_acepta_objetos_date_y_datetime():
    import datetime as dt
    assert core.fecha_kml(dt.date(2025, 1, 3)) == '2025-01-03'
    assert (core.fecha_kml(dt.datetime(2025, 1, 3, 11, 31, 17))
            == '2025-01-03T11:31:17')


def test_vacio_y_basura_dan_none():
    for v in (None, '', '   ', 'hola', 'NULL', 0):
        assert core.fecha_kml(v) is None, repr(v)


# --------------------------------------------------------------------------
# El KMZ de LIBKML: href y aplanado
# --------------------------------------------------------------------------
def test_href_ascii_limpio_es_seguro():
    assert core.href_inseguro('layers/Buferes.kml') == ''
    assert core.href_inseguro('layers/Bufer-Oval_357.kml') == ''


def test_href_senala_las_tildes():
    """Lo medido con GDAL 3.8.4: la tilde es lo que rompe el enlace."""
    assert core.href_inseguro('layers/Búfer.kml') == 'ú'
    assert core.href_inseguro('año.kml') == 'ñ'


def test_href_no_repite_un_caracter_ni_pierde_el_orden():
    assert core.href_inseguro('a b[c]d eú') == ' []ú'


def test_href_vacio_o_nulo_no_estalla():
    assert core.href_inseguro(None) == ''
    assert core.href_inseguro('') == ''


def _doc_con_enlace(href):
    return ('<kml><Document id="root_doc"><NetworkLink><Link>'
            '<href>%s</href></Link></NetworkLink></Document></kml>' % href)


def test_promueve_la_unica_capa_de_un_kmz_de_libkml():
    nombres = ['doc.kml', 'layers/', 'layers/Búfer [Unión].kml']
    doc = _doc_con_enlace('layers/Búfer [Unión].kml')
    assert (core.capa_a_promover(nombres, doc)
            == 'layers/Búfer [Unión].kml')


def test_no_aplana_si_doc_kml_ya_tiene_contenido():
    """El camino del raster: KMLSUPEROVERLAY ya escribe el doc completo."""
    nombres = ['doc.kml', 'files/0/0/0.png']
    doc = '<kml><Document><GroundOverlay><Icon/></GroundOverlay></Document></kml>'
    assert core.capa_a_promover(nombres, doc) is None
    doc2 = '<kml><Document><Placemark/></Document></kml>'
    assert core.capa_a_promover(nombres, doc2) is None


def test_no_aplana_con_varias_capas():
    """Con dos capas el NetworkLink hace falta: aplanar perderia una."""
    nombres = ['doc.kml', 'layers/', 'layers/a.kml', 'layers/b.kml']
    assert core.capa_a_promover(nombres, _doc_con_enlace('layers/a.kml')) is None


def test_no_aplana_si_hay_recursos_con_ruta_relativa():
    """Un icono referido desde layers/ se quedaria sin resolver."""
    nombres = ['doc.kml', 'layers/', 'layers/a.kml', 'images/icono.png']
    assert core.capa_a_promover(nombres, _doc_con_enlace('layers/a.kml')) is None


def test_no_aplana_lo_que_no_es_un_kmz_de_libkml():
    assert core.capa_a_promover([], '') is None
    assert core.capa_a_promover(['layers/a.kml'], '') is None
    assert core.capa_a_promover(['doc.kml'], _doc_con_enlace('x')) is None


def test_no_aplana_un_superoverlay_de_una_sola_tesela():
    """Medido con GDAL 3.8.4 sobre un raster de 32x32 px.

    KMLSUPEROVERLAY produce ahi doc.kml con SOLO un <NetworkLink> y un
    unico 0/0/0.kml — la misma forma que LIBKML. Lo unico que lo
    distingue son los PNG y la ruta, que no empieza por «layers/».
    """
    nombres = ['doc.kml', '0/0/0.kml', '0/0/0.png', '0.png', 'tmp.png']
    doc = _doc_con_enlace('0/0/0.kml')
    assert core.capa_a_promover(nombres, doc) is None
    # Y aunque alguien quitara los PNG, la ruta sigue delatandolo.
    assert core.capa_a_promover(['doc.kml', '0/0/0.kml'], doc) is None


# --------------------------------------------------------------------------
# Resolucion en el terreno y extensiones sospechosas
# --------------------------------------------------------------------------
def test_metros_por_pixel_del_caso_real():
    """El basemap WMTS de EOX: mundo entero en 2048 px.

    Es el KMZ que «no cargo la imagen»: 360 grados de longitud repartidos
    entre 2048 pixeles. El numero tiene que delatarlo.
    """
    mx, my = core.metros_por_pixel(-180.0, -85.051129, 180.0, 85.051129,
                                   2048, 968)
    assert 19000 < mx < 20000, mx       # ~19.6 km por pixel en el ecuador
    assert 19000 < my < 20000, my


def test_metros_por_pixel_de_un_area_de_trabajo():
    """Turrialba, 0.1 grados de lado en 2048 px: unos 5 m por pixel."""
    mx, my = core.metros_por_pixel(-83.73, 9.85, -83.63, 9.95, 2048, 2048)
    assert 4 < mx < 6, mx
    assert 5 < my < 6, my


def test_metros_por_pixel_corrige_por_la_latitud():
    """Un grado de longitud mide menos lejos del ecuador."""
    ecuador, _ = core.metros_por_pixel(0, 0, 1, 1, 100, 100)
    norte, _ = core.metros_por_pixel(0, 59.5, 1, 60.5, 100, 100)
    assert norte < ecuador / 1.9, (norte, ecuador)


def test_metros_por_pixel_rechaza_lo_degenerado():
    assert core.metros_por_pixel(0, 0, 0, 1, 10, 10) == (None, None)
    assert core.metros_por_pixel(0, 0, 1, 1, 0, 10) == (None, None)
    assert core.metros_por_pixel(0, 0, 1, 1, 10, -3) == (None, None)
    assert core.metros_por_pixel(0, 0, 1, 1, 'x', 10) == (None, None)


def test_extension_sospechosa_separa_el_mundo_de_un_area():
    assert core.extension_sospechosa(-180, -85, 180, 85)      # mundo
    assert core.extension_sospechosa(-86, 8, -82, 24)         # 16 grados
    assert not core.extension_sospechosa(-83.73, 9.85, -83.63, 9.95)
    assert not core.extension_sospechosa(-86, 8, -82, 12)     # 4 grados
    assert not core.extension_sospechosa(None, 0, 1, 1)
