"""Paso 5, Entrega 0: la DESCARGA de informes por HTTP.

Este fichero existe porque la suite tenía un hueco con forma de bug.

`test_informe_pdf.py` llama a `reports.build_report("pdf")` directamente y
comprueba que produce bytes `%PDF` válidos — y lo hace bien. Pero era la ÚNICA
prueba de informes que había, así que nadie ejercitaba `GET /api/report`: se
probaba el GENERADOR y nunca el TRANSPORTE. El fallo vivía justo ahí.

Concretamente: el dashboard pedía el informe con
`<a href="/api/report?format=pdf" download>`. Una navegación de ancla no puede
llevar cabeceras propias, así que la petición salía sin `X-Rastrillo-Token`, el
`auth_middleware` respondía 401 con cuerpo JSON, y el atributo `download`
guardaba ese JSON de error en un fichero `.pdf`. Nunca se descargaba un PDF.

Lo que se fija aquí:

  - los tres formatos devuelven su Content-Type y su cuerpo correctos,
  - **un `format=pdf` no puede responder JSON** (la regresión exacta),
  - el `Content-Disposition` trae un nombre de fichero usable,
  - sin token se responde 401 y NO se filtra ni un PDF ni datos,
  - y el HTML no puede volver a pedir el informe con un `<a href>`, que es lo
    que estructuralmente hacía imposible autenticar la descarga.

La autenticación no se relaja en ninguna dirección: el endpoint sigue exigiendo
la cabecera como todo `/api/*`.
"""
from __future__ import annotations

import re
from pathlib import Path

from .helpers import IsolatedTestCase, auth_client

_RAIZ = Path(__file__).resolve().parents[1]
_INDEX_HTML = _RAIZ / "rastrillo" / "static" / "index.html"
_APP_JS = _RAIZ / "rastrillo" / "static" / "app.js"


class _BaseInforme(IsolatedTestCase):
    def setUp(self):
        super().setUp()
        from rastrillo import db
        self.db = db
        self.db.init()
        self.cli = auth_client()

    def _alta(self, **campos):
        base = dict(
            source="sherlock", source_site="ejemplo.com",
            profile_url="https://ejemplo.com/u/mar", display_name="Ejemplo",
            confidence="high", status="found",
        )
        base.update(campos)
        plataforma = base.pop("platform", "ejemplo")
        ident = base.pop("identifier", "mar")
        self.db.upsert_account(plataforma, ident, **base)


