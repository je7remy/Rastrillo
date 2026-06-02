# Rastrillo

Herramienta **local** y privada que rastrea tu huella digital, encuentra tus
cuentas regadas por la web y las borra o anonimiza de un solo pase, dejando
solo las que quieres conservar.

Tú das usernames y correos; Rastrillo descubre las cuentas, abre el flujo de
borrado de cada plataforma, lo ejecuta hasta donde se puede automatizar, y te
pide intervención **solo** cuando la plataforma obliga (CAPTCHA / 2FA /
confirmación final). Guarda progreso y reanuda.

> Es para **tus propias cuentas**. Borrar/anonimizar tus datos es tu derecho.

## Por qué NO guarda contraseñas (y por qué eso es lo correcto)

La forma "guarda mis passwords y loguéate solo" suena cómoda pero:
1. Es un riesgo de seguridad enorme (un archivo con todas tus claves en tu disco).
2. Te hace banear: las webs detectan login automatizado (Cloudflare, reCAPTCHA).

En su lugar usamos un **perfil de Chromium persistente** (`~/.rastrillo/browser-profile`).
Inicias sesión **tú, como humano, una sola vez** por sitio; la sesión queda en ese
perfil local y la herramienta la reutiliza. Cero contraseñas almacenadas, cero
servidores externos.

## Lo que se automatiza vs. lo que no

| Etapa | ¿Automático? |
|---|---|
| Descubrir cuentas (Sherlock + Holehe) | ✅ |
| Abrir la página de borrado correcta | ✅ (recetas) |
| Navegar el flujo de clicks conocido | ✅ (recetas) |
| Encontrar el botón cuando no hay receta | ⚙️ best-effort (modo IA) |
| Anonimizar perfiles sin borrado total | ✅ (plantilla) |
| Progreso persistente + reanudar | ✅ (SQLite) |
| Resolver CAPTCHA / 2FA | ❌ por diseño — te lo pide |
| Confirmación final de borrado | 🙋 te lo pide (1 click/Enter) |

## Instalación

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
pip install -e .              # registra los comandos `rastrillo` y `rs`
playwright install chromium
```

`anthropic` es opcional: solo se usa para el **modo IA de respaldo**. Sin
`ANTHROPIC_API_KEY` la herramienta funciona igual con las recetas fijas.

## Uso

```bash
# 1. Descubrir
rastrillo scan -u je7remy -e tucorreo@gmail.com

# 2. Revisar (TikTok/Instagram/LinkedIn/GitHub salen como "conservada" solas)
rastrillo list
rastrillo skip spotify              # conservar algo más, opcional

# 3. Dashboard en vivo (otra terminal)
rastrillo dashboard                 # http://127.0.0.1:8765

# 4. Ejecutar. Se abre el navegador; cuando toque CAPTCHA/2FA te avisa por la terminal.
rastrillo run
rs run --only reddit                # `rs` es el alias corto
```

Si no quieres instalar el paquete, `python cli.py <subcomando>` funciona igual.

## Añadir / arreglar plataformas (extensible)

Las webs cambian su HTML, así que las recetas se rompen con el tiempo. **No tocas
código**: copias un JSON de `rastrillo/recipes/` a `~/.rastrillo/recipes/`, ajustas
los selectores y listo (tus recetas pisan las del paquete). El esquema completo
está documentado en `rastrillo/recipes.py`.

Pasos disponibles: `goto`, `ensure_login`, `click`, `fill`, `fill_random`,
`wait_for`, `ai_assist`, `pause`, `verify`.

## Privacidad

Todo es local: perfil del navegador, base de datos SQLite, logs y screenshots
viven en `~/.rastrillo/`. Lo único que sale a internet son (a) las visitas a
las webs que quieres borrar y (b) si activas el modo IA, la *estructura* de la
página (nunca cookies ni contraseñas) al modelo.

## Realidad que debes asumir

- Las recetas de ejemplo (Reddit, Tumblr, Pinterest, Discord, Spotify) son
  **puntos de partida**; verifica los selectores la primera vez.
- Plataformas marcadas `hard` o sin borrado total: usa la plantilla de anonimizado.
- Algunas webs solo borran por email a soporte → quedan en estado `manual` con el link.
