"""Tier 1 del Nivel 7: onboarding una sola vez + 401 con mensaje útil
+ HTML expone el panel de bienvenida y el overlay de token."""
from .helpers import IsolatedTestCase, auth_client


class TestOnboarding(IsolatedTestCase):

    def setUp(self):
        super().setUp()
        from rastrillo import config, db
        self.config = config
        self.db = db
        db.init()
        self.client = auth_client()

    def test_primera_vez_no_onboarded(self):
        # Desde Tarea 3 todos los GET de /api/* exigen token.
        r = self.client.get("/api/onboarding", headers=self.hdr())
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.json()["onboarded"])
        self.assertFalse(self.config.ONBOARDED_PATH.exists())

    def test_dismiss_marca_y_persiste(self):
        # 1ª vez: no onboarded
        self.assertFalse(self.client.get("/api/onboarding",
            headers=self.hdr()).json()["onboarded"])
        # dismiss
        r = self.client.post("/api/onboarding/dismiss", json={},
                             headers=self.hdr())
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["ok"])
        # marker existe
        self.assertTrue(self.config.ONBOARDED_PATH.exists())
        # 2ª vez: onboarded
        self.assertTrue(self.client.get("/api/onboarding",
            headers=self.hdr()).json()["onboarded"])

    def test_dismiss_idempotente(self):
        """Llamar dismiss varias veces no rompe nada y el marker queda."""
        self.client.post("/api/onboarding/dismiss", json={}, headers=self.hdr())
        self.client.post("/api/onboarding/dismiss", json={}, headers=self.hdr())
        self.assertTrue(self.client.get("/api/onboarding",
            headers=self.hdr()).json()["onboarded"])


class TestAuthFriendly401(IsolatedTestCase):

    def setUp(self):
        super().setUp()
        from rastrillo import db
        db.init()
        self.client = auth_client()

    def test_401_explica_que_hacer(self):
        """POST sin token devuelve 401 con texto accionable, no críptico."""
        r = self.client.post("/api/dry-run", json={"enabled": True})
        self.assertEqual(r.status_code, 401)
        detail = r.json()["detail"].lower()
        self.assertIn("token", detail)
        # Debe mencionar al menos una pista de qué hacer
        self.assertTrue(
            "url" in detail or "consola" in detail or "reinicia" in detail,
            f"el mensaje 401 no es accionable: {detail!r}",
        )

    def test_token_por_query_funciona(self):
        """Por defecto los tests setean RASTRILLO_ALLOW_QUERY_TOKEN=1 en
        helpers.py para no romper el resto de la suite. Aquí confirmamos
        que con esa env el `?token=` sigue funcionando."""
        from rastrillo import config
        r = self.client.post(f"/api/dry-run?token={config.AUTH_TOKEN}",
                             json={"enabled": True})
        self.assertEqual(r.status_code, 200)

    def test_token_por_query_rechazado_sin_env(self):
        """Tarea 5: sin RASTRILLO_ALLOW_QUERY_TOKEN, el server ignora el
        `?token=` (anti-leak en historial/Referer). Solo vale el header
        X-Rastrillo-Token."""
        import os
        from rastrillo import config
        os.environ.pop("RASTRILLO_ALLOW_QUERY_TOKEN", None)
        try:
            r = self.client.post(f"/api/dry-run?token={config.AUTH_TOKEN}",
                                 json={"enabled": True})
            self.assertEqual(r.status_code, 401)
            # Con header explícito sigue funcionando.
            r2 = self.client.post(f"/api/dry-run?token={config.AUTH_TOKEN}",
                                  json={"enabled": True},
                                  headers={"X-Rastrillo-Token": config.AUTH_TOKEN})
            self.assertEqual(r2.status_code, 200)
        finally:
            os.environ["RASTRILLO_ALLOW_QUERY_TOKEN"] = "1"


class TestUIExposesTier1(IsolatedTestCase):

    def setUp(self):
        super().setUp()
        self.client = auth_client()

    def test_html_tiene_panel_de_bienvenida_y_token_prompt(self):
        # Desde Tarea 10 el JS vive en /static/app.js. El HTML solo carga
        # la estructura y el boot dinámico; los textos de welcome / token
        # prompt los inyecta el JS.
        app_js = self.client.get("/static/app.js").text
        # Marcadores del JS (no del DOM inicial, que es lazy)
        self.assertIn("showWelcomePanel", app_js)
        self.assertIn("showTokenPrompt", app_js)
        self.assertIn("/api/onboarding", app_js)
        self.assertIn("/api/onboarding/dismiss", app_js)
        self.assertIn("connectWithToken", app_js)
        self.assertIn("dismissWelcome", app_js)
        # Texto del welcome (cubre los 4 puntos del enunciado)
        self.assertIn("dos ventanas", app_js)
        self.assertIn("logueas tú una vez", app_js)
        self.assertIn("CAPTCHA", app_js)
        self.assertIn("propias cuentas", app_js)
        # Texto del token prompt
        self.assertIn("token de auth", app_js)
