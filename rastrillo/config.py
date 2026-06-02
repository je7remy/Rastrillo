"""Configuración central. Todo vive bajo ~/.rastrillo para que sea local y portable."""
import os
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
AI_MODEL = os.environ.get("RASTRILLO_AI_MODEL", "claude-sonnet-4-20250514")

# Plataformas que NUNCA se tocan (las profesionales que quieres conservar)
KEEP_PLATFORMS = {"tiktok", "instagram", "linkedin", "github"}


def ensure_dirs():
    for d in (BASE_DIR, PROFILE_DIR, LOG_DIR, SCREENSHOT_DIR, USER_RECIPES):
        d.mkdir(parents=True, exist_ok=True)
