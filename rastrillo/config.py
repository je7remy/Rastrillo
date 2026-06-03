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

# Plataformas que NUNCA se tocan (las profesionales que quieres conservar)
KEEP_PLATFORMS = {"tiktok", "instagram", "linkedin", "github"}

# Auth local del servidor: token aleatorio por arranque, override por env para
# tests. El frontend lo recibe en la URL inicial y lo guarda en sessionStorage.
# Todos los POST exigen el token; los GET son libres (lectura).
AUTH_TOKEN: str = os.environ.get("RASTRILLO_TOKEN") or secrets.token_urlsafe(24)

# Dry-run global: el motor no ejecuta acciones destructivas; las simula. Se
# activa por env o desde el toggle del dashboard. Mutable a runtime.
DRY_RUN: bool = (os.environ.get("RASTRILLO_DRY_RUN", "").strip().lower()
                 in ("1", "true", "yes", "on"))


def set_dry_run(enabled: bool) -> None:
    """Activa/desactiva el modo simulación a runtime (lo cambia el endpoint)."""
    global DRY_RUN
    DRY_RUN = bool(enabled)


def ensure_dirs():
    for d in (BASE_DIR, PROFILE_DIR, LOG_DIR, SCREENSHOT_DIR, USER_RECIPES):
        d.mkdir(parents=True, exist_ok=True)
