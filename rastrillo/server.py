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
import json
import logging
from typing import List, Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

from . import audit, config, db, jobs, directory, ai_assist, resolver
from .recipes import get_recipe

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
    "manual":        ("Revisar",          "#854F0B"),
    "skipped":       ("Conservada",       "#5F5E5A"),
    "failed":        ("Error",            "#A32D2D"),
    "not_mine":      ("Descartada",       "#5F5E5A"),
    "dry_run":       ("Simulada",         "#534AB7"),
}

# Acciones que SÍ son irreversibles. Para ellas:
#   - se exige owned=1 en la cuenta (o confirm_owned=true en el body),
#   - se respeta el modo dry-run,
#   - se graba en audit.json antes de tocar nada.
_DESTRUCTIVE_ACTIONS = {"delete", "anonymize", "retry"}


# --- Auth middleware --------------------------------------------------------
@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    """Cualquier POST exige el token del proceso (config.AUTH_TOKEN).

    Lo aceptamos por header X-Rastrillo-Token o por ?token=... en la query
    (útil para tests). Los GET y métodos seguros (OPTIONS, HEAD) son libres.

    Mensaje del 401: enriquecido con instrucciones accionables (no es solo
    "token inválido"; el front lo muestra como toast largo).
    """
    if request.method == "POST":
        tok = request.headers.get("x-rastrillo-token") \
            or request.query_params.get("token")
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


# --- API: lectura -----------------------------------------------------------
@app.get("/api/accounts")
def api_accounts():
    rows = [dict(r) for r in db.list_accounts()]
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
    - kind=semi_auto  → estado `semi_auto`, URL en profile_url.
    - kind=email_draft → estado `email_draft`, borrador en action_meta.
    """
    db.update_account(account_id, action_meta=json.dumps(res.to_meta(), ensure_ascii=False))
    if res.url:
        db.update_account(account_id, profile_url=res.url)

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
    """
    acc = db.get_account(account_id)
    if not acc:
        raise HTTPException(404, "Cuenta no encontrada.")
    if body.owned:
        db.update_account(account_id, owned=1)
        db.log(account_id, "info", "el usuario confirmó: es mía")
        audit.record("own", acc, extra={"via": "triage"})
        return {"ok": True, "owned": True, "status": acc["status"]}
    db.update_account(account_id, owned=0)
    db.set_status(account_id, "not_mine", "descartada por el usuario (triage)")
    audit.record("discard", acc, extra={"via": "triage"})
    return {"ok": True, "owned": False, "status": "not_mine"}


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
    for row in db.list_accounts():
        if row["status"] != "found":
            summary["skipped_status"] += 1
            continue
        if not row["owned"]:
            summary["skipped_unowned"] += 1
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
        # Persistimos la Resolution siempre.
        db.update_account(row["id"], action_meta=json.dumps(res.to_meta(), ensure_ascii=False))
        if res.url:
            db.update_account(row["id"], profile_url=res.url)
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


@app.post("/api/accounts/discard-low")
def api_discard_low():
    """Bulk: descarta como 'not_mine' todas las cuentas en estado 'found' con
    confidence='low' que no estén marcadas como propias. Una sola pulsación
    barre el ruido típico de Sherlock para usernames cortos/genéricos."""
    n = 0
    for r in db.list_accounts(status="found"):
        if (r["confidence"] or "") == "low" and not r["owned"]:
            db.set_status(r["id"], "not_mine", "descartada en lote (confidence=low)")
            audit.record("discard", r, extra={"via": "bulk_low"})
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

    # Si la cuenta venía de email_draft, registramos cuándo se envió (para el
    # seguimiento GDPR: el plazo legal en la UE es 30 días).
    sent_at = None
    if acc["status"] == "email_draft":
        import time as _t
        sent_at = _t.time()
        db.update_account(account_id, sent_at=sent_at)
    audit.record("mark_sent", acc, extra={"sent_at": sent_at})
    db.set_status(account_id, "user_done", "Marcada como tramitada por el usuario.")
    return {"ok": True, "status": "user_done", "sent_at": sent_at}


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

    No incluye el cuerpo completo de los correos para no inflar el archivo;
    incluye sí los destinatarios y asuntos. La consulta completa al audit
    vive en `~/.rastrillo/audit.json`.
    """
    import csv as _csv
    import io
    import time as _t
    from fastapi.responses import PlainTextResponse, Response

    rows = [dict(r) for r in db.list_accounts()]
    now = _t.time()
    enriched = []
    for r in rows:
        meta = None
        if r.get("action_meta"):
            try:
                meta = json.loads(r["action_meta"])
            except Exception:
                meta = None
        days_since_sent = None
        if r.get("sent_at"):
            days_since_sent = int((now - float(r["sent_at"])) // 86400)
        enriched.append({
            **r,
            "days_since_sent": days_since_sent,
            "resolver_layer": (meta or {}).get("layer"),
            "resolver_kind":  (meta or {}).get("kind"),
            "email_to":       (meta or {}).get("email_to"),
            "email_subject":  (meta or {}).get("email_subject"),
        })

    if format == "csv":
        out = io.StringIO()
        cols = ["id", "platform", "display_name", "source", "source_site",
                "identifier", "profile_url", "status", "confidence", "owned",
                "deletion_type", "difficulty", "resolver_layer",
                "resolver_kind", "email_to", "email_subject",
                "updated_at", "sent_at", "days_since_sent", "last_message"]
        w = _csv.DictWriter(out, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for r in enriched:
            w.writerow(r)
        body = out.getvalue()
        ts = _t.strftime("%Y%m%d-%H%M%S", _t.gmtime(now))
        return Response(
            content=body,
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="rastrillo-{ts}.csv"'},
        )

    # Resumen agregado (compartido por JSON y PDF)
    stats = db.stats()
    audit_entries = audit.read_all()
    by_action = {}
    for a in audit_entries:
        by_action[a["action"]] = by_action.get(a["action"], 0) + 1
    summary = {
        "total":         len(enriched),
        "by_status":     stats,
        "audit_actions": by_action,
        "audit_total":   len(audit_entries),
    }

    if format == "pdf":
        from . import report_pdf
        data = report_pdf.render_pdf(
            accounts=enriched,
            summary=summary,
            audit_summary=by_action,
            generated_at=now,
        )
        ts = _t.strftime("%Y%m%d-%H%M%S", _t.gmtime(now))
        return Response(
            content=data,
            media_type="application/pdf",
            headers={"Content-Disposition": f'attachment; filename="rastrillo-{ts}.pdf"'},
        )

    # JSON por defecto
    return {
        "generated_at": now,
        "generated_at_iso": _t.strftime("%Y-%m-%dT%H:%M:%SZ", _t.gmtime(now)),
        "summary": summary,
        "accounts": enriched,
    }


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


@app.post("/api/directory/refresh")
def api_directory_refresh():
    try:
        d = directory.load_directory(force_refresh=True)
    except Exception as e:
        raise HTTPException(502, f"No pude refrescar el directorio: {e}")
    return {"ok": True, "entries": len(d), "source": d.source}


# --- UI ---------------------------------------------------------------------
# Diseño tipo Linear / Vercel: design tokens en :root, 8-pt spacing, tipografía
# system con caída a SF/Inter, badges como pill+dot tonal, sombras muy sutiles,
# focus visibles, microinteracciones de 120-180ms. Toda la funcionalidad
# (endpoints, polling, acciones) se mantiene tal cual.
_INDEX_HTML = r"""<!doctype html><html lang="es"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Rastrillo</title>
<!-- Tema: aplicado ANTES del CSS para evitar el flash (FOUC). Usa la
     preferencia guardada o, en su defecto, prefers-color-scheme del SO. -->
