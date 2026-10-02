# Historial de desarrollo

Este archivo recoge el desarrollo de **BuscadorSTAC** ANTES de su primera
publicación, cuando el complemento se llamaba `BuscarSentinel2CR` y luego
`BuscadorSTAC_CR`.

Los números de versión de aquí (1.19.1 a 1.34.0) corresponden a
iteraciones internas: **ninguna de ellas se publicó**, no están en
plugins.qgis.org y ningún usuario las tuvo instaladas. La primera versión
pública es la **1.0.0**, y a partir de ahí el historial de usuario vive en
el campo `changelog` de `metadata.txt` y en la tabla del
[README del complemento](BuscadorSTAC/README.md).

Se conserva porque documenta por qué el código está como está: cada
entrada dice qué falló, cómo se notó y por qué el remedio es ése. Varias
describen fallos silenciosos —resultados plausibles y equivocados— que son
los que cuesta más volver a encontrar.

---


## 1.34.0 — 2026-10-02

**El paquete pasa a llamarse BuscadorSTAC.**

- Se quita el sufijo _CR. El complemento no esta limitado a Costa Rica: los catalogos STAC, Sentinel, Landsat, el archivo de Esri y HyP3 son globales. Lo costarricense son los VALORES POR OMISION —la extension inicial, el marco seca/lluviosa, la mencion de CRTM05— y eso va en los valores por omision, no en la identidad. Un sufijo regional en el portal desanima a quien trabaja en otro pais sobre un complemento que le serviria igual; la procedencia ya esta en el nombre de la cuenta de GitHub.
- DESINSTALE la version anterior antes de instalar esta: el nombre del paquete es lo que QGIS usa para identificar un complemento, de modo que las dos convivirian y los algoritmos apareceran duplicados.
- El id del proveedor NO cambia: sigue siendo «buscadorstac», asi que los identificadores completos de los algoritmos («buscadorstac:...») son los mismos que en la 1.33.0 y ningun modelo de Processing se rompe por este cambio.
- URLs de repositorio, seguimiento y pagina principal apuntadas a github.com/jfallas56-CR/BuscadorSTAC.
- Corregido el campo «about», que seguia diciendo «Tres algoritmos de Processing» despues de anadir el exportador a Google Earth. Es el primer texto que lee un revisor del portal, y era el primero que estaba mal. Ahora describe los cuatro.
- Sin cambios en el codigo de los algoritmos.

## 1.33.0 — 2026-10-02

**Exportar a Google Earth, titulo nuevo y compatibilidad con QGIS 4.**

