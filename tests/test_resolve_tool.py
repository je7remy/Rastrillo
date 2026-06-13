"""Regresión del bug de portabilidad Win/Linux en el discovery.

Causa raíz: `subprocess(["sherlock", ...])` resuelve solo por PATH. Los
binarios (sherlock/holehe/maigret) son dependencias y viven junto al
intérprete del venv, directorio que NO está en PATH cuando se arranca por el
wrapper sin activar el venv. En Windows "funcionaba" solo si había una copia
global en PATH; en Linux el scan volvía vacío.

`discovery._resolve_tool` arregla esto buscando primero junto a
`sys.executable` (el bin del venv) y cayendo a PATH como respaldo. Estos tests
fijan ese contrato con ficheros reales en disco, sin depender de que las
herramientas estén instaladas.
"""
import os
import sys
from pathlib import Path
from unittest.mock import patch
from .helpers import IsolatedTestCase


def _make_tool(dirpath: Path, name: str) -> Path:
    """Crea un ejecutable falso que `shutil.which` reconozca en este SO."""
    if sys.platform == "win32":
        p = dirpath / f"{name}.cmd"
        p.write_text("@echo off\n", encoding="ascii")
    else:
        p = dirpath / name
        p.write_text("#!/bin/sh\n", encoding="ascii")
        os.chmod(p, 0o755)
    return p


class TestResolveTool(IsolatedTestCase):

    def setUp(self):
        super().setUp()
        from rastrillo import discovery
        self.discovery = discovery
        # Dos directorios distintos: el "bin del venv" (junto al intérprete) y
        # un directorio que sí está en PATH. Comparamos a cuál resuelve.
        self.venv_bin = self.HOME / "venvbin"
        self.path_dir = self.HOME / "pathdir"
        self.venv_bin.mkdir()
        self.path_dir.mkdir()
        # sys.executable falso cuyo dirname es el bin del venv. No hace falta
        # que el fichero exista: _resolve_tool solo usa os.path.dirname.
        fake_python = self.venv_bin / ("python.exe" if sys.platform == "win32"
                                       else "python")
        self._patches = [
            patch.object(self.discovery.sys, "executable", str(fake_python)),
            patch.dict(os.environ, {"PATH": str(self.path_dir), "PATHEXT": ".CMD"}),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in reversed(self._patches):
            p.stop()
        super().tearDown()

    def test_prefiere_el_bin_del_interprete_sobre_path(self):
        # El mismo binario existe en AMBOS sitios; debe ganar el del venv.
        _make_tool(self.venv_bin, "sherlock")
        _make_tool(self.path_dir, "sherlock")
        resolved = self.discovery._resolve_tool("sherlock")
        self.assertIsNotNone(resolved)
        self.assertEqual(
            os.path.normcase(os.path.dirname(os.path.abspath(resolved))),
            os.path.normcase(str(self.venv_bin)),
        )

    def test_cae_a_path_si_no_esta_junto_al_interprete(self):
        # Solo en PATH (caso instalación global o pipx separado).
        _make_tool(self.path_dir, "holehe")
        resolved = self.discovery._resolve_tool("holehe")
        self.assertIsNotNone(resolved)
        self.assertEqual(
            os.path.normcase(os.path.dirname(os.path.abspath(resolved))),
            os.path.normcase(str(self.path_dir)),
        )

    def test_none_si_no_esta_en_ningun_sitio(self):
        self.assertIsNone(self.discovery._resolve_tool("herramienta-inexistente-xyz"))

    def test_run_sherlock_sin_binario_reporta_no_instalado(self):
        # Sin binario en venv ni en PATH: error accionable, no lista vacía muda.
        r = self.discovery.run_sherlock("usuario_valido")
        self.assertEqual(r["hits"], [])
        self.assertTrue(r["incomplete"])
        self.assertIn("Sherlock no instalado", r["error"])