<script>
(function(){
  try{
    var saved=localStorage.getItem("rastrillo-theme");
    var sys=window.matchMedia&&window.matchMedia("(prefers-color-scheme: dark)").matches;
    document.documentElement.dataset.theme=saved||(sys?"dark":"light");
  }catch(e){document.documentElement.dataset.theme="light"}
})();
</script>
<style>
:root{
  /* type & layout */
  --font-sans:-apple-system,BlinkMacSystemFont,"SF Pro Text","Inter","Segoe UI",system-ui,sans-serif;
  --font-mono:ui-monospace,"SF Mono","Roboto Mono",Menlo,Consolas,monospace;
  --r-sm:6px; --r:8px; --r-md:10px; --r-lg:14px; --r-xl:18px;
  /* surface palette */
  --bg:#fafafa;
  --surface:#ffffff;
  --surface-2:#fafafa;          /* fondos sutiles (modal-foot, textarea, etc.) */
  --topbar-bg:rgba(255,255,255,.82);
  --border:#ececec;
  --border-strong:#d8d8d8;
  --border-subtle:#f3f3f3;
  --hover:#fafafa;
  --filter-bg:#efeff1;
  /* text */
  --text:#0a0a0a;
  --text-2:#525252;
  --text-3:#8a8a8a;
  --text-on-fill:#ffffff;       /* sobre btn-primary/accent/warn */
  /* accent */
  --accent:#5e6ad2;
  --accent-2:#4a55b5;
  --accent-soft:#eef0fc;
  /* status tones */
  --success:#06794a;  --success-bg:#e6f7ef;
  --warn:#a05a00;     --warn-bg:#fff4e0;   --warn-bg-strong:#fff0d4;
  --warn-text:#6c3d05;
  --danger:#a62121;   --danger-bg:#fdecec; --danger-border:#f1d4d4;
  --info:#2a52b8;     --info-bg:#ecf1fc;
  --indigo:#4845a8;   --indigo-bg:#eef0fc;
  --neutral:#525252;  --neutral-bg:#f1f1f1;
  /* skeleton (gradiente) */
  --sk-a:#f1f1f1; --sk-b:#e6e6e6;
  /* toast */
  --toast-bg:#111; --toast-fg:#fff; --toast-err-bg:#7a1717;
  /* shadow */
  --shadow-sm:0 1px 2px rgba(15,15,17,.04);
  --shadow:0 1px 3px rgba(15,15,17,.05),0 1px 2px rgba(15,15,17,.04);
  --shadow-lg:0 16px 40px rgba(15,15,17,.10),0 4px 12px rgba(15,15,17,.06);
  /* motion */
  --t-fast:120ms cubic-bezier(.4,0,.2,1);
  --t:180ms cubic-bezier(.4,0,.2,1);
}
/* ── Dark theme: redefinimos solo los tokens. Todo lo demás funciona ── */
[data-theme="dark"]{
  --bg:#0a0a0a;
  --surface:#161616;
  --surface-2:#1a1a1a;
  --topbar-bg:rgba(14,14,14,.85);
  --border:#262626;
  --border-strong:#3a3a3a;
  --border-subtle:#1f1f1f;
  --hover:#1c1c1c;
  --filter-bg:#1c1c1c;
  --text:#f5f5f5;
  --text-2:#a1a1a1;
  --text-3:#6f6f6f;
  --text-on-fill:#0a0a0a;       /* en dark el primary es claro → texto oscuro */
  --accent:#8c97e8;
  --accent-2:#a5aeed;
  --accent-soft:#232753;
  --success:#4ade80;  --success-bg:#0d2e22;
  --warn:#fbbf24;     --warn-bg:#3a2509;   --warn-bg-strong:#4a2f0c;
  --warn-text:#fcd34d;
  --danger:#f87171;   --danger-bg:#3a0e0e; --danger-border:#5a1b1b;
  --info:#93c5fd;     --info-bg:#102648;
  --indigo:#a5b4fc;   --indigo-bg:#1e1d4a;
  --neutral:#a3a3a3;  --neutral-bg:#262626;
  --sk-a:#1c1c1c; --sk-b:#262626;
  --toast-bg:#f5f5f5; --toast-fg:#0a0a0a; --toast-err-bg:#7f1d1d;
  --shadow-sm:0 1px 2px rgba(0,0,0,.5);
  --shadow:0 1px 3px rgba(0,0,0,.6),0 1px 2px rgba(0,0,0,.5);
  --shadow-lg:0 16px 40px rgba(0,0,0,.6),0 4px 12px rgba(0,0,0,.4);
  color-scheme: dark;
}
*{box-sizing:border-box}
html,body{margin:0;background:var(--bg);color:var(--text);
  font-family:var(--font-sans);font-size:14px;line-height:1.5;
  letter-spacing:-.005em;
  -webkit-font-smoothing:antialiased;-moz-osx-font-smoothing:grayscale}
a{color:inherit;text-decoration:none}
:focus-visible{outline:2px solid var(--accent);outline-offset:2px;border-radius:4px}
button:focus-visible,
.btn:focus-visible{outline:none;box-shadow:0 0 0 3px var(--accent-soft);border-color:var(--accent)}
::selection{background:var(--accent-soft);color:var(--text)}

/* ── Topbar ────────────────────────────────────────────────────── */
.topbar{
  position:sticky;top:0;z-index:30;
  background:var(--topbar-bg);
  backdrop-filter:saturate(180%) blur(12px);
  -webkit-backdrop-filter:saturate(180%) blur(12px);
  border-bottom:1px solid var(--border);
  padding:12px 28px;
  display:flex;align-items:center;gap:14px;
}
.brand{display:flex;align-items:center;gap:10px;min-width:0}
.brand-mark{
  width:28px;height:28px;border-radius:8px;
  background:linear-gradient(135deg,#5e6ad2 0%,#8b6ee0 100%);
  display:grid;place-items:center;color:#fff;font-weight:700;font-size:13px;
  box-shadow:0 1px 2px rgba(94,106,210,.4),inset 0 1px 0 rgba(255,255,255,.2);
  flex-shrink:0;
}
[data-theme="dark"] .brand-mark{
  box-shadow:0 1px 2px rgba(0,0,0,.5),inset 0 1px 0 rgba(255,255,255,.15);
}
.brand-title{font-size:14px;font-weight:600;letter-spacing:-.01em;line-height:1.1}
.brand-sub{font-size:11.5px;color:var(--text-3);line-height:1.2;margin-top:2px}
.topbar-right{margin-left:auto;display:flex;gap:8px;align-items:center;flex-wrap:wrap;justify-content:flex-end}

/* ── Chip ────────────────────────────────────────────────────── */
.chip{
  display:inline-flex;align-items:center;gap:6px;
  height:28px;padding:0 10px;
  background:var(--surface);border:1px solid var(--border);
  border-radius:999px;font-size:12px;color:var(--text-2);white-space:nowrap;
}
.chip svg{width:13px;height:13px}
.chip-dot{width:6px;height:6px;border-radius:50%;background:var(--neutral)}
.chip-dot.on{background:var(--success)}
.chip-dot.warn{background:var(--warn)}
.chip-dot.off{background:var(--danger)}
.chip-dot.pulse{animation:pulse 1.4s ease-in-out infinite}
.chip-toggle{border-style:dashed;cursor:pointer;color:var(--text-3)}
.chip-toggle:hover{color:var(--text-2);border-color:var(--border-strong)}
.chip-toggle.on{
  background:var(--warn-bg);color:var(--warn);
  border-color:var(--warn);border-style:solid;
}
.chip-toggle.on .chip-dot{background:var(--warn);animation:pulse 1.4s ease-in-out infinite}
.owned-tick{
  display:inline-flex;vertical-align:-2px;margin-left:6px;
  width:14px;height:14px;border-radius:50%;
  background:var(--success-bg);color:var(--success);
  align-items:center;justify-content:center;
}
.owned-tick svg{width:10px;height:10px;stroke-width:2.5}
@keyframes pulse{0%,100%{opacity:1;transform:scale(1)}50%{opacity:.4;transform:scale(.7)}}

/* ── Layout ────────────────────────────────────────────────────── */
.container{max-width:1080px;margin:0 auto;padding:28px 28px 96px}
@media(max-width:640px){.container{padding:20px 16px 80px}.topbar{padding:12px 16px}}

/* ── Card ────────────────────────────────────────────────────── */
.card{
  background:var(--surface);
  border:1px solid var(--border);
  border-radius:var(--r-xl);
  padding:20px 22px;
}
.card-h{display:flex;align-items:baseline;gap:8px;margin-bottom:14px}
.card-h .h-title{font-size:14px;font-weight:600;letter-spacing:-.01em}
.card-h .h-sub{font-size:12px;color:var(--text-3);margin-left:auto}

/* ── Form / inputs ────────────────────────────────────────────── */
.scan-grid{display:grid;grid-template-columns:1fr 1fr;gap:14px}
@media(max-width:720px){.scan-grid{grid-template-columns:1fr}}
.field{display:flex;flex-direction:column;gap:6px}
.field-label{
  font-size:12px;font-weight:500;color:var(--text-2);
  display:inline-flex;align-items:center;gap:6px;
}
.field-label svg{width:13px;height:13px;color:var(--text-3)}
.input,.textarea{
  width:100%;background:var(--surface);color:var(--text);
  border:1px solid var(--border);border-radius:var(--r-md);
  padding:10px 12px;
  font-family:inherit;font-size:13px;
  transition:border var(--t-fast),box-shadow var(--t-fast);
}
.textarea{min-height:72px;resize:vertical;line-height:1.45}
.textarea::placeholder{color:var(--text-3)}
.input:focus,.textarea:focus{
  outline:none;border-color:var(--accent);
  box-shadow:0 0 0 3px var(--accent-soft);
}
.card-foot{display:flex;align-items:center;gap:12px;margin-top:14px;flex-wrap:wrap}
.status{font-size:12px;color:var(--text-3)}
.status.busy{color:var(--warn)}

/* ── Buttons ────────────────────────────────────────────────── */
.btn{
  display:inline-flex;align-items:center;justify-content:center;gap:6px;
  height:32px;padding:0 12px;
  background:var(--surface);color:var(--text);
  border:1px solid var(--border);border-radius:var(--r);
  font-family:inherit;font-size:13px;font-weight:500;
  cursor:pointer;white-space:nowrap;user-select:none;
  transition:background var(--t-fast),border var(--t-fast),color var(--t-fast),
             box-shadow var(--t-fast),transform var(--t-fast);
}
.btn:hover{background:var(--hover);border-color:var(--border-strong)}
.btn:active{transform:translateY(.5px)}
.btn[disabled],.btn:disabled{opacity:.55;cursor:not-allowed;transform:none}
.btn svg{width:14px;height:14px;flex-shrink:0}
.btn-sm{height:28px;padding:0 10px;font-size:12.5px;gap:5px;border-radius:7px}
.btn-sm svg{width:13px;height:13px}
.btn-primary{background:var(--text);color:var(--text-on-fill);border-color:var(--text)}
.btn-primary:hover{background:var(--text);opacity:.88}
.btn-accent{background:var(--accent);color:var(--text-on-fill);border-color:var(--accent);
  box-shadow:0 1px 2px rgba(94,106,210,.25)}
.btn-accent:hover{background:var(--accent-2)}
[data-theme="dark"] .btn-accent{color:#0a0a0a}
.btn-warn{background:var(--warn);color:var(--text-on-fill);border-color:var(--warn)}
.btn-warn:hover{filter:brightness(.92)}
.btn-danger{color:var(--danger);border-color:var(--danger-border)}
.btn-danger:hover{background:var(--danger-bg);border-color:var(--danger);color:var(--danger)}
.btn-ghost{background:transparent;border-color:transparent;color:var(--text-2)}
.btn-ghost:hover{background:var(--neutral-bg);color:var(--text);border-color:transparent}
.btn-icon{padding:0;width:32px;justify-content:center}
.btn-sm.btn-icon{width:28px}
.kbd{
  display:inline-flex;align-items:center;justify-content:center;
  height:18px;padding:0 6px;
  background:rgba(127,127,127,.18);color:inherit;
  border-radius:4px;font-family:var(--font-mono);font-size:11px;
  margin-left:2px;
}
.btn-primary .kbd,.btn-accent .kbd,.btn-warn .kbd{background:rgba(0,0,0,.18)}
[data-theme="dark"] .btn-primary .kbd,
[data-theme="dark"] .btn-accent .kbd{background:rgba(0,0,0,.25)}

/* ── Stats ────────────────────────────────────────────────── */
.stats{
  display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));
  gap:12px;margin:18px 0 8px;
}
.stat{
  background:var(--surface);border:1px solid var(--border);
  border-radius:var(--r-lg);padding:14px 16px;
  display:flex;flex-direction:column;gap:2px;position:relative;
  transition:border var(--t-fast);
}
.stat-l{font-size:11px;font-weight:600;color:var(--text-3);
  text-transform:uppercase;letter-spacing:.06em}
