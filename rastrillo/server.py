"""Dashboard local: centro de control completo de Rastrillo.

Single-process FastAPI. Toda la coordinación con el motor de borrado vive en
`rastrillo.jobs`. Este módulo solo expone HTTP + UI.

Endpoints:
  GET  /                                    -> HTML
  GET  /api/accounts                        -> {accounts, stats}
  GET  /api/scan/status                     -> {running, last}
  GET  /api/directory                       -> info del directorio cacheado
  GET  /api/dry-run                         -> {enabled}
  POST /api/scan                            -> body {usernames:[], emails:[]}
  POST /api/dry-run                         -> body {enabled:bool}
  POST /api/directory/refresh               -> re-descarga el directorio
  POST /api/accounts/clear                  -> vacía la tabla
  POST /api/accounts/discard-low            -> triage en lote
  POST /api/accounts/{id}/own               -> {owned:bool}: marcar tuya / descartar
  POST /api/accounts/{id}/action            -> body {action,confirm_owned?}
  POST /api/accounts/{id}/mark-sent         -> tramitada por el usuario
  GET  /api/accounts/{id}/resolution        -> la Resolution cacheada

Auth: middleware exige X-Rastrillo-Token (o ?token=) en TODOS los POST.
GET son libres (lectura). El token vive en config.AUTH_TOKEN (env-override).
"""
import hashlib
import json
import logging
import math
import os
import time
from typing import List, Optional

from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import audit, config, db, jobs, directory, ai_assist, resolver, domain_intel
from .recipes import get_recipe

# Tarea 10: el frontend vive en rastrillo/static/ (HTML + CSS + JS).
# El handler GET / lee el HTML, inyecta un pequeño <script> con los
# valores dinámicos en `window.__RASTRILLO_BOOT__` y lo devuelve.
_STATIC_DIR = Path(__file__).parent / "static"

log = logging.getLogger("rastrillo.server")

app = FastAPI(title="Rastrillo")

STATUS_META = {
    "found":         ("Pendiente",        "#888780"),
    "queued":        ("En cola",          "#185FA5"),
    "in_progress":   ("En proceso",       "#BA7517"),
    "awaiting_user": ("Esperándote",      "#D85A30"),
    "deleted":       ("Eliminada",        "#0F6E56"),
    "anonymized":    ("Anonimizada",      "#534AB7"),
    "user_done":     ("Tramitada",        "#0F6E56"),
    "semi_auto":     ("Acción: 1 clic",   "#0F6E56"),
    "email_draft":   ("Solicitud correo", "#534AB7"),
    "pending_deletion": ("Plazo de eliminación", "#BA7517"),
    "manual":        ("Revisar",          "#854F0B"),
    "skipped":       ("Conservada",       "#5F5E5A"),
    "failed":        ("Error",            "#A32D2D"),
    "not_mine":      ("Descartada",       "#5F5E5A"),
    "dry_run":       ("Simulada",         "#534AB7"),
}

# Verificabilidad (Paso 2C, Entrega 1): eje SEPARADO de la confianza. Dice si
# la respuesta del sitio sirve para comprobar algo, nunca si la cuenta es del
# usuario. La ausencia de entrada (verifiability NULL) significa "no evaluado"
# y la UI no pinta nada: no es un juicio, es que el canario no ha mirado.
VERIFIABILITY_META = {
    "indiscriminado": ("No verificable",  "#854F0B"),
    "discrimina":     ("Verificable",     "#0F6E56"),
    "indeterminado":  ("Sin veredicto",   "#5F5E5A"),
}

# Acciones que SÍ son irreversibles. Para ellas:
#   - se exige owned=1 en la cuenta (o confirm_owned=true en el body),
#   - se respeta el modo dry-run,
#   - se graba en audit.json antes de tocar nada.
_DESTRUCTIVE_ACTIONS = {"delete", "anonymize", "retry"}


def _allow_query_token() -> bool:
    """¿Aceptamos `?token=` en la query como fuente del token?

    Solo bajo `RASTRILLO_ALLOW_QUERY_TOKEN` truthy. La activan los tests
    para no tener que reescribir la suite. En uso normal está desactivado:
    el JS lee `?token=` de la URL inicial, lo guarda en sessionStorage y
    lo limpia con history.replaceState; a partir de ahí solo viaja por
    header X-Rastrillo-Token.
    """
    v = (os.environ.get("RASTRILLO_ALLOW_QUERY_TOKEN") or "").strip().lower()
    return v in ("1", "true", "yes", "on")


# --- Auth middleware --------------------------------------------------------
@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    """Endurecido en dos capas:

    1) Allowlist de Host: rechaza cualquier request cuyo header Host no
       esté en config.ALLOWED_HOSTS. Aplica a TODOS los métodos y paths.
       Esto neutraliza DNS rebinding (un dominio público que apunta a
       127.0.0.1 y consigue que el navegador llame a este servidor).

    2) Token de proceso: exigido en
       - todos los POST,
       - todos los GET cuyo path empiece por `/api/` (devuelven datos
         sensibles: cuentas, reports, dry-run, etc.).
       GET / y otros paths estáticos quedan fuera porque el navegador
       entra sin header en la primera carga; el JS lee `?token=` de la
       URL y la limpia con history.replaceState antes de llamar a /api/.

    Mensaje del 401: enriquecido con instrucciones accionables (el front
    lo muestra como toast largo). No se cambia.
    """
    host = (request.headers.get("host") or "").lower()
    if host not in config.ALLOWED_HOSTS:
        return JSONResponse(
            {"detail": (
                "Host no permitido. Rastrillo solo acepta conexiones desde "
                "127.0.0.1 o localhost (puerto 8765). Si necesitas exponerlo "
                "en otro nombre, configura RASTRILLO_ALLOWED_HOSTS."
            )},
            status_code=403,
        )

    needs_token = (
        request.method == "POST"
        or (request.method == "GET" and request.url.path.startswith("/api/"))
    )
    if needs_token:
        tok = request.headers.get("x-rastrillo-token")
        # Tarea 5: el token de la URL (?token=...) queda registrado en
        # historial del navegador y Referer; el JS lo borra con
        # history.replaceState al cargar, pero el server tampoco debe
        # aceptarlo en general. Solo lo permitimos cuando una env de
        # test lo habilita explícitamente. NO HABILITAR EN PRODUCCIÓN.
        if not tok and _allow_query_token():
            tok = request.query_params.get("token")
        if not tok or tok != config.AUTH_TOKEN:
            return JSONResponse(
                {"detail": (
                    "Falta el token de auth o no es el del proceso actual. "
                    "Cierra esta pestaña y abre Rastrillo desde la URL que "
                    "se imprimió al arrancar (incluye ?token=...). Si la "
                    "perdiste, reinicia el servidor para ver una nueva."
                )},
                status_code=401,
            )
    return await call_next(request)


