# Changelog

Todos los cambios notables en este proyecto se documentan en este
fichero.

El formato sigue [Keep a Changelog](https://keepachangelog.com/es-ES/1.1.0/)
y el proyecto se adhiere a [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Changed

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

### Fixed

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

### Added

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

### Fixed

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

### Changed

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

### Added

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

### Fixed

- El botón "Enviado" (`mark-sent`) ahora tiene estado de carga (spinner),
  el polling no pisa la interacción en curso, y maneja el 412 de propiedad
  abriendo el modal de confirmación. Además mandaba un body sin `action`
  que provocaba un 422 silencioso; corregido.
- Guard anti-SSRF en `engine.revisit_profile` (reusa `resolver._is_safe_url`):
  antes hacía un GET sin filtrar el destino. `ALLOWED_HOSTS` se normaliza
  a minúsculas.

### Added (Domain Intelligence)

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

## [0.1.0]

Release inicial. Esqueleto completo y verificado.

### Added

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

### Security

- Cero contraseñas guardadas en disco.
- Token de auth obligatorio en TODOS los POST y en todos los GET de
  `/api/*`.
- Human-in-the-loop obligatorio para CAPTCHA, 2FA y confirmaciones.

[Unreleased]: https://github.com/je7remy/Rastrillo/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/je7remy/Rastrillo/releases/tag/v0.1.0
