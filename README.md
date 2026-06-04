# Rastrillo

Monté esto porque acumulé una cantidad ridícula de cuentas online en quince
años y no me apetecía pasar dos tardes pinchando "close account" en cada
sitio. Le das un username o un correo, busca dónde tienes cuentas, decide
cómo se borra cada una y automatiza lo que puede. Lo que no se puede
automatizar te lo deja preparado para que tú hagas un clic.

Es para tus propias cuentas. Borrar tus datos es tu derecho; husmear en los
de otros no.

## El modelo de las dos ventanas

Rastrillo no guarda contraseñas. Suena cómodo guardarlas, pero un archivo con
todas tus claves en disco es un agujero de seguridad y los sitios detectan
login automatizado y te banean al primer intento.

En vez de eso, mantiene un perfil de Chromium persistente en
`~/.rastrillo/browser-profile`. La primera vez que tocas un sitio te logueas
tú en esa ventana, como harías a mano. La sesión queda guardada en ese
perfil y el motor la reutiliza para los pasos automatizables. Cero
contraseñas, cero servidores externos.

Cuando arrancas verás dos ventanas. Un panel en `http://127.0.0.1:8765`
donde decides qué hacer con cada cuenta, y un Chromium que ejecuta los
borrados. CAPTCHA, 2FA o "confirma con tu contraseña" — eso lo resuelves tú
en el Chromium. Cuando termines pulsas Continuar en el panel y el flujo
sigue.

## Instalación

En Linux o Mac:

```
bash install.sh
```

En Windows, desde PowerShell:

```
.\install.ps1
```

Si PowerShell bloquea el script (política de ejecución), lánzalo una vez con
`powershell -ExecutionPolicy Bypass -File .\install.ps1`.

El instalador crea un venv, mete las dependencias, descarga Chromium y deja
el comando `rastrillo` registrado. Es idempotente: lánzalo dos veces y no
pasa nada.

A mano, si prefieres:

```
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
pip install -e .
playwright install chromium
```

`anthropic` es opcional. Sin clave API las capas que usan IA se saltan; el
programa sigue funcionando con links del directorio JustDeleteMe y
borradores GDPR plantillados.

## Uso

```
rastrillo
```

En Windows tienes `.\rastrillo.ps1` o doble-click en `rastrillo.cmd`. En
Linux/Mac, `./rastrillo.sh`. Cualquiera de ellos arranca el servidor local y
abre el navegador con el token en la URL.

Desde la web:

1. Metes username y/o correo y pulsas Escanear. Sherlock, Holehe y
   (opcional) Maigret corren en segundo plano. Los hallazgos van apareciendo.
2. Antes de borrar nada, confirmas cuáles son tuyas. Sherlock genera falsos
   positivos con usernames cortos, así que el triage no es decorativo —
   importa.
3. Para cada cuenta confirmada eliges Eliminar, Anonimizar o Conservar.
   Las profesionales (tiktok, instagram, linkedin, github) salen como
   conservadas por defecto.
4. Si la plataforma pide CAPTCHA o 2FA, la fila pasa a "Esperándote". Lo
   resuelves en el Chromium y pulsas Continuar en el panel.

Hay un modo Simulación (toggle en la barra superior) que ejecuta el flujo
sin hacer la acción destructiva final. Útil para ver qué haría sin
compromiso.

## Cómo decide qué hacer con cada cuenta

Va probando lo siguiente en orden y se queda con lo primero que funcione:

1. Receta JSON si existe para esa plataforma. Las del paquete son ejemplos
   para reddit, tumblr, pinterest, discord, spotify. Los selectores son
   puntos de partida, no verdades reveladas, así que verifícalos la primera
   vez que toques cada sitio.
2. Directorio de [JustDeleteMe](https://justdelete.me) (descargado y
   cacheado en `~/.rastrillo/directory.json`) combinado con un agente IA
   que conduce el flujo en Chromium.
3. Búsqueda web localizada, si tienes clave de Anthropic.
4. Sondeo de paths comunes: `/settings`, `/cuenta`, `/удалить`, etc.
5. Fallback GDPR: extrae el contacto de privacidad del sitio y te redacta
   un correo en el idioma del TLD pidiendo borrado (Art. 17). Esta capa
   siempre devuelve algo, así que ninguna cuenta queda en callejón sin
   salida.

Si pasados 30 días desde que enviaste un correo GDPR no hay respuesta, hay
un botón "Seguimiento" que te genera el follow-up citando el artículo 12.3
del RGPD.

Refrescar el directorio: botón "↻ Directorio" en la web. También hay un
refresh automático al arrancar si la copia local tiene más de 30 días.