- Algoritmo nuevo «Exportar a Google Earth (KMZ)». Convierte la capa elegida —raster o vectorial— a KMZ y la abre en el Google Earth de escritorio. Un raster se RENDERIZA antes con la simbologia que usted ve en QGIS: KML no guarda valores, solo imagenes pegadas al terreno, de modo que volcar un GeoTIFF de NDVI sin renderizar daria una imagen gris ilegible. Se genera un super-overlay con niveles de detalle, asi que Earth carga solo lo que hace falta al acercarse. Lo vectorial va por LIBKML y conserva los atributos, que en Earth se ven al pulsar cada elemento.
- Sobre «todas las versiones de Google Earth»: el KMZ lo leen todas, pero abrirlo solo se puede automatizar en el de ESCRITORIO, por la asociacion de archivos del sistema. Earth Web y las apps moviles no permiten que otra aplicacion les entregue un archivo; la documentacion de Google describe unicamente la importacion manual. El algoritmo lo intenta y, si no puede, dice donde quedo el archivo y como importarlo en cada version. Prometer mas seria prometer algo falso.
- Se avisa si la capa pasa de 10.000 entidades o 250.000 vertices: por encima de esos topes Earth Web importa el archivo como capa de datos de solo lectura en vez de como elementos editables. El Earth de escritorio no tiene ese limite.
- COMPATIBILIDAD CON QGIS 4: se anade qgisMaximumVersion=4.99. Sin ese campo el portal publica el complemento como 3.x unicamente y NO aparece en QGIS 4, porque al omitirlo se asume compatibilidad solo hasta el final de la serie de qgisMinimumVersion. Todas las importaciones de Qt ya pasaban por el envoltorio qgis.PyQt, que cubre Qt5 y Qt6, y no queda ninguna API de la epoca de QGIS 2.
- QgsMapLayer.RasterLayer NO se usa para distinguir el tipo de capa: ese enum pasa a Qgis.LayerType.Raster en QGIS 4. Se usa isinstance con la clase, que es estable en las dos series.
- Titulo nuevo: «Buscador STAC: Sentinel, Landsat y Esri Wayback». El anterior ocupaba 69 de los 72 caracteres y se truncaba en el administrador de complementos; ademas decia «e Esri» donde el espanol pide «y Esri».
- El paquete pasa a llamarse BuscadorSTAC y el id del proveedor a «buscadorstac». CAMBIO INCOMPATIBLE: los identificadores completos de los algoritmos pasan de «sentinel2cr:...» a «buscadorstac:...», asi que cualquier modelo o script de Processing que los referencie hay que actualizarlo. Se hace ahora porque el complemento aun no se ha publicado y despues ya no seria posible sin perder el historial de versiones.
- La logica de KML que no necesita QGIS (orden de canales del alfa, conteo de vertices) vive en core.py y se prueba sin QGIS: 71 pruebas en 0,2 segundos.

## 1.32.0 — 2026-10-02

**La logica pura sale a core.py, que NO importa QGIS.**

- Modulo nuevo core.py con las formulas, el parseo, la agrupacion temporal, las estimaciones y las validaciones: 16 funciones y las constantes del dominio, 460 lineas sacadas del algoritmo. Su unica regla es no importar nada de qgis ni de osgeo, y es lo que permite probarlo con pytest a secas: 60 pruebas en 0,2 segundos, sin QGIS instalado y sin imitarlo con clases falsas. Un QGIS imitado aporta sus propios errores —un fallo del imitador se confunde con uno del codigo— y eso ya costo un ciclo de depuracion sobre codigo que estaba bien.
- Las funciones se movieron TAL CUAL y se reenganchan en la clase como staticmethod, de modo que ningun sitio de llamada cambio: siguen escribiendose «self._foo(...)». Es donde se cuelan los errores de un refactor, asi que no se toco ninguno.
- Las que reciben una fuente de entidades o un «feedback» lo usan por pato: solo llaman a getFeatures(), fields(), featureCount(), pushInfo() y pushWarning(). No hace falta QGIS para eso.
- Sin cambios de comportamiento. Lo verifican las baterias anteriores (247 + 72 + 3574 comprobaciones) ademas de las 60 nuevas.

## 1.31.1 — 2026-10-02

**Dos enums que se usaban sin respaldo, y herramientas de verificacion.**

- QgsWkbTypes.Polygon y QgsProcessing.TypeVectorPolygon se usaban en forma PLANA y sin pasar por el resolutor _enum_qgis, que si cubria QgsRasterMinMaxOrigin, QgsContrastEnhancement, QgsColorRampShader y QgsProcessingParameterFile.Behavior. En Qt6 esos dos viven en QgsWkbTypes.Type.* y QgsProcessing.SourceType.*, de modo que en una version donde se hayan movido el primero deja sin crear la capa de huellas y el segundo deja vacio el desplegable de capas de AOI. Los encontro tools/api_audit.py, no una lectura a mano.
- El respaldo de TypeVectorPolygon es TypeVector (cualquier vectorial) en vez de un numero: si la constante especifica faltara, aceptar cualquier capa vectorial degrada la ayuda del parametro pero deja el algoritmo utilizable, mientras que un numero equivocado filtraria la lista y pareceria que el proyecto no tiene poligonos.
- Herramienta nueva tools/preflight.py: corre en local las comprobaciones que bloquean en plugins.qgis.org, incluido el escaneo de Bandit, el «%%» sin escapar de metadata.txt, credenciales pegadas en el codigo, rutas de la maquina de desarrollo y la coherencia de version en los nueve sitios. 54 comprobaciones; sale con codigo distinto de cero si algo bloquea.
- Herramienta nueva tools/api_audit.py: recoge con AST cada simbolo de QGIS, Qt y GDAL que usa el complemento y comprueba que exista en el QGIS que corre. Cuando un enum anidado falta pero existe el plano, lo dice: es la informacion que hace falta para arreglarlo.
- Quitado el import de QgsProcessing que quedo sin usar en los algoritmos de Esri y Sentinel-1.

