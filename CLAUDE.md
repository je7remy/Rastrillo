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
navegador con el token en la URL y queda corriendo. Subcomandos `list` y
`run` son auxiliares de debug. Instalado como `rastrillo` y `rs` vía
`pyproject.toml`. Los shims viven en `.venv/(bin|Scripts)/` y esa carpeta
no está en el `PATH` global del usuario: para invocar el comando hay que
usar los wrappers (`rastrillo.sh`/`rastrillo.ps1`/`rastrillo.cmd`),
activar el venv, o instalar global con `pipx install .`.

`rastrillo/config.py` — rutas bajo `~/.rastrillo`, `KEEP_PLATFORMS`,
config de IA, generación del token de auth, flag dry-run mutable, helpers
de onboarding (`is_onboarded`, `mark_onboarded`).

`rastrillo/db.py` — SQLite. Estados completos:
`found, queued, in_progress, awaiting_user, deleted, anonymized,
user_done, semi_auto, email_draft, manual, skipped, failed, not_mine,
dry_run`. Migraciones idempotentes (`recipe_hash`, `source_site`,
`action_meta`, `confidence`, `owned`, `sent_at`) en `init()`. La
unicidad de cuentas se hace en código por `(source_site, identifier)` —
NO con UNIQUE en SQL — para no colapsar Reddit y RedditGifts.
`snapshot_db()` copia la DB a `~/.rastrillo/backups/` antes de
`clear_accounts`.

`rastrillo/discovery.py` — wrappers a Sherlock (CSV), Holehe (CSV),
Maigret (JSON, opt-in si está en PATH) y HIBP (opt-in con clave).
Asigna `confidence` por hit: holehe=high, hibp=medium (es exposición en
brecha, no cuenta confirmada), sherlock/maigret=heurística por longitud
y distintividad del username con bump si la URL del hit contiene el
username literal. Log "crudo -> guardado" por cada fuente.

`rastrillo/hibp.py` — cliente mínimo de HaveIBeenPwned. Skipped en
silencio si no hay API key. Convierte cada brecha confirmada en un hit
con el dominio del sitio brechado.

`rastrillo/directory.py` — directorio público (JustDeleteMe). Fetch
remoto + caché + lookup por host (exacto y por sufijo para subdominios).
Fallback embebido si no hay red en el primer arranque. Refresh
automático al arrancar si la caché supera `RASTRILLO_DIR_MAX_AGE_DAYS`
(default 30).

`rastrillo/resolver.py` — el resolver de la tabla de arriba.
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
ponlo ahí — no reintroduzcas placeholders en `index.html`. Endpoints
relevantes: `GET /` (HTML), `GET /api/accounts`,
`GET/POST /api/scan/*`, `GET /api/directory`,
`POST /api/directory/refresh`,
`POST /api/accounts/{id}/action` (delete/anonymize/keep/retry/continue),
`POST /api/accounts/{id}/own` (triage),
`POST /api/accounts/{id}/mark-sent`,
`POST /api/accounts/{id}/confirm-account` (promueve HIBP a candidato),
`POST /api/accounts/process-all-auto`,
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
thread y, al terminar, dispara el auto-resolver para precalcular la
Resolution de las cuentas pendientes (excluyendo HIBP no confirmadas).
También hay refresh automático del directorio al arrancar si la caché
está vieja.

`rastrillo/audit.py` — log append-only en `~/.rastrillo/audit.json` con
snapshot reducido de cada acción destructiva (delete, anonymize,
mark-sent, own, discard, confirm-account). Rota a `audit_<ts>.json`
cuando supera `RASTRILLO_AUDIT_MAX_BYTES` (default 5 MB).

`rastrillo/report_pdf.py` — generación del informe PDF (reportlab). Lo
usa el endpoint `GET /api/report?format=pdf`.

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
  triage, dry-run, process-all-auto, middleware del token.
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

Alrededor de 130 tests en unos 28 segundos. Antes de cualquier cambio
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
| `RASTRILLO_SHERLOCK_TIMEOUT` | 900 | Timeout global de Sherlock |
| `RASTRILLO_SHERLOCK_SITE_TIMEOUT` | 60 | Timeout per-site de Sherlock |
| `RASTRILLO_HOLEHE_TIMEOUT` | 600 | Timeout global de Holehe |
| `RASTRILLO_MAIGRET_TIMEOUT` | 300 | Timeout global de Maigret |
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

- Endurecer el bucle de IA: presupuesto explícito de tokens, screenshot
  por turno opcional.
- Verificación de borrado todavía más estricta: más keywords
  multi-idioma, comparación de página antes/después.
- Tests con más topologías de flujo: modales anidados, multi-step con
  redirect intermedio, captcha simulado.
- Maigret: documentar mejor el flujo de instalación opt-in y los pros y
  contras frente a Sherlock.
