"""Test de integración del motor contra un servidor HTML local + FakePage.

No usa Playwright real: instanciamos `Engine` directamente y le pasamos un
`FakePage` que cuenta llamadas. La receta apunta a una URL ficticia. Lo que
validamos es la mecánica del motor — resumibilidad, hash de receta y orden
de pasos — sin depender de Chromium.

Para el caso real Playwright hay un smoke aparte (no lo replicamos aquí
porque tarda y exige el binario).
"""
import http.server
import json
import threading
from .helpers import IsolatedTestCase


class _SilentHTTP(http.server.BaseHTTPRequestHandler):
    """Servidor de prueba: cualquier ruta devuelve un HTML mínimo. La página
    /success contiene un selector de éxito ("#ok") que el motor buscaría.
    El test no carga estos HTMLs (usa FakePage), pero el servidor sirve para
    que cualquier futura prueba con Playwright real pueda apuntar aquí.
    """
    def do_GET(self):
        body = (b'<html><body><div id="ok">deleted</div></body></html>'
                if self.path.startswith("/success")
                else b'<html><body><button id="del">Delete</button></body></html>')
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_a, **_k):
        pass


class FakePage:
    """Cuenta llamadas para que el test verifique orden y argumentos."""
    def __init__(self):
        self.calls = []

    def goto(self, url, wait_until=None):
        self.calls.append(("goto", url))

    def click(self, selector, timeout=None):
        self.calls.append(("click", selector))

    def fill(self, selector, value):
        self.calls.append(("fill", selector))

    def wait_for_selector(self, selector, timeout=None):
        self.calls.append(("wait_for", selector))

    def screenshot(self, path=None):
        pass


