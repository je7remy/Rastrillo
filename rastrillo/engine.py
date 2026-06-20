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
import logging
import random
import re
import string
import time
import urllib.error
import urllib.request

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

from . import config, db, ai_assist, resolver, recipes_auto
from .recipes import get_recipe

_log = logging.getLogger("rastrillo.engine")

# ── Endurecimiento contra falsos "deleted" ──────────────────────────────────
# Una redirección a una página de login también cambia la URL. Si la
# heurística marcase eso como éxito, "deleted" se sellaría sin haber borrado
# nada y perderías la oportunidad de hacer seguimiento.
_LOGIN_PATTERNS = re.compile(
    r"(?:^|[/?&])(login|signin|sign-in|sign_in|auth|sso|session"
    r"|account/login|users/login|connexion|anmelden|вход)"
    r"(?:[/?&]|$)",
    re.IGNORECASE,
)


def _looks_like_login_redirect(url: str) -> bool:
    if not url:
        return False
    return bool(_LOGIN_PATTERNS.search(url))


# Keywords multi-idioma que sugieren que un perfil dejó de existir.
_GONE_KEYWORDS = [
    "user not found", "account not found", "page not found",
    "this account has been", "account has been deleted",
    "account has been removed", "user does not exist",
    "this user does not exist", "page doesn't exist",
    "no se encontró", "usuario no existe", "cuenta eliminada",
    "cuenta no existe", "página no encontrada",
    "учётная запись удалена", "пользователь не найден",
    "страница не найдена",
    "compte supprimé", "page introuvable",
    "konto wurde gelöscht", "seite nicht gefunden",
]


