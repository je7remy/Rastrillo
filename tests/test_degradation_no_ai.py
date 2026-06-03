"""Invariante: sin ANTHROPIC_API_KEY, NINGUNA cuenta queda sin acción concreta.

Cubre el caso más exigente: el resolver debe degradar elegantemente a
semi_auto (con link del directorio o probe) o a email_draft (GDPR fallback).
"""
from unittest.mock import patch
from .helpers import IsolatedTestCase, auth_client


class TestDegradationNoAI(IsolatedTestCase):

    def setUp(self):
        super().setUp()
        from rastrillo import db, ai_assist, directory, resolver
        self.db = db
        self.ai_assist = ai_assist
        self.directory = directory
        self.resolver = resolver
        self.client = auth_client()
        db.init()
        # Confirmamos que IA está desactivada
        self.assertFalse(self.ai_assist.available(),
                         "este test asume sin ANTHROPIC_API_KEY")

    def _purge_caches(self):
        if self.resolver.DISCOVERED_PATH.exists():
            self.resolver.DISCOVERED_PATH.unlink()

    def test_host_en_directorio_es_semi_auto(self):
        self.directory._loaded = self.directory.Directory(
            [{"name":"Amazon","url":"https://amazon.com/help/close",
              "difficulty":"medium","notes":"",
              "domains":["amazon.com"]}],
            source="fallback", fetched_at=None,
        )
        self._purge_caches()
        res = self.resolver.resolve("amazon.com", "alice@example.com")
        self.assertEqual(res.kind, "semi_auto")
        self.assertTrue(res.url.startswith("https://"))

    def test_host_no_directorio_cae_a_email_draft(self):
        """Sin directorio, sin IA y sin probe hits → GDPR fallback siempre."""
        self.directory._loaded = self.directory.Directory(
            [], source="fallback", fetched_at=None,
        )
        self._purge_caches()
        with patch.object(self.resolver, "_probe_paths", return_value=[]), \
             patch.object(self.resolver, "_find_privacy_emails", return_value=[]):
            res = self.resolver.resolve("desconocido.example", "alice")
        self.assertEqual(res.kind, "email_draft")
        self.assertTrue(res.email_to)
        self.assertTrue(res.email_subject)
        self.assertTrue(res.email_body)

    def test_lote_de_hosts_todos_accionables(self):
        """Para una muestra de 5 hosts heterogéneos, NINGUNO queda sin acción."""
        self.directory._loaded = self.directory.Directory(
            [{"name":"Amazon","url":"https://amazon.com/help","difficulty":"easy",
              "domains":["amazon.com"]}],
            source="fallback", fetched_at=None,
        )
        self._purge_caches()
        hosts = [
            "amazon.com",            # en directorio
            "muyrara-xyz123.test",   # no en directorio, raro
            "baby.ru",               # ruso, no en directorio
            "fake-no-existe.org",    # genérico
            "site.de",               # alemán
        ]
        with patch.object(self.resolver, "_probe_paths", return_value=[]), \
             patch.object(self.resolver, "_find_privacy_emails", return_value=[]):
            for host in hosts:
                res = self.resolver.resolve(host, "alice")
                self.assertIn(res.kind, ("auto","semi_auto","email_draft"),
                              f"{host}: kind inválido {res.kind!r}")
                self.assertTrue(
                    res.url or res.email_to,
                    f"{host}: resolución sin acción concreta",
                )

    def test_endpoint_action_sin_ia_pasa_por_resolver(self):
        """El endpoint `/action` sobre una cuenta NO en directorio devuelve
        una de las kinds del resolver (no ahoga al usuario en un loop)."""
        self.directory._loaded = self.directory.Directory(
            [], source="fallback", fetched_at=None,
        )
        self._purge_caches()
        aid = self.db.upsert_account(
            "desconocido", "alice", source="sherlock",
            source_site="desconocido.example", display_name="Desconocido",
            status="found", owned=1, confidence="high")
        with patch("rastrillo.resolver._probe_paths", return_value=[]), \
             patch("rastrillo.resolver._find_privacy_emails", return_value=[]):
            r = self.client.post(f"/api/accounts/{aid}/action",
                                 json={"action":"delete"}, headers=self.hdr())
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        # Sin IA, no debería ser status=queued (ese sería kind=auto, requiere IA o receta)
        # Para este host inventado caerá a email_draft via GDPR.
        self.assertEqual(body["status"], "email_draft")
        row = self.db.get_account(aid)
        self.assertEqual(row["status"], "email_draft")
        self.assertTrue(row["action_meta"], "action_meta vacío tras resolver")

    def test_hibp_sin_clave_skip_silencioso(self):
        """HIBP sin RASTRILLO_HIBP_API_KEY debe responder skipped=True."""
        from rastrillo import hibp
        self.assertFalse(hibp.available())
        r = hibp.run_hibp("alice@example.com")
        self.assertTrue(r["skipped"])
        self.assertEqual(r["raw_count"], 0)
        self.assertIsNone(r["error"])