## 1.31.0 — 2026-10-02

**El flujo de dos pasos se verifica y se documenta.**

- La capa de huellas guarda ahora el endpoint que produjo sus URL, en el campo «catalogo». Hacia falta porque la coleccion «sentinel-2-l2a» existe en Earth Search Y en Planetary Computer: el campo «coleccion» por si solo no distingue una de otra. Con huellas de Earth Search y PC elegido en la segunda pasada, la coleccion coincidia mientras los href eran de Element84 y el codigo intentaba firmarlos con un token SAS. El fallo parecia un problema de datos. Es la misma familia de error que las versiones distintas de producto entre PC y ASF en 1.28.0.
- Al volver a entrar por «Huellas ya revisadas» se compara el endpoint y la coleccion de la capa con el catalogo elegido, y la ejecucion se rechaza antes de empezar si no coinciden, nombrando los dos. Hasta ahora la ayuda solo lo PEDIA; nada lo comprobaba. Una capa generada con una version anterior no trae el campo: eso se avisa al ejecutar y no se rechaza, para no dejar inservibles las huellas ya guardadas.
- Se rechaza tambien una capa de huellas revisadas que no aporte ninguna entidad, distinguiendo las dos causas: seleccion vacia con «Entidades seleccionadas solamente» marcado, o capa depurada sin filas restantes.
- Aviso nuevo cuando la capa de huellas se crea como capa TEMPORAL en modo «Solo catalogo». El destino por omision de un sumidero es una capa en memoria, que desaparece al cerrar QGIS y se lleva las URL del campo «assets»; el segundo paso se quedaba sin nada que leer y el usuario no lo descubria hasta la sesion siguiente.
- Documentadas las DOS formas de quedarse con las escenas utiles, que antes solo mencionaban la seleccion: seleccionar entidades, o borrar filas de la tabla de atributos y guardar la capa. La segunda ya funcionaba —se leen las entidades que queden— pero no estaba escrita en ninguna parte. Deja un GeoPackage que documenta que escenas uso el estudio, reejecutable y transferible; una seleccion se pierde al cerrar el proyecto.
- Sin cambios en el calculo, la descarga ni la simbologia.

## 1.30.0 — 2026-10-01

**La amplitud se carga de verdad, en los DOS algoritmos.**

- La capa de amplitud se registraba con context.addLayerToLoadOnCompletion y no aparecia en el proyecto. El archivo quedaba bien escrito en disco, de modo que el sintoma era «el proceso termino y no veo nada», sin ninguna linea en el registro que distinguiera un fallo de calculo de un fallo de carga. Ese registro delega la resolucion de la fuente en mapLayerFromString, que cuando no resuelve no informa.
- Ahora las amplitudes se acumulan durante la ejecucion y se cargan en postProcessAlgorithm, que es el hilo principal y el unico punto seguro para anadir capas al proyecto. Se construye QgsRasterLayer, se comprueba isValid(), se aplica el mismo post-procesador de realce y se informa: cuantas capas se anadieron de cuantas, y por cada fallo si el archivo no estaba o si QGIS no pudo abrirlo. Es el mismo remedio que funciono en 1.24.1 con las capas XYZ de Esri.
- Afecta a los DOS algoritmos. En el de Sentinel-2 la amplitud fenologica tenia el mismo defecto, latente porque esa rama no se habia ejecutado desde la 1.22.1. Las composiciones y los indices siguen cargandose como hasta ahora: ese camino si funciona y no se toca.

