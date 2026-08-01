"""Paso 5, Entrega 1: vigilancia del aviso de `httpx2`.

El aviso de `starlette.testclient` se silencia en `tests/__init__.py` (ver el
comentario largo de ahí para el análisis completo). Silenciar un aviso tiene
un riesgo obvio: que el día que se convierta en rotura, nadie se entere o se
entere en forma de cuarenta errores inconexos.

Este fichero es el contrapeso. No prueba código de Rastrillo: prueba que la
deuda que decidimos no pagar sigue siendo la que creíamos.

  - Si starlette retira el soporte de `httpx`, importar testclient lanza
    `RuntimeError` — una excepción, que ningún filtro de warnings tapa. Aquí
    se convierte en UN fallo con instrucciones en vez de una cascada.
  - Si el silenciado deja de hacer falta (porque el entorno ya trae `httpx2`),
    también conviene saberlo: el filtro pasaría a ser código muerto.

Nada de esto toca el runtime: `rastrillo/` no importa `starlette.testclient`
en ningún sitio, así que la deuda vive entera dentro de la suite.
"""
from __future__ import annotations

import unittest
import warnings


class TestDeudaHttpx2(unittest.TestCase):

    def test_testclient_sigue_importando(self):
        """La rotura real es un RuntimeError, y así se lee claro.

        Si esto falla con "requires the httpx2 package", la acción es:
        instalar `httpx2`, añadirlo a requirements y actualizar el lock EN EL
        MISMO cambio. Es decisión del mantenedor (invariante 8).
        """
        try:
            # El aviso se silencia igual que en `helpers.auth_client`: lo que
            # vigila este test es la EXCEPCIÓN, no el aviso, y si no se filtra
            # este import lo emite y ensucia la salida de la suite.
            with warnings.catch_warnings():
                warnings.filterwarnings(
                    "ignore",
                    message=r".*`httpx` with `starlette\.testclient` is deprecated.*",
                    category=UserWarning,
                )
                from starlette.testclient import TestClient  # noqa: F401
        except RuntimeError as e:
            self.fail(
                "starlette.testclient ya no admite `httpx`: la deuda vencida "
                "del Paso 5 (Entrega 1). Toca instalar httpx2 y actualizar "
                f"requirements + lock en el mismo cambio.\nOriginal: {e}"
            )

    def test_el_aviso_de_httpx2_no_sale_por_stderr(self):
        """Se comprueba en un SUBPROCESO, y tiene que ser así.

        Dentro de este mismo intérprete `fastapi.testclient` ya está importado,
        y un aviso de módulo solo se emite en el primer import: cualquier
        comprobación in-process daría verde sin probar nada. De hecho la
        primera versión de este test hacía exactamente eso —reinstalaba el
        filtro dentro del propio test y afirmaba que casaba— y pasaba en verde
        mientras la suite seguía escupiendo el aviso por pantalla.

        Un intérprete limpio que llame a `auth_client()` es la única forma de
        verificar que el silenciado de `helpers.py` funciona de verdad.
        """
        import subprocess
        import sys
        from pathlib import Path

        raiz = Path(__file__).resolve().parents[1]
        guion = (
            "import tests.helpers as h;"
            "h.IsolatedTestCase;"          # fuerza el módulo completo
            "import os, tempfile;"
            "os.environ['RASTRILLO_HOME'] = tempfile.mkdtemp();"
            "os.environ['RASTRILLO_TOKEN'] = 'test-token';"
            "h.auth_client()"
        )
        proc = subprocess.run(
            [sys.executable, "-c", guion],
            cwd=str(raiz), capture_output=True, text=True, timeout=120,
        )
        self.assertEqual(proc.returncode, 0,
                         f"el subproceso falló:\n{proc.stderr}")
        self.assertNotIn(
            "httpx2", proc.stderr,
            "el aviso de httpx2 sigue saliendo por stderr; el silenciado de "
            f"helpers.auth_client() no está funcionando.\nstderr:\n{proc.stderr}")

    def test_el_filtro_no_silencia_otros_avisos(self):
        """Silenciar uno no puede ser silenciarlos todos.

        Si el filtro fuera por categoría a secas, un aviso futuro de starlette
        (o de cualquiera) se perdería. Tiene que casar por MENSAJE.
        """
        with warnings.catch_warnings(record=True) as capturados:
            warnings.simplefilter("always")
            warnings.filterwarnings(
                "ignore",
                message=r".*`httpx` with `starlette\.testclient` is deprecated.*",
                category=UserWarning,
            )
            warnings.warn("otra cosa completamente distinta", UserWarning)
        self.assertEqual(len(capturados), 1,
                         "el filtro se está comiendo avisos que no son el suyo")

    def test_el_runtime_no_depende_de_testclient(self):
        """La deuda vive en los tests. Si algún módulo del paquete importara
        testclient, dejaría de ser cosmética y habría que replantearla."""
        from pathlib import Path
        raiz = Path(__file__).resolve().parents[1]
        culpables = []
        for py in list((raiz / "rastrillo").glob("*.py")) + [raiz / "cli.py"]:
            texto = py.read_text(encoding="utf-8", errors="ignore")
            if "testclient" in texto and "# testclient" not in texto:
                # `config.py` lo MENCIONA en un comentario (el Host que usa
                # fastapi.testclient); lo que importa es que no lo importe.
                for linea in texto.splitlines():
                    if "testclient" in linea and "import" in linea:
                        culpables.append(f"{py.name}: {linea.strip()}")
        self.assertEqual(culpables, [],
                         "el paquete importa starlette/fastapi testclient:\n"
                         + "\n".join(culpables))
