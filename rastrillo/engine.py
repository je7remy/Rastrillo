"""Motor de ejecución. Usa un perfil de Chromium persistente (tus sesiones viven
ahí, no guardamos contraseñas). Ejecuta los pasos de cada receta y, cuando una
plataforma obliga a intervención humana (CAPTCHA/2FA/confirmación final), pausa
y te lo pide por la terminal. Guarda progreso paso a paso para reanudar.

Resumibilidad:
- `current_step` representa "siguiente paso a ejecutar". Se persiste DESPUÉS de
  completar el paso, no antes; así si el proceso muere, al reanudar arrancamos
  exactamente donde quedamos (con re-ejecución, en el peor caso, de un paso
  idempotente).
- Antes de cada run calculamos un hash de la receta. Si cambió desde el último
  run para esa cuenta, reseteamos `current_step` a 0 (los pasos viejos ya no
  corresponden a la receta nueva).
- Ctrl-C en medio de un run marca la cuenta como `queued` con el paso pendiente
  en `last_message`, en lugar de dejarla colgada como `in_progress`.
"""
import hashlib
import json
import random
import string
import time

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

from . import config, db, ai_assist, resolver
from .recipes import get_recipe

ADJ = ["azul", "lento", "gris", "vacio", "mudo", "lejano", "anonimo"]
NOUN = ["usuario", "perfil", "cuenta", "nadie", "x", "ninguno"]