class TestContentTypes(_BaseInforme):
    """Cada formato responde lo suyo, y el PDF responde un PDF."""

    def test_pdf_devuelve_pdf(self):
        self._alta()
        r = self.cli.get("/api/report?format=pdf", headers=self.hdr())
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.headers["content-type"], "application/pdf")
        self.assertTrue(r.content.startswith(b"%PDF"),
                        f"el cuerpo no es un PDF: {r.content[:60]!r}")
        self.assertIn(b"%%EOF", r.content[-2048:])

    def test_pdf_NUNCA_devuelve_json(self):
        """LA regresión que arreglamos en el Paso 5.

        Si alguien vuelve a servir el informe por una vía que no puede
        autenticarse, lo que llega al cliente es el JSON de un 401 con nombre
        de PDF. Este test lo impide en los dos extremos: el tipo declarado y
        el primer byte del cuerpo.
        """
        self._alta()
        r = self.cli.get("/api/report?format=pdf", headers=self.hdr())
        self.assertNotIn("json", r.headers["content-type"].lower())
        self.assertFalse(r.content.lstrip().startswith(b"{"),
                         "format=pdf respondió un cuerpo JSON")

    def test_csv_devuelve_csv(self):
        """La cabecera cambió en el Paso 6, y a propósito.

        Antes se afirmaba `startswith(b"id,platform,")`: nombres de columna de
        la base de datos y sin BOM. Ahora el CSV empieza por el BOM UTF-8 (sin
        él Excel destroza acentos y cirílico) y las cabeceras están en español.
        No es una regresión: es el contenido de la Entrega 1.
        """
        import codecs
        self._alta()
        r = self.cli.get("/api/report?format=csv", headers=self.hdr())
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.headers["content-type"].startswith("text/csv"))
        self.assertTrue(r.content.startswith(codecs.BOM_UTF8),
                        f"el CSV no empieza por el BOM: {r.content[:12]!r}")
        cabecera = r.content.decode("utf-8-sig").splitlines()[0]
        self.assertTrue(cabecera.startswith("id,"),
                        f"cabecera CSV inesperada: {cabecera[:80]!r}")
        for titulo in ("Sitio", "Identificador", "Estado", "Confianza"):
            self.assertIn(titulo, cabecera)

    def test_json_devuelve_json(self):
        self._alta()
        r = self.cli.get("/api/report?format=json", headers=self.hdr())
        self.assertEqual(r.status_code, 200)
        self.assertIn("application/json", r.headers["content-type"])
        datos = r.json()
        self.assertIn("accounts", datos)
        self.assertIn("summary", datos)

    def test_default_sigue_siendo_json(self):
        """Sin `format` el contrato previo era JSON; no se cambia."""
        r = self.cli.get("/api/report", headers=self.hdr())
        self.assertEqual(r.status_code, 200)
        self.assertIn("application/json", r.headers["content-type"])

    def test_formato_no_soportado_da_400(self):
        r = self.cli.get("/api/report?format=xml", headers=self.hdr())
        self.assertEqual(r.status_code, 400)

    def test_db_vacia_tambien_da_pdf(self):
        """Sin cuentas el informe sigue siendo un PDF, no un error."""
        r = self.cli.get("/api/report?format=pdf", headers=self.hdr())
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.content.startswith(b"%PDF"))


class TestContentDisposition(_BaseInforme):
    """El navegador tiene que poder nombrar el fichero."""

    def test_pdf_trae_nombre_de_fichero(self):
        r = self.cli.get("/api/report?format=pdf", headers=self.hdr())
        cd = r.headers.get("content-disposition", "")
        self.assertIn("attachment", cd)
        m = re.search(r'filename="([^"]+)"', cd)
        self.assertIsNotNone(m, f"sin filename usable en {cd!r}")
        self.assertTrue(m.group(1).endswith(".pdf"))
        self.assertTrue(m.group(1).startswith("rastrillo-"))

    def test_csv_trae_nombre_de_fichero(self):
        r = self.cli.get("/api/report?format=csv", headers=self.hdr())
        cd = r.headers.get("content-disposition", "")
        self.assertIn("attachment", cd)
        self.assertTrue(re.search(r'filename="rastrillo-[^"]+\.csv"', cd),
                        f"sin filename .csv en {cd!r}")


class TestAuth(_BaseInforme):
    """Sin token no sale nada. El 401 no es un informe."""

    def test_sin_token_401_y_sin_pdf(self):
        self._alta()
        r = self.cli.get("/api/report?format=pdf")
        self.assertEqual(r.status_code, 401)
        self.assertFalse(r.content.startswith(b"%PDF"),
                         "un 401 no puede devolver el PDF")

    def test_sin_token_401_y_sin_datos(self):
        """El cuerpo del 401 es solo el `detail`: nada de cuentas."""
        self._alta(identifier="secreto-mio", source_site="privado.example")
        for fmt in ("pdf", "csv", "json"):
            with self.subTest(fmt=fmt):
                r = self.cli.get(f"/api/report?format={fmt}")
                self.assertEqual(r.status_code, 401)
                self.assertNotIn(b"secreto-mio", r.content)
                self.assertNotIn(b"privado.example", r.content)
                self.assertEqual(list(r.json().keys()), ["detail"])

    def test_token_malo_401(self):
        r = self.cli.get("/api/report?format=pdf",
                         headers={"X-Rastrillo-Token": "no-es-el-bueno"})
        self.assertEqual(r.status_code, 401)
        self.assertFalse(r.content.startswith(b"%PDF"))


