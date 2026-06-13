"""Recetas auto-generadas a partir de ejecuciones exitosas del agente IA.

Cuando `engine._run_ai_flow` cierra con `outcome=done`, el motor llama aquí
con el log de acciones que el agente ejecutó. Lo serializamos como una
receta JSON determinista en `~/.rastrillo/recipes/<slug>.json`. La próxima
vez que toque esa plataforma, el motor usará la receta (Capa 1 del resolver)
en vez de gastar tokens en el agente.

Diseño y seguridad:
- Solo serializamos pasos estructurales: `goto`, `click`, `wait_for`. Los
  `fill` se OMITEN: una receta nunca debe contener datos rellenados por el
  agente (riesgo de PII / credenciales / texto generado a partir del email).
- Incluimos un `pause` antes del `verify` con un mensaje claro: la primera
  vez que reutilizamos una receta auto-generada queremos confirmación humana
  porque los selectores pueden ser frágiles.
- La receta se marca con `auto_generated: true` y `auto_generated_at`.
- Si la receta sintetizada tiene < 2 pasos accionables, no se guarda
  (probablemente el agente no hizo nada significativo: la página ya estaba
  resuelta o entró directamente a éxito).
- El slug usa el host normalizado. Pisa cualquier receta de usuario previa
  para el mismo slug (es el comportamiento deseado: aprender de lo último
  que funcionó). NO pisa la del paquete: esas viven en otra carpeta y el
  loader (`recipes.load_recipes`) ya resuelve la prioridad usuario > paquete.
"""
from __future__ import annotations
import json
import logging
import time
from pathlib import Path
from typing import List, Optional, Tuple

from . import config
# Tarea 9: el slug usado para nombrar la receta auto-generada vive ahora
# en hostutil; antes había una copia local idéntica a discovery._slugify.
from .hostutil import slugify as _slug

log = logging.getLogger("rastrillo.recipes_auto")


def _build_steps(start_url: str, agent_log: List[dict]) -> List[dict]:
    """Convierte el log del agente (`ai_assist.run_agent.log`) a pasos de receta.

    Entradas típicas del log:
      {turn, action: {action: "click", selector|text, reason}, result: {ok, note}}
      {turn, action: {action: "fill", selector, value}, result: ...}
      {turn, action: {action: "wait", selector, timeout}, result: ...}
      {turn, action: {action: "need_user|done|failed", reason}, result: None}
    """
    steps: List[dict] = [{"action": "goto", "url": start_url}]
    for entry in agent_log or []:
        action = entry.get("action") or {}
        kind = action.get("action")
        result = entry.get("result") or {}
        # Solo serializamos acciones efectivamente ejecutadas con éxito.
        if not isinstance(result, dict) or not result.get("ok"):
            # need_user/done/failed/no-op: no son ejecuciones de página.
            continue
        if kind == "click":
            sel = action.get("selector")
            txt = action.get("text")
            if sel:
                steps.append({"action": "click", "selector": sel})
            elif txt:
                # Playwright admite "text=..." como selector.
                steps.append({"action": "click", "selector": f"text={txt}"})
        elif kind == "wait":
            sel = action.get("selector")
            if sel:
                step = {"action": "wait_for", "selector": sel}
                if action.get("timeout"):
                    step["timeout"] = int(action["timeout"])
                steps.append(step)
        elif kind == "fill":
            # OMITIDO POR DISEÑO: el value podría contener PII/texto generado.
            # Si en el futuro queremos persistir el campo (sin value), añadir
            # un step `fill_random` aquí.
            log.debug("omitiendo fill (selector=%s) por seguridad", action.get("selector"))
            continue
    return steps


def synthesize_and_save(
    host: str,
    start_url: str,
    agent_log: List[dict],
    result_status: str,
    display_name: Optional[str] = None,
) -> Optional[Tuple[Path, dict]]:
    """Si el log es suficientemente informativo, genera y guarda la receta.

    Devuelve `(path, recipe_dict)` si se guardó, `None` si se descartó.
    """
    if result_status not in ("deleted", "anonymized"):
        return None

    slug = _slug(host)
    if not slug:
        log.warning("no pude derivar slug del host %r; descarto receta", host)
        return None

    steps = _build_steps(start_url, agent_log)
    # Contamos pasos "accionables" reales (sin contar el goto inicial).
    actionable = [s for s in steps if s.get("action") in ("click", "wait_for")]
    if len(actionable) < 1:
        log.info("agent_log demasiado corto (slug=%s, %d accionables); no guardo receta",
                 slug, len(actionable))
        return None

    # Pause humano + verify final. El primer rerun pasa por el humano.
    steps.append({
        "action": "pause",
        "message": ("Receta auto-generada por el agente IA. Confirma que la "
                    "página muestra el éxito antes de continuar."),
    })

    # Verify heurístico: si el primer click del agente fue por CSS selector
    # (no por texto), asumimos que ese control DESAPARECE en la página de
    # éxito. Es una señal mucho más útil que el genérico body=presente.
    # Si no podemos derivar nada, caemos al fallback antiguo.
    first_click_sel = next(
        (s["selector"] for s in steps
         if s.get("action") == "click"
         and s.get("selector")
         and not s["selector"].startswith("text=")),
        None,
    )
    if first_click_sel:
        verify_step = {
            "action": "verify",
            "expect_gone": first_click_sel,
            "on_success": result_status,
        }
    else:
        verify_step = {
            "action": "verify",
            "success_selector": "body",
            "on_success": result_status,
        }
    steps.append(verify_step)

    recipe = {
        "platform": slug,
        "display_name": display_name or host,
        "difficulty": "auto",
        "deletion_type": "full" if result_status == "deleted" else "anonymize",
        "url": start_url,
        "auto_generated": True,
        "auto_generated_at": time.time(),
        "source_host": host,
        "steps": steps,
    }

    config.ensure_dirs()
    out = config.USER_RECIPES / f"{slug}.json"
    out.write_text(json.dumps(recipe, ensure_ascii=False, indent=2), encoding="utf-8")
    log.info("receta auto-generada para %s -> %s (%d pasos accionables)",
             host, out, len(actionable))
    return out, recipe