# --- Modelos de request -----------------------------------------------------
class ScanBody(BaseModel):
    usernames: List[str] = []
    emails: List[str] = []


class ActionBody(BaseModel):
    action: str
    # Si la cuenta no estaba marcada owned y el usuario confirma desde el modal
    # pre-vuelo, el frontend manda confirm_owned=true. El server marca owned=1.
    confirm_owned: bool = False
    # Para "delete"/"anonymize" se puede pasar la deletion_type esperada; opcional.
    expect: Optional[str] = None


class OwnBody(BaseModel):
    owned: bool


class DryRunBody(BaseModel):
    enabled: bool


class DomainBody(BaseModel):
    domain: str


class ScheduleBody(BaseModel):
    # Exactamente uno de los dos: días restantes (lo computa el server) o un
    # timestamp UNIX `eta` ya resuelto en el cliente (fin de día en hora local).
    days: Optional[int] = None
    eta: Optional[float] = None
    confirm_owned: bool = False


# --- API: lectura -----------------------------------------------------------
# --- Cuenta regresiva de eliminación (FASE 4) -------------------------------
# Máximo plazo aceptado al programar: ~10 años. Cota dura contra fechas absurdas.
_MAX_DELETION_DAYS = 3650


def deletion_progress(started_at, eta, now=None) -> Optional[dict]:
    """Helper PURO para la cuenta regresiva. Único sitio donde se calcula el
    progreso de un plazo de eliminación: el cliente solo renderiza lo que
    devolvemos. Sin red, sin estado.

    Devuelve {days_left, overdue, pct} o None si no hay `eta`.
      - days_left: días enteros que faltan, con techo (ceil). 0 si ya venció.
      - overdue: True si `now >= eta`.
      - pct: 0..100 de progreso entre `started_at` y `eta`.
    """
    if not eta:
        return None
    now = time.time() if now is None else now
    overdue = now >= eta
    days_left = 0 if overdue else int(math.ceil((eta - now) / 86400.0))
    if started_at and eta > started_at:
        pct = (now - started_at) / (eta - started_at) * 100.0
    else:
        pct = 100.0 if overdue else 0.0
    pct = max(0.0, min(100.0, pct))
    return {"days_left": days_left, "overdue": bool(overdue), "pct": round(pct, 1)}


def _deletion_url(action_meta, profile_url):
    """URL de borrado que guardó el resolver en `action_meta`, o None.

    Devuelve None si no hay Resolution, si su JSON está corrupto (misma
    tolerancia que `db.parse_reasons`: mejor sin enlace que una vista rota) o
    si coincide con la del hit, para no pintar el mismo enlace dos veces.
    """
    if not action_meta:
        return None
    try:
        meta = json.loads(action_meta)
    except (ValueError, TypeError):
        return None
    if not isinstance(meta, dict):
        return None
    url = (meta.get("url") or "").strip() or None
    if url and url == (profile_url or "").strip():
        return None
    return url


# Paso 3, Entrega 2: señal agregada por sitio. Si el usuario descartó el mismo
# sitio con VARIOS identificadores distintos, eso sugiere que el sitio genera
# ruido. Mínimo 2: una sola vez no es señal de nada.
_UMBRAL_SENAL_SITIO = 2


def _discard_site_counts() -> dict:
    """{source_site: nº de identificadores distintos descartados} filtrado al
    umbral. Por debajo del umbral la clave NO está, y eso es lo que hace que la
    UI no pinte nada: el umbral vive AQUÍ y en un solo sitio.

    Esta señal es DELIBERADAMENTE informativa y nada más:

      - NO mueve `confidence` en ninguna dirección, ni un tramo.
      - NO dispara ninguna acción, ni individual ni en lote.
      - NO se persiste en la fila: se calcula al vuelo desde `discard_memory`,
        así que deshacer un descarte la baja sola.

    El motivo importa. El canario se construyó sobre una hipótesis razonable y,
    tras calibrarlo, sus tres detecciones resultaron ser errores suyos. Una
    inferencia del usuario sobre una muestra minúscula (2 identificadores) no
    entra a escribir en la DB sin medirla antes durante unos cuantos escaneos.
    Si resulta útil, se promueve en otro paso.
    """
    return {sitio: n for sitio, n in db.count_discards_by_site().items()
            if n >= _UMBRAL_SENAL_SITIO}


@app.get("/api/accounts")
def api_accounts():
    rows = []
    por_sitio = _discard_site_counts()
    for r in db.list_accounts():
        d = dict(r)
        # Señal agregada del sitio (informativa). `None` = sin señal; la UI
        # solo pinta el chip cuando viene un número.
        d["site_discards"] = por_sitio.get(
            (d.get("source_site") or "").strip().lower())
        # Campo computado para la UI (cuenta regresiva). Solo presente si la
        # cuenta tiene un plazo fijado; si no, queda None.
        d["deletion"] = deletion_progress(d.get("deletion_started_at"),
                                          d.get("deletion_eta"))
        # Motivos de la confianza: la columna guarda JSON (como action_meta);
        # la UI recibe la lista ya parseada para no repetir el json.parse.
        d["confidence_reasons"] = db.parse_reasons(d.get("confidence_reasons"))
        # URL de BORRADO (directorio/resolver), separada de `profile_url`, que
        # es la URL del HIT. Antes el resolver pisaba `profile_url` y el triage
        # enseñaba una URL de borrado bajo la etiqueta "perfil", contradiciendo
        # los chips de confianza —calculados sobre la del hit—. Ahora van como
        # dos enlaces distintos. `None` si no hay o si coincide con la del hit.
        d["deletion_url"] = _deletion_url(d.get("action_meta"), d.get("profile_url"))
        rows.append(d)
    return JSONResponse({
        "accounts": rows,
        "stats": db.stats(),
        "queue_size": jobs.queue_size(),
    })


@app.get("/api/scan/status")
def api_scan_status():
    return jobs.scan_status()