def revisit_profile(profile_url: str, timeout: float = 8.0):
    """GET ligero (sin Playwright) sobre la URL del perfil para confirmar
    su estado tras una acción de borrado.

    Devuelve:
      True   si responde 404 / 410, o si el body contiene keywords de "no
             existe / cuenta cerrada" (en cualquier idioma soportado).
      False  si responde 200 y el body NO contiene esas keywords (el perfil
             probablemente sigue activo).
      None   si no se puede determinar (timeout, error de red, redirect
             extraño): el caller debe ser conservador y NO sellar deleted.
    """
    if not profile_url:
        return None
    # Mismo guard anti-SSRF que usa el resolver para sus GET (Tarea 6): solo
    # https:// y hosts que resuelven a IPs públicas. Sin esto, un profile_url
    # apuntando a la red interna (o al propio dashboard) haría que el motor lo
    # visite. Si no pasa, somos conservadores y NO concluimos (return None).
    if not resolver._is_safe_url(profile_url):
        _log.info("revisit_profile(%s): URL no segura (guard SSRF); no concluyo",
                  profile_url)
        return None
    try:
        req = urllib.request.Request(
            profile_url,
            headers={"User-Agent": "rastrillo/0.1 (+post-deletion check)",
                     "Accept-Language": "en,es,ru;q=0.8"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as r:
            status = r.status
            body = r.read(50_000).decode("utf-8", errors="ignore")
            final_url = r.geturl() or profile_url
    except urllib.error.HTTPError as e:
        if e.code in (404, 410):
            return True
        # 5xx / 403 / 429: ambiguo, no concluimos.
        return None
    except Exception as e:
        _log.info("revisit_profile(%s) sin red: %s", profile_url, e)
        return None
    # Status 200 — buscamos señales positivas de eliminación en el body.
    body_low = body.lower()
    if any(kw in body_low for kw in _GONE_KEYWORDS):
        return True
    # Redirect a login post-cierre = ambiguo, no podemos confirmar borrado.
    if _looks_like_login_redirect(final_url):
        return None
    return False


def _body_has_deletion_keyword(page) -> bool:
    """¿La página actual contiene texto de éxito de borrado?"""
    try:
        text = page.inner_text("body") or ""
    except Exception:
        return False
    text = text.lower()
    return any(kw in text for kw in _GONE_KEYWORDS) or "deleted" in text or "удалено" in text

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
                status_proposed = result2.get("result_status") or "deleted"
                if self._confirm_and_seal(account_id, page, host,
                                          status_proposed, reason):
                    # Aprendizaje solo cuando el sellado fue real.
                    full_log = list(result.get("log") or []) + list(result2.get("log") or [])
                    self._save_synthesized_recipe(account_id, host, url, full_log, status_proposed)
                return

        if outcome == "done":
            status_proposed = result.get("result_status") or "deleted"
            if self._confirm_and_seal(account_id, page, host,
                                      status_proposed, reason):
                self._save_synthesized_recipe(account_id, host, url,
                                              result.get("log") or [], status_proposed)
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

    def _confirm_and_seal(self, account_id, page, host, status_proposed, reason):
        """Aplica el sellado de éxito con la verificación reforzada del Nivel 6
        Parte 2. Devuelve True si selló como deleted/anonymized, False si lo
        degradó a 'manual'.

        Reglas:
          - status_proposed != 'deleted'  → no aplica verificación adicional
            (anonymized sigue su camino: la cuenta no desaparece, solo cambia
            sus datos, no podemos confirmar con un GET).
          - URL final del agente parece un redirect a login → manual.
          - profile_url disponible → revisita HTTP.
              True  → deleted confirmado.
              False → manual con "la URL sigue cargando".
              None  → manual con "no pude confirmar".
          - sin profile_url → exigir 2 señales (URL cambió + keyword en body
            de la página actual). Solo entonces deleted; sino manual.
        """
        acc = db.get_account(account_id)
        profile_url = acc["profile_url"]
        self._shot(page, account_id, "final")

        if status_proposed != "deleted":
            # anonymized u otros estados: confiamos en el agente como antes.
            db.set_status(account_id, status_proposed, f"verificado por IA: {reason}")
            return True

        # Comprobación rápida del lado del browser: ¿estamos en una pantalla
        # de login? Eso es ambiguo: pudo redirigirnos sin haber borrado nada.
        try:
            current_url = page.url or ""
        except Exception:
            current_url = ""
        if _looks_like_login_redirect(current_url):
            msg = (f"El agente dijo 'done' pero la URL actual parece una "
                   f"pantalla de login ({current_url}). Verifica tú que la "
                   f"cuenta está eliminada.")
            db.set_status(account_id, "manual", msg)
            return False

        # Opción A: revisitar profile_url (más fiable).
        if profile_url:
            verdict = revisit_profile(profile_url)
            if verdict is True:
                db.set_status(
                    account_id, "deleted",
                    f"verificado: revisita a {profile_url} indica que no existe ({reason})",
                )
                return True
            if verdict is False:
                db.set_status(
                    account_id, "manual",
                    f"El agente reportó éxito pero {profile_url} sigue "
                    f"cargando como cuenta activa. Revisa y, si está "
                    f"realmente cerrada, marca como hecha desde la UI.",
                )
                return False
            # verdict is None → no pude confirmar; conservador → manual.
            db.set_status(
                account_id, "manual",
                f"El agente reportó éxito pero no pude confirmar el borrado "
                f"haciendo una revisita a {profile_url} (sin red, timeout o "
                f"respuesta ambigua). Verifica tú.",
            )
            return False

        # Opción B: sin profile_url, exigimos dos señales en la página actual.
        # Señal 1: la URL cambió (no es la URL de entrada del flujo).
        # Señal 2: keyword de borrado en el body visible.
        try:
            entry_url = (acc["profile_url"] or "")
        except Exception:
            entry_url = ""
        url_changed = bool(current_url) and current_url != entry_url
        has_keyword = _body_has_deletion_keyword(page)
        if url_changed and has_keyword:
            db.set_status(
                account_id, "deleted",
                f"verificado: URL cambió + keyword en página ({reason})",
            )
            return True
        # Una sola señal o ninguna → manual.
        db.set_status(
            account_id, "manual",
            "El agente reportó éxito pero no pude confirmar con dos "
            "señales (URL cambiada + keyword de borrado). Verifica tú.",
        )
        return False

    def _save_synthesized_recipe(self, account_id, host, url, agent_log, status):
        """Si el agente cerró el flujo, persistimos la secuencia como receta
        determinista en ~/.rastrillo/recipes/. Es best-effort: cualquier fallo
        se loggea pero no rompe el motor."""
        try:
            res = recipes_auto.synthesize_and_save(host, url, agent_log, status)
            if res:
                path, _recipe = res
                db.log(account_id, "info",
                       f"receta auto-generada guardada en {path.name}")
        except Exception as e:
            db.log(account_id, "warn", f"no pude guardar receta auto-generada: {e}")

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
        """Verifica el cierre del flujo. Combina hasta 4 criterios; todos los
        que estén presentes deben pasar (AND).

          success_selector: CSS que DEBE estar presente tras la acción
          success_url     : URL (subcadena, normalizada minúsculas) que DEBE
                            coincidir con la URL final
          success_text    : substring (case-insensitive) presente en el body
          expect_gone     : selector que NO debe existir (login form re-aparece,
                            perfil dejó de cargarse, etc.)

        Si no hay ningún criterio, asumimos éxito y dejamos `manual` con un
        aviso para que el humano revise (compatible con recetas viejas).
        """
        checks = []
        sel = step.get("success_selector")
        url_expect = step.get("success_url")
        text_expect = step.get("success_text")
        gone = step.get("expect_gone")

        if sel:
            try:
                page.wait_for_selector(sel, timeout=8000)
                checks.append(("success_selector", True, sel))
            except PWTimeout:
                checks.append(("success_selector", False, sel))

        if url_expect:
            try:
                cur_url = (page.url or "").lower()
            except Exception:
                cur_url = ""
            ok_url = url_expect.lower() in cur_url
            checks.append(("success_url", ok_url, url_expect))

        if text_expect:
            try:
                body_text = (page.inner_text("body") or "").lower()
            except Exception:
                body_text = ""
            ok_text = text_expect.lower() in body_text
            checks.append(("success_text", ok_text, text_expect))

        if gone:
            # `query_selector` devuelve None si no existe; pasa el check.
            try:
                exists = page.query_selector(gone) is not None
            except Exception:
                exists = False   # error consultando = asumimos que no está
            checks.append(("expect_gone", not exists, gone))

        self._shot(page, account_id, "final")

        if not checks:
            db.set_status(account_id, "manual",
                          "No tenías criterios de verificación en la receta; "
                          "revisa el screenshot/log para confirmar.")
            return

        all_ok = all(c[1] for c in checks)
        detail = ", ".join(f"{name}={'ok' if ok else 'fail'} ({val!r})"
                           for name, ok, val in checks)
        if all_ok:
            db.set_status(account_id, step.get("on_success", "deleted"),
                          f"verificado: {detail}")
        else:
            db.set_status(account_id, "manual",
                          f"No pude verificar el borrado ({detail}). "
                          "Revisa el screenshot/log.")

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
