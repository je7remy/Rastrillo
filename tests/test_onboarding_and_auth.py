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
        r = self.client.get("/api/onboarding")
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.json()["onboarded"])
        self.assertFalse(self.config.ONBOARDED_PATH.exists())

    def test_dismiss_marca_y_persiste(self):
        # 1ª vez: no onboarded
        self.assertFalse(self.client.get("/api/onboarding").json()["onboarded"])
        # dismiss
        r = self.client.post("/api/onboarding/dismiss", json={},
                             headers=self.hdr())
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["ok"])
        # marker existe
        self.assertTrue(self.config.ONBOARDED_PATH.exists())
        # 2ª vez: onboarded
        self.assertTrue(self.client.get("/api/onboarding").json()["onboarded"])

    def test_dismiss_idempotente(self):
        """Llamar dismiss varias veces no rompe nada y el marker queda."""
        self.client.post("/api/onboarding/dismiss", json={}, headers=self.hdr())
        self.client.post("/api/onboarding/dismiss", json={}, headers=self.hdr())
        self.assertTrue(self.client.get("/api/onboarding").json()["onboarded"])


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
        from rastrillo import config
        r = self.client.post(f"/api/dry-run?token={config.AUTH_TOKEN}",
                             json={"enabled": True})
        self.assertEqual(r.status_code, 200)


class TestUIExposesTier1(IsolatedTestCase):

    def setUp(self):
        super().setUp()
        self.client = auth_client()

    def test_html_tiene_panel_de_bienvenida_y_token_prompt(self):
        html = self.client.get("/").text
        # Marcadores del JS (no del DOM inicial, que es lazy)
        self.assertIn("showWelcomePanel", html)
        self.assertIn("showTokenPrompt", html)
        self.assertIn("/api/onboarding", html)
        self.assertIn("/api/onboarding/dismiss", html)
        self.assertIn("connectWithToken", html)
        self.assertIn("dismissWelcome", html)
        # Texto del welcome (cubre los 4 puntos del enunciado)
        self.assertIn("dos ventanas", html)
        self.assertIn("logueas tú una vez", html)
        self.assertIn("CAPTCHA", html)
        self.assertIn("propias cuentas", html)
        # Texto del token prompt
        self.assertIn("token de auth", html)