# --- API: acciones ----------------------------------------------------------
@app.post("/api/scan")
def api_scan(body: ScanBody):
    usernames = [u.strip() for u in body.usernames if u and u.strip()]
    emails = [e.strip() for e in body.emails if e and e.strip()]
    if not usernames and not emails:
        raise HTTPException(400, "Da al menos un username o un email.")
    # Cada escaneo arranca limpio: borramos lo acumulado para que "Todas" no
    # mezcle hallazgos de sesiones previas. El directorio y la caché del
    # resolver NO se tocan (eso vive en discovered.json/directory.json).
    db.clear_accounts()
    jobs.scan_async(usernames, emails)
    return {"ok": True, "queued": {"usernames": usernames, "emails": emails}}


def _apply_resolution(account_id: int, res: resolver.Resolution,
                      dry_run: bool = False) -> dict:
    """Persiste la Resolution en la fila de la DB.

    Devuelve {ok, status} para que el endpoint lo refleje al cliente.

    - kind=auto       → encolar al motor (o simular si dry_run).
    - kind=semi_auto  → estado `semi_auto`, URL en action_meta.
    - kind=email_draft → estado `email_draft`, borrador en action_meta.

    La URL de borrado NO pisa `profile_url` (ver `db.backfill_profile_url`):
    esa columna guarda la URL del hit, que es lo que enseña el triage.
    """
    db.update_account(account_id, action_meta=json.dumps(res.to_meta(), ensure_ascii=False))
    db.backfill_profile_url(account_id, res.url)

    if res.kind == "auto":
        if dry_run:
            db.set_status(account_id, "dry_run",
                          f"[simulación] kind=auto layer={res.layer}; "
                          f"el motor habría conducido el flujo en {res.url}")
            return {"ok": True, "status": "dry_run", "layer": res.layer, "dry_run": True}
        db.update_account(account_id, current_step=0)
        jobs.enqueue_for_run(account_id, reason=f"resolver/{res.layer}")
        return {"ok": True, "status": "queued", "layer": res.layer}

    if res.kind == "semi_auto":
        msg = (f"{res.title}\n{res.notes}\nEnlace: {res.url}".strip())
        db.set_status(account_id, "semi_auto", msg)
        return {"ok": True, "status": "semi_auto", "layer": res.layer,
                "url": res.url}

    if res.kind == "email_draft":
        msg = (f"{res.title}\n{res.notes}\n"
               f"Para: {res.email_to}\nAsunto: {res.email_subject}").strip()
        db.set_status(account_id, "email_draft", msg)
        return {"ok": True, "status": "email_draft", "layer": res.layer,
                "email_to": res.email_to}

    # No debería pasar (el resolver siempre devuelve un kind válido), pero por
    # robustez tratamos esto como un error de programación.
    raise HTTPException(500, f"Resolution kind desconocido: {res.kind!r}")


def _account_summary(acc) -> dict:
    """Snapshot mínimo para el cliente cuando lanzamos un 412 de pre-vuelo."""
    return {
        "id": acc["id"],
        "platform": acc["platform"],
        "display_name": acc["display_name"],
        "identifier": acc["identifier"],
        "source": acc["source"],
        "source_site": acc["source_site"],
        "profile_url": acc["profile_url"],
        "confidence": acc["confidence"],
    }


@app.post("/api/accounts/{account_id}/action")
def api_action(account_id: int, body: ActionBody):
    acc = db.get_account(account_id)
    if not acc:
        raise HTTPException(404, "Cuenta no encontrada.")
    action = body.action

    if action == "keep":
        db.set_status(account_id, "skipped", "conservada desde UI")
        return {"ok": True, "status": "skipped"}

    if action == "continue":
        ok = jobs.continue_account(account_id)
        if not ok:
            raise HTTPException(409, "La cuenta no está esperando una confirmación.")
        return {"ok": True, "status": "resumed"}

    if action in _DESTRUCTIVE_ACTIONS:
        # ── Verificación de propiedad (pre-vuelo) ────────────────────────
        # Sherlock genera falsos positivos. Ninguna acción destructiva
        # ejecuta sin owned=1 o confirm_owned=true en el body.
        if not acc["owned"] and not body.confirm_owned:
            raise HTTPException(
                status_code=412,
                detail={
                    "code": "needs_ownership_confirmation",
                    "message": ("Confirma que esta cuenta es tuya antes de "
                                "proceder con una acción destructiva."),
                    "account": _account_summary(acc),
                },
            )
        # Si llega con confirm_owned, marcamos owned=1 antes de seguir.
        if not acc["owned"] and body.confirm_owned:
            db.update_account(account_id, owned=1)
            audit.record("own", acc, extra={"via": "confirm_owned"})
            acc = db.get_account(account_id)   # refrescar para el audit posterior

        dry_run = config.DRY_RUN
        # ── Audit log ANTES de tocar nada ───────────────────────────────
        audit.record(action, acc, dry_run=dry_run, extra={"via": "action_endpoint"})

        # ── Receta JSON: ruta determinista ──────────────────────────────
        if get_recipe(acc["platform"]):
            if dry_run:
                db.set_status(account_id, "dry_run",
                              f"[simulación] receta '{acc['platform']}' "
                              f"acción='{action}'; el motor no se ejecutó")
                return {"ok": True, "status": "dry_run", "layer": "recipe",
                        "dry_run": True}
            db.update_account(account_id, current_step=0)
            jobs.enqueue_for_run(account_id, reason=f"acción '{action}' desde UI (receta)")
            return {"ok": True, "status": "queued", "layer": "recipe"}

        # ── Resolver en capas ──────────────────────────────────────────
        host = acc["source_site"] or acc["platform"]
        try:
            res = resolver.resolve(host, acc["identifier"] or "",
                                   force_refresh=(action == "retry"))
        except Exception as e:
            raise HTTPException(500, f"resolver falló: {e}")
        return _apply_resolution(account_id, res, dry_run=dry_run)

    raise HTTPException(400, f"Acción desconocida: {action!r}")


