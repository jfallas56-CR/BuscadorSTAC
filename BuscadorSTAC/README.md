# Buscador STAC: Sentinel, Landsat y Esri Wayback

![version](https://img.shields.io/badge/version-1.0.2-blue)
![QGIS](https://img.shields.io/badge/QGIS-%E2%89%A5%203.28-green)
![licencia](https://img.shields.io/badge/licencia-GPL%20v2%2B-orange)

Complemento de QGIS para buscar, previsualizar, descargar y analizar escenas
**Sentinel-2 L2A** y **Landsat Collection 2 Nivel 2** desde catálogos STAC
públicos, leyendo los datos como COG sobre `/vsicurl/`.

Pensado para un problema concreto: **separar dosel leñoso de vegetación
herbácea y arbustiva** en un paisaje estacional, con la nubosidad alta del
trópico húmedo como condición de trabajo, no como excepción.

Autor: **Jorge Fallas** <jfallas56@gmail.com>

---

## Qué hace

**Búsqueda y revisión previa**

- Consulta paginada a catálogos STAC públicos, con filtro de nubosidad y cinco
  estrategias de selección temporal (más recientes, más antiguas, mejor escena
  por año, mejor escena por mes).
- Capa vectorial de huellas con identificador, fecha, plataforma, malla MGRS,
  nubosidad, URL de cada escena y el catálogo de procedencia, con consejo
  emergente que muestra la vista previa recortada a su AOI. Es la capa que
  cierra el flujo de dos pasos: se depura a mano y se reinyecta para descargar
  solo lo elegido.
- Hoja de contactos HTML que se abre en el navegador: todas las escenas
  ordenadas por nubosidad **dentro del AOI**, para elegir antes de descargar.

**Nubosidad medida donde importa**

La nubosidad que publica el catálogo (`eo:cloud_cover`) se mide sobre la tesela
completa de 110 km. Un lote de 150 ha puede estar despejado dentro de una
escena declarada al 70 %, y nublado dentro de una declarada al 10 %. El
complemento recalcula la nubosidad **sobre el AOI** a partir de la banda SCL
(Sentinel-2) o de los bits de `QA_PIXEL` (Landsat), y esa es la cifra que
ordena la revisión.

**Descarga y composiciones**

- Modo remoto: carga los assets como capas `/vsicurl/` acotadas al AOI, sin
  ocupar disco.
- Modo descarga: GeoTIFF comprimido recortado al AOI, en el SRC nativo de la
  escena.
- Ocho composiciones RGB (color natural, infrarrojo color, agricultura,
  vegetación sana, análisis de vegetación, SWIR urbano, geología, borde rojo).

**Índices y fenología**

- Diez índices: NDVI, SAVI, NDMI, NBR, MSI, NDRE, CIre y Tasseled Cap
  (brightness, greenness, wetness), con coeficientes propios de cada sensor.
- **Amplitud fenológica**: mediana de estación seca menos mediana de estación
  lluviosa, por píxel. Es el producto más discriminante entre copa leñosa y
  herbácea, porque mide la capacidad de sostener agua durante el año en lugar
  del vigor de un día concreto.
- Recuento de observaciones válidas por píxel y estación (capa `_NOBS`), con
  umbral mínimo configurable: un píxel sustentado en una sola observación no
  entra en el resultado.

**Radiometría correcta**

Los índices se calculan sobre reflectancia, no sobre DN. La escala y el
desplazamiento se leen de `raster:bands` del propio catálogo, en vez de
inferirlos de la línea base de procesamiento. Esto incluye el desplazamiento
BOA de −1000 de la línea base 04.00 de Sentinel-2 y el término aditivo de
Landsat Collection 2 (`DN × 0.0000275 − 0.2`), que **no se cancelan** en un
cociente normalizado.

**Simbología comparable entre fechas**

Media ± N·σ en vez de corte acumulado (que colapsa cuando el recorte tiene
muchos NaN de nube), límites fijos declarables para que dos fechas compartan
escala, y paletas divergentes ancladas en cero para la amplitud. La
clasificación por cuantiles se evita deliberadamente: reasigna los colores en
cada capa y hace que dos fechas distintas se vean iguales.

---

## Radar: Sentinel-1 RTC vía ASF HyP3

El tercer algoritmo existe por una razón concreta: **la mitad débil de una
amplitud NDMI es la mediana de estación lluviosa.** Se construye con pocas
escenas y contaminadas por nube — es la razón de ser del umbral de
observaciones mínimas. El radar no ve nubes, de modo que esa mediana se
sustenta en todas las adquisiciones de la estación. El conteo de escenas
óptico es bruto; el de radar es neto.

**Lo que el radar no resuelve.** La retrodispersión en banda C responde con
fuerza a la **humedad del suelo**: un potrero desnudo también presenta
amplitud estacional grande, sin vegetación de por medio. No es un
discriminante limpio de leñosas, es uno **confundido de otra manera**. El
valor está en cruzarlo con la amplitud óptica y, si hace falta, con HV en
banda L: los tres fallan por motivos distintos.

**No se mezclan trazas.** Cada órbita relativa observa con otro ángulo de
incidencia y otra dirección de mirada, así que el retrodispersado del mismo
suelo cambia entre trazas por geometría y no por vegetación. Una amplitud
sobre trazas mezcladas mide, en buena parte, el cambio de geometría. El modo
de inventario obliga a elegir una traza antes de pedir nada, y **la traza va
en el nombre del archivo** (`AMPL_S1_2025_T157D_VH_…`) para que un apilado
mezclado se vea en el panel de capas en lugar de descubrirse en los
resultados.

**Tres modos, en orden.**

1. **Inventario** — qué trazas cubren el AOI, cuántas adquisiciones tiene
   cada una por estación, y qué costaría pedirlas. No consume créditos y no
   necesita cuenta. Las trazas se ordenan por la estación *más débil*, que es
   la que limita una diferencia de medianas.
2. **Pedido** — envía los trabajos RTC y escribe un manifiesto JSON. Exige
   marcar una casilla de confirmación y respeta un tope de créditos
   comprobado en el diálogo. Un envío fallido **no se reintenta**: si la
   petición llegó al servidor, repetirla gastaría los créditos dos veces.
2. **Pedido** — además comprueba el **saldo real** de créditos en HyP3
   antes de enviar, no la asignación mensual: si no alcanza, dice cuánto
   falta y a qué espaciamiento sí cabrían los mismos gránulos.
3. **Colecta de datos** — repetible. Lee el manifiesto, consulta el estado,
   recorta por `/vsicurl/` los productos listos y calcula la amplitud.

**Qué escribe la colecta.**

| Archivo | Qué es |
|---|---|
| `AMPL_…` | amplitud: mediana de seca − mediana de lluvia, en potencia |
| `MEDSECA_…`, `MEDLLUV_…` | las dos medianas por separado |
| `…_NOBS.tif` | observaciones válidas por píxel (banda 1 seca, banda 2 lluvia) |
| `INFORME_<lote>.html` | informe de auditoría del lote |

Las medianas no son decorativas: la amplitud sola no dice **sobre qué nivel**
se mide. Una diferencia de 0,001 no significa lo mismo sobre un fondo de
0,005 que sobre uno de 0,05. Llevan la misma máscara que la amplitud, de modo
que restarlas da exactamente el ráster de amplitud.

El informe HTML reúne lo que antes estaba repartido entre el manifiesto, los
metadatos de cada GeoTIFF y el registro de QGIS —que no se guarda—:
procedencia, parámetros del pedido, parámetros de la ejecución, fechas por
estación, productos con sus estadísticos, entorno, fuentes y licencias. Es
autocontenido, sin CDN ni fuentes remotas: un informe de auditoría se abre
cuando hace falta, y entonces puede no haber red.

**SRC de salida.** Los productos de HyP3 vienen en el UTM de la escena
(EPSG:32616 o 32617 en Costa Rica) y el recorte lo conserva a propósito:
reproyectar la retrodispersión antes de la mediana la remuestrearía 31 veces
para un estadístico que no depende de la rejilla. Si indica un SRC de salida
—CRTM05, EPSG:8908— se reproyectan los **productos finales** al terminar, por
vecino más próximo, que conserva los valores medidos a cambio de hasta medio
píxel de desplazamiento. El método queda escrito en los metadatos del
archivo.

**Créditos.** HyP3 Basic da 8 000 gratis al mes; un trabajo RTC cuesta 5
créditos a 30 m, 15 a 20 m y 60 a 10 m. Una traza de un año a 10 m ronda los
1 500–3 000, así que cabe de sobra. A 10 m una copa aislada de 5–15 m ocupa
uno o dos píxeles; a 30 m vuelve a ser subpíxel, que es la limitación del
SWIR que motivó buscar radar.

**Radiometría.** Se pide escala `power` y no dB. La mediana es invariante al
cambio monótono, así que para la amplitud daría igual, pero cualquier
**cociente** (RVI, VH/VV) y cualquier **promedio** hay que calcularlos en
potencia: promediar decibelios da un número que no es el promedio de nada.
Los COG de HyP3 ya vienen calibrados en float32, sin escala ni desplazamiento
que leer.

**Cuenta y token.** Hace falta una cuenta gratuita de Earthdata Login y un
token portador. **Nunca se escribe en el diálogo**: Processing vuelca todos
los parámetros de entrada en el registro y en el historial antes de ejecutar
el algoritmo, de modo que un token escrito en un campo queda ahí — lo guarda
QGIS, no este código — y acaba pegado en cualquier registro que se comparta.

Tres vías, probadas en orden:

1. **Configuración de autenticación de QGIS** — recomendada. Base cifrada,
   protegida por la contraseña maestra; por el diálogo pasa solo un
   identificador de siete caracteres. Método *API Header*, clave
   `Authorization`, valor `Bearer <token>`; también sirve el método *Básico*
   con el token en el campo de contraseña. El prefijo `Bearer ` es opcional:
   si falta se añade, y si está no se duplica.
2. **Archivo de texto** cuya primera línea es el token. Sin contraseña
   maestra, pero el token queda en claro en el disco.
3. **Variable de entorno** `EARTHDATA_TOKEN`, útil para `qgis_process`.

Una configuración indicada que falle no cae al archivo en silencio: se avisa.
Los tokens de Earthdata duran unos 60 días y el algoritmo lee esa fecha del
propio token para avisar antes de que caduque en medio de un lote.

---

## Catálogos

| Catálogo | Colección | Credenciales |
|---|---|---|
| Earth Search v1 (Element84) | `sentinel-2-l2a` | Ninguna |
| Earth Search — Colección 1 | `sentinel-2-c1-l2a` | Ninguna |
| Planetary Computer | `sentinel-2-l2a` | Token SAS anónimo, automático |
| Planetary Computer | `landsat-c2-l2` | Token SAS anónimo, automático |
| ASF Search (inventario y pedido) | `SENTINEL-1 / GRD_HD` | Ninguna |
| ASF HyP3 (productos RTC) | `RTC_GAMMA` | **Earthdata Login** (gratis) |

El token SAS de Planetary Computer caduca a unos 45 minutos; el complemento lo
solicita y lo renueva por su cuenta durante la ejecución. En **modo remoto**,
en cambio, la URL firmada queda escrita dentro del VRT y las capas dejan de
cargar al caducar — eso ninguna renovación lo evita. Para trabajo prolongado
sobre Planetary Computer, use el modo de descarga.

---

## Instalación

**Desde ZIP**

1. Complementos → Administrar e instalar complementos → Instalar a partir de ZIP
2. Elegir `BuscadorSTAC.zip` → Instalar complemento
3. El algoritmo aparece en la Caja de Herramientas de Processing, bajo
   **Sentinel-2 / Landsat (STAC) → Teledetección**, y en el menú
   Complementos → Sentinel-2 / Landsat (STAC).

**Requisitos**

- QGIS ≥ 3.28 (probado en 3.28 LTR, **3.34 LTR** y 3.44 LTR)
- El complemento **Processing** habilitado (viene con QGIS)
- GDAL con soporte `/vsicurl/` (el de cualquier instalación estándar)
- NumPy (incluido en QGIS)
- Salida a internet hacia los catálogos STAC

**Si actualiza desde una versión anterior**, desinstale, cierre QGIS, borre a
mano la carpeta del complemento y reinstale. Los `.pyc` en caché hacen que
QGIS ejecute código antiguo pese a haber actualizado los archivos:

```
Windows: %APPDATA%\QGIS\QGIS3\profiles\default\python\plugins\BuscadorSTAC\
Linux:   ~/.local/share/QGIS/QGIS3/profiles/default/python/plugins/BuscadorSTAC/
```

---

## Flujo recomendado (nubosidad alta)

1. **Explorar.** Modo *Solo catálogo*, miniaturas activadas, nubosidad de
   escena permisiva (50–70 %). Revise la hoja de contactos. Marque «Capa de
   huellas» y **dele un archivo GeoPackage**: el destino por omisión de un
   sumidero es una capa en memoria, que desaparece al cerrar QGIS y se lleva
   con ella las URL del campo `assets` que necesita el paso 3. El algoritmo
   avisa si detecta ese caso.
2. **Elegir.** Ordene la capa de huellas por `nubes_aoi` ascendente — ése es
   el número que importa, no `nubes_pct`, que corresponde a la tesela de
   110 km entera.
3. **Depurar**, de una de estas dos formas:
   - **Seleccionar** las entidades útiles y, al reejecutar, marcar «Entidades
     seleccionadas solamente». Rápido.
   - **Borrar** de la tabla de atributos las filas que no quiera y guardar la
     capa; al reejecutar, indicarla *sin* marcar «Entidades seleccionadas
     solamente». Se descarga lo que quedó.
4. **Producir.** Vuelva a ejecutar indicando esas huellas en «Huellas ya
   revisadas», esta vez en modo descarga con los índices que necesite.

Las dos vías del paso 3 funcionan, pero no son equivalentes. Borrar filas deja
un GeoPackage que **documenta exactamente qué escenas usó el estudio**:
reejecutable meses después, transferible a otra persona y revisable por quien
tenga que validar el resultado. Una selección vive en el proyecto y se pierde
al cerrarlo. Para trabajo que haya que defender o repetir, use la segunda.

El **catálogo debe ser el mismo** en los pasos 1 y 4. Desde la versión 1.31.0
se verifica: la capa de huellas guarda su endpoint en el campo `catalogo` y la
ejecución se rechaza si no coincide con el elegido, nombrando los dos. La
comprobación hace falta porque la colección `sentinel-2-l2a` existe en Earth
Search *y* en Planetary Computer, de modo que el nombre de colección no las
distingue, mientras que las URL de `assets` solo sirven en su propio servicio.
Una capa de huellas generada con una versión anterior no trae ese campo: se
avisa al ejecutar y no se rechaza, pero conviene regenerarla.

Para amplitud fenológica: estrategia *Mejor escena por mes*, un año completo,
«Nubosidad máxima DENTRO del AOI» alta (≈ 90) y el control de calidad puesto
en «Mínimo de observaciones por píxel». Descartar escenas enteras por su nube
media tira píxeles despejados; lo que hay que evitar es que un píxel quede
sustentado en una sola observación.

---

## Exportar a Google Earth (KMZ)

Cuarto algoritmo. Convierte la capa elegida —ráster o vectorial— a KMZ y la
abre en Google Earth.

**Un ráster se renderiza antes.** KML no guarda valores, solo imágenes pegadas
al terreno: un GeoTIFF de NDVI volcado sin más daría una imagen gris, porque
sus valores van de −1 a 1 y nadie los ha llevado a color. El algoritmo
renderiza la capa **con la simbología que usted ve en QGIS** —paleta, realce,
clases— y empotra esa imagen. Lo del lienzo es lo que verá en Earth. Se genera
un *super-overlay*: la imagen se parte en teselas con niveles de detalle, de
modo que Earth carga solo lo necesario al acercarse.

**Lo vectorial** va por LIBKML y conserva los atributos, que en Earth se ven
al pulsar cada elemento. Es la vía para llevarse la capa de huellas con su
nubosidad y sus fechas.

**Línea de tiempo.** Escribiendo el nombre de un **campo de fecha** —en la
capa de huellas, `fecha`— cada entidad sale con su `<TimeStamp>` y Google
Earth muestra el
control deslizante de tiempo: la serie se recorre o se acota a un intervalo
en vez de verse toda encimada.

**Por qué se escribe y no se elige de una lista.** Lo natural sería un
desplegable con los campos de la capa, y así estaba. Pero un parámetro de campo
de QGIS tiene que colgar de otro parámetro que le diga de qué capa sacar los
nombres, y aquí ese parámetro acepta ráster **y** vectorial. En
`QgsProcessingFieldWidgetWrapper::setParentLayerWrapperValue`, la rama de «una
sola capa» hace `qobject_cast<QgsVectorLayer *>` y acto seguido llama a
`layer->id()` sin comprobar el resultado: con un ráster el cast da `nullptr` y
QGIS **se cierra con una violación de acceso**. La rama de «varias capas», justo
encima, sí comprueba `vlayer && vlayer->isValid()`, y los envoltorios hermanos
también — es un olvido, y sigue en `release-3_44` y en `master`. Como el widget
se inicializa al construir el diálogo, bastaba con que la capa activa del
proyecto fuese un ráster para que el diálogo no llegara a abrirse, lo que lo
hacía parecer intermitente. Si escribe un nombre que no existe, el algoritmo se
detiene y le dice qué campos tiene la capa.

Las fechas se normalizan a ISO 8601 antes de escribir, porque Earth ignora en
silencio cualquier otra forma: el archivo abre, las entidades se ven y la
línea de tiempo no aparece sin que nada lo explique. Se informa cuántas se
entendieron y cuántas no. Un `03/01/2025` se rechaza a propósito —no se puede
saber si es 3 de enero o 1 de marzo, y adivinarlo desplazaría la serie
entera—; `2025/01/03` sí se acepta, porque empieza por el año.

**Por dentro: un solo `doc.kml`.** El KMZ que sale lleva el documento
directamente en `doc.kml`, sin archivos auxiliares. No es un detalle estético:
el controlador LIBKML de GDAL, por omisión, escribe en `doc.kml` solo un
`<NetworkLink>` que apunta a `layers/<nombre de la capa>.kml`, y mete ese
nombre en el ZIP **en UTF-8 crudo dejando en cero el bit 11 de las banderas
del archivo**, que es el que declara «este nombre está en UTF-8». Sin ese bit,
la norma del formato obliga a leer el nombre como CP437: el lector ve
`layers/BÃºfer.kml` mientras el enlace pide `layers/Búfer.kml`, no resuelve, y
**el KMZ abre vacío sin un solo mensaje**.

Medido con GDAL 3.8.4: con una capa llamada `Búfer Oval 357x179 m [Unión]`,
`ogr.Open` devuelve `None` sobre el KMZ recién escrito. Con
`Bufer Oval 357` (espacios) y con `Buferes[Union]` (corchetes) lo lee bien. Lo
que rompe son **las tildes y la eñe** — en español, casi cualquier nombre de
capa. El complemento aplana el archivo para que esto no pueda pasar, y si por
alguna razón quedara un enlace interno con caracteres que no valen en una URI,
lo avisa en vez de entregar un KMZ mudo.

**Qué versión de Google Earth.** El KMZ lo leen todas: Earth Pro de
escritorio, Earth Web y las apps móviles. Abrirlo automáticamente solo se
puede en el de **escritorio**, mediante la asociación de archivos del sistema.
Earth Web y las apps no permiten que otra aplicación les entregue un archivo
—la [documentación de Google](https://developers.google.com/maps/documentation/earth/import-data)
describe únicamente la importación manual—, así que para esas versiones el
algoritmo deja el archivo y dice dónde está; usted lo importa con «Importar
archivo» o «Abrir archivo KML local».

**Topes de Earth Web.** Por encima de 10 000 entidades o 250 000 vértices,
Earth Web importa el archivo como capa de datos de solo lectura en lugar de
como elementos editables. Se ve igual; cambia lo que puede hacer con él. El
Earth de escritorio no tiene ese límite. El algoritmo avisa si su capa pasa
de ahí.

No se sube nada a ningún servidor y no hace falta cuenta de Google.

---

## Límites conocidos

**Resolución frente a copas aisladas.** Las bandas SWIR son de 20 m
(Sentinel-2) o 30 m (Landsat). Un árbol aislado de 5–15 m de copa es
subpíxel: su señal se mezcla con la del pasto que lo rodea. La amplitud
fenológica funciona para **dosel continuo**, no para individuos dispersos.
Para esos hace falta resolución submétrica.

**Memoria.** Los índices y la amplitud se calculan cargando cada banda
completa en memoria, sin proceso por bloques. El complemento estima el pico
antes de descargar nada y rechaza la ejecución por encima de 4 GiB, indicando
el tamaño en píxeles. La extensión **por omisión abarca todo Costa Rica** y
pediría unos 42 GiB: indique una capa de AOI o encuadre la extensión. Un lote
de 150 ha ocupa menos de 2 MB y un cuadrado de 100 km cabe de sobra.

**Nombres de archivo.** El nombre lleva año y mes, no día. Cuando dos escenas
del mismo mes caen sobre la misma malla, a partir de la segunda se añade el
día (`_d09`); el complemento avisa por adelantado y lista las fechas
implicadas. El día de la primera no queda en el nombre: está en el campo
`fecha` de la capa de huellas.

---

## Datos y atribución

- **Sentinel-2**: Copernicus Sentinel data, procesados por ESA. Acceso
  abierto bajo la licencia de datos de Copernicus.
- **Landsat Collection 2**: cortesía del U.S. Geological Survey / NASA.
  Dominio público.

Cite la fuente de las imágenes en cualquier producto derivado.

---

## Licencia

GPL **v2 o posterior** (SPDX: `GPL-2.0-or-later`). `LICENSE` trae el
texto de la versión 2; el «o posterior» lo da la nota de licencia de
cada archivo fuente, de modo que puede acogerse a la v2 o a cualquier
versión posterior, a su elección.

Copyright © 2026 Jorge Fallas <jfallas56@gmail.com>

---

## Historial

| Versión | Fecha | Cambios |
|---|---|---|
| 1.0.2 | 2026-10-07 | «Campo de fecha» se pide escribiendo el nombre: como parámetro de campo colgado de una capa que puede ser ráster, abrir el diálogo cerraba QGIS (puntero nulo en QGIS, no en el complemento). |
| 1.0.1 | 2026-10-07 | Corrección del KMZ vectorial: el documento pasa a `doc.kml` y se quita el enlace interno de LIBKML, que con tildes en el nombre de la capa no resolvía y dejaba el archivo vacío en Google Earth. |
| 1.0.0 | 2026-10-02 | Primera versión pública. Cuatro algoritmos: búsqueda y descarga Sentinel-2 / Landsat por STAC y COG, Esri World Imagery / Wayback, Sentinel-1 RTC vía ASF HyP3, y exportación a Google Earth (KMZ). |

El desarrollo previo a esta publicación —27 iteraciones internas que nunca
se publicaron— está documentado en
[CHANGELOG.md](https://github.com/jfallas56-CR/BuscadorSTAC/blob/main/CHANGELOG.md)
del repositorio. Se conserva porque cada entrada dice qué falló y por qué el
remedio es ése.