## 1.29.2 — 2026-10-01

**Correccion: las bandas van DENTRO del ZIP del producto.**

- La recogida buscaba un archivo terminado en «_VH.tif» en el bloque «files» de cada trabajo. HyP3 no publica las bandas sueltas: «files» trae UNA entrada, el ZIP del producto completo (unos 420 MB a 30 m). No se encontraba nada y los 31 productos quedaban sin usar, pese a estar correctamente generados.
- Peor que el fallo era el diagnostico: se informaba «un granulo de una sola polarizacion no trae las dos» sobre granulos 1SDV, que son de polarizacion DOBLE. Un mensaje segutro y equivocado manda a buscar un problema de datos que no existe. Ahora el aviso dice solo que no se localizo la banda.
- Las bandas se leen dentro del ZIP remoto con /vsizip/{/vsicurl/...}, sin descargarlo ni descomprimirlo en disco, y se recorta el AOI como antes. Las llaves son obligatorias: la URL prefirmada de S3 lleva «?» y «&», y sin delimitar GDAL corta la ruta en el primer caracter especial.
- El contenido del ZIP se LISTA en vez de componer la ruta interna suponiendo «<base>/<base>_VH.tif». Suponerlo funcionaria hoy y fallaria el dia que ASF cambie la estructura, con un mensaje que culparia al dato. Si no aparece la banda pedida, se informa que GeoTIFF SI contiene el producto.
- Se avisa del volumen antes de empezar: un miembro comprimido no admite acceso aleatorio, asi que GDAL descomprime desde el principio y la transferencia se parece al tamano de la banda aunque el AOI sea diminuto.

## 1.29.1 — 2026-10-01

**La recogida recorta al AOI que se pidio.**

- El recorte usaba la extension que hubiera en el dialogo en ese momento, no la del lote. La recogida se repite varias veces hasta que terminan todos los trabajos, y los recortes de un lote comparten nombre: con otra extension, los archivos de la primera pasada se daban por descargados y la serie mezclaba dos encuadres sin avisar. Ahora se usa el bbox guardado en el manifiesto y se informa cual es; si difiere del dialogo se dice. Un manifiesto de una version anterior no lo trae y se avisa.
- Se retira el filtro «platform» de la consulta a ASF. Comprobado contra la API que «SENTINEL-1», la lista explicita «SENTINEL-1A,SENTINEL-1C» y la ausencia del parametro devuelven lo mismo: GRD_HD + IW ya es vocabulario propio de Sentinel-1. Omitirlo evita depender de que el alias del grupo se mantenga al dia cuando entre en servicio un satelite nuevo.

## 1.29.0 — 2026-10-01

**Los granulos se buscan en ASF, no en Planetary Computer.**

- Causa del rechazo «Some requested scenes could not be found» en el primer pedido real: el grupo final de 4 hex del nombre SAFE identifica la VERSION del producto, no la adquisicion, y las dos colecciones guardan versiones distintas de la misma toma. Comprobado contra ambos servicios sobre la traza 157 de 2025: coincidian los seis primeros campos y diferia solo el ultimo (PC «..._070C02_A80F» frente a ASF «..._070C02_BD01»). HyP3 solo procesa lo que hay en el archivo de ASF, de modo que 31 de 50 granulos se rechazaban; los de Sentinel-1C pasaban por ser demasiado recientes para haberse reprocesado.
- Ahora el inventario Y el pedido consultan la API de busqueda de ASF, que no pide credenciales y entrega el mismo nombre que HyP3 acepta. Buscar donde se va a pedir es lo que impide que las dos listas discrepen.
- Los resultados de ASF se traducen al vocabulario de las extensiones STAC «sat:» y «sar:», de modo que el desglose por traza, el reparto estacional y la comprobacion de polarizacion siguen siendo el mismo codigo ya probado.
- Aviso si ASF devuelve tantos resultados como el tope pedido: una lista truncada en silencio falsearia el inventario.