# --- Triage: marcar "es mía" / "no es mía" ----------------------------------
@app.post("/api/accounts/{account_id}/own")
def api_own(account_id: int, body: OwnBody):
    """Triage rápido: el usuario confirma si la cuenta es suya o la descarta.

    - owned=True  → marca owned=1 (la cuenta sigue en su estado actual).
    - owned=False → status='not_mine'; no se vuelve a proponer.

    Memoria de descartes (Paso 3): las dos ramas escriben en `discard_memory`,
    y son simétricas a propósito.

    - Descartar ANOTA la decisión, para que sobreviva a `clear_accounts()` y el
      hallazgo entre ya descartado en el próximo escaneo.
    - Confirmar "es mía" OLVIDA la decisión. Este es el DESHACER: sin él, un
      clic equivocado en "No es mía" sería permanente y ni siquiera un
      reescaneo lo arreglaría (el hallazgo volvería a entrar como `not_mine`).
      Además devolvemos la fila a `found`, que es donde estaría si nunca se
      hubiera descartado; en cualquier otro estado no tocamos el status.

    Solo se escribe por decisión EXPLÍCITA del usuario: este endpoint es
    exactamente el clic de los botones "Es mía" / "No es mía".
    """
    acc = db.get_account(account_id)
    if not acc:
        raise HTTPException(404, "Cuenta no encontrada.")
    if body.owned:
        db.update_account(account_id, owned=1)
        olvidada = db.forget_discard(acc["source_site"], acc["identifier"])
        if acc["status"] == "not_mine":
            db.set_status(account_id, "found",
                          "recuperada por el usuario: sí era mía")
        db.log(account_id, "info", "el usuario confirmó: es mía"
               + (" (olvido la decisión previa de descarte)" if olvidada else ""))
        audit.record("own", acc, extra={"via": "triage",
                                        "discard_memory_forgotten": olvidada})
        nuevo = "found" if acc["status"] == "not_mine" else acc["status"]
        return {"ok": True, "owned": True, "status": nuevo,
                "discard_memory_forgotten": olvidada}
    db.update_account(account_id, owned=0)
    db.set_status(account_id, "not_mine", "descartada por el usuario (triage)")
    recordada = db.remember_discard(acc["source_site"], acc["identifier"],
                                    reason="triage")
    audit.record("discard", acc, extra={"via": "triage",
                                        "discard_memory": recordada})
    return {"ok": True, "owned": False, "status": "not_mine",
            "discard_memory": recordada}


@app.post("/api/accounts/process-all-auto")
def api_process_all_auto():
    """Encola todas las cuentas confirmadas como mías (owned=1) en estado
    'found' cuyo camino sea automatizable:
      - hay receta JSON para su platform, O
      - el resolver devuelve kind='auto' (directorio + IA disponible).

    Las cuentas semi_auto / email_draft de la resolución se persisten en su
    estado correspondiente para que las gestiones uno a uno desde "Tu turno".

    Respeta dry-run y graba audit por cada acción destructiva.
    """
    dry_run = config.DRY_RUN
    summary = {
        "ok": True, "dry_run": dry_run,
        "queued": 0, "semi_auto": 0, "email_draft": 0,
        "skipped_unowned": 0, "skipped_status": 0,
        "visited": 0,
    }
    summary["skipped_exposure"] = 0
    for row in db.list_accounts():
        if row["status"] != "found":
            summary["skipped_status"] += 1
            continue
        if not row["owned"]:
            summary["skipped_unowned"] += 1
            continue
        # Exposición en brecha (source='hibp'): requiere confirmación explícita
        # del usuario antes de tratarse como cuenta para borrar.
        if (row["source"] or "") == "hibp":
            summary["skipped_exposure"] += 1
            continue
        summary["visited"] += 1
        # Receta: ruta determinista; encolar directamente.
        if get_recipe(row["platform"]):
            audit.record("delete", row, dry_run=dry_run,
                         extra={"via": "process_all_auto", "route": "recipe"})
            if dry_run:
                db.set_status(row["id"], "dry_run",
                              f"[simulación] batch: receta '{row['platform']}'")
            else:
                db.update_account(row["id"], current_step=0)
                jobs.enqueue_for_run(row["id"], reason="process_all_auto (receta)")
                summary["queued"] += 1
            continue
        # Sin receta: probamos resolver. Solo encolamos si kind=auto.
        host = row["source_site"] or row["platform"]
        try:
            res = resolver.resolve(host, row["identifier"] or "")
        except Exception as e:
            db.log(row["id"], "warn", f"process_all_auto resolver falló: {e}")
            continue
        # Persistimos la Resolution siempre. `profile_url` solo se rellena si
        # está vacía: la URL del hit manda (ver `db.backfill_profile_url`).
        db.update_account(row["id"], action_meta=json.dumps(res.to_meta(), ensure_ascii=False))
        db.backfill_profile_url(row["id"], res.url)
        if res.kind == "auto":
            audit.record("delete", row, dry_run=dry_run,
                         extra={"via": "process_all_auto", "route": "resolver",
                                "layer": res.layer})
            if dry_run:
                db.set_status(row["id"], "dry_run",
                              f"[simulación] batch: kind=auto layer={res.layer}")
            else:
                db.update_account(row["id"], current_step=0)
                jobs.enqueue_for_run(row["id"], reason=f"process_all_auto/{res.layer}")
                summary["queued"] += 1
        elif res.kind == "semi_auto":
            msg = f"{res.title}\n{res.notes}\nEnlace: {res.url}".strip()
            db.set_status(row["id"], "semi_auto", msg)
            summary["semi_auto"] += 1
        elif res.kind == "email_draft":
            msg = (f"{res.title}\n{res.notes}\n"
                   f"Para: {res.email_to}\nAsunto: {res.email_subject}").strip()
            db.set_status(row["id"], "email_draft", msg)
            summary["email_draft"] += 1
    return summary


@app.post("/api/accounts/{account_id}/confirm-account")
def api_confirm_account(account_id: int):
    """El usuario confirma que tras una brecha HIBP sí tiene cuenta activa
    en el sitio. Promovemos `source` a 'hibp_confirmed' para que el resto
    del sistema (auto-resolver, process-all-auto, filtros) la trate como
    cuenta normal candidata a borrado.

    No marca owned: el preflight de propiedad sigue siendo obligatorio antes
    de cualquier acción destructiva."""
    acc = db.get_account(account_id)
    if not acc:
        raise HTTPException(404, "Cuenta no encontrada.")
    if (acc["source"] or "") != "hibp":
        raise HTTPException(409, "Esta cuenta no es una exposición HIBP.")
    db.update_account(account_id, source="hibp_confirmed")
    db.log(account_id, "info",
           "usuario confirmó cuenta activa tras brecha HIBP")
    audit.record("confirm_account", acc,
                 extra={"from": "hibp", "to": "hibp_confirmed"})
    return {"ok": True, "source": "hibp_confirmed"}


