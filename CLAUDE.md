# CLAUDE.md — contexto del proyecto para Claude Code

## Qué es esto
**Rastrillo**: herramienta local y privada que rastrea la huella digital del
dueño, encuentra sus cuentas regadas por la web y las borra o anonimiza de un
solo pase, dejando solo las que quiere conservar. El usuario da usernames y
correos; la herramienta descubre cuentas (Sherlock/Holehe), abre el flujo de
borrado de cada plataforma, lo ejecuta hasta donde se puede automatizar y pide
intervención humana **solo** cuando la plataforma obliga (CAPTCHA / 2FA /
confirmación final). Persiste progreso y reanuda.

Stack: Python, Playwright (Chromium persistente), FastAPI (dashboard), SQLite.
Todo corre local en Linux (el usuario usa Arch/Hyprland).

## Invariantes — NO los rompas
1. **Nunca almacenar contraseñas.** La autenticación es vía perfil de Chromium
   persistente en `~/.rastrillo/browser-profile`; el humano se loguea una vez
   por sitio. Si una tarea sugiere guardar credenciales, recházala y propón la vía
   del perfil persistente.
2. **Human-in-the-loop obligatorio para CAPTCHA/2FA/confirmación final.** No
   intentes "resolver" CAPTCHAs ni evadir detección de bots. El paso `pause` existe
   para eso.
3. **Privacidad total local.** Nada sale a internet salvo (a) las webs que se
   borran y (b) en modo IA, solo la *estructura* de la página (árbol de
   accesibilidad + texto visible), nunca cookies ni contraseñas.
4. **Solo cuentas del propio usuario.** No es una herramienta contra terceros.
5. **Conservar siempre** las plataformas en `config.KEEP_PLATFORMS`
   (tiktok, instagram, linkedin, github).

## Modelo de borrado: RESOLVEDOR EN CAPAS
Para cada cuenta producimos SIEMPRE una acción concreta. El resolver
(`rastrillo/resolver.py`) prueba las capas en orden y se detiene en la
primera que produzca algo accionable. Cada hallazgo se cachea en
`~/.rastrillo/discovered.json` para no repetir trabajo en re-ejecuciones.

| # | Capa            | Cómo                                                     | Devuelve                  |
|---|-----------------|----------------------------------------------------------|---------------------------|
| 1 | **Receta JSON** | `rastrillo/recipes/*.json` u `~/.rastrillo/recipes/`     | `kind=auto` (override)    |
| 2 | **Directorio**  | JustDeleteMe (`directory.lookup(host)`)                  | `auto`/`semi_auto`        |
| 3 | **Web search**  | Tool `web_search` del API de Anthropic (consulta en el idioma del TLD) | `auto`/`semi_auto` |
| 4 | **Probe paths** | GET a paths localizados (`/settings`, `/cuenta`, `/удалить`, …) | `semi_auto`       |
| 5 | **Agente IA**   | Bucle ver/decidir/pulsar (`ai_assist.run_agent`) sobre la URL hallada | `deleted/anonymized/manual` |
| 5 | **GDPR mail**   | Extrae contacto de privacidad y redacta solicitud Art. 17 en el idioma del sitio | `email_draft` (siempre)  |

Estados accionables (no más callejones sin salida):
- `auto` interno = `queued/in_progress/awaiting_user/deleted/anonymized`.
- `semi_auto`: 1 clic del usuario en el enlace cacheado.
- `email_draft`: borrador GDPR listo (mailto: y copia).

Sin `ANTHROPIC_API_KEY`: se saltan las capas 2 (web_search) y 5 (bucle IA
sobre la página). El resolver sigue produciendo `semi_auto` (probe) o
`email_draft` (GDPR) con plantillas estáticas; el programa sigue accionable.

## Mapa del código
- `cli.py` — entrypoint. **Sin argumentos** levanta el dashboard y abre el
  navegador automáticamente; toda la UX vive ahí. Subcomandos `list` y `run`
  son auxiliares de debug. Instalado como `rastrillo`/`rs` vía `pyproject.toml`.
- `rastrillo/config.py` — rutas bajo `~/.rastrillo`, KEEP_PLATFORMS, config IA.
  Env vars: `RASTRILLO_HOME`, `RASTRILLO_AI_MODEL`.
- `rastrillo/db.py` — SQLite. Estados: found, queued, in_progress, awaiting_user,
  deleted, anonymized, manual, skipped, failed. Migraciones idempotentes
  (recipe_hash, source_site) en `init()`.