## 1.28.0 — 2026-10-01

**El token puede guardarse en la base cifrada de QGIS.**

- Parametro nuevo «Configuracion de autenticacion de QGIS», con el selector propio de QGIS (QgsProcessingParameterAuthConfig). Es la via mas segura: la base de autenticacion esta cifrada y protegida por la contrasena maestra, y por el dialogo —y por tanto por el registro y el historial— pasa solo un identificador de siete caracteres.
- Se crea con el boton «+» del propio parametro, o en Configuracion → Opciones → Autenticacion. Metodo «API Header», clave Authorization, valor «Bearer » seguido del token. Tambien se acepta el metodo «Basico» con el token en el campo de contrasena.
- Tres vias, probadas en orden: configuracion de autenticacion, archivo de texto, variable de entorno EARTHDATA_TOKEN. El archivo sigue disponible para quien no quiera manejar la contrasena maestra.
- Una configuracion indicada que falla NO cae al archivo en silencio: se informa ese fallo, porque indicarla fue una eleccion explicita y un respaldo callado dejaria al usuario creyendo que uso la via cifrada.
- Si la configuracion existe pero no trae el token, el mensaje dice que claves SI trae, en vez de un fallo sin pista.

## 1.27.2 — 2026-10-01

**Correccion: dos lotes en la misma carpeta se pisaban.**

- Los recortes intermedios se llamaban «S1_AAAA_MM_DD_VH.tif», sin traza ni espaciamiento. Como un archivo que ya existe se da por descargado, recoger una segunda traza en la misma carpeta REUTILIZABA los recortes de la primera: la amplitud se calculaba con pixeles de la otra orbita —el error que todo el algoritmo intenta evitar— y el archivo de salida seguia rotulado con la traza que se pidio. Lo mismo entre resoluciones: un lote a 10 m se quedaba con los recortes de 30 m.
- Ahora cada recorte lleva la marca del lote, «S1_T157D_30m_20250203T001234_VH.tif», con la traza, la direccion, el espaciamiento y la marca de tiempo COMPLETA del granulo. La hora importa: IW se publica en lonjas y una misma traza puede entregar varios granulos del mismo dia, que con solo la fecha compartian nombre.
- El nombre de la amplitud incluye el espaciamiento: la misma traza y ano a 30 y a 10 m son dos productos que hay que poder comparar, no uno que sobreescriba al otro.
- Con esto se pueden recoger varias trazas, o la misma a dos resoluciones, en una sola carpeta.

## 1.27.1 — 2026-10-01

**Correcciones del primer envio real a HyP3.**

- SEGURIDAD. El token de Earthdata pasa de un campo de texto a la RUTA de un archivo. El dialogo de Processing vuelca todos los parametros de entrada en el registro y en el historial ANTES de ejecutar el algoritmo, de modo que un token escrito en un campo quedaba registrado por QGIS, no por este codigo, y acababa pegado en cualquier registro que se comparta. Ahora en el registro aparece la ruta. Si no se indica archivo se busca la variable de entorno EARTHDATA_TOKEN. Quien haya ejecutado 1.27.0 debe REVOCAR su token y generar otro.
- Se lee la fecha de caducidad del JWT, sin verificar la firma, para avisar antes de empezar en vez de dejar que HyP3 responda un 401 desnudo. Un token caducado se rechaza en el dialogo.
- Correccion: el nombre de granulo era el id del item de Planetary Computer, al que le FALTA el identificador unico de 4 hex del final. HyP3 rechazaba el lote completo con HTTP 400 y el motivo no era evidente, porque el prefijo si encaja con su expresion regular. Ahora se usa s1:product_identifier, que trae el nombre SAFE completo.
- El cuerpo del error HTTP ya no se recorta a 400 caracteres. HyP3 repite la peticion entera antes de decir que campo rechazo, asi que el tope corto ocultaba justo la parte que explica el fallo; ahora se recorta por el final.
- Viabilidad comprobada en el dialogo, antes de gastar: si la traza no alcanza el minimo de observaciones por pixel en las dos estaciones, la amplitud saldria entera a NaN por construccion y los creditos se perderian. Se rechaza indicando a cuanto bajar el minimo.
- Los granulos de polarizacion simple (1SSV, 1SSH) ya no se piden cuando se pidio la cruzada: no la contienen. Antes se habrian gastado creditos en productos inservibles para lo pedido.
- El primer nombre de granulo y los parametros del trabajo se imprimen antes de enviar, para poder verlos cuando un lote se rechaza.
- resolution se envia como numero con decimales (10.0), que es lo que declara el esquema.

