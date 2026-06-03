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
| Saber dónde se borra cada plataforma | ✅ (directorio JustDeleteMe cacheado) |
| Navegar el flujo de clicks | ✅ (IA en bucle; receta JSON si existe) |
| Anonimizar perfiles sin borrado total | ✅ (IA) |
| Progreso persistente + reanudar | ✅ (SQLite) |
| Resolver CAPTCHA / 2FA | ❌ por diseño — te lo pide |
| Confirmación final de borrado | 🙋 te lo pide (1 click en la web) |
| Plataforma sin entrada en el directorio | 🙋 marca `manual` con el mejor link |

## Instalación

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
pip install -e .              # registra los comandos `rastrillo` y `rs`
playwright install chromium
```

`anthropic` es opcional. Si no defines `ANTHROPIC_API_KEY`, la IA queda
desactivada y Rastrillo cae a degradación elegante: te muestra en el dashboard
el link de borrado de cada plataforma (del directorio) para que lo termines
tú con un clic.

## Uso

```bash
rastrillo
```

Eso es todo. Levanta el dashboard local en `http://127.0.0.1:8765` y abre el
navegador automáticamente. Desde la web:

1. Mete usernames y/o correos en el formulario y pulsa **Escanear**. Sherlock y
   Holehe corren en segundo plano; los hallazgos aparecen solos.
2. Para cada cuenta, decide con un click: **Eliminar**, **Anonimizar** o
   **Conservar**. TikTok/Instagram/LinkedIn/GitHub salen como conservadas solas.
3. Cuando una plataforma exija CAPTCHA/2FA, Rastrillo abre la ventana de
   Chromium y la fila pasa a **Esperándote**. Resuélvelo en el navegador y
   pulsa **Ya lo resolví, continuar** desde la web.

La terminal solo arranca el servidor y se queda viva (Ctrl+C para salir). Para
debug rápido sin abrir UI están `rastrillo list` y `rastrillo run`.

Si no quieres instalar el paquete, `python cli.py` funciona igual.

## ¿Cómo decide Rastrillo qué hacer con cada cuenta?

Tres caminos, por orden:

1. **Receta JSON** (`rastrillo/recipes/<plataforma>.json` o
   `~/.rastrillo/recipes/<plataforma>.json`). Es el fast-path más fiable; las
   del paquete son ejemplos para reddit, tumblr, pinterest, discord, spotify.
2. **Directorio + IA** (caso por defecto): se descarga al primer arranque el
   dataset abierto de [JustDeleteMe](https://justdelete.me) (~2500 sitios) y
   se cachea en `~/.rastrillo/directory.json`. Para cada cuenta, el motor
   abre la URL de borrado del directorio y un agente IA conduce el flujo.
3. **Manual con link** (degradación elegante): si no hay receta, ni entrada
   del directorio, ni IA disponible, la fila queda en `manual` con el mejor
   link disponible para que la cierres con un clic.

Refrescar el directorio: botón "↻ Directorio" en la web, o
`POST /api/directory/refresh`.

Tú **no tienes que escribir recetas**. Solo crea una si quieres tener un
fast-path muy fiable para un sitio concreto. El esquema está documentado en
[rastrillo/recipes.py](rastrillo/recipes.py).

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
