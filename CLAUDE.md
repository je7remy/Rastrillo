# CLAUDE.md

Notas para que Claude Code (o yo mismo en tres meses) se ponga al día rápido.

## Qué es Rastrillo

Herramienta local para que el dueño borre o anonimice sus propias cuentas
online de una sentada. Le das usernames y correos; descubre dónde tienes
cuentas con Sherlock, Holehe y, si está instalado, Maigret. Decide cómo
borrar cada una y conduce el flujo en un Chromium persistente. Cuando una
plataforma exige humano (CAPTCHA, 2FA, "confirma con tu contraseña"), pausa
y te lo pide. Persiste progreso y reanuda.

Stack: Python 3.10+, Playwright con Chromium persistente, FastAPI + uvicorn
para el dashboard local en `127.0.0.1:8765`, SQLite. Se desarrolla
principalmente en Arch/Hyprland pero también corre en Windows y Mac.

## Invariantes — no los rompas

1. Nunca almacenar contraseñas. La autenticación va por el perfil
   persistente de Chromium en `~/.rastrillo/browser-profile`; el humano se
   loguea una vez por sitio. Si una tarea sugiere guardar credenciales,
   rechaza y propón la vía del perfil persistente.
2. Human-in-the-loop obligatorio para CAPTCHA, 2FA y confirmación final.
   No intentes "resolver" CAPTCHAs ni evadir detección de bots. El paso
   `pause` y el estado `awaiting_user` existen para eso.
3. Privacidad local. Lo único que sale a internet es: las webs donde vas a
   borrar, la descarga puntual del directorio JustDeleteMe, las llamadas
   a Anthropic con estructura de página + texto visible (nunca cookies ni
   contraseñas), y las consultas a HIBP cuando hay clave.
4. Solo cuentas del propio usuario. Antes de cualquier acción destructiva
   el endpoint exige `confirm_owned=true` o que la cuenta esté ya marcada
   como propia.
5. Las plataformas en `config.KEEP_PLATFORMS` (tiktok, instagram, linkedin,
   github) van siempre a `skipped`. No entran al flujo de borrado.