class TestFrontendPuedeAutenticar(IsolatedTestCase):
    """El front tiene que pedir el informe por una vía que lleve el token.

    Un `<a href="/api/report..." download>` NO puede: las cabeceras propias no
    viajan en una navegación. Si alguien lo reintroduce, la descarga vuelve a
    romperse exactamente igual y en silencio (el fichero se guarda, solo que
    con el JSON del 401 dentro). Por eso se comprueba el HTML.
    """

    def test_html_no_pide_el_informe_con_un_ancla(self):
        html = _INDEX_HTML.read_text(encoding="utf-8")
        anclas = re.findall(r"<a\b[^>]*>", html, flags=re.I | re.S)
        culpables = [a for a in anclas if "/api/report" in a]
        self.assertEqual(
            culpables, [],
            "hay un <a href> apuntando a /api/report: no puede mandar el "
            "token y la descarga volverá a guardar el JSON del 401.\n"
            + "\n".join(culpables))

    def test_los_botones_de_informe_existen(self):
        html = _INDEX_HTML.read_text(encoding="utf-8")
        for ident in ("report-csv-btn", "report-pdf-btn"):
            with self.subTest(ident=ident):
                self.assertIn(ident, html)

    def test_el_js_descarga_con_token_y_blob(self):
        """La descarga pasa por `getAPI` (que pone la cabecera) y por Blob."""
        js = _APP_JS.read_text(encoding="utf-8")
        self.assertIn("descargarInforme", js)
        self.assertIn("/api/report?format=", js)
        # getAPI es el único punto que añade X-Rastrillo-Token a los GET.
        m = re.search(r"async function descargarInforme.*?\n}", js, flags=re.S)
        self.assertIsNotNone(m, "no encuentro descargarInforme() en app.js")
        cuerpo = m.group(0)
        self.assertIn("getAPI(", cuerpo)
        self.assertIn("blob()", cuerpo)
        self.assertIn("createObjectURL", cuerpo)

    def test_ambos_botones_estan_enganchados(self):
        js = _APP_JS.read_text(encoding="utf-8")
        for ident, fmt in (("report-csv-btn", "csv"), ("report-pdf-btn", "pdf")):
            with self.subTest(ident=ident):
                self.assertRegex(
                    js, rf'\$\("{ident}"\)\.onclick\s*=.*descargarInforme\("{fmt}"')


class TestNoSeRelajaLaAuth(IsolatedTestCase):
    """El arreglo no puede consistir en debilitar la autenticación."""

    def test_report_no_esta_exceptuado_del_middleware(self):
        """Nadie puede meter un `if path == "/api/report"` que salte el token.

        Se mira el CUERPO, no el docstring: el docstring del middleware
        menciona "reports" legítimamente al enumerar qué devuelven los GET de
        /api/*, y buscar la palabra suelta daba un falso positivo.
        """
        from rastrillo import server
        import ast
        import inspect
        arbol = ast.parse(inspect.getsource(server.auth_middleware).lstrip())
        funcion = arbol.body[0]
        cuerpo = funcion.body
        if (cuerpo and isinstance(cuerpo[0], ast.Expr)
                and isinstance(cuerpo[0].value, ast.Constant)
                and isinstance(cuerpo[0].value.value, str)):
            cuerpo = cuerpo[1:]          # fuera el docstring
        codigo = "\n".join(ast.dump(n) for n in cuerpo)
        self.assertNotIn("report", codigo,
                         "el middleware no puede tener un caso especial para "
                         "/api/report: la descarga se autentica, no se exime")

    def test_el_front_no_manda_el_token_por_query(self):
        """`?token=` queda en el historial del navegador y en el Referer."""
        js = _APP_JS.read_text(encoding="utf-8")
        m = re.search(r"async function descargarInforme.*?\n}", js, flags=re.S)
        self.assertNotIn("token=", m.group(0))
