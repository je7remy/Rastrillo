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
import json
import concurrent.futures
import logging
import os
import queue
import threading
from typing import Dict, List

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
def _resolver_workers() -> int:
    """Pool acotado para las capas HTTP del resolver. NO toca la concurrencia
    del motor Playwright (eso es un único worker secuencial siempre)."""
    try:
        n = int(os.environ.get("RASTRILLO_RESOLVER_WORKERS", "5"))
    except ValueError:
        n = 5
    return max(1, min(n, 16))   # cota dura: nunca > 16


def _candidates_to_resolve() -> List[dict]:
    """Filas elegibles para auto-resolver: status='found', sin receta JSON,
    sin action_meta previa, con host derivable.

    Excluye explícitamente source='hibp': los hits de HIBP son una exposición
    en brecha, no una cuenta confirmada — pre-calcular su Resolution sería
    invitar al usuario a mandar correos GDPR a sitios donde quizá no tiene
    cuenta. Solo cuando confirme con confirm-account, el source cambia a
    'hibp_confirmed' y la fila pasa a ser candidato normal.
    """
    from .recipes import get_recipe
    out = []
    for row in db.list_accounts(status="found"):
        if (row["source"] or "") == "hibp":
            continue
        if get_recipe(row["platform"]):
            continue
        if row["action_meta"]:
            continue
        host = row["source_site"] or row["platform"]
        if not host:
            continue
        out.append(dict(row))   # snapshot inmutable para pasar al thread
    return out


def _resolve_one(row: dict) -> bool:
    """Resuelve UNA cuenta y persiste el resultado. Devuelve True si guardó.
    No re-lanza excepciones: el pool seguiría aunque un host explote.

    Importante: `resolver.resolve()` ya respeta el caché en
    ~/.rastrillo/discovered.json — si el host ya está cacheado, no hace red.
    Así no re-fetcheamos lo conocido (parte del Tier 2.2).

    `profile_url` NO se pisa si ya trae la URL del hit: el triage la enseña
    como "perfil" y los chips de confianza se calcularon sobre ella. La URL de
    borrado del resolver ya viaja en `action_meta.url`, que es de donde la
    saca la UI (ver `_backfill_profile_url`).
    """
    from . import resolver as _resolver
    host = row["source_site"] or row["platform"]
    try:
        res = _resolver.resolve(host, row["identifier"] or "")
    except Exception as e:
        log.warning("auto-resolver(%s) fallo: %s", host, e)
        return False
    try:
        db.update_account(
            row["id"],
            action_meta=json.dumps(res.to_meta(), ensure_ascii=False),
        )
        db.backfill_profile_url(row["id"], res.url)
        return True
    except Exception as e:
        log.warning("auto-resolver guardado fallo acc=%s: %s", row["id"], e)
        return False


def _auto_resolve_pending() -> int:
    """Tras discovery, paraleliza la resolución de cuentas en ThreadPoolExecutor
    acotado. Solo cubre las capas HTTP (directorio + probe + GDPR fetch); el
    motor Playwright sigue siendo un worker único.

    Actualiza `_scan_status['resolved']` y `_scan_status['total']` a medida que
    cada futuro completa, para que la UI pueda enseñar "Resolviendo N/total".
    """
    pending = _candidates_to_resolve()
    total = len(pending)
    with _lock:
        _scan_status["total"] = total
        _scan_status["resolved"] = 0
    if not pending:
        log.info("auto-resolver: 0 candidatos")
        return 0

    workers = min(_resolver_workers(), total)
    n_ok = 0
    log.info("auto-resolver: %s candidatos, %s workers", total, workers)
    with concurrent.futures.ThreadPoolExecutor(
            max_workers=workers, thread_name_prefix="rastrillo-resolve") as pool:
        futures = {pool.submit(_resolve_one, r): r for r in pending}
        for fut in concurrent.futures.as_completed(futures):
            try:
                ok = fut.result()
            except Exception as e:
                log.warning("auto-resolver future inesperado: %s", e)
                ok = False
            if ok:
                n_ok += 1
            with _lock:
                # +1 por cada cuenta procesada, ok o no
                _scan_status["resolved"] = (_scan_status.get("resolved", 0) or 0) + 1
    log.info("auto-resolver: %s/%s cuentas enriquecidas", n_ok, total)
    return n_ok


