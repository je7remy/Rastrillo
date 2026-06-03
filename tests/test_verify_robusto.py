"""Verify robusto del Nivel 5: success_url + expect_gone + success_text +
success_selector combinables. Retrocompatible con recetas viejas que solo
usan success_selector.

Usa FakePage para evitar Playwright real.
"""
from playwright.sync_api import TimeoutError as PWTimeout
from .helpers import IsolatedTestCase


class FakePage:
    """Page mínima para tests del verify."""
    def __init__(self, url="", body_text="", has_selector=None):
        self.url = url
        self._body = body_text
        # has_selector: callable(selector)->bool. Default: True (existe).
        self._has = has_selector or (lambda s: True)

    def wait_for_selector(self, selector, timeout=None):
        if not self._has(selector):
            raise PWTimeout(f"no existe {selector}")
        return True

    def query_selector(self, selector):
        return object() if self._has(selector) else None

    def inner_text(self, selector="body"):
        return self._body

    def screenshot(self, path=None):
        pass


class TestVerifyRobusto(IsolatedTestCase):

    def setUp(self):
        super().setUp()
        from rastrillo import db, engine
        self.db = db
        self.engine = engine
        db.init()
        self.aid = db.upsert_account(
            "x", "u", source="manual", source_site="x.com",
            display_name="X", status="in_progress", owned=1)

    def _eng(self):
        eng = self.engine.Engine(headless=True)
        eng._shot = lambda *a, **kw: None
        return eng

    # ── retrocompat ──
    def test_solo_success_selector_presente_ok(self):
        eng = self._eng()
        eng._verify(self.aid, FakePage(has_selector=lambda s: True),
                    {"success_selector": "#ok", "on_success": "deleted"})
        self.assertEqual(self.db.get_account(self.aid)["status"], "deleted")

    def test_solo_success_selector_ausente_falla(self):
        eng = self._eng()
        eng._verify(self.aid, FakePage(has_selector=lambda s: False),
                    {"success_selector": "#ok", "on_success": "deleted"})
        self.assertEqual(self.db.get_account(self.aid)["status"], "manual")

    def test_sin_criterios_va_a_manual(self):
        eng = self._eng()
        eng._verify(self.aid, FakePage(), {"on_success": "deleted"})
        self.assertEqual(self.db.get_account(self.aid)["status"], "manual")

    # ── success_url ──
    def test_success_url_match_substring(self):
        eng = self._eng()
        page = FakePage(url="https://example.com/account/deleted-confirmation")
        eng._verify(self.aid, page,
                    {"success_url": "/deleted", "on_success": "deleted"})
        self.assertEqual(self.db.get_account(self.aid)["status"], "deleted")

    def test_success_url_no_match(self):
        eng = self._eng()
        page = FakePage(url="https://example.com/settings")
        eng._verify(self.aid, page,
                    {"success_url": "/deleted", "on_success": "deleted"})
        self.assertEqual(self.db.get_account(self.aid)["status"], "manual")

    # ── expect_gone ──
    def test_expect_gone_ausente_ok(self):
        eng = self._eng()
        # query_selector devuelve None → gone, OK
        page = FakePage(has_selector=lambda s: False)
        eng._verify(self.aid, page,
                    {"expect_gone": "#delete-btn", "on_success": "deleted"})
        self.assertEqual(self.db.get_account(self.aid)["status"], "deleted")

    def test_expect_gone_presente_falla(self):
        eng = self._eng()
        page = FakePage(has_selector=lambda s: True)
        eng._verify(self.aid, page,
                    {"expect_gone": "#delete-btn", "on_success": "deleted"})
        self.assertEqual(self.db.get_account(self.aid)["status"], "manual")

    # ── success_text ──
    def test_success_text_match_case_insensitive(self):
        eng = self._eng()
        page = FakePage(body_text="Your account has been DELETED successfully.")
        eng._verify(self.aid, page,
                    {"success_text": "account has been deleted",
                     "on_success": "deleted"})
        self.assertEqual(self.db.get_account(self.aid)["status"], "deleted")

    def test_success_text_no_match(self):
        eng = self._eng()
        page = FakePage(body_text="Please log in to continue.")
        eng._verify(self.aid, page,
                    {"success_text": "account deleted", "on_success": "deleted"})
        self.assertEqual(self.db.get_account(self.aid)["status"], "manual")

    # ── combinaciones AND ──
    def test_combina_url_y_gone_todos_ok(self):
        eng = self._eng()
        page = FakePage(
            url="https://x.com/closed",
            has_selector=lambda s: False,   # selector NO existe → gone OK
        )
        eng._verify(self.aid, page,
                    {"success_url": "/closed",
                     "expect_gone": "#delete-btn",
                     "on_success": "deleted"})
        self.assertEqual(self.db.get_account(self.aid)["status"], "deleted")

    def test_combina_uno_falla_status_manual(self):
        eng = self._eng()
        # URL OK pero el selector que debía estar ausente sigue presente
        page = FakePage(
            url="https://x.com/closed",
            has_selector=lambda s: True,   # NO se fue
        )
        eng._verify(self.aid, page,
                    {"success_url": "/closed",
                     "expect_gone": "#delete-btn",
                     "on_success": "deleted"})
        self.assertEqual(self.db.get_account(self.aid)["status"], "manual")

    def test_combina_los_4_criterios(self):
        eng = self._eng()
        page = FakePage(
            url="https://x.com/done",
            body_text="account closed",
            has_selector=lambda s: (s == "#confirm"),
        )
        eng._verify(self.aid, page,
                    {"success_selector": "#confirm",
                     "success_url": "/done",
                     "success_text": "account closed",
                     "expect_gone": "#delete-btn",   # selector="#delete-btn" → False (gone)
                     "on_success": "deleted"})
        self.assertEqual(self.db.get_account(self.aid)["status"], "deleted")

    # ── on_success custom ──
    def test_on_success_anonymized(self):
        eng = self._eng()
        page = FakePage(url="https://x.com/settings/done")
        eng._verify(self.aid, page,
                    {"success_url": "/done", "on_success": "anonymized"})
        self.assertEqual(self.db.get_account(self.aid)["status"], "anonymized")
