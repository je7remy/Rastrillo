"""Audit log: registro inmutable de acciones destructivas o cuasi-destructivas.

Antes de borrar/anonimizar/enviar una solicitud GDPR escribimos una entrada en
~/.rastrillo/audit.json con un snapshot de la cuenta. Es nuestra red de
seguridad: si algo se borra que no debías, sigues teniendo el rastro de qué
cuenta, en qué plataforma, con qué identificador y cuándo.

Append-only por convención. Sin rotación (asume volumen bajo: pocos miles de
acciones a lo largo de la vida del usuario). Si el archivo crece demasiado, se
puede archivar manualmente.

API:
  record(action, account, dry_run=False, extra=None)
  read_all() -> List[dict]
"""
from __future__ import annotations
import json
import logging
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import config

log = logging.getLogger("rastrillo.audit")

AUDIT_PATH: Path = config.BASE_DIR / "audit.json"
_lock = threading.Lock()


def _account_snapshot(account) -> Dict[str, Any]:
    """Recorta a los campos relevantes para auditoría (no incluimos events,
    action_meta crudo ni timestamps internos)."""
    if not account:
        return {}
    # `account` puede ser sqlite3.Row o dict
    g = account.get if isinstance(account, dict) else account.__getitem__
    keys_needed = (
        "id", "platform", "display_name", "source", "source_site",
        "identifier", "profile_url", "status", "confidence", "owned",
    )
    out = {}
    for k in keys_needed:
        try:
            out[k] = g(k)
        except (KeyError, IndexError):
            out[k] = None
    return out


def record(action: str,
           account,
           dry_run: bool = False,
           extra: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Registra una acción y devuelve la entrada escrita (para tests/logging).

    `action` típicos: 'delete', 'anonymize', 'email_sent', 'mark_sent',
    'discard' (triage), 'own' (triage).
    """
    entry: Dict[str, Any] = {
        "ts": time.time(),
        "ts_iso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "action": action,
        "dry_run": bool(dry_run),
        "account": _account_snapshot(account),
    }
    if extra:
        entry["extra"] = extra
    with _lock:
        config.ensure_dirs()
        data: List[dict]
        if AUDIT_PATH.exists():
            try:
                data = json.loads(AUDIT_PATH.read_text(encoding="utf-8"))
                if not isinstance(data, list):
                    log.warning("audit.json no era lista; reseteando archivo")
                    data = []
            except Exception:
                log.exception("audit.json corrupto; reescribiendo")
                data = []
        else:
            data = []
        data.append(entry)
        AUDIT_PATH.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    log.info("audit: %s acc=%s dry_run=%s", action,
             entry["account"].get("id"), dry_run)
    return entry


def read_all() -> List[dict]:
    if not AUDIT_PATH.exists():
        return []
    try:
        data = json.loads(AUDIT_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except Exception:
        log.exception("audit.json no se pudo leer")
        return []
