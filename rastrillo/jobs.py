"""Coordinación entre el servidor web y el motor de borrado.

Diseño:
- Un único thread worker del motor (Chromium persistente; no abrimos varias
  ventanas a la vez). Consume cuentas desde una `queue.Queue` y las procesa.
- Cada cuenta que entra en `awaiting_user` se queda esperando un
  `threading.Event` específico para esa cuenta. El endpoint web "Continuar"
  llama a `continue_account(id)` y el motor sigue.
- El escaneo (Sherlock/Holehe) corre en su propio thread por invocación; varios
  pueden correr a la vez sin chocar (cada uno con su tempdir).
- Todo vive en el mismo proceso que uvicorn, así que no hace falta IPC: basta
  con threading + estructuras protegidas por un lock.

Invariantes que respeta este módulo:
- No guarda contraseñas (sigue dependiendo del perfil persistente de Chromium).
- La pausa humana es OBLIGATORIA: el motor llama a `web_pause_handler`, que
  bloquea hasta que el humano confirme desde la UI.
"""
import logging
import queue
import threading
from typing import Dict

from . import db
from .discovery import discover
from .engine import Engine

log = logging.getLogger("rastrillo.jobs")

# --- Estado compartido (singletons del proceso) ------------------------------
_run_queue: "queue.Queue[int]" = queue.Queue()
_continue_events: Dict[int, threading.Event] = {}
_lock = threading.Lock()
_worker_started = False
_scan_status: Dict[str, object] = {"running": False, "last": None}


def web_pause_handler(account_id: int, message: str) -> None:
    """Pause-handler que usa el motor cuando corre desde el dashboard.

    Pone la cuenta en `awaiting_user`, crea (o reutiliza) un Event para esa
    cuenta y se bloquea hasta que la UI llame a `continue_account(id)`.
    El motor sigue su flujo después del .wait().
    """
    db.set_status(account_id, "awaiting_user", message)
    with _lock:
        evt = _continue_events.setdefault(account_id, threading.Event())
        evt.clear()
    log.info("pause: acc=%s msg=%s (esperando 'continuar' desde la UI)", account_id, message)
    evt.wait()
    with _lock:
        _continue_events.pop(account_id, None)
    db.log(account_id, "info", "pausa liberada por el usuario desde la UI")


def continue_account(account_id: int) -> bool:
    """Destraba la pausa de una cuenta. Devuelve True si había alguien esperando."""
    with _lock:
        evt = _continue_events.get(account_id)
    if not evt:
        return False
    evt.set()
    return True


# --- Cola del motor ----------------------------------------------------------
def _engine_worker():
    # El motor en modo web: pausa = web_pause_handler. Headless False para que
    # el humano pueda loguearse / resolver CAPTCHA en la misma ventana.
    eng = Engine(headless=False, pause_handler=web_pause_handler)
    while True:
        try:
            account_id = _run_queue.get()
        except Exception:
            continue
        if account_id is None:                # señal de parada (tests)
            break
        try:
            log.info("worker: procesando acc=%s", account_id)
            eng.run_account(account_id)
        except Exception as e:
            log.exception("worker: error procesando acc=%s", account_id)
            try:
                db.set_status(account_id, "failed", f"worker: {type(e).__name__}: {e}")
            except Exception:
                pass
        finally:
            _run_queue.task_done()


def start_workers() -> None:
    """Arranca el thread del motor una sola vez por proceso."""
    global _worker_started
    with _lock:
        if _worker_started:
            return
        _worker_started = True
    t = threading.Thread(target=_engine_worker, daemon=True, name="rastrillo-engine")
    t.start()


def enqueue_for_run(account_id: int, reason: str = "encolada") -> None:
    """Marca la cuenta como en cola y la encola para el worker del motor."""
    db.set_status(account_id, "queued", reason)
    _run_queue.put(account_id)


def queue_size() -> int:
    return _run_queue.qsize()


# --- Escaneo asíncrono -------------------------------------------------------
def scan_async(usernames, emails) -> None:
    """Lanza Sherlock+Holehe en un thread aparte para no bloquear el servidor."""
    def _run():
        with _lock:
            _scan_status["running"] = True
        try:
            summary = discover(usernames or [], emails or [])
            with _lock:
                _scan_status["last"] = summary
            log.info("scan completado: %s", summary)
        except Exception as e:
            log.exception("scan falló")
            with _lock:
                _scan_status["last"] = {"error": str(e)}
        finally:
            with _lock:
                _scan_status["running"] = False

    threading.Thread(target=_run, daemon=True, name="rastrillo-scan").start()


def scan_status() -> dict:
    with _lock:
        return dict(_scan_status)
