"""Cliente mínimo de HaveIBeenPwned (HIBP) — fuente extra de discovery.

Por cada email del usuario consultamos `/api/v3/breachedaccount/{email}` y
convertimos cada brecha confirmada en un hit (source='hibp'). El identificador
es un email, así que el match es de alta confianza.

Es OPT-IN: requiere `RASTRILLO_HIBP_API_KEY`. Sin clave, el módulo devuelve
`skipped=True` y discovery la omite silenciosamente — el programa sigue
funcionando con sherlock + holehe.

Privacidad: la única información que sale a HIBP es el email que el usuario
ya nos dio para escanearlo. No mandamos nada más. Respuestas se procesan
localmente; no se cachean en disco.
"""
from __future__ import annotations
import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List

from . import config

log = logging.getLogger("rastrillo.hibp")

_HIBP_URL = "https://haveibeenpwned.com/api/v3/breachedaccount/{email}?truncateResponse=false"
_USER_AGENT = "rastrillo-personal-deletion-tool"


def available() -> bool:
    return bool(config.HIBP_API_KEY)


def run_hibp(email: str, timeout: float = 30.0) -> Dict[str, Any]:
    """Devuelve `{hits, error, incomplete, raw_count, skipped}` con la misma
    forma que `discovery.run_sherlock/run_holehe`.

    - 404 de HIBP significa "el email no aparece en ninguna brecha conocida".
      Eso NO es un error: es un resultado válido (raw_count=0).
    - 429 (rate limit) → error visible para que el caller lo reporte.
    - 401 / 403 → API key inválida; lo reportamos pero no abortamos discovery.
    """
    if not available():
        return {"hits": [], "error": None, "incomplete": False,
                "raw_count": 0, "skipped": True}

    if not email or "@" not in email:
        return {"hits": [], "error": f"email inválido: {email!r}",
                "incomplete": False, "raw_count": 0, "skipped": False}

    url = _HIBP_URL.format(email=urllib.parse.quote(email, safe=""))
    headers = {
        "hibp-api-key": config.HIBP_API_KEY,
        "User-Agent": _USER_AGENT,
        "Accept": "application/json",
    }
    breaches: List[dict] = []
    error = None
    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read()
            data = json.loads(body)
            if isinstance(data, list):
                breaches = data
    except urllib.error.HTTPError as e:
        if e.code == 404:
            # No breaches conocidas para este email.
            log.info("hibp(%s) → 404 (sin brechas conocidas)", email)
            return {"hits": [], "error": None, "incomplete": False,
                    "raw_count": 0, "skipped": False}
        # Otros HTTPs: reportamos pero no abortamos.
        error = f"hibp HTTP {e.code}"
        if e.code == 429:
            error += " (rate-limit)"
        elif e.code in (401, 403):
            error += " (clave inválida / sin permisos)"
        log.warning("hibp(%s) %s", email, error)
    except Exception as e:
        error = f"hibp: {type(e).__name__}: {e}"
        log.warning("hibp(%s) %s", email, error)

    hits = []
    for b in breaches:
        domain = (b.get("Domain") or "").strip().lower()
        title = (b.get("Title") or b.get("Name") or "").strip()
        if not domain:
            continue
        hits.append({
            "name": title or domain,
            "url": None,
            "source_site": domain,
            "extra": {
                "breach_date": b.get("BreachDate"),
                "pwn_count": b.get("PwnCount"),
                "data_classes": b.get("DataClasses") or [],
            },
        })
    return {"hits": hits, "error": error, "incomplete": False,
            "raw_count": len(hits), "skipped": False}


# Atajo para uso en discovery.discover
def is_enabled() -> bool:
    return available()
