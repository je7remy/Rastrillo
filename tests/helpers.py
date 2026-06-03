"""Helpers para tests aislados.

Cada test corre con su PROPIO sandbox: RASTRILLO_HOME apunta a un tempdir
limpio y se reimportan los módulos `rastrillo.*` para que tomen la nueva
config. Sin esto, los tests compartirían DB/caches entre métodos de la
misma clase y se contaminarían.

Las subclases que definen su propio `setUp` deben llamar a `super().setUp()`
ANTES de importar nada del paquete (porque el sandbox se crea ahí).
"""
from __future__ import annotations
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path


def _purge_rastrillo_modules():
    for m in list(sys.modules):
        if m == "rastrillo" or m.startswith("rastrillo."):
            del sys.modules[m]


class IsolatedTestCase(unittest.TestCase):
    """Base TestCase: levanta un sandbox limpio en cada test y reimporta."""

    HOME: Path
    TOKEN = "test-token"

    def setUp(self):
        self.HOME = Path(tempfile.mkdtemp(prefix="rastrillo-test-"))
        os.environ["RASTRILLO_HOME"] = str(self.HOME)
        os.environ["RASTRILLO_TOKEN"] = self.TOKEN
        os.environ.pop("ANTHROPIC_API_KEY", None)
        os.environ.pop("RASTRILLO_HIBP_API_KEY", None)
        os.environ.pop("RASTRILLO_DRY_RUN", None)
        _purge_rastrillo_modules()

    def tearDown(self):
        _purge_rastrillo_modules()
        try:
            shutil.rmtree(self.HOME, ignore_errors=True)
        except Exception:
            pass

    def hdr(self) -> dict:
        return {"X-Rastrillo-Token": self.TOKEN}


def auth_client():
    """Devuelve un TestClient de FastAPI ya importado tras el sandbox."""
    from fastapi.testclient import TestClient
    from rastrillo.server import app
    return TestClient(app)


__all__ = ["IsolatedTestCase", "auth_client"]
