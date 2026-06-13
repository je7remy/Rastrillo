"""Generación de informes en JSON / CSV / PDF.

Antes la lógica vivía dentro del endpoint `/api/report` en `server.py`;
Tarea 11 la sacó aquí para que tanto el endpoint como el CLI (`rastrillo
report --format ... --out FILE`) compartan el mismo código y produzcan
informes idénticos.

`build_report(fmt)` devuelve:
  - (bytes,      "text/csv; charset=utf-8",            "rastrillo-<ts>.csv")
  - (bytes,      "application/pdf",                    "rastrillo-<ts>.pdf")
  - (dict,       "application/json",                   "rastrillo-<ts>.json")

El caller decide qué hacer con el contenido (devolverlo por HTTP o
escribirlo a un fichero).
"""
from __future__ import annotations
import csv as _csv
import io
import json
import time as _t
from typing import Any, Dict, List, Tuple, Union

from . import audit, db


def _enrich_rows() -> Tuple[List[Dict[str, Any]], float]:
    """Carga todas las cuentas y las anota con días desde el envío de la
    solicitud GDPR (si hay), la capa del resolver y el destinatario/asunto
    del correo (sin el cuerpo, que puede llevar PII)."""
    rows = [dict(r) for r in db.list_accounts()]
    now = _t.time()
    enriched: List[Dict[str, Any]] = []
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
    return enriched, now


def _summary(enriched: List[Dict[str, Any]]) -> Tuple[Dict[str, Any], Dict[str, int]]:
    """Resumen agregado compartido por JSON y PDF."""
    stats = db.stats()
    audit_entries = audit.read_all()
    by_action: Dict[str, int] = {}
    for a in audit_entries:
        by_action[a["action"]] = by_action.get(a["action"], 0) + 1
    return {
        "total":         len(enriched),
        "by_status":     stats,
        "audit_actions": by_action,
        "audit_total":   len(audit_entries),
    }, by_action


_CSV_COLS = [
    "id", "platform", "display_name", "source", "source_site",
    "identifier", "profile_url", "status", "confidence", "owned",
    "deletion_type", "difficulty", "resolver_layer", "resolver_kind",
    "email_to", "email_subject",
    "updated_at", "sent_at", "days_since_sent", "last_message",
]


def build_report(fmt: str) -> Tuple[Union[bytes, dict], str, str]:
    """Construye el informe en el formato pedido.

    Devuelve `(contenido, media_type, suggested_filename)`. El contenido
    es `bytes` para csv/pdf y `dict` para json (más fácil de manipular
    desde el caller; quien lo necesite serializado puede `json.dumps`).
    """
    fmt = (fmt or "json").lower()
    if fmt not in ("json", "csv", "pdf"):
        raise ValueError(f"formato no soportado: {fmt!r}")

    enriched, now = _enrich_rows()
    ts = _t.strftime("%Y%m%d-%H%M%S", _t.gmtime(now))

    if fmt == "csv":
        out = io.StringIO()
        w = _csv.DictWriter(out, fieldnames=_CSV_COLS, extrasaction="ignore")
        w.writeheader()
        for r in enriched:
            w.writerow(r)
        return (
            out.getvalue().encode("utf-8"),
            "text/csv; charset=utf-8",
            f"rastrillo-{ts}.csv",
        )

    summary, by_action = _summary(enriched)

    if fmt == "pdf":
        from . import report_pdf
        data = report_pdf.render_pdf(
            accounts=enriched,
            summary=summary,
            audit_summary=by_action,
            generated_at=now,
        )
        return data, "application/pdf", f"rastrillo-{ts}.pdf"

    # JSON
    payload = {
        "generated_at": now,
        "generated_at_iso": _t.strftime("%Y-%m-%dT%H:%M:%SZ", _t.gmtime(now)),
        "summary": summary,
        "accounts": enriched,
    }
    return payload, "application/json", f"rastrillo-{ts}.json"
