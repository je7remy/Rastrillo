# Transparencia y modelo de amenazas

> **No te pedimos confianza, te damos cómo verificar.**
>
> Esta página describe exactamente qué hace Rastrillo en tu máquina, qué
> sale a internet, qué nunca toca y cómo comprobarlo tú mismo leyendo el
> código. Cada afirmación enlaza a un archivo y una línea concretos. Si
> encuentras una discrepancia entre lo que dice este documento y lo que
> hace el código, **es un bug, no un detalle** — mándamelo a
> [je7remy@gmail.com](mailto:je7remy@gmail.com).

---

## Índice

- [Lo que Rastrillo NUNCA hace](#lo-que-rastrillo-nunca-hace)
- [Tabla de tráfico de red saliente](#tabla-de-tráfico-de-red-saliente)
- [Notas honestas](#notas-honestas)
- [Cómo verificarlo tú mismo](#cómo-verificarlo-tú-mismo)
- [Dependencias auditables](#dependencias-auditables)

---

## Lo que Rastrillo NUNCA hace

Cada afirmación es comprobable leyendo el archivo citado.

- **Nunca guarda contraseñas, tokens de sesión ni cookies.**
  La autenticación vive en el perfil persistente de Chromium en
  `~/.rastrillo/browser-profile/` (`config.py:7`). El esquema de la base
  de datos no tiene columnas para credenciales (`db.py:50-78`). El
  snapshot del audit log enumera los campos que sí guarda — y ninguno es
  una credencial (`audit.py:79-89`). Las recetas auto-generadas a partir
  de ejecuciones del agente IA **omiten** explícitamente los pasos
  `fill` para no persistir PII tecleada (`recipes_auto.py:73-78`).

- **Nunca lee tus pulsaciones de teclado.**
  No hay listeners `keydown`, no se usa `page.keyboard`, no se conecta a
  los eventos `console`/`request`/`response` de Playwright (grep negativo
  sobre `rastrillo/`). Cuando tecleas tu contraseña en el Chromium, esa
  pulsación va del navegador al sitio remoto — Rastrillo no está en el
  medio.

- **Nunca intenta resolver CAPTCHAs ni evadir detección de bots.**
  El system prompt del agente IA lo prohíbe explícitamente
  (`ai_assist.py:157-177`): si detecta CAPTCHA, 2FA o pantalla de login,
  responde `need_user` y el motor pausa la cuenta en `awaiting_user`.

- **Nunca envía analytics, telemetría ni reportes de error a terceros.**
  Grep sobre `analytics|telemetr|phone_home|sentry|amplitude|mixpanel|
  segment|google_analytics`: 0 coincidencias. No hay auto-update ni check
  de versiones.

- **Nunca toca cuentas que no has confirmado como tuyas.**
  Cualquier acción destructiva exige `owned=1` o `confirm_owned=true` en
  el body de la petición; si faltan, el endpoint devuelve 412 con el
  resumen de la cuenta para que confirmes (`server.py:271-289`). Las
  exposiciones de HIBP no son tratadas como cuentas hasta que pulsas
  "Sí, tengo cuenta aquí" (`server.py:420-439`).

- **Nunca habla con tu red interna.**
  El SSRF guard del resolver (`resolver.py:171-216`) sólo permite
  `https://` con IPs públicas; rechaza loopback, RFC1918, link-local,
  reservadas y multicast antes de cada GET. El módulo Domain Intelligence
  aplica el mismo criterio antes de abrir el socket WHOIS:43
  (`domain_intel.py:109-137`), por si una referencia de servidor estuviera
  envenenada.

- **Nunca acepta peticiones de otro origen.**
  Allowlist de hosts (anti DNS rebinding) en `server.py:107-116` y
  `config.py:41-46`: cualquier `Host:` distinto de
  `127.0.0.1:8765`/`localhost:8765`/`testserver` recibe 403.

- **Nunca expone el panel sin token.**
  Todos los `POST` y todos los `GET /api/*` exigen el token de auth
  generado al arrancar (`server.py:118-141`). El token va por header
  `X-Rastrillo-Token`; la única excepción es la entrega inicial por URL,
  que el frontend limpia con `history.replaceState`
  (`static/app.js:46-57`).

---

## Tabla de tráfico de red saliente

Lo que sale del proceso de Rastrillo a internet. Punto por punto, con
archivo y línea para que lo confirmes tú.

| # | Destino | Cuándo se dispara | Qué se envía | Opt-in | Archivo:línea |
|---|---|---|---|---|---|
| 1 | `raw.githubusercontent.com/jdm-contrib/justdelete.me/master/_data/sites.json` | Primer arranque sin caché y refresh manual o automático si la caché supera `RASTRILLO_DIR_MAX_AGE_DAYS` (default 30 días) | GET con `User-Agent: rastrillo/0.1 (+local-tool)`. Cero datos del usuario | No (pero idempotente) | `directory.py:34-37`, `:125-130`; planificador en `jobs.py:264-305` |
| 2 | `haveibeenpwned.com/api/v3/breachedaccount/{email}` | Una llamada por email pasado a `discover()`, **sólo** si `RASTRILLO_HIBP_API_KEY` está definida | URL-encoded del email + headers `hibp-api-key`, `User-Agent: rastrillo-personal-deletion-tool`, `Accept: application/json` | **Sí** (requiere clave) | `hibp.py:28`, `:32-33`, `:53-63` |
| 3 | `api.anthropic.com` (vía SDK `anthropic`) | Sólo si el SDK está instalado **Y** `ANTHROPIC_API_KEY` está definida. Tres puntos: `find_control` (1 turno), `run_agent` (≤ 8 iter), `web_search_deletion` (capa 2 del resolver, `tool.max_uses=4`) | Snapshot generado por `_snapshot()`: `url`, `title`, `inner_text("body")[:3500]`, accessibility tree (`interesting_only=True`) recortado a 7000 chars, objetivo en lenguaje natural, log de las últimas 6 acciones | **Sí** (requiere clave) | `ai_assist.py:87-88`, `:100-116`, `:140-145`, `:192-197`, `:384-389` |
| 4 | Sitios del usuario (`https://{host}/...`) | Capas 3-4 del resolver, sólo cuando 1-2 no resolvieron. Filtrado por `_is_safe_url` y throttle por host | GET con `User-Agent: rastrillo/0.1 (+local, contact via dashboard)` y `Accept-Language: en,es,ru;q=0.8`. Throttle por host vía `RASTRILLO_PROBE_DELAY` (default 1.5 s) | No | `resolver.py:171-251`, `:309-335`, `:420-461` |
| 5 | URL del perfil tras una acción de borrado | Tras `done` del agente IA o un step `verify` del motor, para confirmar `404`/`410` o keyword de "cuenta cerrada" multi-idioma | GET con `User-Agent: rastrillo/0.1 (+post-deletion check)` | No | `engine.py:67-106` (GET en `:82-90`) |
| 6 | Sitios donde decides borrar (Chromium persistente) | Cada cuenta que procesas | Tráfico de tu propio Chromium con tus cookies del perfil persistente. Rastrillo abre la URL pero **no** extrae cookies ni storage state | No | `engine.py:204-208` |
| 7 | Backend local (`/api/*`) | UI | Sólo URLs relativas. Cero `fetch` a dominios externos, cero CDN, cero tracker | – | `static/app.js:90,113` |
| 8 | Subprocesos: Sherlock, Holehe, opcionalmente Maigret | Durante `discover()` | Cada binario hace sus propios GETs a las plataformas que conoce. **Rastrillo no controla ni inspecciona ese tráfico** | Maigret opt-in (auto-detecta en `PATH`); Sherlock/Holehe siempre | `discovery.py:171-182, 239-242, 341-348` |
| 9 | Servidores WHOIS (TCP **puerto 43**): `whois.iana.org` y, siguiendo sus referencias, el WHOIS del registro/registrar del TLD | Solo cuando **tú** lanzas un análisis desde la sección "Inteligencia de dominio" (`POST /api/domain/analyze`) | El dominio consultado en texto plano (`<dominio>\r\n`). Cero datos del usuario, cero credenciales. Antes de conectar, el guard `_host_resolves_public` verifica que el servidor resuelve a IPs públicas (mismo criterio que el SSRF guard del resolver) | No (lo disparas tú por dominio) | `domain_intel.py:109-137` (guard), `:139-162` (socket 43), `:277-339` (recursión) |
| 10 | Resolutor DNS del sistema (UDP/TCP **53**, vía `dnspython`) | Mismo disparo que la fila 9 | Consultas A/MX/NS/TXT del dominio al resolutor que tengas configurado (local/ISP). `dnspython` usa la config DNS del SO; no hardcodeamos ningún resolutor de terceros | No (lo disparas tú por dominio) | `domain_intel.py:341-358` (`_dns_query`), `:360-440` (`lookup_dns`) |

**La correlación NO hace HTTP ni red extra.** `correlate()`
(`domain_intel.py:488-530`) solo interpreta los registros DNS/WHOIS ya
obtenidos en las filas 9-10 para inferir proveedores (MX→correo, NS→DNS,
SPF/verificaciones TXT→SaaS). No abre ninguna conexión nueva, por lo que no
necesita el guard anti-SSRF. Las inferencias son heurísticas y van
etiquetadas con su confianza, nunca presentadas como hechos confirmados.

Eso es todo. Si tu firewall ve algo distinto saliendo del proceso de
Rastrillo o del Chromium que lanza, dímelo: es un bug.

---

## Notas honestas

Cosas que el código hace tal cual están descritas, pero que merece la
pena explicar para que no te lleves una sorpresa.

1. **El texto enviado a Anthropic puede incluir tu username o email** si
   están renderizados en la página objetivo (cabeceras del tipo
   "Hola, je7remy@gmail.com"). Es texto visible, no credenciales; el
   snapshot va recortado a 3500 chars de `inner_text("body")` y 7000
   chars del árbol de accesibilidad (`ai_assist.py:96-116`). Los valores
   de `<input type=password>` quedan fuera porque Playwright no los
   incluye en `inner_text`.

2. **El SSRF guard tiene una ventana TOCTOU** entre la resolución DNS y
   la conexión, documentada en el propio código (`resolver.py:184-187`).
   En este modelo de amenaza (resolver corre local y la respuesta sólo
   alimenta regex de paths/emails) es un riesgo aceptado. Si te
   preocupa, ejecuta Rastrillo detrás de un firewall que bloquee
   conexiones hacia tu red privada.

3. **Sherlock, Holehe y Maigret son binarios externos.** Sus GETs a
   plataformas sociales **no** los hace Rastrillo. Para auditar su
   comportamiento de red, ve a sus repos:
   [Sherlock](https://github.com/sherlock-project/sherlock),
   [Holehe](https://github.com/megadose/holehe),
   [Maigret](https://github.com/soxoj/maigret).

4. **El audit log local (`~/.rastrillo/audit.json`) no está firmado.**
   Protege contra fallos de Rastrillo (te queda rastro de qué se borró y
   cuándo), no contra un atacante con acceso a tu `~/.rastrillo/`. Si
   alguien tiene acceso de escritura a esa carpeta, también tiene acceso
   a tu perfil de Chromium — el audit log no es la capa que te protege
   de eso.

5. **`RASTRILLO_ALLOW_QUERY_TOKEN=1` reduce la seguridad del token.**
   Está pensada para tests (`server.py:73-83`) y el propio código lo
   marca "NO HABILITAR EN PRODUCCIÓN" (`:128`). Si la activas, el token
   puede aparecer en el historial del navegador y en cabeceras
   `Referer`. Déjala desactivada.

---

## Cómo verificarlo tú mismo

Tres niveles de verificación, del más rápido al más estricto.

### A. Lee los archivos que tocan red o credenciales

La lista completa, en orden de importancia:

```
rastrillo/ai_assist.py       # capa IA opcional (Anthropic)
rastrillo/hibp.py            # cliente HIBP opt-in
rastrillo/directory.py       # descarga del directorio JustDeleteMe
rastrillo/resolver.py        # GETs a los sitios + SSRF guard
rastrillo/engine.py          # motor Playwright + re-visita post-borrado
rastrillo/server.py          # auth middleware + allowlist de hosts
rastrillo/config.py          # claves, token, allowlist
rastrillo/audit.py           # snapshot que se persiste por acción
rastrillo/db.py              # esquema de SQLite
rastrillo/recipes_auto.py    # exclusión de `fill` al serializar
rastrillo/static/app.js      # frontend: fetch sólo a `/api/*`
```

Para tener la foto completa son ~3000 líneas. Para validar el modelo de
amenaza descrito aquí, basta con `ai_assist.py`, `hibp.py`,
`directory.py`, `resolver.py`, `server.py` y `static/app.js`.

### B. Pon un monitor de red y comprueba que sólo sale lo de la tabla

Arranca Rastrillo y observa el tráfico saliente con la herramienta que
prefieras:

- **Wireshark** (`tshark -i any -f "host not 127.0.0.1"`) — todos los
  paquetes que salen.
- **mitmproxy** con `export HTTPS_PROXY=http://127.0.0.1:8080` — descifra
  TLS y enseña cuerpo y cabeceras (sirve para Rastrillo, no para el
  Chromium persistente, que tendría que confiar en tu CA).
- **Tu firewall** (UFW, pf, Windows Defender Firewall) — bloquea por
  defecto y comprueba qué destinos pide permiso para alcanzar.

Lo único que deberías ver del proceso de Rastrillo:

- `raw.githubusercontent.com` (sólo al primer arranque o tras 30 días);
- `haveibeenpwned.com` (sólo si activaste `RASTRILLO_HIBP_API_KEY`);
- `api.anthropic.com` (sólo si activaste `ANTHROPIC_API_KEY`);
- los hosts de los sitios que estás resolviendo (capas 3-4 del resolver
  y re-visita post-borrado del motor).

Del Chromium persistente verás los sitios donde te logues — eso es por
diseño, lo decides tú al pulsar "Eliminar" en el panel.

### C. Corre Rastrillo "en seco" sin claves

Quita las claves opcionales y activa el modo simulación:

```bash
unset ANTHROPIC_API_KEY
unset RASTRILLO_HIBP_API_KEY
RASTRILLO_DRY_RUN=1 ./rastrillo.sh
```

```powershell
$env:ANTHROPIC_API_KEY = ""
$env:RASTRILLO_HIBP_API_KEY = ""
$env:RASTRILLO_DRY_RUN = "1"; .\rastrillo.ps1
```

En este modo el flujo es completo (escaneo, resolver, panel) pero
**ninguna acción destructiva real ocurre**: cada cuenta sobre la que
actuarías pasa al estado `dry_run` con un registro de lo que habría
pasado. Buen entorno para ver el comportamiento sin riesgo y sin
contratar Anthropic ni HIBP.

---

## Dependencias auditables

- **`requirements.lock`** — versiones fijadas con `pip-compile` (176
  líneas). Reproducible bit-a-bit.
- **CI público con `pip-audit`** — el job `audit` de
  [`.github/workflows/ci.yml`](.github/workflows/ci.yml) corre
  `pip-audit --strict -r requirements.lock` en cada push y PR a `main`.
  Si aparece una CVE en una dependencia fijada, el CI rompe y se
  bloquea el merge hasta regenerar el lock.
- **Tests con `unittest` de stdlib** — 189 tests, sin dependencias de
  testing nuevas (`tests/`). Cada test corre con su propio
  `RASTRILLO_HOME` en tempdir. Algunos cubren explícitamente los
  invariantes de seguridad (`tests/test_onboarding_and_auth.py`,
  `tests/test_state_transitions.py`).

Para regenerar el lock cuando cambien dependencias:

```bash
pip install pip-tools
python -m piptools compile pyproject.toml -o requirements.lock
```

---

## Si esto no te basta

Tengo razones para creer que el código hace lo que dice este documento,
pero si necesitas certeza absoluta, las opciones son:

1. **Ejecuta Rastrillo en una VM aislada** sin acceso a tu red interna,
   con un firewall que sólo permita los hosts de la tabla de arriba.
2. **Lee el commit del que vas a partir** y fija esa versión: si el día
   de mañana introduzco una conexión nueva, tu copia local no se
   actualiza sola (Rastrillo no se auto-actualiza).
3. **Reporta cualquier discrepancia** a [je7remy@gmail.com](mailto:je7remy@gmail.com).
   Si encuentras una conexión saliente que no esté en la tabla, es un
   hallazgo de seguridad y respondo en máximo 72 horas (ver
   [SECURITY.md](SECURITY.md)).
