# BuscadorSTAC

![version](https://img.shields.io/badge/version-1.2.1-blue)
![QGIS](https://img.shields.io/badge/QGIS-3.28%20–%204.x-green)
![licencia](https://img.shields.io/badge/licencia-GPL%20v2%2B-blue)

Complemento de QGIS para imágenes satelitales por STAC/COG: busca y descarga
escenas **Sentinel-2** y **Landsat**, trae radar **Sentinel-1 RTC** a través de
ASF HyP3, recupera el archivo histórico de **Esri World Imagery** y exporta
cualquier capa a **Google Earth (KMZ)**.

Pensado para trabajo de inventario forestal y agronómico donde la nube es el
problema principal: la nubosidad se mide **dentro del área de interés**, no en
la tesela de 110 km, y hay un flujo de dos pasos para revisar escenas antes de
transferir datos.

> Los valores por omisión están ajustados a Costa Rica (extensión inicial,
> marco de estación seca y lluviosa). El complemento funciona en cualquier
> parte del mundo; solo cambie la extensión o indique una capa de AOI.

---

## Instalación

**Desde el repositorio de complementos de QGIS** (cuando esté publicado):
Complementos → Administrar e instalar complementos → buscar «Buscador STAC».

**Desde el ZIP:** descargue `BuscadorSTAC-<versión>.zip` de la pestaña
[Releases](https://github.com/jfallas56-CR/BuscadorSTAC/releases) y use
Complementos → Administrar e instalar complementos → Instalar a partir de ZIP.

> **Si viene de `BuscarSentinel2CR` o `BuscadorSTAC_CR`, desinstale primero esa
> versión.** QGIS identifica un complemento por el nombre de su carpeta, así que
> las dos convivirían y vería cada algoritmo duplicado en la Caja de
> Herramientas.

Requisitos: QGIS 3.28 o posterior, incluido QGIS 4. No hace falta instalar nada
en el Python de QGIS — el complemento usa solo la biblioteca estándar, GDAL y
numpy, que ya vienen incluidos. Esto es deliberado: pedirle a un usuario que
haga `pip install` dentro de QGIS puede romperle otros complementos.

---

## Los cuatro algoritmos

Aparecen en la Caja de Herramientas de Processing, bajo **Teledetección**, y
también en el menú Complementos → Buscador STAC.

| Algoritmo | Qué hace |
|---|---|
| **Buscar y descargar Sentinel-2 / Landsat (STAC / COG)** | Consulta catálogos STAC públicos (Element84 Earth Search, Microsoft Planetary Computer). Capa de huellas con metadatos, hoja de contactos HTML, nubosidad medida dentro del AOI con SCL o QA_PIXEL, carga remota por `/vsicurl/` o recorte a disco, ocho composiciones RGB, diez índices espectrales y amplitud fenológica estacional. |
| **Esri World Imagery / Wayback** | Imágenes de alta resolución, actuales e históricas (archivo Wayback desde 2014), recortadas al AOI. Detecta qué versiones cambian de verdad sobre el área y da la fecha de captura real, no la de publicación del mosaico. |
| **Sentinel-1 RTC (ASF HyP3)** | Radar en banda C corregido por terreno, en tres modos: inventario por traza sin gastar créditos, pedido con confirmación explícita de gasto y comprobación del saldo real, y colecta de datos con amplitud estacional de retrodispersión. Escribe la amplitud, las dos medianas por estación, el recuento por píxel y un informe HTML de auditoría; puede reproyectar los productos finales al SRC que se indique. |
| **Exportar a Google Earth (KMZ)** | Convierte la capa elegida a KMZ y la abre en Google Earth de escritorio. Un ráster se renderiza antes con la simbología que usted ve en QGIS, sobre el área que se indique —necesario con un mapa base remoto, que declara extensión mundial—; a una capa vectorial se le puede indicar un campo de fecha y sale con línea de tiempo. |

El detalle de cada parámetro está en el panel de ayuda del propio diálogo, y la
documentación de usuario completa en
[`BuscadorSTAC/README.md`](BuscadorSTAC/README.md).

---

## Estructura del repositorio

```
BuscadorSTAC/            el complemento — es lo único que va en el ZIP
├── core.py              lógica pura: NI QGIS NI Qt (ver más abajo)
├── buscar_sentinel2_algoritmo.py
├── esri_wayback_algoritmo.py
├── sentinel1_hyp3_algoritmo.py
├── google_earth_algoritmo.py
├── buscar_sentinel2_provider.py
├── buscar_sentinel2_plugin.py
└── metadata.txt
tools/                   herramientas de desarrollo, NO se empaquetan
├── preflight.py         las comprobaciones que bloquean en el portal, en local
├── api_audit.py         cada símbolo de QGIS que usa el complemento
├── smoke_test.py        arranca el complemento en un QGIS de verdad
├── kmz_test.py          exporta un KMZ y verifica imagen y línea de tiempo
└── vsi_zip_test.py      lee un ZIP remoto por /vsizip/{/vsicurl/…}
tests/                   pytest, sin QGIS
.github/workflows/ci.yml
requirements-dev.txt
```

### `core.py` no importa QGIS, y es a propósito

Es la única regla de ese módulo. Ahí viven las fórmulas de los índices, el
parseo, la agrupación temporal, las estimaciones de memoria, las validaciones
de ortonormalidad de Tasseled Cap y la aritmética de KML. Nada que construya
una capa, un sumidero o un diálogo.

La consecuencia práctica es que esa lógica se prueba con `pytest` a secas, en
menos de un segundo, sin QGIS instalado y **sin imitarlo con clases falsas**.
Un QGIS imitado aporta sus propios errores: un fallo del imitador se confunde
con un fallo del código, y eso ya costó un ciclo de depuración sobre código que
estaba bien.

Si una función nueva necesita QGIS, su sitio es el módulo del algoritmo. Hay
una prueba que falla si alguien añade `from qgis.core import …` a `core.py`, y
dice por qué.

---

## Desarrollo

```bash
python -m pip install -r requirements-dev.txt

python -m pytest tests -v          # lógica pura, sin QGIS
python tools/preflight.py          # todo lo que el portal comprueba
```

### `tools/preflight.py`

Corre en local las comprobaciones que bloquean una subida a
plugins.qgis.org, para fallar aquí en vez de en el formulario. Sale con código
distinto de cero si algo bloquea, de modo que sirve de puerta en CI.

Entre otras cosas: un parámetro de campo de Processing colgado de una capa que
puede ser ráster, que **cierra QGIS** con una violación de acceso al abrir el
diálogo —un puntero nulo sin comprobar en QGIS, presente en `release-3_44` y en
`master`—, y que ninguna otra herramienta de aquí puede ver porque el archivo
compila y el smoke test registra los algoritmos sin abrir sus diálogos; el
escaneo de Bandit que corre el portal; un `%` sin escapar
en `metadata.txt`, que hace que `configparser` lea mal ese campo **sin lanzar
ninguna excepción**; credenciales pegadas en el código y rutas de la máquina de
desarrollo en lo que se entrega; la coherencia de la versión en todos los
sitios donde aparece; que cada constante `ALGORITMO*_ID` del menú corresponda a
un algoritmo que el proveedor registre de verdad; y la extracción del ZIP para
importarlo como lo hará QGIS.

Tiene sus propias pruebas en `tests/test_preflight.py`, que **rompen a
propósito** una copia del complemento defecto por defecto y exigen que
preflight lo bloquee. Una herramienta de verificación que nunca ha fallado no
está probada: da seguridad falsa, que es peor que no tenerla.

`tools/kmz_test.py` aplica la misma idea a su manera: después de comprobar que
el KMZ con un nombre de capa en español se puede releer, escribe el mismo caso
**sin** el arreglo y verifica que así no se pueda. Si algún día GDAL corrige el
bit 11 del ZIP, ese control lo dirá —como nota, no como fallo: romper CI porque
una dependencia mejoró no tendría sentido.

### `tools/api_audit.py`

Recoge con AST cada símbolo de QGIS, Qt y GDAL que usa el complemento y
comprueba que exista en el QGIS que lo ejecuta. Es la pregunta que importa
cuando sale una versión nueva, porque la migración de enums a Qt6 no fue
uniforme: en una misma versión de QGIS conviven formas planas y anidadas, y un
enum que se movió falla **en silencio** dentro de un post-procesador — la capa
se carga sin estilo y solo se nota leyendo el registro completo. Cuando la
forma anidada falta pero existe la plana, la herramienta lo dice, que es la
información que hace falta para arreglarlo.

```bash
python3 tools/api_audit.py --listar     # qué se comprobaría (no necesita QGIS)
python3 tools/api_audit.py              # auditar contra el QGIS que corre
```

En Windows, con OSGeo4W: `C:\OSGeo4W\bin\python-qgis-ltr.bat tools\api_audit.py`

### Integración continua

`.github/workflows/ci.yml` corre cuatro trabajos:

- **pruebas** — `pytest` y `preflight` sobre Python 3.9 (la que trae QGIS 3.28,
  la versión mínima declarada) y 3.12.
- **estilo** — flake8, Bandit con todas las severidades, cero `except:`
  desnudos y ningún `# nosec` sin su código `B###`.
- **qgis** — el complemento **dentro de las imágenes oficiales `qgis/qgis`**:
  3.28 fijada, LTR y estable actuales, y `latest`, que sigue a master para que
  una QGIS nueva se rompa aquí antes de romperse para el usuario. Audita la
  API, arranca el complemento con `xvfb-run`, lee un ZIP remoto de verdad por
  `/vsizip/{/vsicurl/…}` y exporta un ráster y un vector a KMZ comprobando que
  la imagen tenga contenido, que la línea de tiempo salga escrita y que el KMZ
  se pueda releer con `ogr.Open` cuando la capa se llama en español.
- **paquete** — construye el ZIP y lo sube como artefacto de la ejecución.

---

## Cómo se verifica

Un algoritmo de QGIS no se puede probar bien pidiéndole al usuario que lo
ejecute y reporte. Lo que hay aquí:

| Qué | Dónde | Necesita QGIS |
|---|---|---|
| Lógica pura (fórmulas, parseo, KML, estimaciones) | `tests/test_core.py` | no |
| Que preflight detecte los defectos | `tests/test_preflight.py` | no |
| Ciclo de Processing, parámetros, validación | QGIS imitado, fuera del repo | no |
| Que la ruta `/vsizip/{/vsicurl/…}` lea un ZIP remoto | `tools/vsi_zip_test.py` | no (solo GDAL) |
| Que la API siga existiendo | `tools/api_audit.py` | sí |
| Que el complemento cargue y registre | `tools/smoke_test.py` | sí |
| Que la exportación a KMZ produzca imagen, línea de tiempo y un archivo legible con tildes en el nombre de la capa | `tools/kmz_test.py` | sí |

Los tres últimos son los que no se pueden responder sin QGIS, y para eso existe
el trabajo `qgis` de CI.

**Lo que esto todavía no cubre, dicho claro:** nada de aquí abre el diálogo de
un algoritmo. El smoke test registra los algoritmos y comprueba sus parámetros,
pero construir el diálogo necesita `iface`, que solo existe en el QGIS de
escritorio. Esa es exactamente la grieta por la que pasó el fallo de la versión
1.0.2 —un parámetro mal emparentado que cierra QGIS en `postInitialize`— y por
eso ese caso concreto se comprueba de forma estática en preflight, con una
prueba que lo rompe a propósito y otra que verifica que el caso bueno no
bloquea. Un diálogo que se abre de verdad sigue siendo cosa de probarlo en
QGIS.

`vsi_zip_test.py` levanta un servidor HTTP local y sirve un ZIP de verdad —
incluido uno con más de 65 535 miembros, que fuerza el formato ZIP64, y uno
servido por un servidor que rechaza `HEAD`. Es la prueba que faltaba cuando la
colecta de radar devolvía 31 ZIP «abiertos pero vacíos»: la causa no estaba en
el ZIP ni en la URL firmada, sino en `CPL_VSIL_CURL_ALLOWED_EXTENSIONS`, una
lista blanca de GDAL que, sin `.zip`, hace que `VSIFOpenL` devuelva nulo **sin
emitir ninguna petición HTTP y sin error**.

---

## Datos y atribución

- **Copernicus Sentinel-1 y Sentinel-2** (ESA) y **Landsat Collection 2**
  (USGS/NASA): acceso abierto. Cite la fuente en cualquier producto derivado.
- **Productos RTC** generados por **ASF DAAC HyP3** con software GAMMA.
  Requiere una cuenta gratuita de [Earthdata Login](https://urs.earthdata.nasa.gov/).
- **Esri World Imagery NO es dato abierto.** Se rige por el Esri Master License
  Agreement. El complemento avisa de ello en cada ejecución; revise sus
  términos antes de usar esas imágenes en un producto.

---

## Historial

El historial de usuario está en el campo `changelog` de
[`metadata.txt`](BuscadorSTAC/metadata.txt) y en la tabla del
[README del complemento](BuscadorSTAC/README.md).

[CHANGELOG.md](CHANGELOG.md) recoge el desarrollo **previo** a la primera
publicación: 27 iteraciones internas, ninguna publicada. Vale la pena leerlo
si va a tocar el código — varias entradas describen fallos silenciosos, de los
que dan un resultado plausible y equivocado, y dicen cómo se detectaron.

## Licencia

GPL **v2 o posterior** (SPDX: `GPL-2.0-or-later`).

[LICENSE](LICENSE) es el texto de la GPL versión 2; el «o posterior» lo
da la nota de licencia de cada archivo fuente, que es donde vive la
concesión. Quien lo reciba puede acogerse a la v2 o a cualquier versión
posterior, a su elección. El archivo está repetido dentro del paquete
—[BuscadorSTAC/LICENSE](BuscadorSTAC/LICENSE)— porque el ZIP que se
sube al portal tiene que llevar el suyo.

## Autor

Jorge Fallas — <jfallas56@gmail.com>

Especialista en SIG e inventario forestal, Costa Rica.

## Problemas y sugerencias

[Issues](https://github.com/jfallas56-CR/BuscadorSTAC/issues). Para un fallo en
QGIS, incluya la **primera línea del panel de ayuda** del algoritmo, que trae
el número de versión, y el registro completo de Processing: ahí está la
diferencia entre un fallo de cálculo y uno de carga.