## 1.27.0 — 2026-10-01

**Algoritmo nuevo: Sentinel-1 RTC via ASF HyP3.**

- Radar en banda C, corregido radiometrica y geometricamente por terreno, con amplitud estacional seca-lluviosa de retrodispersion. Resuelve la mitad debil del camino optico: la mediana de estacion lluviosa, que se construye con pocas escenas y contaminadas por nube. El radar no pierde datos por nube, asi que el conteo de escenas pasa de bruto a neto.
- Advertencia que viene con el metodo: la retrodispersion responde con fuerza a la HUMEDAD DEL SUELO, de modo que un potrero desnudo tambien presenta amplitud estacional grande. No es un discriminante limpio de lenosas, es uno confundido de otra manera; vale como contraste de la amplitud optica, no como sustituto.
- No se mezclan trazas. Cada orbita relativa observa con otro angulo de incidencia, asi que una serie con trazas mezcladas mide cambio de geometria y no de vegetacion. El modo de inventario obliga a elegir una traza antes de pedir nada y la traza va en el NOMBRE del archivo, para que un apilado mezclado se vea en el panel de capas en lugar de descubrirse en los resultados.
- Tres modos: inventario por traza con reparto estacional y costo en creditos (no consume creditos ni necesita cuenta); pedido de trabajos RTC con casilla de confirmacion de gasto y tope de creditos comprobado en el dialogo; y recogida, repetible, que lee el manifiesto, consulta el estado y calcula la amplitud con recuento de observaciones por pixel.
- Se pide escala «power» y no dB: la mediana es invariante al cambio monotono, pero cualquier cociente (RVI, VH/VV) y cualquier promedio hay que calcularlos en potencia. Con VH y VV se calcula ademas el RVI dual-pol.
- Requiere cuenta gratuita de Earthdata Login y un token portador. El token NO se guarda: ni en el proyecto, ni en el manifiesto, ni en el registro.
- Un envio fallido no se reintenta: si la peticion llego al servidor, repetirla gastaria los creditos dos veces.

## 1.26.0 — 2026-09-30

**La deteccion de duplicados compara pixeles, no bytes.**

- Raiz del problema de las imagenes repetidas. Se agrupaban las versiones candidatas por tamano en bytes y solo se comparaban dentro de cada grupo, siguiendo la optimizacion de la aplicacion de Esri. Cuando Esri republica el mosaico recomprimiendo la MISMA fotografia el tamano cambia, asi que las dos versiones caian en grupos distintos, nunca se comparaban y las dos se descargaban.
- Ahora se decodifica una tesela por version (en memoria, con /vsimem/) y se comparan los pixeles: por debajo de 2 niveles de gris de diferencia media es la misma fotografia recomprimida; una imagen distinta mueve decenas.
- Funciona aunque la capa de metadatos no responda, que es justo cuando no hay fecha de captura con la que agrupar despues: se veian dos archivos capND que eran la misma imagen.

