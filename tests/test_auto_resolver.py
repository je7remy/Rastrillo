"""Auto-resolver post-scan: tras `discover`, jobs._auto_resolve_pending recorre
las cuentas `found` sin receta y persiste su Resolution en `action_meta` y
`profile_url` SIN cambiar el status.
"""
import json
from unittest.mock import patch
from .helpers import IsolatedTestCase


class TestAutoResolverPostScan(IsolatedTestCase):

    def setUp(self):
        super().setUp()
        from rastrillo import db, jobs, resolver
        self.db = db
        self.jobs = jobs
        self.resolver = resolver
        db.init()

    def _fake_resolve(self, host, identifier, force_refresh=False):
        # Resolution sintética que depende del host (para verificar persistencia)
        return self.resolver.Resolution(
            kind="semi_auto", layer="directory",
            title=f"plan-{host}", notes="",
            url=f"https://{host}/close-account",
            language="en",
        )

    def test_enriquece_cuentas_sin_receta(self):
        # Sembramos: una con receta (no debe tocarse), 2 sin receta.
        a1 = self.db.upsert_account("reddit","alice",source="sherlock",
            source_site="reddit.com", display_name="Reddit",
            status="found", confidence="high")
        a2 = self.db.upsert_account("foo","alice",source="sherlock",
            source_site="foo.com", display_name="Foo",
            status="found", confidence="medium")
        a3 = self.db.upsert_account("bar","alice",source="sherlock",
            source_site="bar.com", display_name="Bar",
            status="found", confidence="medium")

        with patch.object(self.resolver, "resolve", side_effect=self._fake_resolve) as spy:
            n = self.jobs._auto_resolve_pending()

        self.assertEqual(n, 2, "esperaba 2 cuentas sin receta enriquecidas")
        # Reddit tiene receta → no se invocó resolver
        invoked_hosts = [call.args[0] for call in spy.call_args_list]
        self.assertNotIn("reddit.com", invoked_hosts)
        self.assertIn("foo.com", invoked_hosts)
        self.assertIn("bar.com", invoked_hosts)

        # Verificar persistencia
        for aid in (a2, a3):
            row = self.db.get_account(aid)
            self.assertIsNotNone(row["action_meta"], "action_meta no se guardó")
            meta = json.loads(row["action_meta"])
            self.assertEqual(meta["kind"], "semi_auto")
            self.assertTrue(row["profile_url"].startswith("https://"))
            # Status NO cambia (sigue 'found' hasta el triage del usuario)
            self.assertEqual(row["status"], "found")

        # Reddit no se tocó
        self.assertIsNone(self.db.get_account(a1)["action_meta"])

    def test_no_reprocesa_si_ya_tiene_action_meta(self):
        aid = self.db.upsert_account("foo","alice",source="sherlock",
            source_site="foo.com", display_name="Foo",
            status="found", confidence="high")
        # Sembramos action_meta para simular "ya resuelto"
        self.db.update_account(aid, action_meta='{"kind":"semi_auto"}')
        with patch.object(self.resolver, "resolve") as spy:
            n = self.jobs._auto_resolve_pending()
        self.assertEqual(n, 0)
        self.assertFalse(spy.called, "resolver no debería invocarse para cuentas ya enriquecidas")

    def test_ignora_estados_no_found(self):
        # Cuentas en queued/skipped/deleted no se procesan
        for st in ("queued","skipped","deleted","not_mine","semi_auto","email_draft"):
            self.db.upsert_account(f"p_{st}", "u", source="sherlock",
                source_site=f"{st}.com", display_name=st, status=st)
        with patch.object(self.resolver, "resolve") as spy:
            n = self.jobs._auto_resolve_pending()
        self.assertEqual(n, 0)
        self.assertFalse(spy.called)

    def test_excepciones_no_rompen_el_lote(self):
        """Si el resolver falla en un host, sigue con los demás."""
        self.db.upsert_account("foo","alice",source="sherlock",
            source_site="foo.com", display_name="Foo", status="found")
        self.db.upsert_account("bar","alice",source="sherlock",
            source_site="bar.com", display_name="Bar", status="found")

        def _flaky(host, identifier, force_refresh=False):
            if host == "foo.com":
                raise RuntimeError("BOOM")
            return self._fake_resolve(host, identifier)

        with patch.object(self.resolver, "resolve", side_effect=_flaky):
            n = self.jobs._auto_resolve_pending()
        self.assertEqual(n, 1, "foo.com falló pero bar.com debe haberse procesado")