.stat-n{font-size:24px;font-weight:600;letter-spacing:-.02em;line-height:1.2}
.stat-sub{font-size:11.5px;color:var(--text-3)}
.stat.accent .stat-n{color:var(--accent)}
.stat.success .stat-n{color:var(--success)}
.stat.warn .stat-n{color:var(--warn)}
.stat.danger .stat-n{color:var(--danger)}

/* ── Section header / filters ────────────────────────────── */
.section-h{display:flex;align-items:center;gap:10px;margin:24px 0 12px;flex-wrap:wrap}
.section-title{font-size:14px;font-weight:600}
.section-tools{margin-left:auto;display:flex;gap:8px;align-items:center}
.filters{
  display:inline-flex;padding:3px;background:var(--filter-bg);
  border-radius:10px;gap:2px;border:1px solid var(--border-subtle);
}
.filter{
  border:0;background:transparent;
  padding:6px 10px;border-radius:7px;
  font-size:12.5px;color:var(--text-2);cursor:pointer;
  font-family:inherit;font-weight:500;
  transition:background var(--t-fast),color var(--t-fast);
}
.filter:hover{color:var(--text)}
.filter.on{background:var(--surface);color:var(--text);box-shadow:var(--shadow-sm)}

/* ── Account list ────────────────────────────────────────── */
.account-list{
  background:var(--surface);border:1px solid var(--border);
  border-radius:var(--r-xl);overflow:hidden;
}
.account-row{
  display:grid;
  grid-template-columns:auto minmax(0,1fr) auto auto;
  align-items:center;gap:14px;
  padding:14px 18px;
  border-bottom:1px solid var(--border-subtle);
  transition:background var(--t-fast);
  position:relative;
}
.account-row:last-child{border-bottom:0}
.account-row:hover{background:var(--hover)}
.account-row.attn{background:var(--warn-bg)}
.account-row.attn:hover{background:var(--warn-bg-strong)}
.account-row.attn::before{
  content:"";position:absolute;left:0;top:0;bottom:0;width:3px;
  background:var(--warn);border-radius:0 3px 3px 0;
}
.avatar{
  width:34px;height:34px;border-radius:9px;
  background:linear-gradient(135deg,var(--neutral-bg),var(--border));
  color:var(--text-2);
  display:grid;place-items:center;
  font-size:13.5px;font-weight:600;flex-shrink:0;
  letter-spacing:-.01em;
}
.row-main{min-width:0;display:flex;flex-direction:column;gap:2px}
.row-title{
  font-size:13.5px;font-weight:500;color:var(--text);
  white-space:nowrap;overflow:hidden;text-overflow:ellipsis;
}
.row-meta{
  font-size:12px;color:var(--text-3);
  white-space:nowrap;overflow:hidden;text-overflow:ellipsis;
  display:flex;gap:6px;align-items:center;
}
.row-meta .dot{color:var(--border-strong)}
.row-meta a{color:var(--text-2)}
.row-meta a:hover{color:var(--text);text-decoration:underline}

/* ── Badge ────────────────────────────────────────────────── */
.badge{
  display:inline-flex;align-items:center;gap:6px;
  height:22px;padding:0 9px;
  font-size:11.5px;font-weight:500;
  border-radius:999px;
  background:var(--neutral-bg);color:var(--neutral);
  white-space:nowrap;flex-shrink:0;
}
.badge::before{
  content:"";width:6px;height:6px;border-radius:50%;
  background:currentColor;flex-shrink:0;
}
.badge.success{background:var(--success-bg);color:var(--success)}
.badge.warn{background:var(--warn-bg);color:var(--warn)}
.badge.danger{background:var(--danger-bg);color:var(--danger)}
.badge.info{background:var(--info-bg);color:var(--info)}
.badge.indigo{background:var(--indigo-bg);color:var(--indigo)}
.badge.accent{background:var(--accent-soft);color:var(--accent-2)}
.badge.attn::before{animation:pulse 1.4s ease-in-out infinite}

.row-actions{display:flex;gap:6px;flex-shrink:0;align-items:center}
.row-actions a.btn{text-decoration:none}

.row-msg{
  grid-column:1 / -1;margin-top:8px;
  padding:10px 12px;
  background:var(--warn-bg);color:var(--warn-text);
  border-radius:var(--r-md);
  font-size:12.5px;line-height:1.5;white-space:pre-wrap;
}
.row-msg a{color:var(--text);text-decoration:underline}
.account-row.attn .row-msg{background:var(--warn-bg-strong)}

/* ── Empty & skeleton ────────────────────────────────────── */
.empty{padding:56px 24px;text-align:center;color:var(--text-3)}
.empty-icon{
  width:44px;height:44px;border-radius:12px;
  background:var(--neutral-bg);
  display:grid;place-items:center;margin:0 auto 14px;
  color:var(--text-3);
}
.empty-icon svg{width:22px;height:22px}
.empty-title{font-size:14px;font-weight:600;color:var(--text);margin-bottom:4px}
.empty-body{font-size:13px;max-width:380px;margin:0 auto;line-height:1.5}
.skeleton-row{
  display:grid;grid-template-columns:34px 1fr 80px;
  gap:14px;padding:14px 18px;
  border-bottom:1px solid var(--border-subtle);align-items:center;
}
.sk{
  background:linear-gradient(90deg,var(--sk-a) 0%,var(--sk-b) 50%,var(--sk-a) 100%);
  background-size:200% 100%;
  animation:shimmer 1.4s linear infinite;
  border-radius:6px;
}
.sk.avatar{width:34px;height:34px;border-radius:9px}
.sk.bar{height:11px;width:40%}
.sk.bar.s{width:22%;margin-top:6px}
.sk.pill{height:20px;width:80px;border-radius:999px;justify-self:end}
@keyframes shimmer{0%{background-position:200% 0}100%{background-position:-200% 0}}

/* ── Modal ────────────────────────────────────────────────── */
.modal-bg{
  position:fixed;inset:0;z-index:50;
  background:rgba(0,0,0,.5);
  backdrop-filter:blur(4px);-webkit-backdrop-filter:blur(4px);
  display:none;align-items:center;justify-content:center;
  padding:40px 20px;opacity:0;transition:opacity var(--t);
}
[data-theme="dark"] .modal-bg{background:rgba(0,0,0,.65)}
.modal-bg.on{display:flex;opacity:1}
.modal{
  background:var(--surface);border-radius:var(--r-xl);
  max-width:720px;width:100%;max-height:90vh;
  display:flex;flex-direction:column;
  box-shadow:var(--shadow-lg);
  transform:translateY(8px);transition:transform var(--t);
  overflow:hidden;
}
.modal-bg.on .modal{transform:translateY(0)}
.modal-head{
  padding:18px 22px;border-bottom:1px solid var(--border-subtle);
  display:flex;align-items:center;gap:12px;
}
.modal-head h3{margin:0;font-size:15px;font-weight:600;letter-spacing:-.01em}
.modal-head .modal-close{margin-left:auto}
.modal-body{padding:18px 22px;overflow:auto}
.modal-body .kv{font-size:12.5px;color:var(--text-2);margin-bottom:6px}
.modal-body .kv b{color:var(--text);font-weight:600;margin-right:6px}
.modal-body .hint{font-size:12px;color:var(--text-3);margin-top:10px;line-height:1.5}
.modal-body textarea{
  width:100%;margin-top:12px;
  border:1px solid var(--border);border-radius:var(--r-md);
  padding:12px;background:var(--surface-2);color:var(--text);
  font-family:var(--font-mono);font-size:12.5px;line-height:1.5;
  resize:vertical;min-height:200px;
}
.modal-body textarea:focus{outline:none;border-color:var(--accent);box-shadow:0 0 0 3px var(--accent-soft)}
.modal-foot{
  padding:14px 22px;border-top:1px solid var(--border-subtle);
  display:flex;gap:8px;justify-content:flex-end;align-items:center;
  background:var(--surface-2);
}