def _filas_low_confidence():
    """Las filas que barrería "Descartar low". ÚNICO sitio donde vive el
    criterio: lo comparten el preview (que alimenta el texto de confirmación) y
    el endpoint que escribe, así que el número que se le enseña al usuario no
    puede desviarse de lo que va a pasar."""
    return [r for r in db.list_accounts(status="found")
            if (r["confidence"] or "") == "low" and not r["owned"]]


@app.get("/api/accounts/discard-low/preview")
def api_discard_low_preview():
    """Cuántas filas y cuáles barrería "Descartar low". SOLO LEE.

    Existe para que el modal de confirmación pueda decir el número exacto en
    vez de un "todas las de confianza baja" que no se puede auditar antes de
    pulsar. Devuelve también la lista corta (sitio + identificador) para que el
    usuario reconozca lo que va a descartar.
    """
    filas = _filas_low_confidence()
    return {
        "count": len(filas),
        "reversible": True,   # se deshace una a una desde "Descartadas"
        "accounts": [{"id": r["id"],
                      "display_name": r["display_name"] or r["platform"],
                      "source_site": r["source_site"],
                      "identifier": r["identifier"]}
                     for r in filas],
    }


@app.post("/api/accounts/discard-low")
def api_discard_low():
    """Bulk: descarta como 'not_mine' todas las cuentas en estado 'found' con
    confidence='low' que no estén marcadas como propias. Una sola pulsación
    barre el ruido típico de Sherlock para usernames cortos/genéricos.

    El criterio es `confidence=='low'` y SOLO eso (Paso 2C, Entrega 1). En
    particular NO barre inverificables: `verifiability=='indiscriminado'` dice
    que el sitio no sirve para comprobar nada, no que la cuenta sea ajena, y
    encadenar lo uno con lo otro marcaba `not_mine` cuentas que sí eran del
    usuario. Si algún día se quiere una acción en lote sobre inverificables,
    tiene que ser otro endpoint con otro estado: `not_mine` y la pestaña
    "Descartadas" son el mismo estado y ya significan "no es mía".

    Memoria de descartes (Paso 3): cada fila barrida se anota en
    `discard_memory`. SÍ cuenta como decisión explícita del usuario —este
    endpoint es el botón "Descartar low", que se pulsa a mano y con
    confirmación— aunque el criterio de selección lo ponga la máquina. Deshacer
    una a una desde "Descartadas" ("Era mía") borra su entrada."""
    n = 0
    for r in _filas_low_confidence():
        db.set_status(r["id"], "not_mine", "descartada en lote (confidence=low)")
        recordada = db.remember_discard(r["source_site"], r["identifier"],
                                        reason="descarte en lote (confianza baja)")
        audit.record("discard", r, extra={"via": "bulk_low",
                                          "discard_memory": recordada})
        n += 1
    return {"ok": True, "discarded": n}


_FOLLOWUP_PREFIX = {
    "en": ("Follow-up: pending GDPR Article 17 erasure request",
           "Dear Privacy Team,\n\nOn {sent_iso} I sent a formal request for "
           "the erasure of all my personal data and account on {host} "
           "({identifier}). It has been {days} days since the request and I "
           "have not received confirmation of completion.\n\nUnder GDPR "
           "Article 12(3) you are required to respond within one month. "
           "Please provide written confirmation that my data has been deleted "
           "or explain in writing why the deadline cannot be met.\n\n"
           "--- Original request below ---\n\n"),
    "es": ("Seguimiento: solicitud de supresión (RGPD Art. 17) pendiente",
           "Estimado equipo de privacidad:\n\nEl {sent_iso} envié una "
           "solicitud formal de supresión de todos mis datos personales y "
           "cuenta asociada en {host} ({identifier}). Han pasado {days} días "
           "desde la solicitud y no he recibido confirmación.\n\n"
           "El Artículo 12.3 del RGPD obliga a responder en el plazo de un "
           "mes. Les ruego confirmen por escrito que se han eliminado mis "
           "datos o expliquen por qué no es posible cumplir el plazo.\n\n"
           "--- Solicitud original abajo ---\n\n"),
    "ru": ("Напоминание: запрос на удаление данных (ст. 17 GDPR) без ответа",
           "Здравствуйте,\n\n{sent_iso} я отправил(а) официальный запрос на "
           "удаление всех моих персональных данных и учётной записи на "
           "{host} ({identifier}). С момента запроса прошло {days} дней, "
           "подтверждения не получено.\n\nСогласно статье 12(3) GDPR ответ "
           "обязателен в течение одного месяца. Прошу подтвердить удаление "
           "данных в письменном виде.\n\n--- Исходный запрос ниже ---\n\n"),
    "pt-BR": ("Acompanhamento: solicitação de exclusão (LGPD/RGPD Art. 17)",
              "Prezada equipe de Privacidade,\n\nEm {sent_iso} enviei uma "
              "solicitação formal de exclusão de meus dados em {host} "
              "({identifier}). Já se passaram {days} dias sem confirmação.\n\n"
              "Solicito a confirmação por escrito da exclusão dos dados.\n\n"
              "--- Solicitação original abaixo ---\n\n"),
    "fr": ("Relance : demande d'effacement (RGPD Art. 17) sans réponse",
           "Madame, Monsieur,\n\nLe {sent_iso} j'ai envoyé une demande "
           "d'effacement de mes données et compte sur {host} ({identifier}). "
           "{days} jours se sont écoulés sans réponse.\n\nL'article 12.3 du "
           "RGPD vous impose un délai d'un mois. Veuillez me confirmer par "
           "écrit l'effacement.\n\n--- Demande originale ci-dessous ---\n\n"),
    "de": ("Erinnerung: Löschungsantrag (DSGVO Art. 17) ohne Antwort",
           "Sehr geehrtes Datenschutzteam,\n\nam {sent_iso} habe ich einen "
           "Antrag auf vollständige Löschung meiner Daten auf {host} "
           "({identifier}) gestellt. Seitdem sind {days} Tage vergangen ohne "
           "Bestätigung.\n\nNach Art. 12 Abs. 3 DSGVO ist eine Antwort "
           "innerhalb eines Monats verpflichtend. Bitte bestätigen Sie die "
           "Löschung schriftlich.\n\n--- Ursprünglicher Antrag unten ---\n\n"),
}


