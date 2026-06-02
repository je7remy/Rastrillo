"""Modo IA de respaldo (opcional).

Cuando un paso "ai_assist" no tiene selector fijo, le pasamos al modelo el
árbol de accesibilidad de la página + el objetivo, y el modelo devuelve el
selector o el texto del control a pulsar. Es best-effort: si falla, el flujo
cae a "pause" para que tú lo hagas a mano.

Privacidad: solo se envía la estructura de la página (roles/textos visibles),
nunca tus contraseñas ni cookies. Si no defines ANTHROPIC_API_KEY, este módulo
se desactiva solo y el motor sigue funcionando con las recetas fijas.
"""
import json
from . import config

try:
    import anthropic
except ImportError:
    anthropic = None


def available():
    return bool(anthropic and config.ANTHROPIC_API_KEY)


def find_control(page, goal):
    """Devuelve {'selector': str} o {'text': str} o None.

    page: objeto Page de Playwright (síncrono).
    """
    if not available():
        return None

    # Snapshot ligero: roles + nombres accesibles de elementos clicables/visibles.
    try:
        snapshot = page.accessibility.snapshot(interesting_only=True)
    except Exception:
        snapshot = None
    visible_text = ""
    try:
        visible_text = page.inner_text("body")[:6000]
    except Exception:
        pass

    client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
    prompt = (
        "Eres un asistente que ubica controles de UI en una página web.\n"
        f"OBJETIVO: {goal}\n\n"
        "Árbol de accesibilidad (JSON):\n"
        f"{json.dumps(snapshot)[:8000]}\n\n"
        "Texto visible (recortado):\n"
        f"{visible_text}\n\n"
        "Responde SOLO con un objeto JSON, sin markdown, con una de estas formas:\n"
        '  {"text":"<texto exacto del boton/enlace a pulsar>"}\n'
        '  {"selector":"<selector css valido>"}\n'
        '  {"none":true}  (si no encuentras el control)\n'
    )
    try:
        resp = client.messages.create(
            model=config.AI_MODEL,
            max_tokens=300,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
        raw = raw.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        data = json.loads(raw)
        if data.get("none"):
            return None
        return data
    except Exception:
        return None