## 1.25.1 — 2026-09-30

**El mosaico vigente se compara antes de anadirlo.**

- El mosaico vigente no figura en el catalogo de Wayback, asi que no tiene capa de metadatos y su archivo salia sin fecha de captura; y en el caso normal es la MISMA imagen que la version mas reciente del archivo (comprobado sobre un lote real). Se anadia siempre, dejando un duplicado sin fecha, que es justo lo que un analisis temporal no puede usar.
- Ahora se compara una tesela de cada uno. Si coinciden, se avisa y no se anade; si difieren, significa que Esri publico imagen mas nueva que la del catalogo y entonces si se incluye. La casilla pasa a responder la unica pregunta para la que sirve.
- Si la comparacion falla por red, se incluye igualmente y se avisa.

## 1.25.0 — 2026-09-30

**Una sola imagen por fecha de captura.**

- Varias versiones del archivo pueden llevar LA MISMA fotografia reprocesada: Esri republica el mosaico ajustando color o compresion sin imagen nueva. Sus teselas difieren byte a byte, asi que la deteccion de cambio las separaba, pero la fecha de captura es la misma y a la vista son indistinguibles (comprobado sobre un lote real).
- Nueva opcion, activada por omision: se conserva una version por fecha de captura, la publicada mas tarde, y las equivalentes se anotan en el registro y en la etiqueta VERSIONES_EQUIVALENTES del GeoTIFF. En el caso real, 5 versiones pasan a 3 fotografias distintas: 40 %% menos descarga y disco.
- Las versiones sin fecha de captura conocida nunca se agrupan: sin fecha no hay forma de saber si son la misma imagen.

## 1.24.1 — 2026-09-30

**Correccion: las capas XYZ no se cargaban.**

- Se registraban con context.addLayerToLoadOnCompletion, que resuelve la fuente con mapLayerFromString y prueba los proveedores de archivo (ogr, gdal). Una URI «type=xyz&url=…» necesita el proveedor «wms» indicado de forma explicita, asi que las seis capas salian nulas y QGIS las listaba como «no se generaron correctamente». Ahora se construyen con QgsRasterLayer(uri, nombre, 'wms') en postProcessAlgorithm, que es el hilo principal y el unico sitio seguro para tocar el proyecto.
- Solo queda activada la version mas reciente: son mosaicos opacos y activarlas todas no deja ver mas, pero las descarga todas.
- Cada capa lleva la procedencia en su resumen y la atribucion en sus derechos.

## 1.24.0 — 2026-09-30

**Procedencia completa de cada imagen Esri.**

- Se recupera lo mismo que muestra la aplicacion Wayback de Esri: proveedor, sensor, fecha de captura, resolucion del original y exactitud posicional. Antes se pedian los cinco campos a la capa de metadatos y se descartaban dos.
- La procedencia se escribe DENTRO del GeoTIFF (PROCEDENCIA, PROVEEDOR_SENSOR, FECHAS_CAPTURA, RESOLUCION_ORIGEN_M, EXACTITUD_POSICIONAL_M), de modo que viaja con el archivo y se lee con gdalinfo.
- Segunda pasada para las consultas de metadatos que fallan: el servicio de Esri responde de forma intermitente y un reintento tras una pausa recupera buena parte.

## 1.23.3 — 2026-09-30

**Ayuda al dia con el comportamiento real.**

- Se explica como leer la columna «capturada»: fecha unica, rango «2022-02-03…2023-07-01 (2)» cuando el lote abarca varios footprints de origen, o «?» cuando el servicio de metadatos no respondio.
- Se documenta el nombre de los archivos de salida y cuando aparece «capND».
- Ayuda de «Puntos de muestreo» ampliada: esos mismos puntos se usan para la fecha de captura, asi que con rejilla 1 no se detecta un lote heterogeneo.
- Ayuda de «Modo» con el tiempo de descarga esperable, y la de «Carpeta de salida» con la procedencia escrita en los metadatos del GeoTIFF.

