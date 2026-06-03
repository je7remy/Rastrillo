"""Modo IA: localización puntual + bucle agente para conducir flujos sin receta.

Dos modos:

  find_control(page, goal) -> dict|None
      Un solo turno: dado un objetivo, devuelve el selector/texto del control.
      Usado por el step "ai_assist" de las recetas JSON.

  run_agent(page, goal, ...) -> dict
      Bucle de varios turnos: ver → decidir → actuar → repetir. Conduce el
      flujo completo desde la URL de eliminación que dio el directorio,
      pausando si detecta CAPTCHA/2FA o si necesita una confirmación humana.

Privacidad: solo se envía la *estructura* de la página (árbol de
accesibilidad + texto visible). Nunca cookies ni contraseñas. Si no defines
ANTHROPIC_API_KEY, este módulo se desactiva y el motor cae a la degradación
elegante (mostrar el link del directorio para clic manual).
"""
import json
import logging
from typing import Callable, List, Optional

from . import config

log = logging.getLogger("rastrillo.ai")

try:
    import anthropic
except ImportError:
    anthropic = None


# --- Disponibilidad ---------------------------------------------------------
def available() -> bool:
    return bool(anthropic and config.ANTHROPIC_API_KEY)


def _client():
    return anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)


# --- Snapshot de la página -------------------------------------------------
_TEXT_CAP = 3500
_AX_CAP = 7000


def _snapshot(page) -> dict:
    """Estructura mínima que enviamos al modelo. Sin cookies ni contraseñas."""
    try:
        ax = page.accessibility.snapshot(interesting_only=True)
        ax_str = json.dumps(ax)[:_AX_CAP] if ax else ""
    except Exception:
        ax_str = ""
    try:
        text = page.inner_text("body")[:_TEXT_CAP]
    except Exception:
        text = ""
    try:
        url = page.url
        title = page.title()
    except Exception:
        url, title = "", ""
    return {"url": url, "title": title, "text": text, "accessibility": ax_str}