/* ── Toast ────────────────────────────────────────────────── */
.toast{
  position:fixed;bottom:24px;right:24px;z-index:60;
  background:var(--toast-bg);color:var(--toast-fg);
  padding:10px 14px;border-radius:10px;
  font-size:13px;font-weight:500;
  display:flex;align-items:center;gap:8px;
  opacity:0;transform:translateY(8px);
  transition:opacity var(--t),transform var(--t);
  box-shadow:var(--shadow-lg);max-width:380px;
  pointer-events:none;
}
.toast.on{opacity:1;transform:translateY(0);pointer-events:auto}
.toast.err{background:var(--toast-err-bg);color:#fff}

/* ── Reduced motion ─────────────────────────────────────── */
@media (prefers-reduced-motion:reduce){
  *,*::before,*::after{animation:none!important;transition:none!important}
}
</style></head><body>

<!-- Topbar: marca + estado global del sistema (directorio + IA + refresh) -->
<header class="topbar">
  <div class="brand">
    <div class="brand-mark" aria-hidden="true">R</div>
    <div>
      <div class="brand-title">Rastrillo</div>
      <div class="brand-sub">Tu huella digital, recogida y borrada</div>
    </div>
  </div>
  <div class="topbar-right">
    <button id="dry-toggle" class="chip chip-toggle"
            title="Modo simulación: nada destructivo se ejecuta"
            aria-pressed="false"
            aria-label="Modo simulación">
      <span class="chip-dot"></span>
      <span class="dry-label">Simulación</span>
    </button>
    <div id="chip-dir" class="chip" title="Estado del directorio de borrado"></div>
    <div id="chip-ai" class="chip" title="Estado del modo IA"></div>
    <button id="dir-refresh" class="btn btn-icon btn-ghost"
            aria-label="Refrescar directorio"
            title="Refrescar directorio"></button>
    <a id="report-csv-btn" class="btn btn-sm btn-ghost"
       href="/api/report?format=csv" download
       title="Descargar informe en CSV (apto para análisis)">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3v12"/><path d="m7 10 5 5 5-5"/><path d="M5 21h14"/></svg>
      CSV
    </a>
    <a id="report-pdf-btn" class="btn btn-sm btn-ghost"
       href="/api/report?format=pdf" download
       title="Descargar informe en PDF (listo para imprimir/archivar)">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M14 3H6a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V9z"/><path d="M14 3v6h6"/><path d="M9 13h6M9 17h4"/></svg>
      PDF
    </a>
    <button id="theme-toggle" class="btn btn-icon btn-ghost"
            aria-label="Cambiar tema"
            title="Cambiar entre modo claro y oscuro"></button>
  </div>
</header>

<main class="container">

  <!-- 1. Nuevo escaneo ─────────────────────────────────────── -->
  <section class="card" aria-labelledby="scan-h">
    <div class="card-h">
      <span id="scan-h" class="h-title">Nuevo escaneo</span>
      <span class="h-sub">Sherlock + Holehe, todo local</span>
    </div>
    <div class="scan-grid">
      <div class="field">
        <label class="field-label" for="users">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="8" r="4"/><path d="M4 21a8 8 0 0 1 16 0"/></svg>
          Usernames
        </label>
        <textarea id="users" class="textarea" placeholder="je7remy, otro_user"></textarea>
      </div>
      <div class="field">
        <label class="field-label" for="mails">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="5" width="18" height="14" rx="2"/><path d="m3 7 9 6 9-6"/></svg>
          Correos
        </label>
        <textarea id="mails" class="textarea" placeholder="tu@correo.com"></textarea>
      </div>
    </div>
    <div class="card-foot">
      <button id="scan-btn" class="btn btn-accent">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><circle cx="11" cy="11" r="7"/><path d="m20 20-3-3"/></svg>
        Escanear
        <span class="kbd">Ctrl ↵</span>
      </button>
      <span id="scan-status" class="status"></span>
    </div>
  </section>

  <!-- 2. Stats ─────────────────────────────────────────── -->
  <div id="stats" class="stats"></div>

  <!-- 3. Lista de cuentas ───────────────────────────── -->
  <div class="section-h">
    <div class="section-title">Cuentas detectadas</div>
    <div class="section-tools">
      <div class="filters" role="tablist" aria-label="Filtros de cuentas">
        <button class="filter on" id="f-all"       data-f="all"       onclick="setFilter('all')"       role="tab">Todas</button>
        <button class="filter"    id="f-triage"    data-f="triage"    onclick="setFilter('triage')"    role="tab">Triage</button>
        <button class="filter"    id="f-pending"   data-f="pending"   onclick="setFilter('pending')"   role="tab">Pendientes</button>
        <button class="filter"    id="f-your_turn" data-f="your_turn" onclick="setFilter('your_turn')" role="tab">Tu turno</button>
        <button class="filter"    id="f-action"    data-f="action"    onclick="setFilter('action')"    role="tab">Acción</button>
        <button class="filter"    id="f-done"      data-f="done"      onclick="setFilter('done')"      role="tab">Completadas</button>
        <button class="filter"    id="f-kept"      data-f="kept"      onclick="setFilter('kept')"      role="tab">Conservadas</button>
        <button class="filter"    id="f-discarded" data-f="discarded" onclick="setFilter('discarded')" role="tab">Descartadas</button>
      </div>
      <button id="process-all-btn" class="btn btn-sm btn-accent"
              onclick="askProcessAllAuto()"
              title="Encola todas las cuentas confirmadas como tuyas cuyo flujo es automatizable">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M3 12l3 3 5-5"/><path d="M11 12l3 3 7-7"/></svg>
        <span id="process-all-label">Procesar automáticas</span>
      </button>
      <!-- visibilidad controlada por JS según filtro activo -->
      <button id="bulk-low-btn" class="btn btn-sm btn-ghost"
              onclick="discardLowConfidence()"
              title="Descartar en lote todas las cuentas pendientes con confianza baja"
              style="display:none">
        Descartar low
      </button>
      <button id="clear-btn" class="btn btn-sm btn-danger"
              onclick="askClear()"
              title="Borrar todas las cuentas detectadas. El próximo escaneo empieza limpio."
              aria-label="Limpiar todas las cuentas">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M3 6h18M8 6V4a1 1 0 0 1 1-1h6a1 1 0 0 1 1 1v2M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6"/><path d="M10 11v6M14 11v6"/></svg>
        Limpiar
      </button>
    </div>
  </div>
  <div class="account-list" id="list" aria-live="polite"></div>
</main>

<div id="toast" class="toast" role="status" aria-live="polite"></div>
<div id="modal" class="modal-bg" role="dialog" aria-modal="true" aria-labelledby="modal-title"></div>

<script>
/* eslint-disable */
const META=__META__;
const $=id=>document.getElementById(id);

/* ── Estado global de la UI ───────────────────────────────── */
let CURRENT_FILTER="all";

/* Tono visual de cada estado (clases CSS, desacoplado de colores backend). */
const TONE={
  found:"", queued:"info", in_progress:"warn",
  awaiting_user:"warn", deleted:"success", anonymized:"indigo",
  user_done:"success",
  semi_auto:"accent", email_draft:"indigo",
  manual:"warn", skipped:"", failed:"danger",
  not_mine:"", dry_run:"indigo",
};
/* Tono del badge de confianza. */
const CONF_TONE={ high:"success", medium:"warn", low:"danger" };
const CONF_LABEL={ high:"Confianza alta", medium:"Confianza media", low:"Confianza baja" };

/* Agrupación de estados por intención del usuario.
 *   triage = found sin confirmar dueño (de discovery, esperando triage)
 *   pending/action/done/kept/discarded son los otros apartados
 */
const GROUPS={
  pending  :["queued","in_progress"],
  your_turn:["semi_auto","email_draft","awaiting_user"],   /* 1 clic / 1 envío / 1 confirmación */
  action   :["semi_auto","email_draft","awaiting_user","manual","failed"],
  done     :["deleted","anonymized","user_done","dry_run"],
  kept     :["skipped"],
  discarded:["not_mine"],
};
const SHOW_MSG=new Set(["awaiting_user","manual","failed","skipped","semi_auto",
                        "email_draft","user_done","dry_run","not_mine"]);

/* ── Auth: token del proceso ─────────────────────────────────────
 * El backend genera un token aleatorio en cada arranque. Lo recibimos en la
 * URL (?token=...) al primer load y lo guardamos en sessionStorage. Todos
 * los POST lo mandan en X-Rastrillo-Token. Sin token mostramos una página
 * explicativa con un input para pegarlo manualmente. */
let _TOKEN=(function(){
  try{
    const u=new URL(location.href);
    const t=u.searchParams.get("token");
    if(t){
      sessionStorage.setItem("rastrillo-token", t);
      // limpiamos la URL para no dejarlo expuesto en el historial
      history.replaceState({}, "", u.pathname);
    }
    return sessionStorage.getItem("rastrillo-token") || "";
  }catch(e){ return ""; }
})();

/* ── Iconos SVG (lucide-like, 1.8px stroke) ───────────────── */
const ICONS={
  refresh:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M21 12a9 9 0 1 1-3-6.7"/><path d="M21 3v6h-6"/></svg>',
  sparkles:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3v3M12 18v3M3 12h3M18 12h3M5.6 5.6l2.1 2.1M16.3 16.3l2.1 2.1M5.6 18.4l2.1-2.1M16.3 7.7l2.1-2.1"/></svg>',
  external:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M14 4h6v6"/><path d="M20 4 10 14"/><path d="M14 14v6H4V4h6"/></svg>',
  mail:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="5" width="18" height="14" rx="2"/><path d="m3 7 9 6 9-6"/></svg>',
  copy:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><rect x="9" y="9" width="11" height="11" rx="2"/><path d="M5 15V5a2 2 0 0 1 2-2h10"/></svg>',
  check:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="m5 12 5 5L20 7"/></svg>',
  x:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M6 6l12 12M6 18 18 6"/></svg>',
  inbox:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M3 12V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2v7"/><path d="M3 12h6l2 3h2l2-3h6"/><path d="M3 12v6a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-6"/></svg>',
  filter:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M3 4h18M6 12h12M10 20h4"/></svg>',
  sun:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.93 4.93l1.41 1.41M17.66 17.66l1.41 1.41M2 12h2M20 12h2M4.93 19.07l1.41-1.41M17.66 6.34l1.41-1.41"/></svg>',
  moon:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"/></svg>',
};
const ic=n=>ICONS[n]||"";

/* ── Utils ─────────────────────────────────────────────── */
function toast(msg, ms, kind){
  const t=$("toast"); t.textContent=msg;
  t.classList.add("on"); t.classList.toggle("err", kind==="err");
  clearTimeout(toast._t); toast._t=setTimeout(()=>t.classList.remove("on"), ms||2400);
}
function splitInput(s){return s.split(/[,\n]/).map(x=>x.trim()).filter(Boolean)}
function escapeHtml(s){return (s||"").replace(/[&<>"']/g,c=>(
  {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))}
function escapeAttr(s){return (s||"").replace(/"/g,"&quot;").replace(/</g,"&lt;")}
function linkify(s){
  return (s||"").replace(/(https?:\/\/[^\s<>"']+)/g,
    '<a href="$1" target="_blank" rel="noreferrer">$1</a>');
}
async function postJSON(url, body){
  const r=await fetch(url,{method:"POST",headers:{
      "Content-Type":"application/json",
      "X-Rastrillo-Token": _TOKEN,
    },
    body:JSON.stringify(body||{})});
  const data=await r.json().catch(()=>({}));
  if(!r.ok){
    // 412: el server pide confirmación de propiedad. Subimos `detail` como
    // objeto para que el caller decida si abre el modal pre-vuelo.
    if(r.status===412 && data && typeof data.detail==="object"){
      const err=new Error(data.detail.message||"Confirma propiedad");
      err.preflight=data.detail;
      throw err;
    }
    const msg = (typeof data.detail==="string") ? data.detail : (data.detail||r.statusText);
    throw new Error(typeof msg==="string"?msg:JSON.stringify(msg));
  }
  return data;
}

/* ── Filtros ───────────────────────────────────────────── */
function setFilter(name){
  CURRENT_FILTER=name;
  document.querySelectorAll(".filter").forEach(el=>{
    el.classList.toggle("on", el.dataset.f===name);
    el.setAttribute("aria-selected", el.dataset.f===name ? "true" : "false");
  });
  load();
}
function filterAccounts(acc){
  if(CURRENT_FILTER==="all") return acc.filter(a=>a.status!=="not_mine");
  if(CURRENT_FILTER==="triage") return acc.filter(a=>a.status==="found" && !a.owned);
  const set=new Set(GROUPS[CURRENT_FILTER]||[]);
  return acc.filter(a=>set.has(a.status));
}

/* ── Render: avatar / badge / row / actions ─────────── */
function avatarFor(a){
  const label=(a.display_name||a.platform||"?").trim();
  const letter=label.charAt(0).toUpperCase()||"?";
  return `<div class="avatar" aria-hidden="true">${escapeHtml(letter)}</div>`;
}
function badgeFor(a){
  const meta=META[a.status]||["?","#888"];
  const tone=TONE[a.status]||"";
  const attn=a.status==="awaiting_user"?" attn":"";
  return `<span class="badge ${tone}${attn}">${escapeHtml(meta[0])}</span>`;
}
function btnHtml(label, action, id, opts){
  opts=opts||{};
  const cls="btn btn-sm "+(opts.cls||"");
  const icon=opts.icon?ic(opts.icon):"";
  const safeLabel=String(label).replace(/'/g,"&#39;");
  return `<button class="${cls}" onclick="doAction(${id},'${action}','${safeLabel}')">${icon}${escapeHtml(label)}</button>`;
}
function actionsFor(a){
  const id=a.id;
  if(a.status==="awaiting_user"){
    return btnHtml("Continuar","continue",id,{cls:"btn-warn",icon:"check"});
  }
  if(a.status==="not_mine"){
    return `<button class="btn btn-sm btn-ghost" onclick="markMine(${id})">${ic("check")}Era mía</button>`;
  }
  if(a.status==="found"){
    // Triage: cuando aún no se confirmó propiedad, ofrecemos el camino rápido
    // (Es mía / No es mía) además de las acciones (que pasarán por modal
    // pre-vuelo si no se ha hecho ownership).
    if(!a.owned){
      return `<button class="btn btn-sm btn-accent" onclick="markMine(${id})">${ic("check")}Es mía</button>`
        + `<button class="btn btn-sm btn-danger" onclick="markNotMine(${id})">${ic("x")}No es mía</button>`
        + btnHtml("Eliminar","delete",id,{cls:"btn-primary"})
        + btnHtml("Conservar","keep",id,{cls:"btn-ghost"});
    }
    return btnHtml("Eliminar","delete",id,{cls:"btn-primary"})
         + btnHtml("Anonimizar","anonymize",id)
         + btnHtml("Conservar","keep",id,{cls:"btn-ghost"});
  }
  if(a.status==="semi_auto"){
    const link=a.profile_url
      ? `<a class="btn btn-sm btn-accent" href="${escapeAttr(a.profile_url)}" target="_blank" rel="noreferrer">${ic("external")}Abrir enlace</a>`
      : "";
    return link
      + `<button class="btn btn-sm" onclick="markSent(${a.id})">${ic("check")}Hecho</button>`
      + btnHtml("Reintentar","retry",id,{cls:"btn-ghost"});
  }
  if(a.status==="email_draft"){
    return `<button class="btn btn-sm btn-accent" onclick="openDraft(${a.id})">${ic("mail")}Ver borrador</button>`
      + `<button class="btn btn-sm" onclick="markSent(${a.id})">Enviado</button>`
      + btnHtml("Reintentar","retry",id,{cls:"btn-ghost"});
  }
  if(a.status==="failed"||a.status==="manual"){
    return btnHtml("Reintentar","retry",id)
         + btnHtml("Conservar","keep",id,{cls:"btn-ghost"});
  }
  if(a.status==="user_done"){
    // Si pasaron 30+ días desde el envío sin respuesta, ofrecemos un
    // borrador de seguimiento (GDPR Art. 12.3: respuesta exigible en 30 días).
    const days = a.sent_at ? Math.floor((Date.now()/1000 - a.sent_at)/86400) : 0;
    const followup = (a.sent_at && days >= 30)
      ? `<button class="btn btn-sm btn-warn" onclick="openFollowup(${a.id})">${ic("mail")}Seguimiento</button>`
      : "";
    return followup
      + btnHtml("Reintentar","retry",id,{cls:"btn-ghost"});
  }
  if(a.status==="deleted"||a.status==="anonymized"||a.status==="skipped"){
    return btnHtml("Reintentar","retry",id,{cls:"btn-ghost"});
  }
  // queued / in_progress: no interrumpimos al motor.
  return "";
}
function renderRow(a){
  const cls=a.status==="awaiting_user"?"account-row attn":"account-row";
  const id=a.identifier?escapeHtml(a.identifier):"";
  const site=a.source_site && a.source_site.toLowerCase()!==(a.display_name||"").toLowerCase()
    ? `<span class="dot">·</span>${escapeHtml(a.source_site)}` : "";
  const link=a.profile_url
    ? `<span class="dot">·</span><a href="${escapeAttr(a.profile_url)}" target="_blank" rel="noreferrer">perfil</a>`
    : "";
  // Seguimiento GDPR: si la cuenta tiene sent_at, calculamos días + estado
  let sent="";
  if(a.sent_at){
    const days=Math.floor((Date.now()/1000 - a.sent_at)/86400);
    const overdue = days >= 30;
    const tone = overdue ? "danger" : "info";
    const label = overdue
      ? `Vencida (${days}d)`
      : `Enviada hace ${days}d`;
    sent = `<span class="dot">·</span><span class="badge ${tone}" title="GDPR: respuesta exigible en 30 días">${label}</span>`;
  }
  const msg=SHOW_MSG.has(a.status) && a.last_message
    ? `<div class="row-msg">${linkify(escapeHtml(a.last_message))}</div>` : "";
  // Indicador de confianza: solo lo mostramos cuando NO está confirmada como
  // propia, porque es ahí donde sirve (filtrar falsos positivos).
  const conf = a.confidence && !a.owned
    ? `<span class="badge ${CONF_TONE[a.confidence]||""}" title="${escapeAttr(CONF_LABEL[a.confidence]||"")} · source=${escapeAttr(a.source||"")}">${escapeHtml(a.confidence)}</span>`
    : "";
  // Tilde verde: la cuenta se confirmó como tuya.
  const ownedMark = a.owned
    ? `<span class="owned-tick" title="Confirmada como tuya">${ic("check")}</span>`
    : "";
  return `<div class="${cls}">
    ${avatarFor(a)}
    <div class="row-main">
      <div class="row-title">${escapeHtml(a.display_name||a.platform)}${ownedMark}</div>
      <div class="row-meta">${id}${site}${link}${sent}</div>
    </div>
    ${conf}
    ${badgeFor(a)}
    <div class="row-actions">${actionsFor(a)}</div>
    ${msg}
  </div>`;
}

/* ── Triage rápido ─────────────────────────────────── */
async function markMine(id){
  try{
    await postJSON(`/api/accounts/${id}/own`,{owned:true});
    toast("Confirmada como tuya");
  } catch(e){ toast(e.message, 7000, "err"); }
  load();
}
async function markNotMine(id){
  try{
    await postJSON(`/api/accounts/${id}/own`,{owned:false});
    toast("Descartada");
  } catch(e){ toast(e.message, 7000, "err"); }
  load();
}
async function discardLowConfidence(){
  showConfirm({
    title:"Descartar todo low-confidence",
    body:"Vas a marcar como 'No es mía' todas las cuentas en estado 'Pendiente' "
        +"con confianza baja. Útil para limpiar el ruido típico de Sherlock "
        +"con usernames cortos. Puedes recuperar cada una individualmente desde "
        +"el filtro 'Descartadas'.",
    danger:false, confirmLabel:"Descartar low",
    onYes: async ()=>{
      try{
        const r=await postJSON("/api/accounts/discard-low",{});
        toast(`${r.discarded} cuenta(s) descartadas`);
      } catch(e){ toast(e.message, 7000, "err"); }
      load();
    },
  });
}

function askProcessAllAuto(){
  showConfirm({
    title:"Procesar todo lo automatizable",
    body:"Vas a encolar TODAS las cuentas confirmadas como tuyas que tengan "
        +"receta o resolver kind=auto. Las que necesiten tu input (semi_auto, "
        +"email_draft) se prepararán para que las gestiones desde 'Tu turno'.\n\n"
        +"Si Simulación está activada, ninguna acción destructiva ocurrirá: solo "
        +"verás qué habría pasado.",
    danger:false, confirmLabel:"Procesar",
    onYes: async ()=>{
      try{
        const r=await postJSON("/api/accounts/process-all-auto",{});
        const parts = [];
        if(r.queued)      parts.push(`${r.queued} encoladas`);
        if(r.semi_auto)   parts.push(`${r.semi_auto} a "Tu turno" (1 clic)`);
        if(r.email_draft) parts.push(`${r.email_draft} a "Tu turno" (correo)`);
        if(!parts.length) parts.push("nada nuevo");
        const dr = r.dry_run ? " · [simulación]" : "";
        toast(`Procesado: ${parts.join(", ")}${dr}`, 4500);
      } catch(e){ toast(e.message, 7000, "err"); }
      load();
    },
  });
}

/* ── Stats ───────────────────────────────────────────── */
function computeStats(stats, accounts){
  const sum=k=>k.reduce((n,x)=>n+(stats[x]||0),0);
  const acc = accounts || [];
  const triage = acc.filter(a=>a.status==="found" && !a.owned).length;
  const owned_found = acc.filter(a=>a.status==="found" && a.owned).length;
  return {
    total:     sum(Object.keys(stats)),
    triage:    triage,
    pending:   sum(GROUPS.pending) + owned_found,
    your_turn: sum(GROUPS.your_turn),
    action:    sum(GROUPS.action),
    done:      sum(GROUPS.done),
    kept:      sum(GROUPS.kept),
    discarded: sum(GROUPS.discarded),
    /* Candidatas a "Procesar todo automatizable": owned=1 y status=found. */
    automatable: owned_found,
  };
}
function renderStats(s){
  return `
    <div class="stat">
      <div class="stat-l">Total detectadas</div>
      <div class="stat-n">${s.total}</div>
      <div class="stat-sub">en la sesión actual</div>
    </div>
    <div class="stat ${s.triage>0?"warn":""}">
      <div class="stat-l">Por triar</div>
      <div class="stat-n">${s.triage}</div>
      <div class="stat-sub">${s.triage===0?"todo confirmado":"confirma propiedad"}</div>
    </div>
    <div class="stat ${s.action>0?"accent":""}">
      <div class="stat-l">Acción requerida</div>
      <div class="stat-n">${s.action}</div>
      <div class="stat-sub">${s.action===0?"todo controlado":"necesitan tu input"}</div>
    </div>
    <div class="stat ${s.done>0?"success":""}">
      <div class="stat-l">Completadas</div>
      <div class="stat-n">${s.done}</div>
      <div class="stat-sub">eliminadas o tramitadas</div>
    </div>
    <div class="stat">
      <div class="stat-l">Conservadas</div>
      <div class="stat-n">${s.kept}</div>
      <div class="stat-sub">que no quieres borrar</div>
    </div>
  `;
}

/* ── Empty & skeleton ──────────────────────────────── */
function skeletonRows(n){
  let html="";
  for(let i=0;i<n;i++){
    html+=`<div class="skeleton-row">
      <div class="sk avatar"></div>
      <div><div class="sk bar"></div><div class="sk bar s"></div></div>
      <div class="sk pill"></div>
    </div>`;
  }
  return html;
}
function emptyState(){
  return `<div class="empty">
    <div class="empty-icon">${ic("inbox")}</div>
    <div class="empty-title">Aún no hay cuentas</div>
    <div class="empty-body">Introduce un username o un correo arriba y pulsa
      <b>Escanear</b>. Rastrillo descubrirá tus cuentas y propondrá una acción
      para cada una.</div>
  </div>`;
}
function emptyFilter(){
  return `<div class="empty">
    <div class="empty-icon">${ic("filter")}</div>
    <div class="empty-title">Nada en este filtro</div>
    <div class="empty-body">Cambia el filtro o pulsa <b>Todas</b> para ver el resto.</div>
  </div>`;
}

/* ── Acciones de UI ────────────────────────────────── */
async function doAction(id, action, label){
  await doActionImpl(id, action, label, false);
}
async function doActionImpl(id, action, label, confirmOwned){
  try{
    await postJSON(`/api/accounts/${id}/action`,{action, confirm_owned: confirmOwned});
    toast(label||"OK");
  } catch(e){
    if(e.preflight){
      askOwnership(e.preflight.account, action, label);
      return;
    }
    toast(e.message, 7000, "err");
  }
  load();
}

/* Modal pre-vuelo: el server respondió 412 pidiendo confirmación de propiedad.
 * Le mostramos al usuario la cuenta concreta y le damos 3 caminos:
 *   - Sí, es mía → reenvía la acción con confirm_owned=true
 *   - No es mía  → marca not_mine y cancela la acción
 *   - Cancelar   → no hace nada */
function askOwnership(account, action, label){
  const confLabel = CONF_LABEL[account.confidence] || "Confianza desconocida";
  const confTone  = CONF_TONE[account.confidence] || "warn";
  const idLine = account.identifier ? `<div class="kv"><b>Identificador:</b>${escapeHtml(account.identifier)}</div>`:"";
  const urlLine = account.profile_url ? `<div class="kv"><b>URL:</b><a href="${escapeAttr(account.profile_url)}" target="_blank" rel="noreferrer">${escapeHtml(account.profile_url)}</a></div>`:"";
  const m=$("modal");
  m.innerHTML=`<div class="modal" role="document" style="max-width:520px">
    <div class="modal-head">
      <h3 id="modal-title">¿Esta cuenta es tuya?</h3>
      <button class="btn btn-sm btn-icon btn-ghost modal-close"
              onclick="closeModal()" aria-label="Cerrar">${ic("x")}</button>
    </div>
    <div class="modal-body">
      <div style="font-size:12.5px;color:var(--text-2);margin-bottom:12px">
        Antes de proceder con <b>${escapeHtml(label||action)}</b> necesitamos
        que confirmes que esta cuenta es tuya. Sherlock genera falsos positivos
        para usernames cortos o muy comunes.
      </div>
      <div class="kv"><b>Plataforma:</b>${escapeHtml(account.display_name||account.platform||"")}</div>
      ${idLine}
      ${urlLine}
      <div class="kv" style="margin-top:8px">
        <span class="badge ${confTone}">${escapeHtml(confLabel)}</span>
        <span class="badge" style="margin-left:6px">source: ${escapeHtml(account.source||"")}</span>
      </div>
    </div>
    <div class="modal-foot">
      <button class="btn" onclick="closeModal()">Cancelar</button>
      <button class="btn btn-danger" onclick="closeModal(); discardFromPreflight(${account.id})">
        No es mía
      </button>
      <button class="btn btn-primary"
              onclick="closeModal(); doActionImpl(${account.id},'${escapeAttr(action)}','${escapeAttr(label||action)}', true)">
        Sí, es mía y procede
      </button>
    </div>
  </div>`;
  m.classList.add("on");
  m.onclick=(e)=>{ if(e.target===m) closeModal(); };
}

async function discardFromPreflight(id){
  try{
    await postJSON(`/api/accounts/${id}/own`,{owned:false});
    toast("Descartada");
  } catch(e){ toast(e.message, 7000, "err"); }
  load();
}
async function markSent(id){
  try{
    await postJSON(`/api/accounts/${id}/mark-sent`,{});
    toast("Marcada como hecha");
  } catch(e){ toast(e.message, 7000, "err"); }
  load();
}
async function openDraft(id){
  let meta;
  try{
    const r=await fetch(`/api/accounts/${id}/resolution`);
    meta=(await r.json()).meta;
  } catch(e){ toast("No pude cargar el borrador", 5000, "err"); return; }
  if(!meta || !meta.email_to){ toast("No hay borrador para esta cuenta", 5000, "err"); return; }
  const mailto="mailto:"+encodeURIComponent(meta.email_to)
    + "?subject="+encodeURIComponent(meta.email_subject||"")
    + "&body="   +encodeURIComponent(meta.email_body||"");
  showDraftModal(meta, mailto);
}

async function openFollowup(id){
  let data;
  try{
    const r = await fetch(`/api/accounts/${id}/followup-draft`);
    if(!r.ok){
      const d=await r.json().catch(()=>({}));
      toast(d.detail || "No pude generar el seguimiento", 6000, "err");
      return;
    }
    data = await r.json();
  } catch(e){ toast("Error generando seguimiento", 5000, "err"); return; }
  // Reusamos el mismo modal. data ya viene con email_to/subject/body listos.
  const mailto="mailto:"+encodeURIComponent(data.email_to)
    + "?subject="+encodeURIComponent(data.email_subject||"")
    + "&body="   +encodeURIComponent(data.email_body||"");
  showDraftModal({
    title: `Seguimiento GDPR · ${data.host} (${data.days_since_sent}d sin respuesta)`,
    email_to: data.email_to,
    email_subject: data.email_subject,
    email_body: data.email_body,
    notes: "Envío de seguimiento basado en la solicitud original. "
         + "GDPR Art. 12.3: respuesta exigible en un mes.",
  }, mailto);
}
function showDraftModal(meta, mailto){
  const m=$("modal");
  m.innerHTML=`<div class="modal" role="document">
    <div class="modal-head">
      <h3 id="modal-title">${escapeHtml(meta.title||"Solicitud de baja")}</h3>
      <button class="btn btn-sm btn-icon btn-ghost modal-close"
              onclick="closeModal()" aria-label="Cerrar">${ic("x")}</button>
    </div>
    <div class="modal-body">
      <div class="kv"><b>Para:</b>${escapeHtml(meta.email_to||"")}</div>
      <div class="kv"><b>Asunto:</b>${escapeHtml(meta.email_subject||"")}</div>
      <textarea id="draft-body" rows="14">${escapeHtml(meta.email_body||"")}</textarea>
      <div class="hint">${escapeHtml(meta.notes||"")}</div>
    </div>
    <div class="modal-foot">
      <button class="btn" onclick="copyDraft()">${ic("copy")}Copiar todo</button>
      <a class="btn btn-accent" href="${escapeAttr(mailto)}" target="_blank">${ic("mail")}Abrir en correo</a>
    </div>
  </div>`;
  m.classList.add("on");
  m.onclick=(e)=>{ if(e.target===m) closeModal(); };   // cerrar al clicar el backdrop
}
function copyDraft(){
  const txt=$("draft-body").value;
  navigator.clipboard.writeText(txt).then(
    ()=>toast("Borrador copiado"),
    ()=>toast("No pude copiar", 3000, "err"));
}
function closeModal(){ $("modal").classList.remove("on"); }

/* Confirmación reutilizable: abre el mismo modal con un cuerpo conciso y
 * dos botones (cancelar / acción peligrosa). onYes recibe ninguna arg. */
let _confirmYes=null;
function showConfirm({title, body, danger, confirmLabel, onYes}){
  _confirmYes = onYes;
  const m=$("modal");
  m.innerHTML=`<div class="modal" role="document" style="max-width:480px">
    <div class="modal-head">
      <h3 id="modal-title">${escapeHtml(title)}</h3>
      <button class="btn btn-sm btn-icon btn-ghost modal-close"
              onclick="closeModal()" aria-label="Cerrar">${ic("x")}</button>
    </div>
    <div class="modal-body">
      <div style="font-size:13px;color:var(--text-2);line-height:1.55">${escapeHtml(body)}</div>
    </div>
    <div class="modal-foot">
      <button class="btn" onclick="closeModal()">Cancelar</button>
      <button class="btn ${danger?'btn-danger':'btn-primary'}" onclick="confirmYes()">${escapeHtml(confirmLabel||"Confirmar")}</button>
    </div>
  </div>`;
  m.classList.add("on");
  m.onclick=(e)=>{ if(e.target===m) closeModal(); };
}
function confirmYes(){
  const cb=_confirmYes; _confirmYes=null;
  closeModal();
  if(cb) cb();
}

function askClear(){
  showConfirm({
    title:"Limpiar todas las cuentas",
    body:"Vas a borrar TODAS las cuentas detectadas de esta sesión, junto con su "
        +"historial. El directorio cacheado, los hallazgos del resolver y tu "
        +"perfil de Chromium no se tocan.\n\nDespués podrás escanear de nuevo "
        +"sin acumulado previo.",
    danger:true, confirmLabel:"Borrar todo",
    onYes: async ()=>{
      try{
        await postJSON("/api/accounts/clear",{});
        toast("Sesión limpia");
      } catch(e){ toast(e.message, 7000, "err"); }
      load();
    },
  });
}

/* ── Loop principal ────────────────────────────────── */
async function load(){
  try{
    const [accRes, scanRes, dirRes] = await Promise.all([
      fetch("/api/accounts").then(r=>r.json()),
      fetch("/api/scan/status").then(r=>r.json()).catch(()=>({})),
      fetch("/api/directory").then(r=>r.json()).catch(()=>null),
    ]);
    const stats=computeStats(accRes.stats||{}, accRes.accounts||[]);
    // Stats
    $("stats").innerHTML=renderStats(stats);

    // Filtros: contadores
    $("f-all").textContent=`Todas · ${stats.total - stats.discarded}`;
    $("f-triage").textContent=`Triage · ${stats.triage}`;
    $("f-pending").textContent=`Pendientes · ${stats.pending}`;
    $("f-your_turn").textContent=`Tu turno · ${stats.your_turn}`;
    $("f-action").textContent=`Acción · ${stats.action}`;
    $("f-done").textContent=`Completadas · ${stats.done}`;
    $("f-kept").textContent=`Conservadas · ${stats.kept}`;
    $("f-discarded").textContent=`Descartadas · ${stats.discarded}`;
    // El botón "Limpiar" solo tiene sentido si hay algo que limpiar.
    $("clear-btn").disabled = stats.total === 0;
    // El bulk "descartar low" aparece SOLO en triage.
    $("bulk-low-btn").style.display = (CURRENT_FILTER==="triage" && stats.triage>0) ? "" : "none";
    // Botón "Procesar automáticas" muestra el contador y se deshabilita en cero.
    $("process-all-label").textContent = `Procesar automáticas · ${stats.automatable}`;
    $("process-all-btn").disabled = stats.automatable === 0;

    // Lista
    const filtered=filterAccounts(accRes.accounts||[]);
    const list=$("list");
    if(!accRes.accounts || !accRes.accounts.length){
      list.innerHTML=emptyState();
    } else if(!filtered.length){
      list.innerHTML=emptyFilter();
    } else {
      list.innerHTML=filtered.map(renderRow).join("");
    }

    // Scan status: mostramos la fase (Descubriendo / Resolviendo N/total) en
    // curso, y al terminar el resumen "Último: N detectadas, M resueltas".
    const scanEl=$("scan-status");
    if(scanRes && scanRes.running){
      let label;
      const ph = scanRes.phase;
      if(ph === "resolving"){
        const tot = scanRes.total||0, done = scanRes.resolved||0;
        label = tot>0 ? `Resolviendo ${done}/${tot}…` : "Resolviendo…";
      } else {
        // discovery o phase desconocida
        label = "Descubriendo…";
      }
      scanEl.innerHTML=
        '<span class="chip-dot warn pulse" style="display:inline-block;margin-right:6px"></span>'
        + escapeHtml(label);
      scanEl.classList.add("busy");
    } else {
      scanEl.classList.remove("busy");
      if(scanRes && scanRes.last && !scanRes.last.error){
        const e=scanRes.last.errors||[];
        const resolved = (scanRes.resolved!=null) ? scanRes.resolved : null;
        scanEl.textContent=
          `Último escaneo: ${scanRes.last.found||0} detectadas`
          + (resolved!=null ? `, ${resolved} resueltas`:"")
          + `, ${scanRes.last.kept||0} conservadas`
          + (e.length?`, ${e.length} errores`:"");
      } else if(scanRes && scanRes.last && scanRes.last.error){
        scanEl.textContent="Último escaneo falló: "+scanRes.last.error;
      } else {
        scanEl.textContent="";
      }
    }

    // Chips: directorio + IA
    if(dirRes){
      const dotCls=dirRes.source==="upstream"?"on":(dirRes.source==="cache"?"warn":"off");
      const src=dirRes.source==="upstream"?"online":(dirRes.source==="cache"?"caché":"fallback");
      $("chip-dir").innerHTML=
        `<span class="chip-dot ${dotCls}"></span>Directorio · ${dirRes.entries} · ${src}`;
      $("chip-ai").innerHTML=
        `${ic("sparkles")}<span class="chip-dot ${dirRes.ai_enabled?"on":""}"></span>IA ${dirRes.ai_enabled?"activa":"desactivada"}`;
    }
    // Dry-run state
    try{
      const dr=await fetch("/api/dry-run").then(r=>r.json());
      const btn=$("dry-toggle");
      btn.classList.toggle("on", !!dr.enabled);
      btn.setAttribute("aria-pressed", dr.enabled ? "true" : "false");
      btn.querySelector(".dry-label").textContent =
        dr.enabled ? "Simulación: ON" : "Simulación";
    } catch(_) {}
  } catch(e){
    console.error("load() falló:", e);
  }
}

/* Toggle del modo simulación */
$("dry-toggle").onclick = async () => {
  const cur = $("dry-toggle").classList.contains("on");
  try{
    const r = await postJSON("/api/dry-run", {enabled: !cur});
    toast(r.enabled ? "Simulación activada (nada se ejecuta de verdad)" : "Simulación desactivada");
  } catch(e){ toast(e.message, 7000, "err"); }
  load();
};

/* ── Handlers ──────────────────────────────────────── */
$("scan-btn").onclick=async()=>{
  const usernames=splitInput($("users").value);
  const emails=splitInput($("mails").value);
  if(!usernames.length && !emails.length){
    toast("Pon al menos un username o un correo", 3000); return;
  }
  $("scan-btn").disabled=true;
  try{
    await postJSON("/api/scan",{usernames,emails});
    toast("Escaneo lanzado");
  } catch(e){ toast(e.message, 7000, "err"); }
  finally{ $("scan-btn").disabled=false; }
};

$("dir-refresh").innerHTML=ic("refresh");
$("dir-refresh").onclick=async()=>{
  const b=$("dir-refresh"); b.disabled=true;
  try{
    const r=await postJSON("/api/directory/refresh",{});
    toast(`Directorio: ${r.entries} sitios actualizados`);
  } catch(e){ toast(e.message, 7000, "err"); }
  finally{ b.disabled=false; load(); }
};

/* ── Toggle de tema ───────────────────────────────────────── */
function applyTheme(theme, save){
  document.documentElement.dataset.theme = theme;
  if (save) {
    try { localStorage.setItem("rastrillo-theme", theme); } catch(e) {}
  }
  // El botón muestra el icono del tema *contrario* (lo que cambias al pulsar).
  const btn = $("theme-toggle");
  btn.innerHTML = theme === "dark" ? ic("sun") : ic("moon");
  btn.setAttribute("aria-label",
    theme === "dark" ? "Cambiar a modo claro" : "Cambiar a modo oscuro");
}
applyTheme(document.documentElement.dataset.theme || "light", false);

$("theme-toggle").onclick = () => {
  const cur = document.documentElement.dataset.theme === "dark" ? "dark" : "light";
  applyTheme(cur === "dark" ? "light" : "dark", true);
};

/* Si el SO cambia su preferencia Y el usuario no ha tocado el toggle, seguimos. */
const _mql = window.matchMedia("(prefers-color-scheme: dark)");
if (_mql.addEventListener) {
  _mql.addEventListener("change", (e) => {
    let saved=null;
    try { saved = localStorage.getItem("rastrillo-theme"); } catch(_) {}
    if (!saved) applyTheme(e.matches ? "dark" : "light", false);
  });
}

/* Teclado: Ctrl/⌘+Enter dispara scan; Esc cierra modal */
document.addEventListener("keydown",(e)=>{
  if((e.metaKey||e.ctrlKey) && e.key==="Enter"){
    e.preventDefault(); $("scan-btn").click();
  } else if(e.key==="Escape" && $("modal").classList.contains("on")){
    closeModal();
  }
});

/* ── Onboarding + token prompt ────────────────────────────────── */
function showTokenPrompt(){
  /* Sin token = la web acaba de cargarse sin ?token=. Mostramos un panel
   * explicativo con un input para pegarlo manualmente. */
  const m=$("modal");
  m.innerHTML=`<div class="modal" role="document" style="max-width:520px">
    <div class="modal-head">
      <h3 id="modal-title">Necesitas el token de auth</h3>
    </div>
    <div class="modal-body">
      <div style="font-size:13px;color:var(--text-2);line-height:1.55">
        Rastrillo arrancó pero esta pestaña no sabe el token de auth del proceso.
        Por seguridad, todas las acciones POST exigen ese token (te protege de
        que otro proceso local te dispare un borrado).
      </div>
      <div style="font-size:13px;color:var(--text-2);line-height:1.55;margin-top:10px">
        <b>Forma rápida:</b> en la consola donde arrancaste Rastrillo verás una
        URL del tipo <code>http://127.0.0.1:8765/?token=...</code>. Cierra esta
        pestaña y abre esa URL.
      </div>
      <div style="font-size:13px;color:var(--text-2);line-height:1.55;margin-top:10px">
        <b>O pega el token aquí:</b>
      </div>
      <input id="token-input" class="input" type="text" autocomplete="off"
             placeholder="pega el token (la parte después de ?token=)"
             style="margin-top:8px;font-family:var(--font-mono);font-size:12.5px" />
      <div class="hint">Si reiniciaste el servidor, el token cambió. Vuelve a abrir desde la URL nueva.</div>
    </div>
    <div class="modal-foot">
      <button class="btn btn-accent" onclick="connectWithToken()">Conectar</button>
    </div>
  </div>`;
  m.classList.add("on");
  m.onclick=null;   /* este modal NO se cierra clicando fuera */
  setTimeout(()=>{ const i=$("token-input"); if(i) i.focus(); }, 50);
}

function connectWithToken(){
  const v=($("token-input").value||"").trim();
  if(!v){ toast("Pega un token primero", 3000, "err"); return; }
  try{ sessionStorage.setItem("rastrillo-token", v); }catch(_) {}
  _TOKEN=v;
  closeModal();
  bootstrap();   /* re-arranca init con el token nuevo */
}

function showWelcomePanel(){
  const m=$("modal");
  m.innerHTML=`<div class="modal" role="document" style="max-width:560px">
    <div class="modal-head">
      <h3 id="modal-title">Bienvenida a Rastrillo</h3>
    </div>
    <div class="modal-body">
      <div style="font-size:13px;color:var(--text-2);line-height:1.55">
        Antes de empezar, cuatro cosas que te van a pasar:
      </div>
      <ul style="font-size:13px;color:var(--text-2);line-height:1.6;
                 padding-left:18px;margin-top:10px">
        <li>Verás <b>dos ventanas</b>: este panel y, cuando proceses una cuenta,
            un Chromium que conduce el flujo de borrado.</li>
        <li>Te <b>logueas tú una vez por sitio</b> en ese Chromium. Rastrillo
            <b>nunca</b> guarda contraseñas; viven solo en el perfil de
            navegador local.</li>
        <li>Si una plataforma exige <b>CAPTCHA, 2FA o confirmación final</b>,
            el panel se queda esperando a que tú lo resuelvas en el Chromium
            y pulses "Continuar".</li>
        <li>Esto es <b>solo para tus propias cuentas</b>. Antes de cualquier
            acción destructiva te pedimos confirmar que la cuenta es tuya.</li>
      </ul>
      <div class="hint" style="margin-top:12px">
        Puedes activar el modo <b>Simulación</b> (chip en la barra superior)
        para ver qué haría Rastrillo sin tocar nada.
      </div>
    </div>
    <div class="modal-foot">
      <button class="btn btn-accent" onclick="dismissWelcome()">Entendido</button>
    </div>
  </div>`;
  m.classList.add("on");
  m.onclick=null;
}

async function dismissWelcome(){
  try{ await postJSON("/api/onboarding/dismiss", {}); }
  catch(e){ /* sin token todavía? igual cerramos */ }
  closeModal();
}

/* ── Bootstrap: token -> onboarding -> polling ───────────────── */
let _bootstrapped=false;
async function bootstrap(){
  if(!_TOKEN){ showTokenPrompt(); return; }
  /* Comprobamos onboarding (GET libre, sin token). */
  if(!_bootstrapped){
    try{
      const ob=await fetch("/api/onboarding").then(r=>r.json());
      if(!ob.onboarded) showWelcomePanel();
    }catch(_){}
    _bootstrapped=true;
    /* Skeleton inicial + arranque del polling (solo la primera vez). */
    $("list").innerHTML=skeletonRows(3);
    load();
    setInterval(load, 2000);
  } else {
    /* Si re-bootstrapeamos por entrada manual de token, basta un load. */
    load();
  }
}
bootstrap();
</script>
</body></html>"""


def _meta_json() -> str:
    return json.dumps(STATUS_META)


@app.get("/", response_class=HTMLResponse)
def index():
    return _INDEX_HTML.replace("__META__", _meta_json())


# --- Arranque desde "python -m rastrillo.server" ----------------------------
if __name__ == "__main__":
    import uvicorn
    db.init()
    jobs.start_workers()
    print("Dashboard en http://127.0.0.1:8765")
    uvicorn.run(app, host="127.0.0.1", port=8765, log_level="warning")
