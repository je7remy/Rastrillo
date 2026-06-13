"""Sistema de recetas (modular y extensible).

Cada plataforma es un archivo JSON. Las recetas del paquete viven en
rastrillo/recipes/*.json y las tuyas en ~/.rastrillo/recipes/*.json
(estas últimas tienen prioridad, así puedes corregir un flujo cuando la web cambie
sin tocar el código).

Esquema de una receta:
{
  "platform": "reddit",                 # slug único (sin espacios)
  "display_name": "Reddit",
  "difficulty": "easy|medium|hard",
  "deletion_type": "full|anonymize|manual",
  "url": "https://...",                 # punto de entrada
  "login_check": "css selector que SOLO existe si ya iniciaste sesión",
  "steps": [ <paso>, ... ]
}

Tipos de paso (action):
  {"action":"goto", "url":"..."}
  {"action":"ensure_login", "login_url":"...", "check":"<css>"}
       -> si no estás logueado, abre login_url y pausa hasta que entres a mano
  {"action":"click", "selector":"<css o text=...>", "optional":true}
  {"action":"fill", "selector":"<css>", "value":"<texto>"}
  {"action":"fill_random", "selector":"<css>", "kind":"name|email|text"}
       -> para anonimizar: rellena con basura aleatoria
  {"action":"wait_for", "selector":"<css>", "timeout":15000}
  {"action":"pause", "message":"Resuelve el CAPTCHA/2FA y confirma en el navegador"}
       -> marca awaiting_user; el motor espera tu OK
  {"action":"verify", "success_selector":"<css>", "on_success":"deleted|anonymized"}
  {"action":"ai_assist", "goal":"encuentra y pulsa el botón de eliminar cuenta"}
       -> usa el LLM para localizar el control cuando no hay selector fijo

Los selectores aquí son PUNTOS DE PARTIDA. Las webs cambian su HTML seguido;
cuando un paso falle, ajusta el selector en tu receta de usuario. Por eso el
sistema es extensible: la lógica no se toca, solo el JSON.
"""
import json
from . import config


def load_recipes():
    recipes = {}
    for folder in (config.PKG_RECIPES, config.USER_RECIPES):
        if not folder.exists():
            continue
        for f in folder.glob("*.json"):
            try:
                data = json.loads(f.read_text())
                recipes[data["platform"]] = data  # user pisa al paquete
            except Exception as e:
                print(f"[recetas] error leyendo {f.name}: {e}")
    return recipes


def get_recipe(platform):
    return load_recipes().get(platform)