# --- Modo: un solo turno (compat con recetas) ------------------------------
def find_control(page, goal: str) -> Optional[dict]:
    """Devuelve {'selector':...} | {'text':...} | None. Para el step ai_assist."""
    if not available():
        return None
    snap = _snapshot(page)
    prompt = (
        "Eres un asistente que ubica controles de UI en una página web.\n"
        f"OBJETIVO: {goal}\n\n"
        f"URL: {snap['url']}\n"
        f"Título: {snap['title']}\n\n"
        "Árbol de accesibilidad (JSON):\n"
        f"{snap['accessibility']}\n\n"
        "Texto visible (recortado):\n"
        f"{snap['text']}\n\n"
        "Responde SOLO con un objeto JSON, sin markdown, con una de estas formas:\n"
        '  {"text":"<texto exacto del botón/enlace a pulsar>"}\n'
        '  {"selector":"<selector css válido>"}\n'
        '  {"none":true}  (si no encuentras el control)\n'
    )
    try:
        resp = _client().messages.create(
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
    except Exception as e:
        log.warning("find_control fallo: %s", e)
        return None


# --- Modo: bucle agente (sin receta) ---------------------------------------
_AGENT_SYSTEM = """Eres un agente que opera un navegador (Playwright) para
eliminar o anonimizar una cuenta en un servicio web. Decides la siguiente
acción a partir del estado de la página.

Reglas estrictas:
- Nunca intentes resolver CAPTCHAs ni evadir detección de bots: si detectas
  CAPTCHA/2FA/contraseña, responde con action "need_user".
- Nunca rellenes contraseñas. Si la página pide login, devuelve "need_user".
- Devuelve "done" cuando la página confirme el borrado/anonimizado.
- Devuelve "failed" si llegas a un callejón sin salida explícito.
- Sé conservador: 1 acción por turno.

Respondes SIEMPRE con un único objeto JSON, sin markdown, con una de estas formas:
  {"action":"click","selector":"<css>","reason":"..."}
  {"action":"click","text":"<texto exacto>","reason":"..."}
  {"action":"fill","selector":"<css>","value":"<texto>","reason":"..."}
  {"action":"wait","selector":"<css>","timeout":8000,"reason":"..."}
  {"action":"need_user","reason":"<por qué necesitas humano>"}
  {"action":"done","outcome":"deleted|anonymized|manual","reason":"..."}
  {"action":"failed","reason":"..."}
"""


def _ask_agent(client, goal: str, snapshot: dict, history: List[dict]) -> Optional[dict]:
    user = (
        f"OBJETIVO: {goal}\n\n"
        f"URL actual: {snapshot['url']}\n"
        f"Título: {snapshot['title']}\n\n"
        f"Acciones previas y resultado (más recientes al final):\n"
        f"{json.dumps(history[-6:], ensure_ascii=False)}\n\n"
        f"Árbol de accesibilidad (JSON, recortado):\n{snapshot['accessibility']}\n\n"
        f"Texto visible (recortado):\n{snapshot['text']}\n\n"
        "Tu siguiente acción (JSON):"
    )
    try:
        resp = client.messages.create(
            model=config.AI_MODEL,
            max_tokens=400,
            system=_AGENT_SYSTEM,
            messages=[{"role": "user", "content": user}],
        )
        raw = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
        raw = raw.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        return json.loads(raw)
    except Exception as e:
        log.warning("agente: respuesta no parseable: %s", e)
        return None


def _execute(page, action: dict) -> dict:
    """Aplica la acción a la página. Devuelve {ok: bool, note: str}."""
    kind = action.get("action")
    try:
        if kind == "click":
            if action.get("selector"):
                page.click(action["selector"], timeout=action.get("timeout", 6000))
            elif action.get("text"):
                page.get_by_text(action["text"], exact=False).first.click(timeout=6000)
            else:
                return {"ok": False, "note": "click sin selector ni text"}
            return {"ok": True, "note": "click ejecutado"}
        if kind == "fill":
            page.fill(action["selector"], action.get("value", ""))
            return {"ok": True, "note": "fill ejecutado"}
        if kind == "wait":
            sel = action.get("selector")
            if sel:
                page.wait_for_selector(sel, timeout=action.get("timeout", 8000))
            else:
                page.wait_for_load_state("domcontentloaded")
            return {"ok": True, "note": "wait ejecutado"}
        return {"ok": False, "note": f"acción desconocida: {kind}"}
    except Exception as e:
        return {"ok": False, "note": f"{type(e).__name__}: {e}"}


def run_agent(
    page,
    goal: str,
    max_iters: int = 8,
    on_action: Optional[Callable[[dict], None]] = None,
) -> dict:
    """Conduce un flujo de borrado/anonimizado con IA. Devuelve:
        {"outcome": "done|need_user|failed|exhausted|no_ai",
         "result_status": "deleted|anonymized|manual"|None,
         "reason": str,
         "log": [acciones ejecutadas]}
    """
    if not available():
        return {"outcome": "no_ai", "result_status": None,
                "reason": "ANTHROPIC_API_KEY no definida; IA desactivada",
                "log": []}

    client = _client()
    history: List[dict] = []

    for i in range(max_iters):
        snap = _snapshot(page)
        action = _ask_agent(client, goal, snap, history)
        if action is None:
            return {"outcome": "failed", "result_status": None,
                    "reason": "el modelo no devolvió JSON válido", "log": history}

        kind = action.get("action")
        log.info("agente turno %s: %s (%s)", i, kind, action.get("reason", "")[:80])
        if on_action:
            try:
                on_action(action)
            except Exception:
                pass

        if kind == "need_user":
            history.append({"turn": i, "action": action, "result": None})
            return {"outcome": "need_user", "result_status": None,
                    "reason": action.get("reason", "intervención humana requerida"),
                    "log": history}
        if kind == "done":
            history.append({"turn": i, "action": action, "result": None})
            return {"outcome": "done",
                    "result_status": action.get("outcome", "deleted"),
                    "reason": action.get("reason", ""),
                    "log": history}
        if kind == "failed":
            history.append({"turn": i, "action": action, "result": None})
            return {"outcome": "failed", "result_status": None,
                    "reason": action.get("reason", ""), "log": history}

        result = _execute(page, action)
        history.append({"turn": i, "action": action, "result": result})

    return {"outcome": "exhausted", "result_status": None,
            "reason": f"límite de {max_iters} iteraciones sin completar",
            "log": history}


# --- Búsqueda web localizada (Capa 2 del resolver) -------------------------
_WEB_SEARCH_TOOL = {
    "type": "web_search_20250305",
    "name": "web_search",
    "max_uses": 4,
}


def web_search_deletion(host: str, query: str, language: str = "en") -> Optional[dict]:
    """Pide al modelo que busque cómo eliminar la cuenta para `host`.

    Devuelve dict con {url, notes, type: full|anonymize|email_only} o None si
    la herramienta no está disponible / el modelo no devuelve JSON parseable.

    Sin ANTHROPIC_API_KEY → None (el resolver salta a Capa 3).
    """
    if not available():
        return None
    instructions = (
        "Tu trabajo es encontrar la forma OFICIAL de eliminar (o anonimizar) la "
        f"cuenta en el dominio '{host}'. Usa la herramienta web_search con la "
        f"consulta sugerida (en el idioma del sitio: {language}). Lee 1-3 "
        "fuentes oficiales (preferentemente del propio dominio o documentación "
        "del servicio).\n\n"
        f"Consulta sugerida: {query!r}\n\n"
        "Cuando hayas leído lo suficiente, responde SOLO con un objeto JSON, "
        "sin markdown, con esta forma:\n"
        '  {"url": "<URL directa al flujo de borrado>", '
        '"notes": "<2-4 líneas: pasos clave>", '
        '"type": "full|anonymize|email_only"}\n'
        "Si la única forma es escribir a soporte, usa type=\"email_only\" y "
        "deja en notes la dirección de contacto si la encuentras.\n"
        "Si no encuentras NADA fiable, responde {\"none\": true}."
    )
    try:
        resp = _client().messages.create(
            model=config.AI_MODEL,
            max_tokens=1500,
            tools=[_WEB_SEARCH_TOOL],
            messages=[{"role": "user", "content": instructions}],
        )
    except Exception as e:
        log.warning("web_search no disponible: %s", e)
        return None

    # El modelo puede devolver varios bloques (tool_use + text). Cogemos el
    # último bloque de texto y lo parseamos.
    raw = ""
    for b in resp.content:
        if getattr(b, "type", "") == "text":
            raw = b.text
    raw = (raw or "").strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except Exception:
        # A veces hay prosa alrededor del JSON; cogemos el primer {...}.
        import re as _re
        m = _re.search(r"\{[\s\S]*\}", raw)
        if not m:
            return None
        try:
            data = json.loads(m.group(0))
        except Exception:
            return None
    if data.get("none"):
        return None
    return data if data.get("url") else None
