"""Parte 2 del Nivel 6: endurecer contra falso 'deleted'.

  - revisit_profile sobre 404 → True (deleted)
  - revisit_profile sobre 200 con keyword 'user not found' → True
  - revisit_profile sobre 200 sin keywords → False (sigue activa)
  - revisit_profile con redirect a login → None (ambiguo)
  - _looks_like_login_redirect detecta patrones de login
  - _confirm_and_seal:
      * URL final del agente parece login → manual, NO deleted
      * profile_url devuelve 404 → deleted confirmado
      * profile_url devuelve 200 sin keywords → manual
      * sin profile_url, URL cambiada + keyword en body → deleted
      * sin profile_url, solo URL cambiada (sin keyword) → manual
      * status_proposed=anonymized → se respeta tal cual (no aplica verificación)
"""
import urllib.error
from unittest.mock import patch, MagicMock
from .helpers import IsolatedTestCase


class _FakePage:
    """Mínimo: url, inner_text. Para los tests no necesitamos más."""
    def __init__(self, url="", body=""):
        self.url = url
        self._body = body
    def inner_text(self, _sel):
        return self._body


class TestRevisitProfile(IsolatedTestCase):
    def setUp(self):
        super().setUp()
        from rastrillo import engine
        self.engine = engine

    def _patch_urlopen(self, *, status=200, body="", final_url=None,
                       http_error_code=None, raise_=None):
        def _open(req, timeout=None):
            if raise_:
                raise raise_
            if http_error_code:
                raise urllib.error.HTTPError(req.full_url, http_error_code,
                                             "x", {}, None)
            mock = MagicMock()
            mock.status = status
            mock.read = lambda n=None: body.encode("utf-8")
            mock.geturl = lambda: final_url or req.full_url
            mock.__enter__ = lambda s: mock
            mock.__exit__ = lambda *a: None
            return mock
        return patch.object(
            self.engine.urllib.request, "urlopen", side_effect=_open)

    def test_sin_url_es_none(self):
        self.assertIsNone(self.engine.revisit_profile(""))
        self.assertIsNone(self.engine.revisit_profile(None))

    def test_404_es_true(self):
        with self._patch_urlopen(http_error_code=404):
            self.assertTrue(self.engine.revisit_profile("https://x/u/alice"))

    def test_410_es_true(self):
        with self._patch_urlopen(http_error_code=410):
            self.assertTrue(self.engine.revisit_profile("https://x/u/alice"))

    def test_200_con_keyword_es_true(self):
        with self._patch_urlopen(status=200,
                body="<h1>User not found</h1><p>This account has been deleted</p>"):
            self.assertTrue(self.engine.revisit_profile("https://x/u/alice"))

    def test_200_sin_keyword_es_false(self):
        with self._patch_urlopen(status=200, body="<h1>Welcome alice</h1>"):
            self.assertFalse(self.engine.revisit_profile("https://x/u/alice"))

    def test_redirect_a_login_es_none(self):
        with self._patch_urlopen(status=200, body="login here",
                                  final_url="https://x/login?next=/u/alice"):
            self.assertIsNone(self.engine.revisit_profile("https://x/u/alice"))

    def test_timeout_es_none(self):
        with self._patch_urlopen(raise_=TimeoutError("net")):
            self.assertIsNone(self.engine.revisit_profile("https://x/u/alice"))

    def test_403_es_none(self):
        with self._patch_urlopen(http_error_code=403):
            self.assertIsNone(self.engine.revisit_profile("https://x/u/alice"))


class TestLooksLikeLoginRedirect(IsolatedTestCase):
    def setUp(self):
        super().setUp()
        from rastrillo import engine
        self.engine = engine

    def test_patrones_positivos(self):
        for url in [
            "https://x.com/login",
            "https://x.com/auth/start",
            "https://x.com/signin",
            "https://x.com/sso/callback",
            "https://x.com/account/login?next=/profile",
            "https://x.com/session/new",
            "https://x.com/anmelden",
            "https://x.com/connexion",
            "https://x.com/вход",
        ]:
            self.assertTrue(self.engine._looks_like_login_redirect(url),
                f"deberia detectar login en {url!r}")

    def test_patrones_negativos(self):
        for url in [
            "https://x.com/u/alice",
            "https://x.com/settings/account",
            "https://x.com/deleted",
            "https://x.com/",
            "",
            None,
        ]:
            self.assertFalse(self.engine._looks_like_login_redirect(url),
                f"falso positivo en {url!r}")


