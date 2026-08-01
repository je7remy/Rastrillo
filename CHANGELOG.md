# Changelog

Todos los cambios notables en este proyecto se documentan en este
fichero.

El formato sigue [Keep a Changelog](https://keepachangelog.com/es-ES/1.1.0/)
y el proyecto se adhiere a [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- **Formato XLSX para el informe** (Paso 6, Entrega 2). `report --format xlsx
  --out FILE`, `GET /api/report?format=xlsx` y un botón "Excel" en el
  dashboard, por el mismo camino autenticado que arregló el Paso 5 (`fetch`
  con token → `Blob` → enlace local; **cero anclas**). Tres hojas: **Resumen**
  (totales, distribución por estado / confianza / verificabilidad, recuento del
  audit log y las notas del informe), **Cuentas** (una fila por cuenta, con las
  mismas columnas que el CSV, cabecera congelada, autofiltro, anchos medidos
  sobre el contenido, fechas como fechas, números sumables y URLs como
  hipervínculos) y **Glosario** (los mismos textos que el anexo del PDF, leídos
  de `static/app.js` en tiempo de render). Ningún dato depende del color: la
  cabecera se distingue por peso y estado, confianza y verificabilidad se
  escriben siempre como palabras, así que en blanco y negro se lee igual.
- **`rastrillo/tabular.py`**: la definición ÚNICA de la exportación tabular.
  Una sola lista de columnas y una sola ruta de traducción para CSV y XLSX, con
  un test de paridad que exige que las dos cabeceras coincidan. Aloja también
  las guardas compartidas (fórmulas, recortes, nombres de hoja).
- **`rastrillo/report_xlsx.py`**: el renderizador, con import perezoso de
  openpyxl para que su ausencia solo afecte a `format=xlsx`.
- `openpyxl` pasa a ser una dependencia **declarada** en `pyproject.toml` y
  `requirements.txt`. Ya estaba instalada como transitiva de
  `sherlock-project`, pero depender por accidente de la dependencia de otro es
  frágil. **El pin de `requirements.lock` no cambia** (`openpyxl==3.1.5`) y el
  fichero no se regenera.
- `RASTRILLO_CSV_SEP` y `--sep` / `?sep=`: separador del CSV.
- `tests/test_exportacion_tabular.py`: la suite pasa de 540 a **589 tests**.

### Changed

- **El CSV se reescribió para que sea legible** (Paso 6, Entrega 1). El
  problema medido no era que hubiera demasiados datos —17 filas, 294 caracteres
  la línea más larga— sino el separador: Excel usa el de la configuración
  regional y no el del fichero, así que en es-ES una fila de comas cae entera en
  la columna A. Ahora lleva **BOM UTF-8** (sin él Excel destroza acentos y
  cirílico), terminaciones `\r\n`, **cabeceras en español** (`deletion_eta` →
  "Fecha límite de eliminación"), valores traducidos con las mismas cadenas que
  usa la UI, fechas legibles en vez de timestamps, y las columnas ordenadas para
  leerse: identificación → estado → detalle → brecha. Perfil y baja son dos
  columnas distintas y etiquetadas. Nada de `None` en una celda.

  Se mantiene la **coma** por defecto (RFC 4180): el fichero para mirar es el
  XLSX, así que el CSV se queda como formato de intercambio limpio. Quien lo
  necesite con punto y coma lo pide por `RASTRILLO_CSV_SEP`, `--sep` o `?sep=`.
  No se emite la línea `sep=;` de Microsoft, que añadiría una fila espuria a
  cualquier parser estándar.
- Se añaden columnas que el CSV nunca tuvo: verificabilidad, señales de
  confianza (`confidence_reasons` como etiquetas legibles, **nunca el JSON**),
  el plazo de eliminación, y el detalle de la brecha (`breach_meta`) repartido
  en cuatro columnas propias.
- `build_report(fmt)` pasa a `build_report(fmt, sep=None)`; desaparece
  `reports._CSV_COLS`. Los contratos de `json` y `pdf` no cambian.
- `glosario._cuerpo_del_objeto` tolera espacios alrededor del `=`. Con el ancla
  literal anterior, `const DATA_CLASSES_ES = {` parecía un objeto ausente.

### Security

- **Neutralización de la inyección de fórmulas** en CSV y XLSX. Una celda que
  empieza por `=`, `+`, `-` o `@` la ejecutan Excel y LibreOffice al abrir el
  fichero, y en Rastrillo el `display_name` y el nombre del sitio vienen de
  páginas ajenas: un nombre de perfil malicioso se convertía en código en la
  máquina de quien abriera su propio informe. La detección es única y
  contempla los prefijos precedidos de espacio, tabulador o retorno de carro.
  En XLSX se fuerza el tipo de celda a texto —el valor queda byte-idéntico— y
  en CSV, que no tiene tipos, se prefija un apóstrofo visible y reversible. Lo
  que se toca se cuenta y el XLSX lo declara en su resumen.

## [0.1.0] - 2026-07-31

Release inicial. Esqueleto completo y verificado.

### Added

- **El detalle de las brechas de HIBP, visible** (Paso 5). HIBP ya
  devolvía por cada brecha la fecha, cuánta gente afectó y qué tipos de
  dato se expusieron, y `discovery._register` lo tiraba: era el único dato
  del proyecto que llegaba y se descartaba. Ahora se persiste entero en la
  columna `accounts.breach_meta` (JSON, migración idempotente) y la pestaña
  "Brechas" lo muestra: fecha legible, magnitud con separadores de millares
  y los tipos de dato como chips. Las categorías se traducen con una tabla
  curada y **passthrough**: lo que no está en la tabla se muestra en inglés
  tal cual, porque traducir a ojo una categoría desconocida es inventarse
  qué se expuso. No cambia la semántica de HIBP: sigue siendo exposición en
  brecha y no cuenta activa, sigue entrando como `medium` por política y
  sigue fuera de la corroboración. Es contexto, no una señal.
- **`rastrillo/netguard.py`**: la definición ÚNICA del criterio anti-SSRF
  (rechaza loopback, privadas, link-local, reservadas y multicast). Expone
  `ip_publica`, `host_es_publico(host, puerto)` y `url_es_segura(url)`, y
  aloja el aviso del TOCTOU conocido y aceptado.
- `tests/test_informe_http.py`, `tests/test_netguard.py`,
  `tests/test_hibp_extra.py` y `tests/test_deuda_httpx2.py`: la suite pasa
  de 468 a **540 tests**.
- **El informe PDF pasa a ser un documento** (Paso 4). Hasta ahora
  `report --format pdf` era un volcado de `accounts`: una cabecera, un par de
  tablas de recuentos y una tabla larga de siete columnas con el estado teñido
  de un pastel. Servía para mirar datos por encima, no para archivar ni para
  imprimir. Ahora tiene estructura:

  **Portada** con el alcance (qué identificadores contiene el informe,
  derivados de las propias cuentas: no existe un registro de "qué se escaneó",
  así que el documento declara su contenido y no una intención pasada), los
  cinco totales en grande, y el aviso de que lleva datos personales y se generó
  en local. **Resumen** con la distribución por estado, por confianza y por
  verificabilidad —barras dibujadas con rectángulos del canvas, sin añadir
  ninguna librería de gráficos— más el recuento del audit log. **Detalle por
  cuenta** agrupado por estado, una entrada por cuenta. Y un **anexo** que
  explica los tramos de confianza y los veredictos del canario.

  Los criterios de diseño están escritos en la cabecera del módulo para que se
  puedan auditar, pero dos son de fondo y no de gusto. El primero: **ningún
  dato depende del color**. El estado, la confianza y la verificabilidad se
  imprimen siempre como palabras y cada barra lleva su número al lado, así que
  el informe se lee igual en blanco y negro o con daltonismo; el acento es
  decoración y nada más. El segundo: **la URL del perfil y la de baja son dos
  campos distintos y etiquetados** («Perfil detectado» / «Cómo darse de baja»).
  Ese error ya se arregló en la UI y no tenía ningún sentido reintroducirlo en
  papel, donde además nadie puede pasar el ratón por encima para desambiguar.

- **Unicode que no desaparece en silencio** (Paso 4). Al medir qué fuentes
  había disponibles apareció el problema de verdad, y no era estético:

  | Fuente | Codepoints | Cirílico (U+0400–U+045F) |
  |---|---|---|
  | Helvetica base-14 (WinAnsiEncoding) | ~cp1252 | **0/96** |
  | `Vera.ttf`, empaquetada en reportlab 4.5.1 | 283 | **0/96** |

  Es decir: **ninguna** de las fuentes disponibles sin añadir nada al repo
  cubre cirílico. Y reportlab **no lanza** ante un glifo que no existe —
  simplemente no dibuja nada. Rastrillo soporta 6 idiomas incluido el ruso y la
  DB real tiene cirílico (el borrador GDPR de `baby.ru`), así que el informe
  habría perdido texto sin decirlo, que es el peor fallo posible en un
  documento que se archiva como prueba de lo que se hizo.

  `pdf_fuentes.py` resuelve en tres escalones: primero una fuente del sistema
  con cobertura cirílica (DejaVu, Liberation, Noto, FreeSans, Arial, Segoe UI…,
  buscada en las rutas habituales de Linux, macOS y Windows), después **Vera**,
  que ya viene dentro de reportlab y no añade binario ni licencia al repo, y en
  último caso Helvetica. Cuando el escalón que toca no cubre un carácter, NO se
  calla: sale como `[U+0412]`, se cuenta, y el colofón dice con qué fuente se
  compuso el informe y cuántos caracteres no pudo representar. Es feo a
  propósito — un código se ve y se puede investigar; un hueco en blanco, no.

  **No se ha embebido ninguna fuente en el repo.** La contrapartida asumida es
  que el PDF no es byte-idéntico entre máquinas. Las equivalencias tipográficas
  seguras (`→` → `->`) evitan gastar un escape donde basta ASCII, pero no hay
  transliteración: `ř` no se convierte en `r`, porque eso sería alterar el dato
  en silencio, que es justo lo que se estaba arreglando.

- **El anexo lee los textos de los tooltips, no una copia de ellos** (Paso 4).
  Las explicaciones de qué significa cada señal ya existían: se escribieron con
  cuidado en el Paso 3 para decir sobre todo qué NO significan (`low` es
  «evidencia débil de que sea tuya», no «no es tuya»). Reescribirlas para el
  papel habría creado dos redacciones que se desvían con el primer retoque, y
  de las dos la del papel es la que el usuario archiva.

  `glosario.py` las lee de `static/app.js` en tiempo de render. No es una copia
  sincronizada por un test: es literalmente la misma cadena. Si alguien retoca
  un tooltip, el PDF siguiente lo dice igual sin que nadie tenga que acordarse.
  Si `app.js` deja de ser parseable, el anexo **imprime que no pudo leer las
  definiciones** en vez de inventárselas o reventar a mitad del informe.

  De paso, `STATUS_META` y `VERIFIABILITY_META` bajan de `server.py` a
  `glosario.py`, y `deletion_url()` de `server.py` a `reports.py`: el informe
  las necesita y no puede importar `server` sin arrastrar FastAPI y, por
  `jobs`, la cadena de Playwright, para generar un fichero. Ambos nombres se
  reexportan, así que nada externo cambia.

- **El «Alcance» se resume en vez de enumerarlo todo** (Paso 4). Se anota
  aparte porque fue un fallo de verdad, encontrado y corregido dentro de este
  mismo trabajo. La portada enumeraba TODOS los identificadores del informe.
  Con 300 cuentas de nombres largos el párrafo superaba la altura de la caja de
  texto y reportlab abortaba con `LayoutError`: no es que la portada saliera
  fea, es que **no había informe**.

  Salió generando la muestra de estrés, no en la suite, y por poco: con
  nombres cortos el caso medía 674 pt contra un frame de 723, a un 5% del
  límite. Ahora se listan los primeros y se dice cuántos quedan («… y 288
  más»), que además informa más que una lista de 300. La regresión que lo
  cubre usa identificadores largos y de los dos tipos, y se comprobó que falla
  sin el arreglo.

- **Memoria de decisiones de triage** (Paso 3, Entrega 1). El canario ataca una
  clase de falso positivo: sitios que responden igual para cualquiera. Los dos
  casos confirmados del dueño no son de esa clase — **Periscope** cerró en 2021
  y **HudsonRock** (`cavalier.hudsonrock.com`) es una consulta de exposición a
  infostealers, no un sitio donde se tengan cuentas. Ambos discriminan
  perfectamente entre usernames, así que ninguna heurística automática los va a
  resolver. Lo que sí lo resuelve es decirlo una vez y que la herramienta lo
  recuerde, y hasta ahora no lo recordaba: la decisión vivía en la fila de
  `accounts` y "Limpiar todas las cuentas" se la llevaba por delante.

  Tabla nueva **`discard_memory`** (migración idempotente en `db.init()`) con
  clave UNIQUE por el **par** `(source_site, identifier)` —que `mar` no sea mío
  en un sitio no dice nada sobre `je7remy` allí—, más fecha y motivo. Nada más:
  es memoria de decisiones, no un perfil. **`clear_accounts()` no la toca**
  (invariante 7, con test explícito). En un escaneo posterior,
  `discovery._register` la consulta y el hallazgo entra directamente como
  `not_mine` con el motivo `descartado_antes` — visible como chip, nunca en
  silencio. `KEEP_PLATFORMS` sigue mandando (invariante 5).

  **Deshacer obligatorio**: "Era mía" en el filtro "Descartadas" borra la
  entrada y devuelve la fila a `found`. Sin eso, un clic equivocado sería
  permanente y ni un reescaneo lo arreglaría. Solo escriben en la tabla los dos
  endpoints que el usuario pulsa a mano (`/own` con `owned=false` y
  `discard-low`); ninguna inferencia automática lo hace. No se añade ninguna
  acción destructiva nueva.

- **Señal agregada por sitio, informativa** (Paso 3, Entrega 2). Si el usuario
  descartó el mismo sitio con **2 o más identificadores distintos**, aparece un
  chip ("sitio descartado N veces"). **No mueve `confidence` en ninguna
  dirección y no dispara ninguna acción**, y eso es deliberado: el canario se
  construyó sobre una hipótesis razonable y, tras calibrarlo, sus tres
  detecciones resultaron ser errores suyos. No se conecta una señal sin medir a
  algo que escribe en la DB. Se calcula al vuelo (deshacer un descarte la baja
  sola), el umbral vive en un solo sitio (`server._UMBRAL_SENAL_SITIO`) y hay
  un barrido de 3 tramos × 4 niveles que fija que la confianza no se mueve.
- **Canario a nivel de sitio** (Paso 2B, `rastrillo/canario.py`). El paso 2A
  redujo falsos positivos mirando el username y la URL, pero no puede ver lo
  que el sitio *contesta*: un escaneo real con un username distintivo devolvió
  9 cuentas, todas en `high`, y varias no eran cuentas (HudsonRock responde a
  una consulta de exposición de credenciales, no a un perfil; Periscope cerró
  en marzo de 2021). El canario pregunta al sitio por **dos usernames falsos** y
  compara **los dos canarios entre sí**:
  - ambos 200, cuerpos casi idénticos (`difflib`, umbral 95 % sobre texto
    normalizado) y sin marcadores de "no existe" → `indiscriminado`: el sitio
    responde lo mismo a cualquiera, así que ningún hit suyo tiene valor
    probatorio. Sus filas **heurísticas** (sherlock, maigret) **bajan a `low`**
    con el motivo `canario_indiscriminado`, y "Descartar dudosas" las recoge
    sin cambios en su lógica. Las de confianza **política** (holehe `high`,
    hibp `medium`) reciben el motivo pero conservan el tramo: misma línea que
    trazó el paso 2A —la señal ajusta estimaciones, nunca políticas— y además
    holehe ni siquiera consulta la página de perfil, así que el veredicto no
    es evidencia sobre esa fila.
  - alguno 4xx, o marcador de "usuario no encontrado" en el cuerpo (los 6
    idiomas del proyecto) → `discrimina`: el hit se respeta. **Nunca sube la
    confianza**, solo anota el motivo.
  - red caída, timeout o URL no construible → `indeterminado`: confianza
    intacta y **no se cachea**, para reintentar en el próximo escaneo.

  Comparar canarios entre sí y no contra el perfil real tiene tres
  consecuencias: el veredicto es del **sitio**, no del identificador, así que
  se cachea en `~/.rastrillo/canario.json` con TTL
  (`RASTRILLO_CANARIO_MAX_AGE_DAYS`, default 30) y los escaneos posteriores
  cuestan **cero peticiones**; nunca se pide la URL del perfil real, así que no
  le anunciamos a un sitio ajeno que alguien mira ese username; y el chequeo de
  marcadores evita el falso veredicto en los sitios que devuelven 200 con un
  mensaje de error.

  Coste: **exactamente 2 peticiones por sitio no cacheado**, no por fila. Solo
  hits con `url`, lo que excluye estructuralmente holehe y hibp. Guarda
  anti-SSRF de `resolver._is_safe_url`, throttle por dominio con
  `RASTRILLO_PROBE_DELAY` y pool acotado con `RASTRILLO_RESOLVER_WORKERS` (sin
  variables nuevas). Corre en `jobs.scan_async` entre el discovery y el
  auto-resolver. Cero dependencias nuevas.

- **`rastrillo canario URL|HOST [--user U]`** — subcomando de debug que corre
  el canario contra un solo sitio y vuelca la evidencia (status de cada falso,
  bytes, marcadores encontrados, similitud, veredicto y por qué) **sin tocar la
  DB ni la caché de veredictos**. Es con lo que se valida el mecanismo a mano.
- **Eliminación programada con cuenta regresiva** (FASE 4): cuando una
  plataforma elimina tras un plazo ("en 30 días"), el usuario lo registra
  desde `user_done`, `manual` o `semi_auto`. Columnas `deletion_eta` y
  `deletion_started_at` (migración idempotente), estado `pending_deletion`,
  y filtro propio "Plazos" en el dashboard. La cuenta regresiva (días
  restantes + fecha final + barra de progreso) la computa el servidor con
  el helper puro `deletion_progress` y la UI solo la renderiza. Endpoints
  `POST /api/accounts/{id}/schedule-deletion` (`{days}` o `{eta}` con
  preflight de propiedad 412 + audit), `…/cancel-deletion` y
  `…/verify-deletion`. **Al vencer NO se auto-marca `deleted`**: la fila
  pasa a "plazo vencido / presunta eliminación" y ofrece **Verificar**, que
  reusa `engine.revisit_profile` (con guard anti-SSRF). En `dry_run`,
  Verificar hace la lectura pero no muta estado.
- Histórico de Domain Intelligence cableado en el dashboard: desplegable
  de dominios ya analizados (`GET /api/domain/history`) y recarga del
  último informe guardado al cargar la página (`GET /api/domain/report`),
  sin volver a la red.
- **Histórico de dominios colapsable y limpiable.** Los informes guardados
  se pintan como lista de tarjetas colapsables (la más reciente expandida,
  el resto plegadas) en lugar del desplegable anterior. La cabecera de cada
  tarjeta, visible también plegada, muestra dominio, fecha y un resumen de
  una línea (nº de registros DNS · nº de correlaciones · registrador). El
  toggle es un `<button>` real con `aria-expanded`/`aria-controls`, así que
  el teclado funciona sin JS extra; el cuerpo se oculta con el atributo
  `hidden`. Controles globales "Colapsar todo" / "Expandir todo" y
  persistencia del estado plegado por dominio en `localStorage`
  (`rastrillo.domain.collapsed`, con purga de dominios ya borrados).
  Nuevos endpoints `POST /api/domain/report/delete` (body `{domain}`; 400
  dominio inválido, 404 accionable si no está en el histórico) y
  `POST /api/domain/history/clear` (→ `{ok, deleted}`), respaldados por
  `db.delete_domain_report()` y `db.clear_domain_reports()`. El clear hace
  `snapshot_db()` antes de borrar, igual que `clear_accounts`. Ambos van por
  **POST y no DELETE**: el `auth_middleware` solo exige token en POST y en
  los GET de `/api/*`, así que un DELETE entraría sin token. En la UI, cada
  borrado pasa por el modal de confirmación (el de "Limpiar historial" dice
  cuántos informes se van a borrar). Cero dependencias nuevas.
- Descubrimiento multi-fuente: Sherlock y Holehe siempre; Maigret y
  HaveIBeenPwned opcionales.
- Resolver por capas: recetas JSON → directorio JustDeleteMe → web search
  Anthropic → sondeo de paths → agente IA → borrador GDPR.
- Dashboard local FastAPI en `http://127.0.0.1:8765` con token de auth.
- Allowlist de hosts contra DNS rebinding.
- SSRF guard en el resolver (solo HTTPS, IPs públicas).
- Plantillas GDPR iniciales y de follow-up en 6 idiomas (EN, ES, RU,
  PT-BR, FR, DE), con detección por TLD.
- Engine Playwright sobre perfil persistente de Chromium, resumible
  (persistencia de `current_step` + hash de receta).
- Aprendizaje sobre la marcha: secuencias exitosas serializadas como
  recetas en `~/.rastrillo/recipes/` (omitiendo pasos `fill`).
- Modo simulación (`RASTRILLO_DRY_RUN`).
- Informes exportables en JSON, CSV y PDF (endpoint `/api/report` y CLI
  `rastrillo report`).
- Audit log append-only en `~/.rastrillo/audit.json` con rotación por
  tamaño.
- Backup automático de la DB antes de borrados masivos.
- 5 recetas de ejemplo: Reddit, Tumblr, Pinterest, Discord, Spotify, más
  plantilla de anonimizado.
- Endurecimiento contra falsos `deleted`: revisita HTTP a la
  `profile_url`, detección de redirect a login, keywords multi-idioma de
  cierre.
- Onboarding en una sola pasada (marker en `~/.rastrillo/onboarded.json`).
- Pool acotado del auto-resolver (`RASTRILLO_RESOLVER_WORKERS`,
  default 5, rango [1, 16]).
- Throttle por dominio en el resolver (`RASTRILLO_PROBE_DELAY`,
  default 1.5 s).
- Refresh automático del directorio JustDeleteMe si la caché supera
  `RASTRILLO_DIR_MAX_AGE_DAYS` (default 30).
- Empaquetado para AUR (`packaging/aur/PKGBUILD`).
- Instaladores idempotentes (`install.sh` para Linux/Mac,
  `install.ps1` para Windows) y wrappers sin activar venv
  (`rastrillo.sh`, `rastrillo.ps1`, `rastrillo.cmd`, `rs.sh`).
- Suite de **165 tests** con `unittest` de stdlib (sin deps nuevas).
- CI en GitHub Actions: tests sobre Python 3.10/3.11/3.12 y
  `pip-audit` sobre `requirements.lock`.

**Inteligencia de dominio**

- **Domain Intelligence** (`rastrillo/domain_intel.py`): recon OSINT
  defensivo sobre un dominio. WHOIS (registrador, fechas de
  creación/expiración/actualización, estados, nameservers, registrant),
  DNS (A/MX/NS/TXT) y correlación heurística de proveedores/servicios
  (MX→correo, NS→DNS/hosting, SPF/verificaciones TXT→SaaS), cada
  inferencia etiquetada con confianza y evidencia. Endpoints
  `POST /api/domain/analyze` y `GET /api/domain/report`, tabla
  `domain_reports` en SQLite y una vista resumida en el dashboard.
  Pensado **solo para infra propia o que tienes permiso de auditar**.
- Guard anti-SSRF para el socket WHOIS:43 (`_host_resolves_public`),
  con el mismo criterio que el del resolver: solo IPs públicas.

### Changed

- **El criterio anti-SSRF deja de estar duplicado** (Paso 5). Estaba
  escrito dos veces —`resolver._is_safe_url` y
  `domain_intel._host_resolves_public`— con el bucle de comprobación de IPs
  **idéntico byte a byte**. Ahora ambas son envoltorios de una línea sobre
  `netguard`, conservando exactamente sus firmas y semánticas (una exige
  `https://`, la otra valida un host a secas para TCP/43), y
  `canario._url_segura_por_defecto` apunta al helper directamente.
  Refactor puro: `engine.py` no se tocó y los cinco ficheros de tests que
  ya ejercitaban la guarda pasan **sin editar una línea**.
- El aviso `StarletteDeprecationWarning` de `httpx2` se silencia en un solo
  punto (`tests/helpers.auth_client`), con el análisis completo escrito
  allí. Tiene que ser un `catch_warnings` local: `TextTestRunner.run` hace
  `warnings.simplefilter(...)`, que borra la lista de filtros entera, así
  que un filtro global a nivel de import no sobrevive. **No oculta la
  rotura futura**: el filtro casa por mensaje, y el día que starlette
  retire `httpx` lanzará un `RuntimeError` que ningún filtro tapa —
  `tests/test_deuda_httpx2.py` vigila ambas cosas.
- `tests/test_rebuild_legacy.py`: el test que comprueba que una DB legacy
  se migra por `ALTER` (y no recreando la tabla) deducía el nombre de la
  última columna escribiéndolo a mano. Ahora lo deriva de `init()`, para
  que no vuelva a romperse al añadir una columna sin que haya cambiado
  nada del comportamiento que mide.
- **La UI dice qué va a pasar** (Paso 3, Entrega 3). Los chips explicaban de
  dónde salía la confianza, pero nada explicaba qué hacía cada botón:
  - **Tooltip por motivo** (`REASON_TIP`): qué señal lo produjo y qué **no**
    significa. Un test barre el código fuente en busca de motivos registrados y
    falla si alguno se queda sin etiqueta o sin frase (y también si sobra el
    texto de un motivo ya retirado).
  - **Tooltip por tramo** (`CONF_TIP`). En particular `low` deja claro que es
    "evidencia DÉBIL de que sea tuya", **no** "no es tuya": la etiqueta roja se
    leía al revés, y ese malentendido es exactamente el que encadenaba
    descartes indebidos.
  - **El descarte masivo dice el número exacto** de filas que va a marcar, una
    muestra de cuáles y que la acción es reversible. Antes se pulsaba un botón
    que escribía `not_mine` sin saber sobre cuántas cuentas. El conteo sale del
    endpoint nuevo `GET /api/accounts/discard-low/preview`, que **solo lee** y
    comparte el criterio de selección con el POST que escribe, así que el
    número prometido no puede desviarse de lo que ocurre.
  - "Limpiar todas las cuentas" también dice cuántas son y qué **no** se lleva
    por delante (incluida la memoria de descartes).
  - `showConfirm` ahora respeta los saltos de línea: `confirmBodyHtml` escapa el
    cuerpo **entero** y solo después convierte `\n` en `<br>`. Ese orden es lo
    que hace segura la conversión; los cuerpos con `\n\n` salían pegados.

- **El canario ya no toca la confianza** (Paso 2C, Entrega 1). Bajar a `low` un
  sitio `indiscriminado` metía dos conceptos distintos en la misma variable:
  `low` significa "evidencia débil de que esta cuenta sea tuya",
  `indiscriminado` significa "la respuesta de este sitio no vale nada, no puedo
  verificar ni a favor ni en contra". Como `discard-low` marca `not_mine`, la
  cadena quedaba en **inverificable → low → descartar → "no es mía"**: una
  afirmación sobre la PROPIEDAD de una cuenta derivada de una señal que no
  habla de propiedad. En un escaneo real cayeron así Steam y Duolingo, dos
  sitios donde el dueño sí podía tener cuenta.

  El veredicto vive ahora en su propia columna, **`accounts.verifiability`**
  (`indiscriminado` | `discrimina` | `indeterminado`; NULL = no evaluado, que
  no es lo mismo que `indeterminado`), con migración idempotente en
  `db.init()`. `confidence` vuelve a depender **solo** de `discovery._register`
  y la corroboración de 2A. El motivo se sigue anotando en
  `confidence_reasons` como evidencia, pero **no mueve el tramo**.
  `discard-low` mantiene su criterio (`confidence=='low'`) y por tanto **ya no
  barre inverificables**. Nueva pestaña "No verificables" en el dashboard para
  verlos agrupados; deliberadamente **sin acción en lote** asociada.

  Invariante nuevo, con test de barrido sobre 54 filas (fuente × tramo ×
  veredicto): **el canario no modifica `confidence` en ninguna circunstancia**,
  ni hacia arriba ni hacia abajo.

- **Canario calibrado contra el catálogo de Sherlock** (Paso 2C, Entrega 2,
  `rastrillo/catalogo.py`). Dos veredictos falsos, encontrados sondeando los
  sitios de un escaneo real:
  - **Steam y HudsonRock** decían que el usuario no existía y no los
    escuchábamos: `The specified profile could not be found` y `This username
    is not associated…` no casaban con ninguna frase de la lista, que asumía
    "not found" contiguo. Los dos daban `indiscriminado` siendo sitios que sí
    discriminan — HudsonRock, además, es el caso que motivó el módulo entero.
  - **Duolingo** se sondeaba en la URL equivocada. El `data.json` de Sherlock
    define `urlProbe` para los sitios que se comprueban contra una API; la
    `url` visible de Duolingo es una SPA que devuelve el mismo HTML para
    cualquiera y que Sherlock nunca consulta. Medíamos algo distinto de lo que
    produjo el hit.

  Ahora el canario lee el catálogo del paquete instalado con
  `importlib.resources` (sin duplicar la tabla ni añadir dependencias:
  `sherlock-project` ya era requisito): usa `urlProbe` como plantilla cuando
  existe y `errorMsg` como marcadores específicos del sitio, además de la lista
  genérica multi-idioma —que también se amplía con la familia "could not be
  found" en los 6 idiomas—. El emparejamiento fila ↔ entrada es por forma de la
  URL, no por nombre ni por host, para desambiguar casos como
  `Steam Community (User)` vs `(Group)`.

  Lo que **no** se "arregla" a propósito: un sitio que renderiza el perfil en
  el cliente y no tiene `urlProbe` sigue dando `indiscriminado`, porque es la
  verdad sobre su respuesta. Sin cobertura: los hits de **Maigret**, que trae
  su propio catálogo con otro esquema.

- **El canario distingue un bloqueo de un fallo de red** (Paso 2C, Entrega 3).
  `resolver._http_get` colapsaba 403/429, timeout, DNS y conexión rechazada en
  un único `None`, así que un sitio que nos rechaza y un sitio caído se veían
  idénticos y el informe decía "sin respuesta (timeout, 403/429 o red caída)",
  que vale para todo y no informa de nada — baby.ru devuelve 403 con una página
  "Security Check". Nuevo `resolver._http_get_detallado` que devuelve
  `(resultado, motivo)` con `MOTIVO_BLOQUEADO` / `MOTIVO_RED` / `MOTIVO_SSRF`;
  `_http_get` queda como envoltorio y **su contrato no cambia** (sus tres
  callers solo distinguen "hubo respuesta" de "no la hubo"). El canario reporta
  el `indeterminado` diciendo cuál fue, con motivos `canario_bloqueado` /
  `canario_sin_respuesta`, chip en la UI y causa en el subcomando de debug.
  Ninguno de los dos se cachea. **No se cambian cabeceras ni se intenta
  esquivar el bloqueo**: identificarse honestamente y ser rechazado es
  información válida.
- **Menos falsos positivos en el discovery, sin una sola petición nueva**
  (Paso 2A). Tres cosas:
  - El bump de confianza por URL era `identifier.lower() in url.lower()`
    sobre la URL completa, así que casaba dentro del dominio: el username
    `ana` subía de tramo por `https://banana.com/u/xyz`. Ahora
    `_identificador_en_url()` parsea con `urllib.parse.urlsplit` y solo
    concede señal si el identificador aparece **en el path o el query string
    como segmento completo**, o **como etiqueta más a la izquierda del host
    con igualdad exacta** (`jeremy.tumblr.com` — señal legítima que no
    queríamos perder). La frontera de segmento la impone
    `_match_con_frontera()`: separador (`/ - _ . = ? &`) o extremo de cadena a
    cada lado, recorriendo todas las apariciones. Sin ella el bug reaparecía
    movido de componente (`mar` casaba en `/marca-noticias/123`), y los
    identificadores de 1-2 caracteres bumpeaban por casualidad. La escala base
    (longitud / distintividad) no se tocó. Efecto buscado: hallazgos que antes
    salían `medium` ahora salen `low`, así que "Descartar low" atrapa más. En
    un lote de ejemplo con 20 hits realistas, 10 filas bajan de tramo
    (9 `medium→low`, 1 `high→medium`) y las candidatas a `discard-low` pasan
    de 2 a 11.
  - **Corroboración entre fuentes.** Un `source_site` que sale de dos tipos
    de identificador distintos (un email vía holehe/hibp + un username vía
    sherlock/maigret) son dos caminos independientes al mismo sitio: sube un
    tramo a la fila heurística. Se calcula en `_corroborar_entre_fuentes()`,
    un pase final de `discover()` sin red, porque la DB es el único sitio
    donde existe el conjunto completo y ya deduplicado. **No fusiona filas**:
    la unicidad sigue siendo `(source_site, identifier)`. Sherlock+maigret,
    que sí colapsan en una fila, quedan anotados en el `upsert` como señal
    **débil** que no mueve el tramo (catálogos de sitios que se solapan, así
    que coincidir no es independiente). Nada pasa de `high`, y las fuentes de
    confianza política (holehe `high`, hibp `medium`, manual `high`) reciben
    el motivo pero nunca cambian de tramo.
  - **HIBP solo corrobora cuando la brecha es de un sitio real.** Las brechas
    con `Domain` vacío ya se descartaban (es el caso de los volcados agregados
    tipo "Collection #1": no hay sitio al que ir a borrar), y ahora se registra
    en el log por qué. Las que sí traen dominio pero llevan `IsSpamList`,
    `IsFabricated` o `IsVerified=false` entran al discovery marcadas con el
    motivo `hibp_no_sitio` y quedan **excluidas de la corroboración**: no
    corroboran ni son corroboradas. Siguen visibles para que las revises —
    el filtro es de la señal, no del hallazgo.
  - **La confianza deja de ser una caja negra.** Columna nueva
    `accounts.confidence_reasons` (JSON `[{code, desc}]`, serializada igual
    que `action_meta`, migración idempotente en `db.init()`); `confidence`
    se queda como estaba. `GET /api/accounts` la devuelve ya parseada y la
    fila del dashboard pinta chips cortos (`.chip-xs`) con la descripción en
    el `title` — texto plano, sin HTML. Códigos: `tramo_distintivo`,
    `tramo_corto`, `tramo_muy_corto`, `id_vacio`, `bump_path`,
    `bump_subdominio`, `corrob_misma_fila`, `corrob_cruzada`,
    `fuente_holehe`, `fuente_hibp`, `fuente_manual`.

  Cero dependencias nuevas (`urllib.parse` es stdlib) y cero red: la
  verificación activa (canario, soft-404) es el paso 2B.

### Fixed

- **La descarga de informes desde el dashboard no producía un PDF** (Paso
  5). El botón pedía el informe con `<a href="/api/report?format=pdf"
  download>`, y una navegación de ancla **no puede** enviar cabeceras
  propias: la petición salía sin `X-Rastrillo-Token`, el middleware
  respondía 401 con cuerpo JSON y el atributo `download` guardaba ese JSON
  de error en un fichero con nombre de PDF. El generador y el endpoint
  siempre estuvieron bien — el fallo estaba entero en la capa de
  presentación. Ahora la descarga va por `fetch` con el token, recibe un
  `Blob` y dispara la descarga desde un objeto local; un error se muestra
  como aviso y **nunca** se guarda como fichero. La autenticación no se
  relajó en ninguna dirección: no se habilitó el token por query ni se
  eximió la ruta del middleware.
- La suite tenía un hueco con la forma exacta del bug: la única prueba de
  informes llamaba a `reports.build_report()` **directamente**, así que se
  ejercitaba el generador y nunca el transporte. `tests/test_informe_http.py`
  cubre ahora `GET /api/report` (los tres formatos, el `Content-Disposition`,
  el 401 sin token) e impide que el HTML vuelva a pedir el informe con un
  `<a href>`.
- **Pérdida de datos al migrar una DB legacy con `UNIQUE`** (Paso 2C, Entrega
  4, `db.py`). El rebuild que quita el `UNIQUE(platform, identifier)` heredado
  copiaba una lista de columnas escrita a mano que se había quedado atrás
  respecto al esquema: dejaba fuera `sent_at`, `deletion_eta` y
  `deletion_started_at`, de modo que migrar borraba **en silencio** la fecha de
  envío de las solicitudes GDPR y los plazos de eliminación en curso.

  Dos arreglos. El rebuild corre ahora **antes** de los `ALTER TABLE` de
  `init()` (la tabla recreada nace con el esquema completo), y sobre todo la
  lista de columnas se calcula **en tiempo de ejecución** como intersección de
  lo que hay con el esquema actual. Además el esquema de `accounts` pasa a
  tener una **única** definición (`db._ACCOUNTS_COLUMNS`), de la que salen
  tanto el `CREATE TABLE` como la tabla temporal del rebuild: la duplicación
  era la causa raíz y ya no puede volver a divergir. Un test lo vigila
  comparando los `ADD COLUMN` de `init()` contra la lista.

- **`rastrillo reparar-confianza`** (nuevo subcomando, un solo uso). Restaura
  el tramo de las filas que el canario del paso 2B bajó a `low`. Hacía falta un
  paso explícito porque un reescaneo **no** las recupera: `db.upsert_account`
  hace `return row["id"]` en cuanto la fila existe, así que el `confidence` que
  recalcula `discovery._register` nunca llega a escribirse. El tramo se
  reconstruye sin red desde los motivos persistidos (la escala de
  `_sherlock_confidence` leída al revés). Idempotente, ámbito estrecho (solo
  filas `low` + `canario_indiscriminado` + fuente heurística), y **sin
  `--aplicar` solo enseña el plan**; con él hace `snapshot_db()` antes de
  escribir.
- **`@`, `~` y `#` faltaban en `_SEPARADORES_URL`.** La fila de TikTok
  (`tiktok.com/@usuario`) no obtenía el motivo "coincide en la ruta" porque el
  `@` previo no validaba como frontera de segmento, y el bump se perdía. Con un
  username corto eso cuesta un tramo entero. De paso, `_identificador_en_url`
  ahora también mira el **fragmento** (`urlsplit` lo saca del path, así que se
  perdía entero) con las mismas reglas de frontera: hay SPAs que publican el
  perfil en `/#/user/nombre`.

- **El triage enseñaba una URL que no era la del hit.** Freelancer aparecía
  como `.../users/settings.php#AccountSettings` y Steam como
  `help.steampowered.com/.../HelpDeleteAccount`, ambas bajo un enlace
  etiquetado "perfil". Eran URLs de **borrado**: el resolver las escribía
  encima de `profile_url` en los **cuatro** caminos que tocan esa columna
  (`jobs._resolve_one`, `server._apply_resolution`, `process-all-auto` y
  `engine.run_account` por la rama sin receta). Como los chips de motivo se
  calculan sobre la URL del hit, a ojo parecía que el chip mentía. Ahora los
  cuatro pasan por `db.backfill_profile_url()`, que solo rellena **si está
  vacía** —el caso de holehe y hibp, que nunca traen URL—, y la de borrado
  viaja como `deletion_url` en `/api/accounts` (computada desde `action_meta`)
  con su propio enlace, "cómo darse de baja". Sin cambios de esquema. En
  `engine` el arreglo además refuerza la verificación anti-falso-`deleted`:
  `_confirm_and_seal` revisita el perfil real en vez de la página de baja.
- El botón "Enviado" (`mark-sent`) ahora tiene estado de carga (spinner),
  el polling no pisa la interacción en curso, y maneja el 412 de propiedad
  abriendo el modal de confirmación. Además mandaba un body sin `action`
  que provocaba un 422 silencioso; corregido.
- Guard anti-SSRF en `engine.revisit_profile` (reusa `resolver._is_safe_url`):
  antes hacía un GET sin filtrar el destino. `ALLOWED_HOSTS` se normaliza
  a minúsculas.

### Security

- Cero contraseñas guardadas en disco.
- Token de auth obligatorio en TODOS los POST y en todos los GET de
  `/api/*`.
- Human-in-the-loop obligatorio para CAPTCHA, 2FA y confirmaciones.

[Unreleased]: https://github.com/je7remy/Rastrillo/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/je7remy/Rastrillo/releases/tag/v0.1.0

### Dependencies

- Añadida **`dnspython>=2.6`** (única dep nueva), necesaria porque la
  stdlib no resuelve MX/NS/TXT. WHOIS se hace por socket TCP/43 con la
  stdlib (cero deps). Decisión y alternativas descartadas documentadas en
  la cabecera de `rastrillo/domain_intel.py` y en `CONTRIBUTING.md`.
  Reflejada en `pyproject.toml`, `requirements.txt` y `requirements.lock`
  (regenerado con `pip-compile`).

### Variables de entorno

- `RASTRILLO_WHOIS_TIMEOUT` (default 10 s) y `RASTRILLO_DNS_TIMEOUT`
  (default 5 s): timeouts por consulta del módulo Domain Intelligence.

[Unreleased]: https://github.com/je7remy/Rastrillo/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/je7remy/Rastrillo/releases/tag/v0.1.0