## Lo que NO hace

Por diseño:

- No resuelve CAPTCHA ni evade detección de bots. Si la plataforma pone uno,
  tú lo resuelves.
- No guarda contraseñas.
- No procesa varias cuentas en paralelo. Un Chromium, una a una.
- No verifica que las cuentas detectadas sean tuyas. Confía en el username
  y email que le des, y por eso te obliga a confirmar antes de cualquier
  acción destructiva.

En la práctica:

- Las recetas JSON se rompen cuando los sitios cambian su HTML. Cuando una
  receta falle, copia el JSON del paquete a `~/.rastrillo/recipes/` y
  ajusta el selector que falló.
- HaveIBeenPwned (cuando añades `RASTRILLO_HIBP_API_KEY`) te dice dónde
  apareció tu email en brechas. Eso no quiere decir que tengas cuenta
  activa allí, así que esos hits aparecen en una pestaña aparte ("Brechas")
  y solo entran al flujo de borrado cuando confirmas "sí, tengo cuenta aquí".
- Algunos sitios solo borran por correo a soporte. Esos quedan en estado
  `email_draft` con un borrador listo; tú lo envías y lo marcas como enviado
  para que cuente.
- El agente IA puede equivocarse. Antes de sellar una cuenta como
  "eliminada" intenta confirmar visitando la URL del perfil (un GET de
  fuera): si responde 404 o el body dice "cuenta cerrada" lo da por bueno,
  si sigue cargando lo deja en `manual` para que lo revises tú. Si la
  página actual es una redirección a login no cuenta como éxito.

## Cobertura extra (opcional)

Maigret cruza redes mejor que Sherlock. Lo instalas y el siguiente scan lo
usa automáticamente:

```
pip install maigret
```

Si el binario no está en PATH, Rastrillo lo salta sin ruido.

## Dónde vive todo

Todo bajo `~/.rastrillo/`:

- `rastrillo.db` — SQLite con las cuentas y sus eventos.
- `directory.json` — copia local de JustDeleteMe.
- `discovered.json` — caché del resolver por host.
- `browser-profile/` — perfil de Chromium con tus sesiones.
- `backups/` — snapshots automáticos de la DB antes de borrados masivos.
- `audit.json` — registro de todas las acciones destructivas (rota a >5 MB).
- `recipes/` — tus recetas personales; pisan a las del paquete.
- `screenshots/` — capturas del motor en pasos clave y errores.

Lo único que sale del disco es la descarga del directorio, los GET a los
sitios donde vas a borrar, las llamadas a Anthropic si tienes IA activada,
y la consulta a HIBP si has añadido la clave. El modelo IA recibe
estructura de página (árbol de accesibilidad + texto visible), nunca
cookies ni contraseñas.

## Variables de entorno

| Variable | Default | Para qué |
|---|---|---|
| `RASTRILLO_HOME` | `~/.rastrillo` | Cambiar dónde vive todo |
| `RASTRILLO_TOKEN` | aleatorio por arranque | Token de auth del servidor local |
| `RASTRILLO_DRY_RUN` | off | Modo simulación al arrancar |
| `ANTHROPIC_API_KEY` | — | Activa el agente IA y la búsqueda web |
| `RASTRILLO_AI_MODEL` | `claude-sonnet-4-6` | Modelo de Anthropic a usar |
| `RASTRILLO_HIBP_API_KEY` | — | Activa el discovery por HIBP |
| `RASTRILLO_RESOLVER_WORKERS` | 5 | Concurrencia del pool del resolver |
| `RASTRILLO_PROBE_DELAY` | 1.5 | Segundos entre GETs al mismo host |
| `RASTRILLO_DIR_MAX_AGE_DAYS` | 30 | Edad max del directorio antes de refresh auto |
| `RASTRILLO_SHERLOCK_TIMEOUT` | 900 | Timeout global de Sherlock |
| `RASTRILLO_HOLEHE_TIMEOUT` | 600 | Timeout global de Holehe |
| `RASTRILLO_MAIGRET_TIMEOUT` | 300 | Timeout global de Maigret |
| `RASTRILLO_AUDIT_MAX_BYTES` | 5 MB | Tamaño que dispara rotación del audit |

## Probar sin tocar sitios reales

Si quieres validar el motor sin entrar a webs de verdad, monta un HTML
local con un botón "Delete account" y una página de éxito, y apunta una
receta de prueba a `http://localhost:PORT`. Eso es lo que hace
`tests/test_engine_html_local.py`.

Para correr toda la suite:

```
python -m unittest discover -t . -s tests
```

Son unos 30 segundos.