- `rastrillo/discovery.py` — subprocesos a Sherlock (CSV) y Holehe (CSV);
  validación de input, matching con aliases, errores visibles, log
  "crudo vs guardado"; puebla la DB.
- `rastrillo/directory.py` — directorio público (JustDeleteMe). Fetch + caché
  + lookup por host. Fallback embebido si no hay red al primer arranque.
- `rastrillo/resolver.py` — resolvedor en capas (ver tabla arriba).
  `resolve(host, identifier) -> Resolution` SIEMPRE devuelve una acción.
  Caché de hallazgos personales en `~/.rastrillo/discovered.json`.
- `rastrillo/recipes.py` — loader de recetas JSON (opcionales).
- `rastrillo/recipes/*.json` — recetas de ejemplo / fast-paths.
- `rastrillo/engine.py` — Playwright persistente. Tres caminos según
  disponibilidad: receta → directorio+IA → manual. `pause_handler` inyectable.
  Resumible (current_step persistido tras cada paso; hash de receta).
- `rastrillo/ai_assist.py` — IA opcional (Anthropic). Dos modos:
  `find_control()` (un turno, usado por step ai_assist de recetas) y
  `run_agent()` (bucle de varios turnos para el flujo sin receta).
- `rastrillo/server.py` — dashboard FastAPI: GET /, GET /api/accounts,
  POST /api/scan, GET /api/directory, POST /api/directory/refresh,
  POST /api/accounts/{id}/action (delete/anonymize/keep/retry/continue).
- `rastrillo/jobs.py` — coordinación entre server y motor: cola del worker,
  `web_pause_handler` (bloquea hasta que la UI confirme), `scan_async`.

## Convenciones
- Código y comentarios en español (el usuario trabaja en español).
- Sin dependencias nuevas salvo que sean necesarias; justifícalas.
- **No exigir recetas al usuario**: el camino normal es directorio + IA. Las
  recetas son override opcional para fast-paths. NUNCA hardcodees flujos de
  plataforma en `engine.py`; lo específico vive en recetas o en el directorio.
- Recetas de usuario en `~/.rastrillo/recipes/` pisan las del paquete.
- Antes de dar algo por terminado, corre `python -m py_compile` sobre lo tocado
  y, si aplica, una prueba rápida de import.

## Estado actual
Esqueleto completo y verificado (sintaxis, DB, carga de recetas, dashboard).
5 recetas de ejemplo (reddit, tumblr, pinterest, discord, spotify) + plantilla de
anonimizado. Los selectores son **puntos de partida sin verificar contra el sitio real**.

## Backlog
- **Endurecer el bucle de IA**: mejor detección de éxito (URL changed + texto
  "deleted"), screenshot por turno, presupuesto explícito de tokens.
- **Refresh periódico del directorio**: hoy es manual (botón). Una tarea de
  fondo cada N días podría mantenerlo fresco.
- **Verificación de borrado más robusta**: además del `success_selector`,
  permitir `success_url` y `expect_gone` (selector que NO debe existir).
- **Tests**: ampliar el smoke local con varias topologías de flujo (modal,
  multi-step, captcha simulado).

## Cómo probar sin tocar sitios reales
Monta un HTML local con un botón "Delete account" y un `success` final; apunta una
receta de prueba a `http://localhost:PORT`. Así validas el motor de forma determinista.

## Tests
Suite con `unittest` (sin deps nuevas):

    cd c:\account-nuker
    .venv\Scripts\python -m unittest discover -t . -s tests -v

Cada test usa un `RASTRILLO_HOME` propio en tempdir, con `RASTRILLO_TOKEN=test-token`
y sin `ANTHROPIC_API_KEY`/`RASTRILLO_HIBP_API_KEY`. Estructura:

- `tests/test_upsert_dedup.py` — Reddit/RedditGifts NO se colapsan; no UNIQUE en SQL.
- `tests/test_resolver_layers.py` — orden de capas + caché + force_refresh.
- `tests/test_gdpr_templates.py` — 6 idiomas iniciales + follow-up; detección por TLD.
- `tests/test_state_transitions.py` — preflight, mark-sent, triage, dry-run,
  process-all-auto, token middleware.
- `tests/test_degradation_no_ai.py` — sin API key NINGUNA cuenta queda sin acción.
- `tests/test_engine_html_local.py` — servidor HTTP local + FakePage; resumibilidad,
  hash de receta, recetas auto-generadas omiten `fill`.

Antes de cerrar un cambio: corre la suite. Es rápido (~10 s).
