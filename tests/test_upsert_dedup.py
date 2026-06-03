"""Regresión: `upsert_account` con la nueva unicidad (source_site, identifier)
NO debe colapsar dos sitios distintos que casen con la misma receta.

El bug original: con UNIQUE(platform, identifier), "Reddit" y "RedditGifts"
matcheaban ambos al slug "reddit" y se fusionaban en una sola fila.
"""
from .helpers import IsolatedTestCase


class TestUpsertDedup(IsolatedTestCase):

    def setUp(self):
        super().setUp()
        from rastrillo import db
        db.init()
        # Mantenemos una referencia local al módulo en el test (cada test
        # subclase tiene su sandbox; reusar el módulo es seguro).
        self.db = db

    def _upsert(self, platform, identifier, source_site, **extra):
        return self.db.upsert_account(
            platform, identifier,
            source_site=source_site,
            source=extra.pop("source", "sherlock"),
            display_name=extra.pop("display_name", platform),
            status=extra.pop("status", "found"),
            **extra,
        )

    def test_misma_receta_dos_hosts_dos_filas(self):
        """Dos hits que mapean al mismo slug pero con source_site distinto
        deben convivir como filas independientes (el caso Reddit/RedditGifts)."""
        a = self._upsert("reddit", "alice", "reddit.com",      display_name="Reddit")
        b = self._upsert("reddit", "alice", "redditgifts.com", display_name="RedditGifts")
        c = self._upsert("reddit", "alice", "old.reddit.com",  display_name="Reddit (old)")
        ids = {a, b, c}
        self.assertEqual(len(ids), 3, "esperaba 3 filas distintas")
        rows = list(self.db.list_accounts())
        sites = sorted(r["source_site"] for r in rows)
        self.assertEqual(sites, ["old.reddit.com", "reddit.com", "redditgifts.com"])

    def test_mismo_source_site_no_duplica(self):
        """Misma (source_site, identifier) en upsert → devuelve el id existente,
        sin crear fila nueva."""
        a = self._upsert("reddit", "alice", "reddit.com")
        b = self._upsert("reddit", "alice", "reddit.com",
                          display_name="ignored")   # NO cambia los campos al re-upsert
        self.assertEqual(a, b)
        self.assertEqual(len(list(self.db.list_accounts())), 1)

    def test_distintos_identifiers_distintas_filas(self):
        """Mismo host pero distintos usuarios = distintas cuentas."""
        a = self._upsert("reddit", "alice", "reddit.com")
        b = self._upsert("reddit", "bob",   "reddit.com")
        self.assertNotEqual(a, b)
        self.assertEqual(len(list(self.db.list_accounts())), 2)

    def test_sin_source_site_usa_fallback_legacy(self):
        """Sin source_site, la unicidad cae al par (platform, identifier).
        Útil para inserts manuales / tests."""
        a = self.db.upsert_account("manual_one", "u", source="manual")
        b = self.db.upsert_account("manual_one", "u", source="manual")
        self.assertEqual(a, b)
        self.assertEqual(len(list(self.db.list_accounts())), 1)

    def test_no_unique_constraint_sql(self):
        """La tabla NO debe tener UNIQUE(platform, identifier): toda la lógica
        de unicidad vive en código."""
        with self.db.connect() as con:
            ddl = con.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='accounts'"
            ).fetchone()["sql"]
        self.assertNotIn(
            "UNIQUE", ddl.upper(),
            "la tabla volvió a tener UNIQUE; el bug Reddit/RedditGifts puede regresar",
        )