class TestEngineWithHTMLLocal(IsolatedTestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # Servidor HTTP en thread (puerto efímero).
        cls._httpd = http.server.HTTPServer(("127.0.0.1", 0), _SilentHTTP)
        cls.port = cls._httpd.server_address[1]
        cls._thread = threading.Thread(target=cls._httpd.serve_forever, daemon=True)
        cls._thread.start()

    @classmethod
    def tearDownClass(cls):
        try:
            cls._httpd.shutdown()
        except Exception:
            pass
        super().tearDownClass()

    def setUp(self):
        super().setUp()
        from rastrillo import db, engine, config
        self.db = db
        self.engine = engine
        self.config = config
        db.init()

        # Creamos una receta de usuario que apunta al servidor local.
        url = f"http://127.0.0.1:{self.port}/"
        self.recipe = {
            "platform": "testlocal",
            "display_name": "Test Local",
            "deletion_type": "full",
            "url": url,
            "steps": [
                {"action": "goto", "url": url},
                {"action": "click", "selector": "#del"},
                {"action": "verify", "success_selector": "#ok",
                 "on_success": "deleted"},
            ],
        }
        recipe_path = config.USER_RECIPES / "testlocal.json"
        recipe_path.write_text(json.dumps(self.recipe), encoding="utf-8")

        # Una cuenta lista para procesar
        self.acc_id = db.upsert_account(
            "testlocal", "tester",
            source="manual", source_site="127.0.0.1",
            display_name="Test Local", status="found", owned=1)

    def _engine_with_fake_verify(self):
        """Engine que reemplaza `_verify` por uno que setea el status final
        sin tocar Playwright (FakePage no implementa wait_for_selector real)."""
        eng = self.engine.Engine(headless=True)
        eng._shot = lambda *a, **kw: None
        eng._verify = lambda acc_id, page, step: self.db.set_status(
            acc_id, step.get("on_success", "deleted"), "verificado (fake)")
        return eng

    def test_run_steps_completo_marca_deleted(self):
        eng = self._engine_with_fake_verify()
        page = FakePage()
        eng._run_steps(self.acc_id, self.recipe, page)
        row = self.db.get_account(self.acc_id)
        self.assertEqual(row["status"], "deleted")
        # current_step debe llegar a len(steps) tras verify
        self.assertEqual(row["current_step"], len(self.recipe["steps"]))
        # Solo se llamó la página para goto + click (verify lo mockeamos)
        self.assertEqual(page.calls, [("goto", self.recipe["url"]),
                                       ("click", "#del")])

    def test_reanudable_desde_current_step(self):
        """Si current_step está en N, el motor arranca desde N (no repite
        los anteriores)."""
        # Simulamos que el primer paso (goto) ya se ejecutó antes.
        self.db.update_account(self.acc_id, current_step=1)
        eng = self._engine_with_fake_verify()
        page = FakePage()
        eng._run_steps(self.acc_id, self.recipe, page)
        # Como arranca desde i=1, solo se ejecuta click + verify (mockeado).
        self.assertEqual(page.calls, [("click", "#del")])
        self.assertEqual(self.db.get_account(self.acc_id)["status"], "deleted")

    def test_paso_destructivo_falla_no_avanza_puntero(self):
        """Si un paso revienta, current_step se queda apuntando al paso fallido
        (resumibilidad: al re-ejecutar arranca en ese paso)."""
        eng = self._engine_with_fake_verify()
        page = FakePage()

        # Hacemos que el click reviente.
        def _fail_click(selector, timeout=None):
            page.calls.append(("click", selector))
            raise RuntimeError("explosión simulada")
        page.click = _fail_click

        with self.assertRaises(RuntimeError):
            eng._run_steps(self.acc_id, self.recipe, page)
        # Tras el fallo, current_step debe ser 1 (el índice del paso fallido).
        row = self.db.get_account(self.acc_id)
        self.assertEqual(row["current_step"], 1,
                         "tras fallo, current_step debería apuntar al paso fallido")
        # Status NO debe ser 'deleted'
        self.assertNotEqual(row["status"], "deleted")

    def test_hash_receta_distinto_resetea_current_step(self):
        """Si la receta cambia entre runs (hash distinto), current_step → 0."""
        # Forzamos current_step alto y un hash viejo
        self.db.update_account(self.acc_id, current_step=99, recipe_hash="OLDHASH")
        eng = self._engine_with_fake_verify()
        acc = self.db.get_account(self.acc_id)
        eng._sync_recipe_hash(self.acc_id, acc, self.recipe)
        row = self.db.get_account(self.acc_id)
        self.assertEqual(row["current_step"], 0)
        self.assertNotEqual(row["recipe_hash"], "OLDHASH")


class TestRecipeAutoGenerated(IsolatedTestCase):
    """Verifica el otro lado del aprendizaje: que las recetas auto-generadas
    omiten fills y se cargan correctamente para reuso."""

    def setUp(self):
        super().setUp()
        from rastrillo import recipes_auto, recipes
        self.recipes_auto = recipes_auto
        self.recipes = recipes

    def test_fills_se_omiten(self):
        log = [
            {"turn": 0, "action": {"action": "click", "selector": "#a"},
             "result": {"ok": True}},
            {"turn": 1, "action": {"action": "fill",
                                   "selector": "input[name=email]",
                                   "value": "alice@example.com"},
             "result": {"ok": True}},
            {"turn": 2, "action": {"action": "click", "text": "Confirm"},
             "result": {"ok": True}},
        ]
        out = self.recipes_auto.synthesize_and_save(
            host="x.com", start_url="https://x.com/", agent_log=log,
            result_status="deleted")
        self.assertIsNotNone(out)
        path, recipe = out
        as_str = json.dumps(recipe)
        self.assertNotIn("alice@example.com", as_str,
                         "el value del fill se filtró al disco")
        # Ningún step debe ser 'fill'
        self.assertFalse(any(s.get("action") == "fill"
                             for s in recipe["steps"]))
        # Pero los clicks sí están
        sels = [s.get("selector") for s in recipe["steps"]]
        self.assertIn("#a", sels)
        self.assertIn("text=Confirm", sels)
        # La receta queda accesible por el loader
        loaded = self.recipes.get_recipe("x")
        self.assertIsNotNone(loaded)
        self.assertTrue(loaded.get("auto_generated"))

    def test_no_se_guarda_si_status_no_es_destructivo(self):
        log = [{"turn":0,"action":{"action":"click","selector":"#a"},
                "result":{"ok":True}}]
        out = self.recipes_auto.synthesize_and_save(
            host="y.com", start_url="https://y.com/", agent_log=log,
            result_status="manual")
        self.assertIsNone(out)
