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

## Mapa del código
- `cli.py` — entrypoint: `scan`, `list`, `run`, `skip`, `dashboard`. Instalado
  como comandos `rastrillo` y `rs` (alias corto) vía `pyproject.toml`.
- `rastrillo/config.py` — rutas bajo `~/.rastrillo`, KEEP_PLATFORMS, config IA.
  Env vars: `RASTRILLO_HOME`, `RASTRILLO_AI_MODEL`.
- `rastrillo/db.py` — SQLite. Estados: found, queued, in_progress, awaiting_user,
  deleted, anonymized, manual, skipped, failed.
- `rastrillo/discovery.py` — subprocesos a Sherlock (username) y Holehe (email);
  casa con recetas; puebla la DB.
- `rastrillo/recipes.py` — loader + esquema de recetas (documentado arriba del archivo).
- `rastrillo/recipes/*.json` — una receta por plataforma (modular/extensible).
- `rastrillo/engine.py` — Playwright persistente + ejecutor de pasos + pausa humana.
- `rastrillo/ai_assist.py` — fallback LLM (opcional, Anthropic) para ubicar controles.
- `rastrillo/server.py` — dashboard FastAPI con estado en vivo (poll cada 2s).

## Convenciones
- Código y comentarios en español (el usuario trabaja en español).
- Sin dependencias nuevas salvo que sean necesarias; justifícalas.
- Las recetas se editan como JSON, **nunca** hardcodees flujos de plataforma en
  `engine.py`. La lógica del motor es genérica; lo específico va en recetas.
- Recetas de usuario en `~/.rastrillo/recipes/` pisan las del paquete.
- Antes de dar algo por terminado, corre `python -m py_compile` sobre lo tocado
  y, si aplica, una prueba rápida de import.

## Estado actual
Esqueleto completo y verificado (sintaxis, DB, carga de recetas, dashboard).
5 recetas de ejemplo (reddit, tumblr, pinterest, discord, spotify) + plantilla de
anonimizado. Los selectores son **puntos de partida sin verificar contra el sitio real**.

## Backlog priorizado (trabaja de arriba a abajo)
1. **Verificar/instalar entorno**: crear venv, `pip install -r requirements.txt`,
   `playwright install chromium`, y confirmar que `rastrillo --help` corre.
2. **Endurecer discovery**: el parseo de Sherlock usa el `.txt`; preferir el JSON
   (`--print-found` / archivo `<user>.json`) y manejar rate-limits y timeouts por sitio.
3. **Resumibilidad real**: si el proceso muere a mitad de un flujo, `run` debe
   retomar desde `current_step` correctamente (probar con una receta dummy/local).
4. **Control desde el dashboard**: botones para "reanudar", "saltar" y "reintentar"
   una cuenta (endpoints POST + acción en DB), no solo lectura.
5. **Verificación de borrado más robusta**: además del `success_selector`, permitir
   `success_url` y `expect_gone` (selector que NO debe existir).
6. **Más recetas reales**: pedir al usuario el output de su `scan` y crear recetas
   verificadas para las plataformas que le salgan.
7. **Tests**: un par de tests con un servidor HTML local de prueba que simule un
   flujo "delete" para validar el motor sin depender de sitios reales.

## Cómo probar sin tocar sitios reales
Monta un HTML local con un botón "Delete account" y un `success` final; apunta una
receta de prueba a `http://localhost:PORT`. Así validas el motor de forma determinista.