6. **El canario no modifica `confidence` en ninguna circunstancia.** Confianza
   ("¿es mía esta cuenta?") y verificabilidad ("¿sirve la respuesta de este
   sitio para comprobar algo?") son ejes distintos y viven en columnas
   distintas (`confidence` / `verifiability`). Mezclarlos encadenaba
   inverificable → `low` → "Descartar dudosas" → `not_mine`, que afirma algo
   sobre la propiedad de una cuenta a partir de una señal que no habla de
   propiedad. `canario._aplicar_a_fila` es el único punto de escritura y no
   toca `confidence` en ninguna rama; lo fija un test de barrido de 54 filas.
7. **La memoria de descartes (`discard_memory`) NO se va con
   `clear_accounts()`.** Es el punto entero del Paso 3: la decisión "esto no es
   mío" la tomó el usuario y no es un hallazgo del escáner, así que no se borra
   con los hallazgos. Si se fuera, cada limpieza obligaría a re-triar los
   mismos falsos positivos (Periscope, HudsonRock). Y **nada escribe ahí por
   inferencia**: solo los dos endpoints que el usuario pulsa a mano
   (`/own` con `owned=false` y `discard-low`). Toda decisión guardada tiene que
   ser deshacible desde la UI ("Era mía"), porque una memoria sin deshacer
   convierte un error de un clic en permanente.
8. **No regeneres `requirements.lock`** salvo que el usuario lo pida
   explícitamente en ese mismo mensaje. `pip-compile` re-resuelve TODAS las
   pins contra lo que haya en el índice en ese momento y puede revertir en
   silencio un bump aplicado a mano — ya pasó con `pillow` (12.3.0 → 12.2.0,
   deshaciendo 20 avisos de seguridad). El comentario de `ci.yml` que invita a
   regenerarlo ante un CVE describe una decisión del mantenedor, no una tarea
   que puedas tomarte por tu cuenta. Instalar DESDE el lock
   (`pip install -r requirements.lock`) no lo modifica y es siempre correcto.

## Modelo de borrado: resolver en capas

Para cada cuenta el resolver prueba las capas en orden y se queda con la
primera que produzca una Resolution accionable. Cada hallazgo se cachea en
`~/.rastrillo/discovered.json` para no repetir trabajo entre runs.

| # | Capa | Cómo | Devuelve |
|---|---|---|---|
| 1 | Receta JSON | `rastrillo/recipes/*.json` u `~/.rastrillo/recipes/` | `kind=auto` (override) |
| 2 | Directorio JustDeleteMe | `directory.lookup(host)` | `auto`/`semi_auto` |
| 3 | Web search | tool `web_search` de Anthropic, query en idioma del TLD | `auto`/`semi_auto` |
| 4 | Probe paths | GET a paths localizados (`/settings`, `/cuenta`, `/удалить`…) | `semi_auto` |
| 5 | Agente IA | bucle ver/decidir/pulsar (`ai_assist.run_agent`) sobre la URL | `deleted/anonymized/manual` |
| 5 | GDPR mail | extrae contacto de privacidad, redacta solicitud Art. 17 en el idioma del sitio | `email_draft` (siempre) |

Hay tres `kind` posibles en una Resolution:

- `auto` — el motor lo procesa solo (receta o agente IA).
- `semi_auto` — link directo; el usuario hace 1 clic.
- `email_draft` — borrador GDPR listo (mailto + copia al portapapeles).

Sin `ANTHROPIC_API_KEY` se saltan las capas 2 (web search) y 5 (agente IA).
El resolver sigue produciendo algo: `semi_auto` por probe o `email_draft`
con la plantilla GDPR estática en uno de los 6 idiomas soportados.

## Mapa del código

`cli.py` — entrypoint. Sin argumentos arranca el dashboard, abre el
navegador con el token en la URL y queda corriendo. Subcomandos `list`,
`run`, `canario` y `reparar-confianza` son auxiliares de debug/mantenimiento. `rastrillo canario URL|HOST
[--user U] [--token T --token T]` corre el canario contra UN sitio y vuelca
la evidencia cruda (status de cada falso, bytes, marcadores, similitud,
veredicto y por qué) SIN escribir en la DB ni en la caché de veredictos —
es con lo que se valida el mecanismo a mano contra un sitio concreto. Instalado como `rastrillo` y `rs` vía
`pyproject.toml`. Los shims viven en `.venv/(bin|Scripts)/` y esa carpeta
no está en el `PATH` global del usuario: para invocar el comando hay que
usar los wrappers (`rastrillo.sh`/`rastrillo.ps1`/`rastrillo.cmd`),
activar el venv, o instalar global con `pipx install .`.

`rastrillo/config.py` — rutas bajo `~/.rastrillo`, `KEEP_PLATFORMS`,
config de IA, generación del token de auth, flag dry-run mutable, helpers
de onboarding (`is_onboarded`, `mark_onboarded`).

`rastrillo/db.py` — SQLite. Estados completos:
`found, queued, in_progress, awaiting_user, deleted, anonymized,
user_done, semi_auto, email_draft, pending_deletion, manual, skipped,
failed, not_mine, dry_run`. Migraciones idempotentes (`recipe_hash`,
`source_site`, `action_meta`, `confidence`, `confidence_reasons`,
`verifiability`, `owned`, `sent_at`, `deletion_eta`, `deletion_started_at`) en
`init()`. La unicidad de cuentas se hace en código por
`(source_site, identifier)` — NO con UNIQUE en SQL — para no colapsar Reddit y
RedditGifts.

El esquema de `accounts` tiene UNA sola definición, `_ACCOUNTS_COLUMNS`, de la
que `_ddl_accounts()` genera tanto el `CREATE TABLE` como la tabla temporal del
rebuild de DBs legacy. **No lo dupliques**: antes había una copia literal
dentro de `_REBUILD_WITHOUT_UNIQUE`, se quedó atrás y migrar una DB legacy con
UNIQUE perdía en silencio `sent_at`, `deletion_eta` y `deletion_started_at`.
Hoy `_rebuild_sin_unique` copia la INTERSECCIÓN calculada en runtime y corre
ANTES de los `ALTER TABLE` (paso 2 de `init()`, no 3), de modo que la tabla
recreada nace con el esquema completo. Si añades un `ADD COLUMN` a `init()`,
añade también la columna a `_ACCOUNTS_COLUMNS`; hay un test que compara ambas
listas.

`confidence` y `verifiability` son ejes SEPARADOS: ver el invariante 6.
`snapshot_db()` copia la DB a `~/.rastrillo/backups/` antes de
`clear_accounts`.

**Memoria de descartes** (Paso 3): tabla `discard_memory`, clave UNIQUE por
`(source_site, identifier)` normalizado a minúsculas por `_clave_descarte`. El
PAR y no el sitio suelto: que `mar` no sea mío en un sitio no dice nada sobre
`je7remy` allí. Guarda solo par + fecha + motivo. Helpers:
`remember_discard` (upsert idempotente), `forget_discard` (el deshacer),
`get_discard`, `list_discards`, `count_discards_by_site` y
`clear_discard_memory`. **`clear_accounts()` no la toca** (invariante 7); hay
test explícito. Este último helper existe para que un futuro "reset total"
la incluya sin tener que redescubrir la tabla — hoy no lo llama ningún
endpoint, porque el Paso 3 tenía prohibido crear acciones destructivas nuevas.
`discovery._register` la consulta y mete el hallazgo como `not_mine` con el
motivo `descartado_antes`; `KEEP_PLATFORMS` sigue mandando (invariante 5).
`profile_url` guarda la URL DEL HIT — la que produjo el descubrimiento y
sobre la que se calcularon los motivos de confianza. `backfill_profile_url()`
la rellena SOLO si está vacía (holehe/hibp nunca traen URL). Los CUATRO
caminos que escribían ahí la URL de borrado del resolver pasan hoy por ese
helper (`jobs._resolve_one`, `server._apply_resolution`,
`process-all-auto` y `engine.run_account` por la rama sin receta); si añades
un quinto, úsalo también. La URL de borrado viaja en `action_meta.url` y
llega a la UI como el campo computado `deletion_url` de `GET /api/accounts`.
Pisarla hacía que el triage enseñara una URL de borrado bajo la etiqueta
"perfil" mientras los chips describían otra, y a ojo parecía que el chip
mentía. En `engine` además sale ganando la lógica anti-falso-`deleted`:
`_confirm_and_seal` revisita el perfil real en vez de la página de baja.
`confidence_reasons` guarda el POR QUÉ de la confianza como JSON
`[{code, desc}]`, serializado igual que `action_meta`; helpers
`parse_reasons` (tolerante: [] si falta o está corrupto), `dump_reasons` y
`merge_reasons` (une sin repetir `code`). `upsert_account` es donde se
detecta el duplicado, así que es donde anota la corroboración de misma fila
(`corrob_misma_fila`): sherlock+maigret colapsan en una fila y esa señal se
perdería. Solo anota el motivo, NO mueve el tramo — por construcción ese
caso solo puede ser dos buscadores de username, cuyos catálogos se solapan.

`rastrillo/discovery.py` — wrappers a Sherlock (CSV), Holehe (CSV),
Maigret (JSON, opt-in si está en PATH) y HIBP (opt-in con clave).
Log "crudo -> guardado" por cada fuente.

`_register()` es el ÚNICO sitio donde se decide el tramo BASE de un hit:
holehe=high, hibp=medium (es exposición en brecha, no cuenta confirmada),
manual=high, sherlock/maigret=`_sherlock_confidence()` (heurística por
longitud y distintividad del username). Cada decisión deja motivos
(`_motivo(code, desc)`) que se persisten en `confidence_reasons`.

El bump de `_sherlock_confidence` pasa por `_identificador_en_url()`
(`urllib.parse.urlsplit`, sin red), que concede señal SOLO si el
identificador aparece (a) en el path, el query string o el fragmento **como
segmento completo**, o (b) como etiqueta más a la izquierda del host **con
igualdad exacta** (`jeremy.tumblr.com`). Aparecer a media palabra en
cualquiera de los dos NO cuenta: el viejo `identifier in url` casaba dentro
del dominio (`ana` subía de tramo por `banana.com`) y era la fuente más
directa de falsos positivos. La frontera de (a) la impone
`_match_con_frontera()`: exige separador (`_SEPARADORES_URL`, hoy
`/ - _ . = ? & @ ~ #`) o extremo de cadena a cada lado, y recorre TODAS las
apariciones (que la primera no valga no descarta una posterior legítima:
`/marca/mar`). Sin esa frontera el mismo bug revivía movido de componente —
`mar` casaba en `/marca-noticias/123`. `@` salió de un escaneo real
(`tiktok.com/@usuario` perdía el bump); `#` va acompañado de que el
haystack incluya el **fragmento**, que `urlsplit` saca del path y antes se
perdía entero (hay SPAs con el perfil en `/#/user/nombre`).
La escala base (`len>=8`, `len>=6`+distintivo, `len>=5`, resto) NO cambió.

`_corroborar_entre_fuentes()` es un pase final de `discover()`, SIN red:
relee la DB (el único sitio donde existe el conjunto completo y ya
deduplicado) y sube un tramo a las filas heurísticas cuando el mismo
`source_site` sale de dos tipos de identificador distintos (email vía
holehe/hibp + username vía sherlock/maigret) — dos caminos independientes
al mismo sitio. NO fusiona filas: la unicidad sigue siendo
`(source_site, identifier)`. Las fuentes de confianza POLÍTICA (holehe
high, hibp medium, manual high) reciben el motivo pero nunca cambian de
tramo. Nada sube por encima de `high` (`_subir_tramo` tiene techo).
Quedan fuera del agrupado las filas con el motivo `hibp_no_sitio`: siguen
en el discovery para que el usuario las revise, pero no corroboran ni son
corroboradas (ver `hibp.py`).

`rastrillo/hibp.py` — cliente mínimo de HaveIBeenPwned. Skipped en
silencio si no hay API key. Convierte cada brecha confirmada en un hit
con el dominio del sitio brechado. Las brechas con `Domain` vacío se
DESCARTAN (no hay sitio al que ir a borrar): es el caso de los volcados
agregados tipo "Collection #1". Las que sí traen dominio pero llevan
`IsSpamList`, `IsFabricated` o `IsVerified=false` entran al discovery
marcadas con `no_site=True` → `_register` les pone el motivo
`hibp_no_sitio` y quedan excluidas de la corroboración (no del discovery).

`rastrillo/canario.py` — **canario a nivel de sitio**. Pregunta a un sitio
por DOS usernames falsos y compara los dos canarios ENTRE SÍ (nunca contra
el perfil real del usuario: así el veredicto es del SITIO y no del
identificador, y no le anunciamos a nadie que miramos ese username).
Veredictos: `indiscriminado` (dos 200, cuerpos ≥95% iguales por
`difflib.SequenceMatcher` sobre texto normalizado, y SIN marcadores de "no
existe"), `discrimina` (4xx o marcador presente), `indeterminado`
(bloqueo/red/excepción/URL no construible).

**El canario NO toca `confidence` (invariante 6).** El veredicto se escribe en
`accounts.verifiability`, columna aparte, y en TODAS las filas del sitio —
describe al sitio, no a la fila, mismo criterio que `corrob_cruzada` en 2A. Por
eso `_aplicar_a_fila` ya no distingue fuentes heurísticas de políticas: esa
distinción existía para decidir a quién degradar y aquí no se degrada a nadie.
Los motivos (`canario_indiscriminado`, `canario_discrimina`,
`canario_bloqueado`, `canario_sin_respuesta`) se siguen anotando en
`confidence_reasons` como evidencia, pero no mueven el tramo.
`indeterminado` NO se cachea (se reintenta), aunque sí se registra: "lo intenté
y no pude concluir" ≠ "no lo he mirado" (NULL).

Qué URL se sondea: la que produjo el hit **según Sherlock**, no siempre la
visible. `catalogo.plantilla_sonda` decide (ver `catalogo.py`) y aporta además
los marcadores específicos del sitio. Distinguir bloqueo de caída sale de
`resolver._http_get_detallado`; **no se cambian cabeceras para esquivar un
403**.

Caché por host en `~/.rastrillo/canario.json` con TTL
(`RASTRILLO_CANARIO_MAX_AGE_DAYS`, default 30), patrón de `directory.py`:
con caché fresca, cero red. Solo filas con `profile_url`, lo que excluye
estructuralmente holehe y hibp. **Exactamente 2 peticiones por sitio no
cacheado**, nunca por fila. Guarda anti-SSRF vía `resolver._is_safe_url`
(precedente cross-module en `engine.py:85`) aplicada ANTES del GET; throttle
y timeout vía `resolver._http_get` (`RASTRILLO_PROBE_DELAY`); pool con
`RASTRILLO_RESOLVER_WORKERS` (`_workers()` duplica cuatro líneas de
`jobs._resolver_workers` a propósito: importar `jobs` cerraría el ciclo).
Las dos primitivas de red (`http_get`, `url_segura`) son inyectables por
parámetro, como en `domain_intel.py`; `http_get` acepta la forma simple
`(status, url, body)` y la detallada `(resultado, motivo)`. Debug a mano:
`rastrillo canario`.

`reparar_confianza_2b(aplicar=False)` es un helper de UN SOLO USO que devuelve
a su tramo las filas que el canario del paso 2B bajó a `low`. Hace falta porque
un reescaneo NO las recupera: `db.upsert_account` hace `return row["id"]` en
cuanto la fila existe, así que el `confidence` recalculado por `_register`
nunca se escribe. Reconstruye el tramo sin red desde los motivos persistidos
(`_TRAMO_POR_MOTIVO` + `_MOTIVOS_QUE_SUBEN` son la escala de
`_sherlock_confidence` leída al revés). Expuesto como
`rastrillo reparar-confianza [--aplicar]`.

`rastrillo/catalogo.py` — lee el `data.json` de `sherlock-project` con
`importlib.resources` (sin duplicar la tabla ni añadir dependencias) para que
el canario mida **lo mismo que produjo el hit**. Aporta dos cosas: `urlProbe`,
la URL de sondeo de los sitios que se comprueban contra una API en vez de
contra la página de perfil (Duolingo, Freelancer, Chess…), y `errorMsg`, el
texto exacto con el que Sherlock decide "no existe" (es lo que rescata a Steam
y a HudsonRock, cuyas redacciones no estaban en la lista genérica). El
emparejamiento fila ↔ entrada va por **forma de la URL** —sustituir el
identificador en la plantilla y comparar— y no por nombre ni por host: el
`display_name` puede venir de una receta, y un host puede tener dos entradas
(`Steam Community (User)` / `(Group)`). Degrada sin romper: si no hay entrada
(hits de **Maigret**, que trae su propio catálogo y no cubrimos; sitios
retirados; sherlock no instalado) devuelve la URL del hit y el canario se apoya
en la lista genérica. Los `urlProbe` que no llevan el identificador en la URL
(Discord, Holopin) dejan el sitio en `indeterminado`, que es lo honesto.

`rastrillo/directory.py` — directorio público (JustDeleteMe). Fetch
remoto + caché + lookup por host (exacto y por sufijo para subdominios).
Fallback embebido si no hay red en el primer arranque. Refresh
automático al arrancar si la caché supera `RASTRILLO_DIR_MAX_AGE_DAYS`
(default 30).

`rastrillo/resolver.py` — el resolver de la tabla de arriba. `_http_get`
colapsa todo fallo en `None` y sus tres callers solo distinguen "hubo
respuesta" de "no la hubo"; **ese contrato no ha cambiado**. Si necesitas saber
POR QUÉ falló, usa `_http_get_detallado`, que devuelve `(resultado, motivo)`
con `MOTIVO_BLOQUEADO` (403/429), `MOTIVO_RED` (timeout/DNS/conexión) o
`MOTIVO_SSRF`. Lo usa el canario para no reportar un bloqueo como una caída.
Códigos >= 400 que no sean 403/429 siguen volviendo como respuesta con cuerpo.
`resolve(host, identifier)` siempre devuelve una Resolution. Throttle
por dominio (`RASTRILLO_PROBE_DELAY`, default 1.5 s) para no parecer
escaneo abusivo. Caché en `discovered.json`. Plantillas GDPR en 6
idiomas (en, es, ru, pt-BR, fr, de), inicial y follow-up. `_http_get`
incluye una allowlist anti-SSRF (`_is_safe_url`): solo `https://`,
host debe resolver a IPs públicas — rechaza loopback, privadas,
link-local, reservadas y multicast. Si la URL no pasa, devuelve None
y el caller sigue con la siguiente sin abortar el lote.

`rastrillo/hostutil.py` — normalización de host / slug compartida.
Tres funciones con semánticas distintas (a propósito): `slugify` para
casar recetas, `host_from_url` (con strip + corte de querystring) y
`host_of` (sin strip, corta puerto, no corta querystring) para clave de
throttle del resolver. La caracterización está congelada en
`tests/test_hostutil_caracterizacion.py`; antes de cambiar nada aquí,
asegúrate de que esos tests siguen verdes.

`rastrillo/recipes.py` — loader de recetas JSON (opcionales). El usuario
no debería tener que escribir ninguna.

`rastrillo/recipes/*.json` — recetas de ejemplo (reddit, tumblr,
pinterest, discord, spotify) + plantilla de anonimizado. Los selectores
son puntos de partida, no verdades verificadas.

`rastrillo/recipes_auto.py` — cuando el agente IA cierra un borrado, la
secuencia de acciones que funcionó se serializa como receta en
`~/.rastrillo/recipes/`. Omite por seguridad los pasos `fill` (riesgo de
PII). El próximo run en ese sitio usa la receta determinista en vez de
gastar tokens.

`rastrillo/engine.py` — Playwright persistente, motor de pasos. Tres
caminos: receta JSON, directorio+IA, manual. Resumible: `current_step`
se persiste DESPUÉS de cada paso completado, y un hash de la receta
detecta cambios entre runs. `pause_handler` inyectable (terminal o web).
Tiene endurecimiento contra falsos `deleted`: antes de sellar, hace una
revisita HTTP a la `profile_url`; si responde 404 o el body contiene
keywords multi-idioma de "cuenta cerrada" lo confirma, si sigue cargando
deja la cuenta en `manual`. Detecta redirecciones a login (que también
cambian la URL) y las descarta. Sin `profile_url` exige dos señales
independientes (URL cambiada + keyword en el body actual).

`rastrillo/ai_assist.py` — IA opcional (Anthropic SDK). Dos modos:
`find_control()` (un turno, usado por el step `ai_assist` de recetas) y
`run_agent()` (bucle de varios turnos para el flujo sin receta).
Privacidad: solo envía estructura de página + texto visible, nunca
cookies ni contraseñas.

`rastrillo/server.py` — dashboard FastAPI. Middleware de auth: token
exigido en TODOS los POST y en todos los GET de `/api/*`; `GET /` (HTML
shell) queda fuera porque el navegador entra sin header. Además valida
el header `Host` contra `config.ALLOWED_HOSTS` en TODAS las peticiones
(anti DNS rebinding); fuera de la allowlist → 403. El frontend vive en
`rastrillo/static/` (index.html + styles.css + app.js) y se sirve con
`StaticFiles` bajo `/static/`; la **única** pieza dinámica del HTML es
`window.__RASTRILLO_BOOT__`, inyectada por `_boot_script()` (hoy: solo
`STATUS_META`). Si añades algo nuevo que el JS necesite al arrancar,
ponlo ahí — no reintroduzcas placeholders en `index.html`.

Dos nombres del módulo son hoy REEXPORTS y no definiciones (Paso 4, para que
`report_pdf` no tenga que importar este módulo): `STATUS_META` y
`VERIFIABILITY_META` vienen de `glosario.py`, y `_deletion_url` es
`reports.deletion_url`. Si añades un estado, la etiqueta va en `glosario.py`;
hay un test que exige que todo estado documentado en `db.py` tenga la suya.

Endpoints relevantes: `GET /` (HTML), `GET /api/accounts`,
`GET/POST /api/scan/*`, `GET /api/directory`,
`POST /api/directory/refresh`,
`POST /api/accounts/{id}/action` (delete/anonymize/keep/retry/continue),
`POST /api/accounts/{id}/own` (triage),
`POST /api/accounts/{id}/mark-sent`,
`POST /api/accounts/{id}/schedule-deletion` (fija plazo: `{days}` o `{eta}`),
`POST /api/accounts/{id}/verify-deletion` (Verificar al vencer; reusa
`engine.revisit_profile`),
`POST /api/accounts/{id}/cancel-deletion` (limpia plazo → `manual`),
`POST /api/accounts/{id}/confirm-account` (promueve HIBP a candidato),
`POST /api/accounts/process-all-auto`,
`GET /api/accounts/discard-low/preview` (solo lee: cuántas filas y cuáles
barrería el descarte masivo; alimenta el conteo exacto del modal),
`POST /api/accounts/discard-low`,
`POST /api/accounts/clear`,
`GET/POST /api/dry-run`,
`GET /api/onboarding`, `POST /api/onboarding/dismiss`,
`GET /api/report?format=json|csv|pdf`,
`GET /api/accounts/{id}/followup-draft`.

`rastrillo/jobs.py` — coordinación servidor <-> motor. Cola del worker
Playwright (siempre un worker único; no abrimos N Chromiums). Pool
acotado SOLO para las capas HTTP del resolver
(`RASTRILLO_RESOLVER_WORKERS`, default 5). `web_pause_handler` bloquea
con un Event hasta que la UI confirme. `scan_async` lanza discovery en
thread y, al terminar, corre el **canario** (fase `canario`; entre discovery
y auto-resolver a propósito: `profile_url` todavía es la URL del hit, que es
la plantilla que el canario necesita, y así el auto-resolver no gasta
peticiones planificando el borrado de sitios recién invalidados; un fallo
ahí no aborta el escaneo) y después el auto-resolver para precalcular la
Resolution de las cuentas pendientes (excluyendo HIBP no confirmadas).
También hay refresh automático del directorio al arrancar si la caché
está vieja.

`rastrillo/audit.py` — log append-only en `~/.rastrillo/audit.json` con
snapshot reducido de cada acción destructiva (delete, anonymize,
mark-sent, own, discard, confirm-account). Rota a `audit_<ts>.json`
cuando supera `RASTRILLO_AUDIT_MAX_BYTES` (default 5 MB).

`rastrillo/reports.py` — construcción de informes en JSON / CSV / PDF.
`build_report(fmt)` devuelve `(contenido, media_type, nombre_sugerido)` y lo
comparten el endpoint `GET /api/report` y el subcomando `report`, para que
produzcan lo mismo. Los formatos `json` y `csv` (`_CSV_COLS`) son contrato:
no se tocan.

Aquí vive también `deletion_url(action_meta, profile_url)`, que bajó de
`server.py` en el Paso 4 porque la necesitan DOS consumidores —el endpoint
`/api/accounts` y el informe PDF— y `report_pdf` no puede importar `server`.
`server._deletion_url` es un alias suyo. Devuelve `None` si el JSON está
corrupto o si la URL coincide con la del hit, para no pintar dos veces el
mismo enlace.

`rastrillo/report_pdf.py` — el informe PDF (reportlab). Lo usa el endpoint
`GET /api/report?format=pdf` y el subcomando `report --format pdf`; la firma
`render_pdf(accounts, summary, audit_summary, generated_at)` no ha cambiado.

Desde el Paso 4 es un DOCUMENTO, no un volcado de la tabla. Cuatro partes:
**portada** (alcance, totales grandes, aviso de datos personales), **resumen**
(distribución por estado / confianza / verificabilidad con barras dibujadas a
mano —rectángulos del canvas, cero librerías de gráficos— y el recuento del
audit log), **detalle por cuenta** agrupado por estado, y **anexo** con el
glosario. Los criterios de diseño están escritos en la cabecera del módulo para
que sean auditables; los que no se pueden romper sin romper el documento:

- **Ningún dato depende del color.** Estado, confianza y verificabilidad se
  imprimen SIEMPRE como palabras y cada barra lleva su número al lado. En
  blanco y negro el informe se lee igual. El acento (`ACENTO`) es decoración.
  Las barras van todas del mismo tono a propósito: teñir la más alta hacía que
  una diferencia de 1 sobre 75 pareciera una distinción de categoría.
- **La URL del PERFIL y la de BAJA son campos separados y etiquetados**
  («Perfil detectado» / «Cómo darse de baja»). Es el mismo bug que se arregló
  en la UI y no se reintroduce en papel. La de baja sale de
  `reports.deletion_url`.
- **Una entrada no se parte**: cada una va en un `KeepTogether`. Los títulos
  llevan `keepWithNext` para no quedarse solos al pie. `KeepTogether` solo
  puede mantener unido lo que quepa en una página, así que los topes
  (`MAX_URL`, `MAX_NOTA`, `MAX_SITIO`, `MAX_IDENT`) son parte del mecanismo;
  hay un test que mide el caso peor contra la altura del frame.
- **`MAX_ALCANCE` no es cosmético.** La portada enumeraba TODOS los
  identificadores y con 300 cuentas de nombres largos el párrafo superaba la
  altura de la página: `LayoutError` y el informe entero perdido. Con nombres
  cortos el caso quedaba a un 5% del límite, así que no se veía venir.
- Nada de `None` impreso: los campos vacíos dicen «no disponible», «sin señales
  registradas» o «no evaluada» (que NO es lo mismo que «sin veredicto»: ver
  `glosario.etiqueta_verificabilidad`).

El CUERPO de los correos GDPR sigue sin imprimirse (puede llevar PII);
destinatario y asunto sí, que es lo que ya guardaba `last_message`.

`rastrillo/pdf_fuentes.py` — **qué fuente usa el PDF y qué hace con lo que no
cubre**. El problema medido en el Paso 4: las base-14 de PostScript van con
WinAnsiEncoding (Latin-1 y poco más) y la familia Bitstream Vera que empaqueta
reportlab 4.5.1 tiene 283 codepoints y **0/96 de cirílico**. Y reportlab **no
lanza** ante un glifo ausente: no dibuja nada. Un nombre en ruso desaparecería
en silencio, y la DB real tiene cirílico (borrador GDPR de `baby.ru`).

Tres escalones: (1) fuente del sistema con cobertura cirílica —DejaVu,
Liberation, Noto, FreeSans, Arial, Segoe UI…— buscada en las rutas habituales
de Linux/macOS/Windows; (2) **Vera**, la de dentro de reportlab, que no añade
binario ni licencia al repo; (3) Helvetica base-14. En los escalones 2 y 3 el
cirílico no se puede representar y entonces `sanear()` lo sustituye por
`[U+0412]` **visible**, lo cuenta, y el colofón del informe dice con qué fuente
se compuso y cuántos caracteres no pudo representar. Es feo a propósito: un
código se ve e se investiga, un hueco en blanco no.

**No se embebe ninguna fuente en el repo** (decisión del usuario, Paso 4). La
contrapartida asumida es que el PDF no es byte-idéntico entre máquinas. Las
equivalencias tipográficas seguras (`→` → `->`) evitan gastar un escape donde
basta ASCII; NO hay transliteración (`ř` no se convierte en `r`: eso sería
alterar el dato en silencio).

`rastrillo/glosario.py` — textos compartidos. Aloja `STATUS_META` y
`VERIFIABILITY_META`, que **antes vivían en `server.py`**: se movieron porque
el PDF los necesita y no puede importar `server` (arrastraría FastAPI y, vía
`jobs`, la cadena de Playwright, para generar un fichero). `server` los
reexporta con su nombre de siempre, así que `_boot_script` y los tests no
cambian.

Y sobre todo: **el anexo del PDF lee las explicaciones de `static/app.js`**, en
tiempo de render. No es una copia sincronizada por un test, es literalmente la
misma cadena que el tooltip del dashboard. Se hizo así porque esas frases se
escribieron con cuidado en el Paso 3 para decir qué NO significa cada señal
(`low` es «evidencia débil de que sea tuya», no «no es tuya») y dos redacciones
se desvían con el primer retoque — de las dos, la del papel es la que el
usuario archiva. Si tocas un tooltip, el PDF siguiente lo dice igual solo.

El parseo es deliberadamente tonto (contar llaves, juntar literales separados
por `+`), como el de `tests/test_ui_explicita.py`. Si `app.js` deja de ser
literales planos, `cargar_textos()` devuelve `{}` y el anexo **imprime que no
pudo leerlo** en vez de inventarse las frases o reventar a mitad del informe.

`rastrillo/domain_intel.py` — **Domain Intelligence** (recon OSINT
defensivo sobre UN dominio; AMPLÍA el alcance de "solo tus cuentas" — es
para infra propia o autorizada). `lookup_whois` (recursivo IANA →
registro → registrar por socket TCP/43, sin deps), `lookup_dns`
(A/MX/NS/TXT con `dnspython`, import perezoso), `correlate` (heurística
data-driven MX→correo / NS→DNS / SPF+verificaciones TXT→SaaS, SIN red,
cada inferencia con confianza+evidencia) y `analyze` (orquesta y siempre
devuelve informe; solo lanza en dominio inválido). Estilo `discovery.py`:
errores visibles, un fallo no aborta el resto. Las dos primitivas de red
(`_whois_query`, `_dns_query`) son mockeables. El socket WHOIS pasa por
`_host_resolves_public` (mismo criterio anti-SSRF que `resolver`). La
correlación NO hace HTTP extra. Persistencia en la tabla `domain_reports`
(`db.save_domain_report`/`get_domain_report`/`list_domain_reports`/
`delete_domain_report`/`clear_domain_reports`; el clear hace `snapshot_db()`
antes de borrar, igual que `clear_accounts`).
Endpoints `POST /api/domain/analyze`, `GET /api/domain/report?domain=`,
`GET /api/domain/history`, `POST /api/domain/report/delete` (body
`{domain}`; 400 inválido / 404 si no está en el histórico) y
`POST /api/domain/history/clear` (→ `{ok, deleted}`). Ambos borrados van
por **POST y no DELETE** a propósito: el `auth_middleware` solo exige token
en POST y en los GET de `/api/*`, así que un DELETE entraría sin token.
Vista en el dashboard (sección "Inteligencia de dominio"): el histórico se
pinta como lista de tarjetas colapsables (la más reciente abierta), con
"Colapsar todo"/"Expandir todo", borrado por tarjeta y "Limpiar historial",
todo con confirmación. El estado plegado se recuerda por dominio en
`localStorage` (`rastrillo.domain.collapsed`). Estos borrados NO pasan por
`audit.py`: ese log es de acciones destructivas sobre CUENTAS
(`record(action, account)` serializa un snapshot de cuenta). Decisión de
dependencias justificada en la cabecera del módulo.

## Convenciones

Código y comentarios en español. Sin dependencias nuevas salvo que sean
necesarias; justifícalas. No exigir recetas al usuario: el camino por
defecto es directorio + IA y las recetas son override opcional. Lo
específico de cada plataforma vive en recetas o en el directorio, NUNCA
hardcodeado en `engine.py`. Las recetas de usuario en
`~/.rastrillo/recipes/` pisan a las del paquete.

Antes de cerrar un cambio: `python -m py_compile` sobre lo tocado +
suite completa de tests. No tarda.

## Tests

Suite con `unittest` de stdlib, sin deps nuevas:

    cd c:\account-nuker
    .venv\Scripts\python -m unittest discover -t . -s tests -v

Cada test corre con su propio `RASTRILLO_HOME` en tempdir, con
`RASTRILLO_TOKEN=test-token` y sin `ANTHROPIC_API_KEY`,
`RASTRILLO_HIBP_API_KEY` ni `RASTRILLO_DRY_RUN`. Si una subclase
sobreescribe `setUp`, debe llamar a `super().setUp()` antes de importar
nada del paquete (porque el sandbox se crea ahí).

Archivos:

- `tests/test_upsert_dedup.py` — la unicidad por `(source_site,
  identifier)` no colapsa Reddit/RedditGifts; el DDL no tiene UNIQUE.
- `tests/test_resolver_layers.py` — orden de capas, caché,
  `force_refresh`, manejo de `difficulty=impossible`.
- `tests/test_gdpr_templates.py` — las 6 plantillas iniciales y de
  follow-up renderizan con marcadores específicos por idioma; detección
  por TLD.
- `tests/test_state_transitions.py` — preflight de propiedad, mark-sent,
  triage, dry-run, process-all-auto, middleware del token, y la
  eliminación programada (schedule/verify/cancel, helper puro
  `deletion_progress`, migración idempotente de `deletion_*`).
- `tests/test_degradation_no_ai.py` — sin API key ninguna cuenta queda
  sin acción.
- `tests/test_engine_html_local.py` — servidor HTTP local + FakePage
  para validar resumibilidad, hash de receta y recetas auto-generadas
  que omiten `fill`.
- `tests/test_onboarding_and_auth.py` — marker de onboarding se setea
  una sola vez; 401 con mensaje accionable; token por query también vale.
- `tests/test_tier2_pool_throttle.py` — pool del auto-resolver con
  concurrencia acotada; throttle no repite fetches cacheados; UI expone
  fases (Descubriendo / Resolviendo N/total).
- `tests/test_tier3_ops.py` — refresh auto del directorio si caché
  vieja; snapshot de DB antes de clear; rotación de audit.
- `tests/test_maigret_integration.py` — wrapper de maigret: skipped si
  no está instalado, parseo de los dos schemas de status vistos,
  integración en `discover`, dedup, excepciones que no abortan el lote.
- `tests/test_hibp_exposure.py` — HIBP entra con `confidence=medium`,
  no participa en auto-resolver ni en process-all-auto, y
  `confirm-account` la promueve a `source=hibp_confirmed`.
- `tests/test_anti_false_deleted.py` — `revisit_profile` + detección
  de redirect a login; `_confirm_and_seal` no sella `deleted` en
  falsos positivos.
- `tests/test_confidence_signals.py` — falsos positivos sin red: la regla
  nueva del bump (substring de dominio no cuenta, substring a media palabra
  en el path tampoco, frontera de segmento con sus separadores, path y
  subdominio legítimos sí, identificadores de 1-2 caracteres, `url=None` e
  identificador vacío sin excepción, escala base intacta), corroboración
  entre fuentes (holehe+sherlock = dos filas separadas y señal fuerte;
  sherlock+maigret = una fila y señal débil que no mueve tramo; techo en
  `high`), HIBP en la corroboración (`Domain` vacío no llega a hit; spam
  list / fabricada / sin verificar no corroboran pero siguen en el
  discovery), y persistencia de motivos (migración idempotente, round-trip,
  `parse_reasons` tolerante a basura, la API los devuelve ya parseados).
- `tests/test_url_del_hit.py` — Paso 2B Entrega 1: `@ ~ #` como frontera de
  segmento (TikTok `/@usuario`, `~usuario`, fragmento) sin aflojar nada de
  2A; y que el triage exponga la URL DEL HIT (`profile_url` no se pisa en
  las tres escrituras del resolver, backfill solo si está vacía,
  `deletion_url` en el payload de `/api/accounts`).
- `tests/test_canario.py` — canario a nivel de sitio, offline con `http_get`
  y `url_segura` inyectados: los tres veredictos, el marcador de "no existe"
  que evita el falso veredicto (español y alemán), `indeterminado` que no
  se cachea, la caché (fresca → cero peticiones; vencida por TTL
  → se repite; corrupta → no revienta), 3 hits del mismo host = 2 peticiones,
  hits sin `url` = cero peticiones, la guarda de red (`http://`, `localhost`,
  IP privada), que `discrimina` nunca sube el tramo, la plausibilidad de los
  tokens y la construcción de la URL del canario. Cierra con un barrido de
  (fuente × tramo × veredicto) sobre 54 filas que fija el invariante 6:
  **el canario nunca modifica `confidence`, en ninguna dirección**.
- `tests/test_verificabilidad.py` — 2C Entrega 1: la columna
  `verifiability` y su migración idempotente (incluida una DB anterior a 2C),
  que la API la expone, que `discard-low` sigue mirando SOLO `confidence` (una
  fila `high`+`indiscriminado` no se descarta; una `low` normal sí), que no
  existe ningún endpoint de descarte masivo de inverificables, y la reparación
  de un solo uso (`reparar_confianza_2b`): que el reescaneo NO recupera el
  tramo, que sin `--aplicar` no escribe, que el tramo reconstruido coincide con
  `discovery._sherlock_confidence`, idempotencia, ámbito y snapshot previo.
- `tests/test_canario_calibracion.py` — 2C Entrega 2: el catálogo de sherlock
  con un `data.json` inyectado (emparejar por forma de URL, desambiguar dos
  entradas del mismo host, `errorMsg` string y lista, `errorType` != message,
  basura sin reventar, plantillas con llaves literales); las redacciones nuevas
  de marcadores una por una y que un perfil normal no las dispara; el caso
  Duolingo (sondea `urlProbe`) y el caso Steam (misma URL, marcador nuevo); que
  una SPA legítima sigue siendo `indiscriminado`; que Maigret degrada sin
  romper; y un bloque de contrato contra el `data.json` REAL instalado, que
  avisa si sherlock retira o cambia esas entradas.
- `tests/test_bloqueo_vs_red.py` — 2C Entrega 3: `_http_get_detallado`
  (403/429 → bloqueo; timeout/DNS/conexión → red; SSRF → su motivo; 404 y 500
  siguen siendo respuesta con cuerpo), que `_http_get` conserva su contrato
  viejo, que el canario reporta la causa y la anota en la fila, que ninguno de
  los dos se cachea, y que nada de esto toca `confidence`.
- `tests/test_rebuild_legacy.py` — 2C Entrega 4: DB legacy CON UNIQUE migra sin
  perder `sent_at`/`deletion_eta`/`deletion_started_at` (ni ningún otro campo,
  ni los ids); DB legacy muy vieja sin columnas nuevas; DB sin UNIQUE no se
  reconstruye; DB nueva intacta; idempotencia sin dejar `accounts__new`; y el
  test que evita la recaída: los `ADD COLUMN` de `init()` tienen que estar
  todos en `_ACCOUNTS_COLUMNS`.
- `tests/test_memoria_descartes.py` — Paso 3 Entrega 1: la tabla y su migración
  idempotente (incluida una DB anterior al paso 3), los helpers (clave por par,
  normalización, upsert que no duplica, par incompleto que no revienta), **que
  `clear_accounts()` se lleve las cuentas y deje la memoria**, el escaneo
  posterior (entra `not_mine` con motivo `descartado_antes`; mismo sitio con
  otro identificador entra normal; KEEP sigue mandando), el ciclo completo
  descartar → limpiar → reescanear, los dos endpoints que escriben y el
  deshacer (borra la entrada, devuelve la fila a `found` y el siguiente escaneo
  la trae de vuelta). Cierra con que un discovery entero no inventa decisiones.
- `tests/test_senal_sitio.py` — Paso 3 Entrega 2: el umbral (1 identificador no
  es señal, 2 distintos sí, 2 veces el mismo no), que es por sitio y no se
  contagia, que deshacer la baja (no se persiste en la fila), y un barrido
  (3 tramos × 4 niveles de señal) que fija que **la señal no mueve `confidence`
  ni en la DB ni en la API**. Más: que no exista ningún endpoint que actúe
  sobre ella.
- `tests/test_ui_explicita.py` — Paso 3 Entrega 3: barrido del código fuente en
  busca de motivos registrados (`_motivo("x"` y `{"code": "x"` en
  `discovery/canario/db`) y comprobación de que **cada uno tiene etiqueta Y
  tooltip** en `app.js` — añadir un motivo sin su texto rompe aquí, y sobra un
  texto de un motivo retirado también. Además: `low` dice "evidencia débil de
  que sea tuya" y no "no es tuya", ningún tooltip lleva HTML, `confirmBodyHtml`
  escapa ANTES de romper líneas, y el preview del descarte masivo (conteo
  exacto, no escribe, pide token, y coincide con lo que el POST acaba
  escribiendo).
- `tests/test_domain_intel.py` — Domain Intelligence offline: parseo
  WHOIS recursivo y DNS desde fixtures (primitivas de red mockeadas),
  reglas de correlación (MX→Google, NS→Cloudflare, SPF, verificaciones
  TXT), degradación visible (timeout WHOIS, NXDOMAIN, timeout por tipo),
  guard anti-SSRF del socket WHOIS, y los endpoints (401 sin token, 400
  dominio inválido, happy-path con persistencia).
- `tests/test_informe_pdf.py` — Paso 4, el informe PDF. Sin dependencias nuevas
  no se puede parsear un PDF, así que se comprueba lo que sí se puede: que la
  salida empiece por `%PDF` y acabe en `%%EOF` con `startxref` y `/Type
  /Catalog`; el número de páginas contando objetos de página en los bytes (las
  cuatro partes son un suelo estructural, y más cuentas = más páginas); DB
  vacía → PDF válido; cadenas patológicas (URL de 500+, cirílico, CJK, emoji,
  campos `None`) que no lanzan y **no imprimen "None"** —se inspeccionan los
  `Paragraph` de la entrada, que es donde estaría—; 300 filas con tope de
  tiempo; la fuente elegida y `sanear()` en sus tres escalones; que perfil y
  baja salgan como enlaces distintos; y los saltos (la entrada es un
  `KeepTogether`, el caso peor cabe en una página, los títulos llevan
  `keepWithNext`).

  **EL test del enunciado**: que los textos del anexo salgan de la MISMA fuente
  que los tooltips. Lo comprueba un parser de `app.js` **independiente** del de
  `glosario.py` —por líneas, no contando llaves— para que una divergencia se
  note; luego exige que cada frase aparezca de verdad en los flowables del
  anexo, y que sin `app.js` el anexo lo diga en vez de inventar o reventar.

  Y la regresión del «Alcance»: 300 identificadores largos de los dos tipos.
  Se verificó que sin el tope revienta con `LayoutError`; con nombres cortos
  el caso quedaba a un 5% del límite y pasaba desapercibido.

Alrededor de 468 tests en poco más de un minuto. Antes de cualquier cambio
importante: corre la suite.

## Cómo probar sin tocar sitios reales

Monta un HTML local con un botón "Delete account" y una página de éxito,
apunta una receta de prueba a `http://localhost:PORT` y arranca el motor.
Eso es lo que hace `test_engine_html_local.py`.

## Variables de entorno

| Variable | Default | Para qué |
|---|---|---|
| `RASTRILLO_HOME` | `~/.rastrillo` | Raíz de todo |
| `RASTRILLO_TOKEN` | aleatorio por arranque | Auth del servidor local |
| `RASTRILLO_ALLOWED_HOSTS` | `127.0.0.1:8765,localhost:8765,testserver` | Hosts permitidos en el header `Host` (anti DNS rebinding) |
| `RASTRILLO_ALLOW_QUERY_TOKEN` | off | **Solo para tests.** Acepta `?token=` en la query. En producción el token solo viaja por header `X-Rastrillo-Token`. |
| `RASTRILLO_DRY_RUN` | off | Simulación al arrancar |
| `ANTHROPIC_API_KEY` | — | Activa agente IA y web search |
| `RASTRILLO_AI_MODEL` | `claude-sonnet-4-6` | Modelo de Anthropic |
| `RASTRILLO_HIBP_API_KEY` | — | Activa discovery por HIBP |
| `RASTRILLO_RESOLVER_WORKERS` | 5 (cap 16) | Pool del auto-resolver |
| `RASTRILLO_PROBE_DELAY` | 1.5 | Segundos entre GETs al mismo host |
| `RASTRILLO_DIR_MAX_AGE_DAYS` | 30 | Edad max del directorio antes de refresh auto |
| `RASTRILLO_CANARIO_MAX_AGE_DAYS` | 30 | Edad max de un veredicto del canario antes de re-probar (solo se cachean `indiscriminado` y `discrimina`) |
| `RASTRILLO_SHERLOCK_TIMEOUT` | 900 | Timeout global de Sherlock |
| `RASTRILLO_SHERLOCK_SITE_TIMEOUT` | 60 | Timeout per-site de Sherlock |
| `RASTRILLO_HOLEHE_TIMEOUT` | 600 | Timeout global de Holehe |
| `RASTRILLO_MAIGRET_TIMEOUT` | 300 | Timeout global de Maigret |
| `RASTRILLO_WHOIS_TIMEOUT` | 10 | Segundos por consulta WHOIS (Domain Intelligence) |
| `RASTRILLO_DNS_TIMEOUT` | 5 | Segundos por tipo de registro DNS (Domain Intelligence) |
| `RASTRILLO_AUDIT_MAX_BYTES` | 5242880 | Tamaño que dispara rotación |

## Estado actual

Esqueleto completo y verificado. 5 recetas de ejemplo + plantilla de
anonimizado en el paquete; los selectores son puntos de partida y se
romperán cuando los sitios cambien su HTML. La suite de tests cubre los
invariantes críticos: dedup, capas del resolver, plantillas GDPR,
transiciones de estado, degradación sin IA, motor con HTML local,
onboarding, pool y throttle, ops (backup/rotación/refresh), maigret,
HIBP-como-exposición y anti-falso-deleted.

## Backlog

Cosas que se podrían apretar más:

- **Acción en lote sobre inverificables.** Hoy hay filtro pero no acción, a
  propósito (2C, Entrega 1: primero vivir con la señal separada). Si algún día
  se quiere, NO puede reusar `not_mine`: ese estado y la pestaña "Descartadas"
  son el mismo y ya significan "no es mía". Haría falta un estado nuevo, con su
  entrada en `db.py`, `server.STATUS_META`, `app.js` (`GROUPS`, `TONE`,
  `SHOW_MSG`) y su filtro.
- **Promover la señal agregada del sitio** (`server._discard_site_counts`, hoy
  solo un chip) a algo que mueva la confianza, **si los datos lo justifican**.
  Está desconectada a propósito: el canario se construyó sobre una hipótesis
  razonable y sus tres detecciones resultaron ser errores suyos, así que no se
  conecta una señal sin medir a algo que escribe en la DB. Primero varios
  escaneos mirándola; el umbral (2 identificadores distintos) vive en
  `_UMBRAL_SENAL_SITIO` y la muestra es minúscula.
- **Cobertura del catálogo de Maigret.** `catalogo.py` solo lee el `data.json`
  de sherlock; los hits de maigret sondean la URL visible y se apoyan en la
  lista genérica de marcadores. Maigret trae su propio catálogo con otro
  esquema (y es opt-in, así que puede no estar instalado).
- **El canario da `discrimina` ante un 5xx.** `status >= 400` no distingue "el
  usuario no existe" de "el sitio se rompió". Es la semántica previa a 2C y se
  dejó intacta a propósito; merece revisión con evidencia.

- **El PDF no es byte-idéntico entre máquinas.** Depende de qué fuente haya
  instalada (ver `pdf_fuentes.py`). Es la contrapartida asumida al descartar
  embeber un binario en el repo. Si algún día se quiere salida reproducible,
  la vía es embeber DejaVu Sans —licencia permisiva, cobertura cirílica y
  griega— y eso es una decisión del mantenedor, no una tarea que se pueda
  tomar por cuenta propia.
- **CJK y emoji no se representan nunca.** Ninguna fuente latina los cubre, así
  que salen como `[U+30C6]`. Es honesto pero feo. Solo se arregla embebiendo
  una fuente CJK, que es mucho binario para un caso marginal en una
  herramienta cuyo alcance son 6 idiomas latinos + ruso.
- **El resumen del PDF deja media página en blanco con pocas cuentas**, porque
  cada sección arranca en página nueva. Es convención de documento, no un
  fallo; si molesta, la alternativa es dejar que el detalle siga en la misma
  página y perder el arranque limpio de sección.

- Endurecer el bucle de IA: presupuesto explícito de tokens, screenshot
  por turno opcional.
- Verificación de borrado todavía más estricta: más keywords
  multi-idioma, comparación de página antes/después.
- Tests con más topologías de flujo: modales anidados, multi-step con
  redirect intermedio, captcha simulado.
- Maigret: documentar mejor el flujo de instalación opt-in y los pros y
  contras frente a Sherlock.
- Histórico de Inteligencia de dominio: hoy la vista pide los N informes en
  paralelo (`GET /api/domain/report` por dominio) al cargar. Vale para el
  volumen actual, pero si el histórico crece a 20–30 dominios el arreglo es
  cargar el informe **al expandir** la tarjeta (el endpoint ya sirve
  exactamente eso) y cachearlo en memoria. Ojo: la cabecera plegada muestra
  un resumen calculado del informe (nº de registros DNS · nº de
  correlaciones), así que hacerlo perezoso obliga a que
  `GET /api/domain/history` devuelva esos contadores.
- `~/.rastrillo/backups/` no rota ni tiene tope: `db.snapshot_db()` escribe
  un `.db` nuevo en cada borrado masivo (`clear_accounts` y ahora también
  `clear_domain_reports`) y nadie los borra — a diferencia de `audit.json`,
  que sí rota por `RASTRILLO_AUDIT_MAX_BYTES`. Además el timestamp tiene
  resolución de segundo (`%Y%m%d-%H%M%S`), así que dos snapshots en el mismo
  segundo se pisan (`audit.py` usa microsegundos y no tiene ese problema).