def scan_async(usernames, emails) -> None:
    """Lanza Sherlock+Holehe en un thread aparte para no bloquear el servidor.
    Después corre auto-resolver para que cada cuenta llegue al dashboard con
    su plan ya calculado.
    """
    def _run():
        with _lock:
            _scan_status["running"] = True
            _scan_status["phase"] = "discovery"
            _scan_status["resolved"] = 0
        try:
            summary = discover(usernames or [], emails or [])
            # Canario a nivel de sitio: va DESPUÉS del discovery (necesita las
            # filas en la DB) y ANTES del auto-resolver, por dos razones —
            # `profile_url` todavía es la URL del hit, que es la plantilla que
            # el canario necesita; y así el auto-resolver no gasta peticiones
            # planificando el borrado de sitios recién invalidados.
            # No aborta el escaneo: un fallo aquí solo deja `verifiability`
            # sin rellenar (ver canario.run_canario). La confianza no depende
            # del canario en ningún caso.
            with _lock:
                _scan_status["phase"] = "canario"
            try:
                from .canario import run_canario
                summary["canario"] = run_canario()
            except Exception as e:
                log.exception("canario falló; sigo sin él")
                summary["canario"] = {"error": str(e)}
            with _lock:
                _scan_status["last"] = summary
                _scan_status["phase"] = "resolving"
            n = _auto_resolve_pending()
            with _lock:
                _scan_status["resolved"] = n
            log.info("scan completado: %s | auto-resolver=%s", summary, n)
        except Exception as e:
            log.exception("scan falló")
            with _lock:
                _scan_status["last"] = {"error": str(e)}
        finally:
            with _lock:
                _scan_status["running"] = False
                _scan_status["phase"] = None

    threading.Thread(target=_run, daemon=True, name="rastrillo-scan").start()


def scan_status() -> dict:
    with _lock:
        return dict(_scan_status)


# --- Refresh automático del directorio (Tier 3.1) --------------------------
def _dir_max_age_seconds() -> float:
    try:
        days = float(os.environ.get("RASTRILLO_DIR_MAX_AGE_DAYS", "30"))
    except ValueError:
        days = 30.0
    return max(1.0, days) * 86400.0


def start_dir_refresh_if_stale() -> threading.Thread:
    """Lanza un thread background que comprueba la edad de directory.json y
    re-descarga si supera RASTRILLO_DIR_MAX_AGE_DAYS (default 30). No bloquea
    el arranque del servidor. Errores se loguean en silencio (fallback al
    caché existente).

    El botón manual `/api/directory/refresh` sigue funcionando.
    """
    def _check():
        import time as _t
        # Import diferido: directory toca config en runtime.
        from . import directory as _dir
        try:
            d = _dir.load_directory()
        except Exception as e:
            log.warning("auto-refresh dir: load_directory falló: %s", e)
            return
        fetched = d.fetched_at
        if not fetched:
            log.info("auto-refresh dir: el directorio no tiene fetched_at; refresco")
            should = True
        else:
            age = _t.time() - float(fetched)
            should = age > _dir_max_age_seconds()
            if should:
                log.info("auto-refresh dir: edad %.1fd > umbral; refresco",
                         age / 86400.0)
            else:
                log.info("auto-refresh dir: edad %.1fd <= umbral; no toco",
                         age / 86400.0)
        if not should:
            return
        try:
            _dir.load_directory(force_refresh=True)
            log.info("auto-refresh dir: OK")
        except Exception as e:
            # Sin red / 503 / lo que sea: nos quedamos con la caché.
            log.warning("auto-refresh dir: descarga falló (%s); sigo con caché", e)

    t = threading.Thread(target=_check, daemon=True, name="rastrillo-dir-refresh")
    t.start()
    return t
