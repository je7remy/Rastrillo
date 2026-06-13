"""Directorio público de instrucciones de eliminación.

Sustituye al modelo de recetas-obligatorias: para cualquier host, consultamos
un dataset abierto (JustDeleteMe — https://justdelete.me, repo
jdm-contrib/justdelete.me) que mapea dominio → URL de borrado + dificultad
+ notas. El motor combina esta URL con el modo IA para conducir el flujo
sin que el usuario tenga que escribir un JSON.

Diseño:
- Cacheamos `sites.json` en ~/.rastrillo/directory.json al primer arranque y
  no volvemos a tocar la red hasta que el usuario pida refresh.
- Si nunca pudimos descargarlo (sin red en el primer arranque), usamos un
  fallback embebido con un puñado de servicios habituales para que el
  programa siga siendo útil offline.
- La resolución es por HOST COMPLETO (e.g. "amazon.com", "old.reddit.com"):
  primero match exacto contra `domains[]`, luego match por sufijo (subdominios).

Invariantes que respeta:
- Es lectura pública. No envía datos a internet salvo la descarga del JSON.
- Funciona sin red si la caché ya existe (o con el fallback embebido).
"""
import json
import logging
import os
import time
import urllib.request
from pathlib import Path
from typing import Dict, List, Optional

from . import config

log = logging.getLogger("rastrillo.directory")

DIRECTORY_URL = (
    "https://raw.githubusercontent.com/"
    "jdm-contrib/justdelete.me/master/_data/sites.json"
)
CACHE_PATH = config.BASE_DIR / "directory.json"

# Pequeño "seed" embebido para que el primer arranque sin red sea útil. NO es
# autoritativo; queda eclipsado en cuanto baje el JSON real.
_FALLBACK_SEED: List[dict] = [
    {"name": "Amazon", "difficulty": "medium",
     "url": "https://www.amazon.com/gp/help/customer/contact-us/",
     "notes": "Cerrar cuenta desde Cuenta → Soporte → Cerrar cuenta.",
     "domains": ["amazon.com", "amazon.es"]},
    {"name": "Twitter / X", "difficulty": "easy",
     "url": "https://twitter.com/settings/deactivate",
     "notes": "Settings → Your account → Deactivate.",
     "domains": ["twitter.com", "x.com"]},
    {"name": "WordPress.com", "difficulty": "easy",
     "url": "https://wordpress.com/me/account/close",
     "notes": "Settings → Close account permanently.",
     "domains": ["wordpress.com"]},
    {"name": "Spotify", "difficulty": "medium",
     "url": "https://support.spotify.com/article/close-account/",
     "notes": "Confirmación por email; sigue el enlace 2 veces.",
     "domains": ["spotify.com"]},
    {"name": "Eventbrite", "difficulty": "medium",
     "url": "https://www.eventbrite.com/support/articles/en_US/Troubleshooting/how-to-delete-your-eventbrite-account",
     "notes": "Account Settings → Close Account.",
     "domains": ["eventbrite.com"]},
]


def _normalize_host(host: str) -> str:
    if not host:
        return ""
    s = host.strip().lower()
    if "//" in s:
        s = s.split("//", 1)[1]
    s = s.split("/", 1)[0].split("?", 1)[0]
    if s.startswith("www."):
        s = s[4:]
    return s


class Directory:
    """Carga perezosa del JSON cacheado. Se reusa entre llamadas en el proceso."""

    def __init__(self, entries: List[dict], source: str, fetched_at: Optional[float]):
        self.entries = entries
        self.source = source                # "upstream" | "cache" | "fallback"
        self.fetched_at = fetched_at
        # Índice host → lista de entradas (puede haber colisiones; el primero gana).
        self._by_host: Dict[str, dict] = {}
        for entry in entries:
            for dom in entry.get("domains") or []:
                self._by_host.setdefault(_normalize_host(dom), entry)

    def __len__(self) -> int:
        return len(self.entries)

    def lookup(self, host: str) -> Optional[dict]:
        """Devuelve la entrada del directorio para `host` o None.

        Orden:
          1. Match exacto contra `domains[]` (e.g. "amazon.com").
          2. Match por sufijo (subdominios como "old.reddit.com" → "reddit.com").
        """
        if not host:
            return None
        h = _normalize_host(host)
        hit = self._by_host.get(h)
        if hit:
            return hit
        # match por sufijo: probamos quitando subdominios uno a uno.
        parts = h.split(".")
        for i in range(1, len(parts) - 1):
            cand = ".".join(parts[i:])
            hit = self._by_host.get(cand)
            if hit:
                return hit
        return None


_loaded: Optional[Directory] = None


def fetch_directory(timeout: int = 30) -> Directory:
    """Descarga el JSON de upstream y lo guarda en caché. Si falla, NO borra
    la caché previa: simplemente sigue con lo que ya teníamos.
    """
    config.ensure_dirs()
    log.info("descargando directorio de %s", DIRECTORY_URL)
    req = urllib.request.Request(DIRECTORY_URL, headers={
        "User-Agent": "rastrillo/0.1 (+local-tool)",
    })
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = r.read()
    payload = json.loads(data)
    if not isinstance(payload, list) or not payload:
        raise RuntimeError("upstream devolvió payload inesperado")
    fetched_at = time.time()
    CACHE_PATH.write_text(
        json.dumps({"fetched_at": fetched_at, "entries": payload},
                   ensure_ascii=False),
        encoding="utf-8",
    )
    log.info("directorio cacheado: %s entradas → %s", len(payload), CACHE_PATH)
    global _loaded
    _loaded = Directory(payload, source="upstream", fetched_at=fetched_at)
    return _loaded


def _read_cache() -> Optional[Directory]:
    if not CACHE_PATH.exists():
        return None
    try:
        obj = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
        entries = obj.get("entries") if isinstance(obj, dict) else obj
        fetched_at = obj.get("fetched_at") if isinstance(obj, dict) else None
        if not isinstance(entries, list) or not entries:
            return None
        return Directory(entries, source="cache", fetched_at=fetched_at)
    except Exception as e:
        log.exception("caché de directorio corrupta: %s", e)
        return None


def load_directory(force_refresh: bool = False) -> Directory:
    """Devuelve el directorio actual. Lazy + cacheado en memoria.

    Orden de fuentes:
      1. force_refresh=True → intenta upstream.
      2. caché en disco.
      3. upstream (un solo intento, silencioso si falla).
      4. fallback embebido.
    """
    global _loaded
    if _loaded is not None and not force_refresh:
        return _loaded

    if force_refresh:
        try:
            return fetch_directory()
        except Exception as e:
            log.warning("refresh falló: %s; uso caché/fallback", e)

    cached = _read_cache()
    if cached:
        _loaded = cached
        return cached

    # Primer arranque: intentamos upstream una vez.
    try:
        return fetch_directory()
    except Exception as e:
        log.warning("no pude descargar directorio (%s); uso fallback embebido", e)
        _loaded = Directory(list(_FALLBACK_SEED), source="fallback", fetched_at=None)
        return _loaded


def directory_info() -> dict:
    """Estado para mostrar en la UI."""
    d = load_directory()
    return {
        "entries": len(d),
        "source": d.source,
        "fetched_at": d.fetched_at,
        "cache_path": str(CACHE_PATH),
    }


# Atajos de conveniencia ---------------------------------------------------
def lookup(host: str) -> Optional[dict]:
    return load_directory().lookup(host)


def deletion_url(host: str) -> Optional[str]:
    entry = lookup(host)
    return (entry or {}).get("url")