@app.get("/api/accounts/{account_id}/followup-draft")
def api_followup_draft(account_id: int):
    """Genera un borrador de seguimiento para una cuenta en user_done cuyo
    plazo GDPR (30 días) está vencido. Reutiliza el borrador original que ya
    guardamos en action_meta y le prepende un párrafo de urgencia localizado.
    """
    import time as _t
    acc = db.get_account(account_id)
    if not acc:
        raise HTTPException(404, "Cuenta no encontrada.")
    if not acc["sent_at"]:
        raise HTTPException(409, "Esta cuenta no tiene fecha de envío registrada.")
    if not acc["action_meta"]:
        raise HTTPException(409, "No tengo el borrador original para esta cuenta.")
    try:
        meta = json.loads(acc["action_meta"])
    except Exception:
        raise HTTPException(500, "action_meta corrupto.")
    if not meta.get("email_to"):
        raise HTTPException(409, "El borrador original no es por correo.")

    lang = meta.get("language") or "en"
    tpl = _FOLLOWUP_PREFIX.get(lang) or _FOLLOWUP_PREFIX["en"]
    sent_at = float(acc["sent_at"])
    days = max(0, int((_t.time() - sent_at) // 86400))
    sent_iso = _t.strftime("%Y-%m-%d", _t.gmtime(sent_at))
    host = acc["source_site"] or acc["platform"] or ""
    identifier = acc["identifier"] or ""

    prefix = tpl[1].format(sent_iso=sent_iso, days=days, host=host, identifier=identifier)
    new_subject = tpl[0]
    new_body = prefix + (meta.get("email_body") or "")
    return {
        "email_to": meta["email_to"],
        "email_subject": new_subject,
        "email_body": new_body,
        "language": lang,
        "days_since_sent": days,
        "host": host,
        "identifier": identifier,
    }


@app.get("/api/accounts/{account_id}/resolution")
def api_resolution(account_id: int):
    """Devuelve la Resolution cacheada en la cuenta (action_meta) si la hay.
    La UI la usa para mostrar el borrador de correo en una vista expandida."""
    acc = db.get_account(account_id)
    if not acc:
        raise HTTPException(404, "Cuenta no encontrada.")
    meta = acc["action_meta"]
    if not meta:
        return {"meta": None}
    try:
        return {"meta": json.loads(meta)}
    except Exception:
        return {"meta": None}


@app.post("/api/accounts/{account_id}/mark-sent")
def api_mark_sent(account_id: int, body: ActionBody = ActionBody(action="mark-sent")):
    """El usuario indica que ya envió el correo / completó la acción manual.
    Lo movemos a 'user_done' (tramitada por el usuario). Si es la primera vez
    que se actúa sobre la cuenta sin owned, también pasa por confirm_owned."""
    acc = db.get_account(account_id)
    if not acc:
        raise HTTPException(404, "Cuenta no encontrada.")

    # Confirmación de propiedad si nunca se hizo
    if not acc["owned"] and not body.confirm_owned:
        raise HTTPException(
            status_code=412,
            detail={
                "code": "needs_ownership_confirmation",
                "message": ("Confirma que esta cuenta es tuya antes de "
                            "marcar la solicitud como enviada."),
                "account": _account_summary(acc),
            },
        )
    if not acc["owned"] and body.confirm_owned:
        db.update_account(account_id, owned=1)
        audit.record("own", acc, extra={"via": "mark_sent_confirm"})
        acc = db.get_account(account_id)

    if config.DRY_RUN:
        # Simulación: no marcamos user_done; dejamos rastro de qué se habría enviado.
        meta = acc["action_meta"]
        try:
            email_to = (json.loads(meta) or {}).get("email_to") if meta else None
        except Exception:
            email_to = None
        audit.record("mark_sent", acc, dry_run=True,
                     extra={"would_send_to": email_to})
        db.set_status(account_id, "dry_run",
                      f"[simulación] habría marcado como enviada a {email_to or '?'}")
        return {"ok": True, "status": "dry_run", "dry_run": True}

    # Registramos `sent_at` (cuándo se envió) SOLO cuando hay un borrador de
    # correo real detrás: o la cuenta venía de `email_draft`, o su Resolution
    # cacheada (`action_meta`) lleva un `email_to`. Es lo que alimenta el
    # seguimiento GDPR (plazo legal de 30 días en la UE). Para completados
    # semi_auto/manual sin correo NO marcamos sent_at: no es un envío GDPR y
    # mostrar "Vencida" / "Seguimiento" no tendría sentido (no hay a quién
    # reclamar). FASE 4 usará un campo propio (deletion_eta), no este.
    sent_at = None
    has_email_draft = acc["status"] == "email_draft"
    if not has_email_draft and acc["action_meta"]:
        try:
            has_email_draft = bool((json.loads(acc["action_meta"]) or {}).get("email_to"))
        except Exception:
            has_email_draft = False
    if has_email_draft:
        import time as _t
        sent_at = _t.time()
        db.update_account(account_id, sent_at=sent_at)
    audit.record("mark_sent", acc, extra={"sent_at": sent_at})
    db.set_status(account_id, "user_done", "Marcada como tramitada por el usuario.")
    return {"ok": True, "status": "user_done", "sent_at": sent_at}


# --- Eliminación programada con cuenta regresiva (FASE 4) --------------------
@app.post("/api/accounts/{account_id}/schedule-deletion")
def api_schedule_deletion(account_id: int, body: ScheduleBody):
    """Fija el plazo de eliminación de una cuenta (la plataforma dijo "en N
    días" o "el día X"). El cliente manda `days` (lo computa el server) O un
    `eta` ya resuelto (timestamp UNIX, fin de día en hora local del usuario).

    Exige propiedad como las acciones destructivas (412 + confirm_owned). NO
    se gatea por dry-run: fijar un plazo no borra nada, es metadato reversible.
    """
    acc = db.get_account(account_id)
    if not acc:
        raise HTTPException(404, "Cuenta no encontrada.")

    # Validación de input ANTES del preflight (para no marcar owned si el
    # input es inválido). Exactamente uno de days/eta.
    has_days = body.days is not None
    has_eta = body.eta is not None
    if has_days == has_eta:
        raise HTTPException(400, "Indica días restantes O una fecha, no ambos ni ninguno.")
    now = time.time()
    if has_days:
        if body.days < 1 or body.days > _MAX_DELETION_DAYS:
            raise HTTPException(400, f"Los días deben estar entre 1 y {_MAX_DELETION_DAYS}.")
        eta = now + body.days * 86400.0
    else:
        eta = float(body.eta)
        if eta <= now:
            raise HTTPException(400, "La fecha debe ser futura.")
        if eta > now + _MAX_DELETION_DAYS * 86400.0:
            raise HTTPException(400, "La fecha está demasiado lejos (máx ~10 años).")

    # Preflight de propiedad (idéntico al resto de acciones).
    if not acc["owned"] and not body.confirm_owned:
        raise HTTPException(
            status_code=412,
            detail={
                "code": "needs_ownership_confirmation",
                "message": ("Confirma que esta cuenta es tuya antes de "
                            "programar su eliminación."),
                "account": _account_summary(acc),
            },
        )
    if not acc["owned"] and body.confirm_owned:
        db.update_account(account_id, owned=1)
        audit.record("own", acc, extra={"via": "schedule_deletion_confirm"})
        acc = db.get_account(account_id)

    db.update_account(account_id, deletion_started_at=now, deletion_eta=eta)
    audit.record("schedule_deletion", acc, extra={"eta": eta, "via": "schedule_endpoint"})
    db.set_status(account_id, "pending_deletion",
                  "Eliminación programada por el usuario.")
    return {"ok": True, "status": "pending_deletion",
            "deletion_eta": eta, "deletion_started_at": now}


@app.post("/api/accounts/{account_id}/verify-deletion")
def api_verify_deletion(account_id: int):
    """Botón "Verificar" del estado vencido: comprueba por HTTP ligero si la
    cuenta ya no existe, reusando `engine.revisit_profile` (con guard anti-SSRF).

    - True  → la cuenta ya no existe: sella `deleted`.
    - False → sigue cargando: pasa a `manual` (revísala).
    - None  → inconcluso (timeout/sin profile_url): se queda en pending_deletion.

    En dry-run hace la lectura pero NO muta estado (patrón de mark-sent)."""
    acc = db.get_account(account_id)
    if not acc:
        raise HTTPException(404, "Cuenta no encontrada.")
    from .engine import revisit_profile
    result = revisit_profile(acc["profile_url"])

    if config.DRY_RUN:
        audit.record("verify_deletion", acc, dry_run=True, extra={"result": result})
        return {"ok": True, "status": acc["status"], "result": result, "dry_run": True}

    if result is True:
        audit.record("verify_deletion", acc, extra={"result": "deleted"})
        db.set_status(account_id, "deleted",
                      "Verificada: la cuenta ya no existe (revisita HTTP).")
        return {"ok": True, "status": "deleted", "result": True}
    if result is False:
        audit.record("verify_deletion", acc, extra={"result": "still_active"})
        db.set_status(account_id, "manual",
                      "La cuenta sigue activa tras el plazo; revísala.")
        return {"ok": True, "status": "manual", "result": False}
    # None: inconcluyente, no concluimos nada.
    audit.record("verify_deletion", acc, extra={"result": "inconclusive"})
    db.log(account_id, "info", "verificación inconcluyente; sigue pendiente")
    return {"ok": True, "status": "pending_deletion", "result": None}


@app.post("/api/accounts/{account_id}/cancel-deletion")
def api_cancel_deletion(account_id: int):
    """Cancela un plazo de eliminación: limpia las fechas y manda la cuenta a
    `manual` (revisar). No guardamos el estado previo, así que `manual` es el
    destino seguro para que el usuario re-decida."""
    acc = db.get_account(account_id)
    if not acc:
        raise HTTPException(404, "Cuenta no encontrada.")
    db.update_account(account_id, deletion_eta=None, deletion_started_at=None)
    audit.record("cancel_deletion", acc, extra={"via": "cancel_endpoint"})
    db.set_status(account_id, "manual", "Plazo de eliminación cancelado por el usuario.")
    return {"ok": True, "status": "manual"}


@app.post("/api/accounts/clear")
def api_clear_accounts():
    """Vacía la tabla. Permite empezar un escaneo nuevo sin acumulado previo."""
    db.clear_accounts()
    return {"ok": True}


# --- Informe / export -------------------------------------------------------
@app.get("/api/report")
def api_report(format: str = "json"):
    """Genera un informe completo de la sesión actual.

    format=json (default) → JSON con cuentas + resumen.
    format=csv             → CSV con una fila por cuenta.
    format=pdf             → PDF con tablas y resumen.

    No incluye el cuerpo completo de los correos para no inflar el archivo;
    incluye sí los destinatarios y asuntos. La consulta completa al audit
    vive en `~/.rastrillo/audit.json`. La lógica está en `rastrillo.reports`
    para que el CLI (`rastrillo report --format ...`) la comparta.
    """
    from fastapi.responses import Response
    from . import reports

    try:
        content, media_type, filename = reports.build_report(format)
    except ValueError as e:
        raise HTTPException(400, str(e))

    if media_type == "application/json":
        # JSON: devolvemos el dict directamente (FastAPI lo serializa y le
        # pone el Content-Type adecuado; mantiene el comportamiento previo).
        return content

    return Response(
        content=content,
        media_type=media_type,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# --- Onboarding -------------------------------------------------------------
@app.get("/api/onboarding")
def api_onboarding_status():
    """¿Ya pasó el usuario por el panel de bienvenida en este perfil?"""
    return {"onboarded": config.is_onboarded()}


@app.post("/api/onboarding/dismiss")
def api_onboarding_dismiss():
    """El usuario pulsó "Entendido". No volvemos a mostrar el panel."""
    config.mark_onboarded()
    return {"ok": True}


# --- Dry-run global ----------------------------------------------------------
@app.get("/api/dry-run")
def api_dry_run_get():
    return {"enabled": bool(config.DRY_RUN)}


@app.post("/api/dry-run")
def api_dry_run_set(body: DryRunBody):
    """Activa/desactiva el modo simulación. En dry-run, ninguna acción
    destructiva real ocurre — las cuentas pasan a 'dry_run' con un registro
    detallado de lo que habría pasado."""
    config.set_dry_run(body.enabled)
    log.info("dry-run -> %s", config.DRY_RUN)
    return {"enabled": config.DRY_RUN}


# --- API: directorio --------------------------------------------------------
@app.get("/api/directory")
def api_directory_info():
    info = directory.directory_info()
    info["ai_enabled"] = ai_assist.available()
    return info


# --- API: Domain Intelligence -----------------------------------------------
# Recon OSINT defensivo sobre UN dominio (WHOIS + DNS + correlación). Cuelga
# de /api/* → el auth_middleware ya exige token y valida Host. ALCANCE: solo
# para dominios propios o que el usuario tiene permiso de auditar.
@app.post("/api/domain/analyze")
def api_domain_analyze(body: DomainBody):
    """Analiza un dominio y persiste el informe. Síncrono pero acotado por los
    timeouts de domain_intel (RASTRILLO_WHOIS_TIMEOUT / RASTRILLO_DNS_TIMEOUT):
    en el peor caso, unos pocos saltos WHOIS + 4 consultas DNS."""
    domain = (body.domain or "").strip().lower()
    if not domain_intel.valid_domain(domain):
        raise HTTPException(400, "Dominio inválido. Ejemplo válido: ejemplo.com")
    db.init()
    try:
        report = domain_intel.analyze(domain)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(500, f"análisis de dominio falló: {e}")
    db.save_domain_report(report["domain"], json.dumps(report, ensure_ascii=False))
    return report


@app.get("/api/domain/report")
def api_domain_report(domain: str):
    """Devuelve el último informe guardado de `domain`, o {report: null}."""
    domain = (domain or "").strip().lower()
    if not domain_intel.valid_domain(domain):
        raise HTTPException(400, "Dominio inválido.")
    db.init()
    row = db.get_domain_report(domain)
    if not row or not row["report"]:
        return {"report": None}
    try:
        return {"report": json.loads(row["report"]),
                "created_at": row["created_at"]}
    except Exception as e:
        log.warning("domain report de %s no parsea como JSON (%s); devuelvo null",
                    domain, e)
        return {"report": None}


@app.get("/api/domain/history")
def api_domain_history():
    """Lista breve de dominios analizados (recientes primero)."""
    db.init()
    rows = db.list_domain_reports()
    return {"domains": [{"domain": r["domain"], "created_at": r["created_at"]}
                        for r in rows]}


# POST (no DELETE) a propósito: el auth_middleware exige token en todos los
# POST y en los GET de /api/*, pero NO en otros métodos — un DELETE entraría
# sin token. Mientras eso siga así, cualquier operación destructiva va por POST.
@app.post("/api/domain/report/delete")
def api_domain_report_delete(body: DomainBody):
    """Borra el informe guardado de UN dominio del histórico.

    Normaliza igual que /api/domain/analyze (`.strip().lower()` +
    `valid_domain`) para no crear una segunda semántica de normalización.
    """
    domain = (body.domain or "").strip().lower()
    if not domain_intel.valid_domain(domain):
        raise HTTPException(400, "Dominio inválido. Ejemplo válido: ejemplo.com")
    db.init()
    if not db.delete_domain_report(domain):
        raise HTTPException(
            404,
            f"No hay informe guardado de {domain}. Comprueba el dominio o "
            f"recarga el histórico: puede que ya se haya borrado.",
        )
    return {"ok": True, "domain": domain}


@app.post("/api/domain/history/clear")
def api_domain_history_clear():
    """Vacía el histórico de Domain Intelligence. Devuelve cuántos borró.

    `db.clear_domain_reports()` hace snapshot de la DB antes de tocar nada
    (mismo patrón que clear_accounts). No pasa por audit.py: ese log es de
    acciones destructivas sobre CUENTAS, no sobre datos públicos WHOIS/DNS.
    """
    db.init()
    n = db.clear_domain_reports()
    return {"ok": True, "deleted": n}


@app.post("/api/directory/refresh")
def api_directory_refresh():
    try:
        d = directory.load_directory(force_refresh=True)
    except Exception as e:
        raise HTTPException(502, f"No pude refrescar el directorio: {e}")
    return {"ok": True, "entries": len(d), "source": d.source}


# --- UI ---------------------------------------------------------------------
# Tarea 10: el frontend vive en `rastrillo/static/` (index.html + styles.css +
# app.js). Servimos los assets con StaticFiles bajo `/static/` y el HTML shell
# por GET / con la única pieza dinámica del arranque (un script `boot` que
# expone `window.__RASTRILLO_BOOT__` con los valores que antes inyectábamos
# por replace de placeholder).
app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")


def _meta_json() -> str:
    return json.dumps(STATUS_META)


def _boot_script() -> str:
    """Devuelve el `<script>` que el handler GET / inyecta en el HTML.

    Es la única pieza dinámica: el resto de assets va por /static. Si
    aparece otro valor que el frontend necesite al arrancar, añádelo aquí
    en vez de reintroducir placeholders en el HTML estático.
    """
    payload = {"META": STATUS_META, "VERIF_META": VERIFIABILITY_META}
    return (
        "<script>window.__RASTRILLO_BOOT__ = "
        + json.dumps(payload)
        + ";</script>"
    )


def _asset_version(filename: str) -> str:
    """Hash corto del contenido de un asset de `static/`, para cache-busting.

    Se inyecta como `?v=<hash>` en la etiqueta del HTML servido por GET /.
    Al basarse en el contenido (no en mtime), cambia EXACTAMENTE cuando cambia
    el archivo, forzando al navegador a re-bajar la versión nueva y nunca
    servir una cacheada vieja. Si el archivo no existe, degrada a "0"."""
    try:
        data = (_STATIC_DIR / filename).read_bytes()
    except OSError:
        return "0"
    return hashlib.sha256(data).hexdigest()[:10]


@app.get("/", response_class=HTMLResponse)
def index():
    html = (_STATIC_DIR / "index.html").read_text(encoding="utf-8")
    html = html.replace("<!--RASTRILLO_BOOT-->", _boot_script())
    # Cache-busting: cada asset lleva su propio hash de contenido. Reemplazamos
    # la URL exacta (con la comilla de cierre) para no tocar otras apariciones.
    html = html.replace('/static/app.js"',
                        f'/static/app.js?v={_asset_version("app.js")}"')
    html = html.replace('/static/styles.css"',
                        f'/static/styles.css?v={_asset_version("styles.css")}"')
    # Sin esto el navegador podría servir un index.html cacheado viejo, que
    # seguiría apuntando a un ?v= antiguo y anularía el cache-busting de los
    # assets. no-cache obliga a revalidar el HTML con el server en cada carga.
    return HTMLResponse(html, headers={"Cache-Control": "no-cache"})


# --- Arranque desde "python -m rastrillo.server" ----------------------------
if __name__ == "__main__":
    import uvicorn
    db.init()
    jobs.start_workers()
    print("Dashboard en http://127.0.0.1:8765")
    uvicorn.run(app, host="127.0.0.1", port=8765, log_level="warning")
