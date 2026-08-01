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
        # Tarea 5: por defecto el server NO acepta ?token= en la query (el
        # token solo entra por header X-Rastrillo-Token). Los tests sí lo
        # usan para no tener que rescribirlos, así que lo habilitamos aquí.
        os.environ["RASTRILLO_ALLOW_QUERY_TOKEN"] = "1"
        os.environ.pop("ANTHROPIC_API_KEY", None)
        os.environ.pop("RASTRILLO_HIBP_API_KEY", None)
        os.environ.pop("RASTRILLO_DRY_RUN", None)
        _purge_rastrillo_modules()

    def tearDown(self):
        _purge_rastrillo_modules()
        os.environ.pop("RASTRILLO_ALLOW_QUERY_TOKEN", None)
        try:
            shutil.rmtree(self.HOME, ignore_errors=True)
        except Exception:
            pass

    def hdr(self) -> dict:
        return {"X-Rastrillo-Token": self.TOKEN}


def auth_client():
    """Devuelve un TestClient de FastAPI ya importado tras el sandbox.

    El `catch_warnings` silencia UN aviso concreto (Paso 5, Entrega 1):

        StarletteDeprecationWarning: Using `httpx` with `starlette.testclient`
        is deprecated; install `httpx2` instead.

    Este es el ÚNICO punto del proyecto que importa testclient, así que es el
    único sitio donde hay que ponerlo. Tiene que ser un `catch_warnings` local
    y no un `filterwarnings` global: `unittest.TextTestRunner.run` ejecuta
    `warnings.simplefilter(self.warnings)`, que borra la lista de filtros
    entera, así que cualquier filtro instalado al importar habría desaparecido
    para cuando corre esta función.

    Por qué se silencia en vez de instalar httpx2: starlette >= 1.3 intenta
    `import httpx2` y SOLO si falla cae a `httpx` y avisa — es un fallback
    blando, sin fecha de retirada anunciada. Afecta exclusivamente a los tests
    (ningún módulo de `rastrillo/` importa testclient) y añadir la dependencia
    obligaría a tocar `requirements.lock`, que el invariante 8 prohíbe
    regenerar por cuenta propia.

    Por qué esto NO oculta la rotura futura: el filtro casa por MENSAJE, así
    que cualquier otro aviso de starlette se sigue viendo; y el día que
    starlette retire el soporte de `httpx` no emite un aviso sino un
    `RuntimeError`, que ningún filtro tapa. `test_deuda_httpx2.py` vigila las
    dos cosas.
    """
    import warnings
    with warnings.catch_warnings():
        warnings.filterwarnings(
            "ignore",
            message=r".*`httpx` with `starlette\.testclient` is deprecated.*",
            category=UserWarning,   # StarletteDeprecationWarning hereda de él
        )
        from fastapi.testclient import TestClient
    from rastrillo.server import app
    return TestClient(app)


__all__ = ["IsolatedTestCase", "auth_client"]