class TestConfirmAndSeal(IsolatedTestCase):
    def setUp(self):
        super().setUp()
        from rastrillo import engine, db
        self.engine = engine
        self.db = db
        db.init()
        self.eng = engine.Engine(headless=True)
        # Reemplazamos screenshot para no tocar Playwright
        self.eng._shot = lambda *a, **kw: None

    def _account(self, profile_url=None):
        aid = self.db.upsert_account(
            "fakesite", "alice", source="sherlock",
            source_site="fakesite.example", display_name="FakeSite",
            status="in_progress", owned=1)
        if profile_url:
            self.db.update_account(aid, profile_url=profile_url)
        return aid

    def test_url_final_login_no_es_deleted(self):
        aid = self._account(profile_url="https://x.com/u/alice")
        page = _FakePage(url="https://x.com/login?next=/")
        sealed = self.eng._confirm_and_seal(aid, page, "fakesite.example",
                                            "deleted", "test")
        self.assertFalse(sealed)
        row = self.db.get_account(aid)
        self.assertEqual(row["status"], "manual")
        self.assertIn("login", row["last_message"].lower())

    def test_profile_404_sella_deleted(self):
        aid = self._account(profile_url="https://x.com/u/alice")
        page = _FakePage(url="https://x.com/settings/account/closed")
        with patch.object(self.engine, "revisit_profile", return_value=True):
            sealed = self.eng._confirm_and_seal(aid, page, "x.com",
                                                "deleted", "agente done")
        self.assertTrue(sealed)
        row = self.db.get_account(aid)
        self.assertEqual(row["status"], "deleted")
        self.assertIn("revisita", row["last_message"].lower())

    def test_profile_sigue_cargando_es_manual(self):
        aid = self._account(profile_url="https://x.com/u/alice")
        page = _FakePage(url="https://x.com/settings/account")
        with patch.object(self.engine, "revisit_profile", return_value=False):
            sealed = self.eng._confirm_and_seal(aid, page, "x.com",
                                                "deleted", "agente done")
        self.assertFalse(sealed)
        row = self.db.get_account(aid)
        self.assertEqual(row["status"], "manual")
        self.assertIn("sigue cargando", row["last_message"].lower())

    def test_revisita_inconcluyente_es_manual(self):
        aid = self._account(profile_url="https://x.com/u/alice")
        page = _FakePage(url="https://x.com/settings/account")
        with patch.object(self.engine, "revisit_profile", return_value=None):
            sealed = self.eng._confirm_and_seal(aid, page, "x.com",
                                                "deleted", "agente done")
        self.assertFalse(sealed)
        row = self.db.get_account(aid)
        self.assertEqual(row["status"], "manual")

    def test_sin_profile_url_dos_senales_es_deleted(self):
        aid = self._account(profile_url=None)
        # URL cambió (profile_url era None, current_url no) + keyword body
        page = _FakePage(url="https://x.com/account/closed",
                         body="Your account has been deleted. Goodbye!")
        sealed = self.eng._confirm_and_seal(aid, page, "x.com",
                                            "deleted", "agente done")
        self.assertTrue(sealed)
        self.assertEqual(self.db.get_account(aid)["status"], "deleted")

    def test_sin_profile_url_una_sola_senal_es_manual(self):
        aid = self._account(profile_url=None)
        # URL cambió pero sin keyword
        page = _FakePage(url="https://x.com/done", body="Welcome back")
        sealed = self.eng._confirm_and_seal(aid, page, "x.com",
                                            "deleted", "agente done")
        self.assertFalse(sealed)
        row = self.db.get_account(aid)
        self.assertEqual(row["status"], "manual")
        self.assertIn("dos señales", row["last_message"].lower())

    def test_anonymized_no_aplica_verificacion(self):
        """status_proposed='anonymized' debería sellar directo (la cuenta
        sigue, solo cambian sus datos; no podemos confirmarlo con GET)."""
        aid = self._account(profile_url="https://x.com/u/alice")
        page = _FakePage(url="https://x.com/settings/account")
        # Ni siquiera mockeamos revisit: no debería llamarse
        with patch.object(self.engine, "revisit_profile") as spy:
            sealed = self.eng._confirm_and_seal(aid, page, "x.com",
                                                "anonymized", "agente done")
            self.assertFalse(spy.called,
                "no debería llamar a revisit_profile para anonymized")
        self.assertTrue(sealed)
        self.assertEqual(self.db.get_account(aid)["status"], "anonymized")
