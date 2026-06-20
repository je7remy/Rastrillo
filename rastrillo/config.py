"""Configuración central. Todo vive bajo ~/.rastrillo para que sea local y portable."""
import os
import secrets
from pathlib import Path

BASE_DIR = Path(os.environ.get("RASTRILLO_HOME", Path.home() / ".rastrillo"))
PROFILE_DIR = BASE_DIR / "browser-profile"   # perfil persistente de Chromium (tus sesiones)
DB_PATH = BASE_DIR / "rastrillo.db"
LOG_DIR = BASE_DIR / "logs"
SCREENSHOT_DIR = BASE_DIR / "screenshots"

# Recetas: primero las del repo, luego las del usuario (para que puedas sobreescribir/añadir)
PKG_RECIPES = Path(__file__).parent / "recipes"
USER_RECIPES = BASE_DIR / "recipes"

# IA de respaldo (opcional). Si no hay API key, el modo IA simplemente se desactiva.
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
AI_MODEL = os.environ.get("RASTRILLO_AI_MODEL", "claude-sonnet-4-6")

# HaveIBeenPwned (opcional). Sin clave se omite la fuente "hibp" en discovery.
# La API key se obtiene en https://haveibeenpwned.com/API/Key (~3-4 USD/mes).
HIBP_API_KEY = os.environ.get("RASTRILLO_HIBP_API_KEY")

# Plataformas que NUNCA se tocan (las profesionales que quieres conservar)
KEEP_PLATFORMS = {"tiktok", "instagram", "linkedin", "github"}

# Auth local del servidor: token aleatorio por arranque, override por env para
# tests. El frontend lo recibe en la URL inicial y lo guarda en sessionStorage.
# Todos los POST y los GET de /api/* exigen el token; GET / (HTML shell) queda
# fuera porque el navegador entra sin header en la primera carga.
AUTH_TOKEN: str = os.environ.get("RASTRILLO_TOKEN") or secrets.token_urlsafe(24)

# Allowlist de Host válidos para neutralizar DNS rebinding: un atacante en
# la misma red podría apuntar un DNS público a 127.0.0.1 y, si el navegador
# manda ese host, hablar con nuestro servidor saltándose la SOP. Solo
# permitimos el origen real del servidor local.
#
# `testserver` está incluido porque es el Host que usa fastapi.testclient
# por defecto y no resuelve a nada en internet: aceptarlo no abre superficie.
# Se puede ampliar con la env RASTRILLO_ALLOWED_HOSTS (lista separada por comas).
ALLOWED_HOSTS: set = {
    h.strip().lower()
    for h in (os.environ.get("RASTRILLO_ALLOWED_HOSTS")
              or "127.0.0.1:8765,localhost:8765,testserver").split(",")
    if h.strip()
}

# Dry-run global: el motor no ejecuta acciones destructivas; las simula. Se
# activa por env o desde el toggle del dashboard. Mutable a runtime.
DRY_RUN: bool = (os.environ.get("RASTRILLO_DRY_RUN", "").strip().lower()
                 in ("1", "true", "yes", "on"))


def set_dry_run(enabled: bool) -> None:
    """Activa/desactiva el modo simulación a runtime (lo cambia el endpoint)."""
    global DRY_RUN
    DRY_RUN = bool(enabled)


# ── Onboarding (panel de bienvenida) ────────────────────────────────────────
# Marker en disco: si el archivo existe, ya pasó por el panel de bienvenida.
ONBOARDED_PATH = BASE_DIR / "onboarded.json"


def is_onboarded() -> bool:
    return ONBOARDED_PATH.exists()


def mark_onboarded() -> None:
    """Persiste el flag de "ya he visto el panel". Idempotente."""
    import json as _json
    import time as _time
    ensure_dirs()
    ONBOARDED_PATH.write_text(
        _json.dumps({"onboarded_at": _time.time(), "version": "0.1.0"}),
        encoding="utf-8",
    )


def ensure_dirs():
    for d in (BASE_DIR, PROFILE_DIR, LOG_DIR, SCREENSHOT_DIR, USER_RECIPES):
        d.mkdir(parents=True, exist_ok=True)