def _recipe_hash(recipe):
    """Hash estable de los pasos de la receta. Cambia el hash → reset de progreso."""
    payload = json.dumps(recipe.get("steps", []), sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _rand(kind):
    if kind == "email":
        u = "".join(random.choices(string.ascii_lowercase + string.digits, k=10))
        return f"{u}@example.invalid"
    if kind == "name":
        return f"{random.choice(ADJ)}_{random.choice(NOUN)}_{random.randint(100,999)}"
    return "".join(random.choices(string.ascii_lowercase + " ", k=40)).strip()


class Engine:
    def __init__(self, headless=False, pause_handler=None):
        self.headless = headless
        # pause_handler: función(account_id, message) -> bloquea hasta que el
        # usuario confirme. Por defecto usa la terminal.
        self.pause_handler = pause_handler or self._terminal_pause

    def _terminal_pause(self, account_id, message):
        db.set_status(account_id, "awaiting_user", message)
        print(f"\n  ⏸  ACCIÓN REQUERIDA: {message}")
        input("     Cuando termines en el navegador, presiona ENTER para continuar...")

    def run_account(self, account_id):
        """Procesa una cuenta. Caminos:

        1. Receta JSON (override): si existe receta para `acc.platform`, se usa
           tal cual — es el modo más fiable.
        2. Sin receta: aplicamos el RESOLVER en capas (directorio → web_search
           → probe → GDPR). Si devuelve kind=auto, conducimos el bucle IA
           sobre la URL. Para semi_auto/email_draft, persistimos la resolución
           y dejamos que la UI ofrezca la acción al usuario (1 clic o correo).

        Invariante: nunca queda una cuenta sin acción concreta. La caída a
        "manual sin instrucciones" desaparece.
        """
        acc = db.get_account(account_id)
        recipe = get_recipe(acc["platform"])

        if recipe:
            self._sync_recipe_hash(account_id, acc, recipe)
            self._run_with_browser(
                account_id,
                lambda page: self._run_steps(account_id, recipe, page),
            )
            return

        # Sin receta → resolver en capas
        host = acc["source_site"] or acc["platform"]
        res = resolver.resolve(host, acc["identifier"] or "")
        db.update_account(
            account_id,
            action_meta=json.dumps(res.to_meta(), ensure_ascii=False),
        )
        if res.url:
            db.update_account(account_id, profile_url=res.url)

        if res.kind == "semi_auto":
            db.set_status(account_id, "semi_auto",
                          f"{res.title}\n{res.notes}\nEnlace: {res.url}")
            return
        if res.kind == "email_draft":
            db.set_status(account_id, "email_draft",
                          f"{res.title}\n{res.notes}\n"
                          f"Para: {res.email_to}\nAsunto: {res.email_subject}")
            return

        # kind == "auto": tenemos URL del resolver e IA disponible (lo garantiza
        # el resolver al elegir 'auto'). Conducimos el bucle de agente.
        self._run_with_browser(
            account_id,
            lambda page: self._run_ai_flow(account_id, res, page, host),
        )

    def _run_with_browser(self, account_id, body):
        """Abre el contexto persistente de Chromium y ejecuta `body(page)`.
        Centraliza el try/except de KeyboardInterrupt y screenshot de error."""
        db.set_status(account_id, "in_progress", "iniciando")
        with sync_playwright() as p:
            ctx = p.chromium.launch_persistent_context(
                str(config.PROFILE_DIR), headless=self.headless,
                viewport={"width": 1280, "height": 900},
            )
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            try:
                body(page)
            except KeyboardInterrupt:
                nxt = (db.get_account(account_id)["current_step"] or 0)
                db.set_status(account_id, "queued",
                              f"interrumpida; reanudar en paso {nxt}")
                raise
            except Exception as e:
                db.set_status(account_id, "failed", f"{type(e).__name__}: {e}")
                self._shot(page, account_id, "error")
            finally:
                ctx.close()

    def _run_ai_flow(self, account_id, res, page, host):
        """Conduce el bucle de IA sobre la URL resuelta. Si el agente no logra
        cerrar el flujo, degrada a `semi_auto` con el link para que el usuario
        lo termine de un clic — nunca a "sin acción"."""
        url = res.url
        notes = res.notes or ""
        db.log(account_id, "info", f"ai-flow: goto {url}")
        page.goto(url, wait_until="domcontentloaded")

        goal = (
            f"Eliminar (o anonimizar) la cuenta en {host}. "
            f"Notas: {notes or '(sin notas)'}. "
            "Localiza y pulsa 'Delete/Close account', sigue los modales y "
            "confirmaciones. Si pide login, CAPTCHA, 2FA, contraseña o un "
            "código por email, responde need_user."
        )

        def _on_action(action):
            kind = action.get("action")
            reason = (action.get("reason") or "")[:120]
            db.log(account_id, "info", f"ai turno: {kind} — {reason}")

        result = ai_assist.run_agent(page, goal, max_iters=8, on_action=_on_action)
        outcome = result["outcome"]
        reason = result.get("reason") or ""

        if outcome == "need_user":
            self.pause_handler(account_id, (reason or "Necesito que confirmes en la ventana.")
                               + f"\nEnlace: {url}")
            db.log(account_id, "info", "ai-flow: reanudando tras pausa humana")
            result2 = ai_assist.run_agent(page, goal, max_iters=3, on_action=_on_action)
            outcome = result2["outcome"]
            reason = result2.get("reason") or reason
            if outcome == "done":
                status = result2.get("result_status") or "deleted"
                db.set_status(account_id, status, f"verificado por IA: {reason}")
                self._shot(page, account_id, "final")
                return

        if outcome == "done":
            status = result.get("result_status") or "deleted"
            db.set_status(account_id, status, f"verificado por IA: {reason}")
            self._shot(page, account_id, "final")
            return

        # Degradación a `semi_auto` (no a 'manual sin acción'): el usuario ve
        # un botón con el link de 1 clic.
        db.set_status(
            account_id, "semi_auto",
            f"IA no pudo cerrar el flujo automáticamente ({outcome}: {reason}). "
            f"Termínalo manualmente desde este enlace:\n{url}"
            + (f"\n\nNotas: {notes}" if notes else ""),
        )
        self._shot(page, account_id, "ai_stopped")

    def _sync_recipe_hash(self, account_id, acc, recipe):
        h = _recipe_hash(recipe)
        prev = acc["recipe_hash"] if "recipe_hash" in acc.keys() else None
        if prev and prev != h:
            db.log(account_id, "warn",
                   "la receta cambió desde el último run; reseteando current_step")
            db.update_account(account_id, current_step=0, recipe_hash=h)
        elif not prev:
            db.update_account(account_id, recipe_hash=h)

    def _run_steps(self, account_id, recipe, page):
        steps = recipe.get("steps", [])
        start = max(0, db.get_account(account_id)["current_step"] or 0)

        # Defensa por si la DB quedó con current_step fuera de rango (p.ej.
        # receta acortada manualmente sin que el hash haya cambiado lo justo).
        if start > len(steps):
            db.log(account_id, "warn",
                   f"current_step={start} > nº pasos={len(steps)}; reseteando a 0")
            start = 0
            db.update_account(account_id, current_step=0)

        for i in range(start, len(steps)):
            step = steps[i]
            action = step.get("action")
            db.log(account_id, "info", f"paso {i}/{len(steps) - 1}: {action}")

            if action == "comment":
                pass
            elif action == "goto":
                page.goto(step["url"], wait_until="domcontentloaded")
            elif action == "ensure_login":
                self._ensure_login(account_id, page, step)
            elif action == "click":
                self._click(page, step)
            elif action == "fill":
                page.fill(step["selector"], step["value"])
            elif action == "fill_random":
                page.fill(step["selector"], _rand(step.get("kind", "text")))
            elif action == "wait_for":
                page.wait_for_selector(step["selector"], timeout=step.get("timeout", 15000))
            elif action == "ai_assist":
                self._ai_step(account_id, page, step)
            elif action == "pause":
                self.pause_handler(account_id, step["message"])
            elif action == "verify":
                self._verify(account_id, page, step)
                # verify es terminal: marcamos el puntero como "consumido" para
                # que un re-run no lo vuelva a ejecutar.
                db.update_account(account_id, current_step=i + 1)
                return
            else:
                db.log(account_id, "warn", f"acción desconocida: {action}")

            # Paso completado → avanzamos el puntero ATÓMICAMENTE al siguiente.
            # Si revienta en el paso `i` antes de llegar aquí, current_step
            # se queda apuntando a `i` y al reanudar lo re-ejecutamos.
            db.update_account(account_id, current_step=i + 1)

        # Si no hubo verify explícito, lo dejamos como hecho-a-confirmar.
        db.set_status(account_id, "manual", "flujo terminado sin verificación automática; revisa.")

    def _ensure_login(self, account_id, page, step):
        check = step.get("check")
        logged = False
        if check:
            try:
                page.wait_for_selector(check, timeout=4000)
                logged = True
            except PWTimeout:
                logged = False
        if not logged:
            page.goto(step["login_url"], wait_until="domcontentloaded")
            self.pause_handler(
                account_id,
                "Inicia sesión TÚ en la ventana del navegador (la sesión queda "
                "guardada en tu perfil local). Cuando estés dentro, continúa.",
            )

    def _click(self, page, step):
        try:
            page.click(step["selector"], timeout=step.get("timeout", 8000))
        except PWTimeout:
            if not step.get("optional"):
                raise

    def _ai_step(self, account_id, page, step):
        target = ai_assist.find_control(page, step["goal"])
        if not target:
            db.log(account_id, "info", "IA no disponible/no encontró control; pausando para humano")
            self.pause_handler(
                account_id,
                f"No pude ubicar automáticamente: '{step['goal']}'. Hazlo tú en el "
                "navegador y luego continúa.",
            )
            return
        try:
            if target.get("text"):
                page.get_by_text(target["text"], exact=False).first.click(timeout=8000)
            elif target.get("selector"):
                page.click(target["selector"], timeout=8000)
        except Exception:
            self.pause_handler(account_id, f"El control sugerido falló. Haz tú: '{step['goal']}'.")

    def _verify(self, account_id, page, step):
        sel = step.get("success_selector")
        ok = True
        if sel:
            try:
                page.wait_for_selector(sel, timeout=8000)
            except PWTimeout:
                ok = False
        self._shot(page, account_id, "final")
        if ok:
            db.set_status(account_id, step.get("on_success", "deleted"), "verificado")
        else:
            db.set_status(account_id, "manual",
                          "No pude verificar el borrado. Revisa el screenshot/log.")

    def _shot(self, page, account_id, tag):
        try:
            path = config.SCREENSHOT_DIR / f"{account_id}_{tag}_{int(time.time())}.png"
            page.screenshot(path=str(path))
        except Exception:
            pass


def process_queue(headless=False):
    """Procesa todas las cuentas en estado found/queued/awaiting_user, una por una."""
    db.init()
    eng = Engine(headless=headless)
    pending = [r for r in db.list_accounts()
               if r["status"] in ("found", "queued", "awaiting_user", "in_progress")]
    print(f"Procesando {len(pending)} cuenta(s)...")
    for acc in pending:
        print(f"\n▶ {acc['platform']} ({acc['identifier']})")
        eng.run_account(acc["id"])
        final = db.get_account(acc["id"])
        print(f"  → {final['status']}: {final['last_message'] or ''}")