## 1.23.2 — 2026-09-30

**La fecha de captura se mide sobre todo el AOI.**

- Se consultaba en un solo punto, el centro, mientras la deteccion de cambio muestrea una rejilla. En una corrida real dos versiones publicadas con un ano de diferencia salian ambas con captura 2022-02-03 pese a tener teselas distintas: una cambio en una esquina del lote y el centro no. Ahora se consultan varios puntos y se informan las fechas distintas («2022-02-03…2023-07-01 (2)»), de modo que un lote que abarca varios footprints se ve.
- Espera de metadatos a 25 s y dos reintentos: ya no es fatal que fallen, asi que conviene ser paciente.
- Cache por version y punto, para no repetir consultas lentas.
- El nombre de archivo usa «capND» tambien cuando la captura es un rango y no una fecha unica.

## 1.23.1 — 2026-09-30

**Correccion: un tiempo de espera agotado abortaba la ejecucion entera.**

- TimeoutError no hereda de urllib.error.URLError, asi que escapaba del manejo de errores de red y tumbaba la corrida DESPUES de completar la deteccion de cambios, que es la parte cara. Ahora todo fallo de red sale como RuntimeError y los llamadores lo absorben.
- La consulta de metadatos es opcional de verdad: si falla, la fecha de captura sale como «?» con un aviso y las imagenes se descargan igual.
- Espera propia y mas corta para los metadatos (15 s) y un reintento ante fallos transitorios.
- Sin fecha de captura el nombre de archivo usa «capND»: «?» no es valido en un nombre de archivo de Windows.

## 1.23.0 — 2026-09-30

**Nuevo algoritmo: Esri World Imagery / Wayback.**

- Imagenes RGB de alta resolucion sobre el AOI, actuales y del archivo historico Wayback (desde 2014).
- Detecta que versiones cambian DE VERDAD sobre el area de interes, con el metodo de la propia aplicacion de Esri (endpoint tilemap y comparacion de teselas byte a byte); sobre un lote suele haber un punado de versiones distintas, no las mas de 115 globales.
- Consulta la FECHA DE CAPTURA real de cada version en la capa de metadatos: la fecha del nombre de la version es la de publicacion del mosaico y puede diferir anos de la de la imagen.
- Tres modos: listar versiones, cargar como capas XYZ, o descargar recorte GeoTIFF del AOI.
- Aviso de licencia en cada ejecucion: las imagenes de Esri no son dato abierto.

## 1.22.1 — 2026-09-29

**Correcciones de auditoria y conversion a complemento.**

- Corregido UnboundLocalError en la amplitud fenologica cuando falla la apertura de la escena de referencia.
- Los indices se acotan a su rango fisico; los pixeles imposibles salen como NaN con aviso de cuantos fueron.
- Tope de memoria comprobado antes de descargar: la extension por omision (Costa Rica entera) pedia ~42 GiB y cerraba QGIS sin mensaje.
- Corregida la fuga de post-procesadores, que crecia en cada ejecucion.
- Suprimida la inundacion del registro por «All-NaN slice encountered» de nanmedian.
- El aviso de amplitud vacia distingue ahora entre estacion nublada, minimo de observaciones inalcanzable y estaciones sin solape.
- Ayuda del dialogo y de cada parametro actualizadas; autoria en el panel de ayuda y en los metadatos de las capas generadas.

## 1.22.0 — 2026-09-21

**Renovacion automatica del token SAS de Planetary Computer, que caduca a los ~45 minutos.**

## 1.21.1 — 2026-09-20

**Corregida la paleta que no se aplicaba: los metodos auxiliares estaban definidos en la clase equivocada.**

## 1.19.1 — 2026-09-20

**Resolutor de enums Qt5/Qt6 para QgsRasterMinMaxOrigin y afines, cuya migracion a Qt6 no fue uniforme.**

## 1.0.0 — 2026-09-12

**Version inicial.**

