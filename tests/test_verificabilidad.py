"""Paso 2C, Entrega 1: verificabilidad y confianza son ejes SEPARADOS.

El canario dejó de tocar `confidence`. Su veredicto vive en la columna nueva
`accounts.verifiability`, y la cadena que cerraba el paso 2B —inverificable →
low → "Descartar dudosas" → `not_mine`— ya no existe: afirmaba algo sobre la
PROPIEDAD de una cuenta a partir de una señal que no habla de propiedad.

Aquí se cubren las piezas que no son del canario en sí:
  - la columna nueva y su migración idempotente,
  - que la API la exponga,
  - que `discard-low` siga mirando SOLO `confidence`,
  - la reparación de un solo uso de las filas que 2B degradó.

El invariante fuerte ("el canario nunca modifica confidence", barrido de 54
filas) vive en `test_canario.py`, junto al módulo que lo tiene que cumplir.
"""
import sqlite3

from .helpers import IsolatedTestCase, auth_client


class TestColumnaVerificabilidad(IsolatedTestCase):

    def setUp(self):
        super().setUp()
        from rastrillo import db
        self.db = db
        db.init()

    def _cols(self):
        with self.db.connect() as con:
            return {r["name"] for r in
                    con.execute("PRAGMA table_info(accounts)").fetchall()}

    def test_la_columna_existe_en_una_db_nueva(self):
        self.assertIn("verifiability", self._cols())

    def test_migracion_idempotente(self):
        """`init()` varias veces no falla ni duplica la columna."""
        for _ in range(3):
            self.db.init()
        cols = [r for r in self._cols() if r == "verifiability"]
        self.assertEqual(len(cols), 1)

    def test_se_anade_a_una_db_vieja_sin_la_columna(self):
        """Simulamos una DB anterior a 2C: tabla sin `verifiability`."""
        with self.db.connect() as con:
            con.execute("DROP TABLE accounts")
            con.execute("""CREATE TABLE accounts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                platform TEXT NOT NULL, display_name TEXT, profile_url TEXT,
                identifier TEXT, source TEXT, source_site TEXT,
                status TEXT NOT NULL DEFAULT 'found', deletion_type TEXT,
                difficulty TEXT, current_step INTEGER DEFAULT 0,
                last_message TEXT, updated_at REAL, confidence TEXT)""")
            con.execute("INSERT INTO accounts (platform, identifier, confidence)"
                        " VALUES ('viejo','ana','high')")
        self.assertNotIn("verifiability", self._cols())
        self.db.init()
        self.assertIn("verifiability", self._cols())
        # La fila sobrevive y queda "no evaluada" (NULL), que NO es lo mismo
        # que `indeterminado`: es que el canario todavía no la ha mirado.
        row = self.db.get_account(1)
        self.assertEqual(row["confidence"], "high")
        self.assertIsNone(row["verifiability"])
        self.assertIs(self.db.VERIFICABILIDAD_NO_EVALUADA, None)

    def test_la_api_expone_verificabilidad(self):
        self.db.upsert_account("sitio", "ana", source="sherlock",
                               source_site="sitio.com", status="found",
                               confidence="high",
                               verifiability="indiscriminado")
        cli = auth_client()
        r = cli.get("/api/accounts", headers=self.hdr())
        self.assertEqual(r.status_code, 200)
        acc = r.json()["accounts"][0]
        self.assertEqual(acc["verifiability"], "indiscriminado")
        self.assertEqual(acc["confidence"], "high")

    def test_el_boot_lleva_las_etiquetas_de_verificabilidad(self):
        """El JS las necesita al arrancar; la única pieza dinámica del HTML es
        `window.__RASTRILLO_BOOT__` (ver `server._boot_script`)."""
        from rastrillo import server
        boot = server._boot_script()
        self.assertIn("VERIF_META", boot)
        self.assertIn("indiscriminado", boot)


class TestDiscardLowNoBarreInverificables(IsolatedTestCase):

    def setUp(self):
        super().setUp()
        from rastrillo import db
        self.db = db
        db.init()

    def _fila(self, ident, conf, verif=None, owned=0):
        return self.db.upsert_account(
            "sitio", ident, source="sherlock", source_site=f"{ident}.com",
            status="found", confidence=conf, verifiability=verif, owned=owned)

    def test_criterio_es_solo_confidence(self):
        alta_inverif = self._fila("steam", "high", "indiscriminado")
        media_inverif = self._fila("duolingo", "medium", "indiscriminado")
        baja_normal = self._fila("ana", "low", None)
        baja_verif = self._fila("corto", "low", "discrimina")

        cli = auth_client()
        r = cli.post("/api/accounts/discard-low", json={}, headers=self.hdr())
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["discarded"], 2,
                         "solo las dos `low`, con verificabilidad o sin ella")

        # Inverificables intactas: no verificable != no es mía.
        for aid in (alta_inverif, media_inverif):
            with self.subTest(fila=aid):
                self.assertEqual(self.db.get_account(aid)["status"], "found")
        for aid in (baja_normal, baja_verif):
            with self.subTest(fila=aid):
                self.assertEqual(self.db.get_account(aid)["status"], "not_mine")

    def test_no_hay_endpoint_de_descarte_masivo_de_inverificables(self):
        """La Entrega 1 pide vivir con la señal separada ANTES de dar una
        acción en lote sobre ella. Si alguien añade una, este test avisa."""
        from rastrillo.server import app
        rutas = {getattr(r, "path", "") for r in app.routes}
        for sospechosa in ("/api/accounts/discard-unverifiable",
                           "/api/accounts/discard-indiscriminado",
                           "/api/accounts/discard-inverificables"):
            self.assertNotIn(sospechosa, rutas)


class TestReparacionDeUnSoloUso(IsolatedTestCase):
    """La reparación de las filas que 2B bajó a `low`.

    Un reescaneo NO las recupera: `db.upsert_account` hace `return row["id"]`
    en cuanto la fila existe, así que el `confidence` recalculado por
    `discovery._register` nunca llega a escribirse. De ahí que haga falta un
    paso explícito.
    """

    def setUp(self):
        super().setUp()
        from rastrillo import canario, db
        self.c = canario
        self.db = db
        db.init()

    def _danada(self, ident, site, codes, conf="low", source="sherlock"):
        motivos = [{"code": c, "desc": c} for c in codes]
        return self.db.upsert_account(
            "sitio", ident, source=source, source_site=site, status="found",
            confidence=conf, profile_url=f"https://{site}/u/{ident}",
            confidence_reasons=self.db.dump_reasons(motivos))

    def test_el_reescaneo_no_recupera_el_tramo(self):
        """El supuesto que hay que descartar antes de justificar la migración:
        el upsert es no-op sobre una fila existente, `confidence` incluido."""
        aid = self._danada("je7remy", "steamcommunity.com",
                           ["tramo_distintivo", "bump_path",
                            "canario_indiscriminado"])
        # Segundo "descubrimiento" del mismo hit, con el tramo bien calculado.
        self.db.upsert_account("sitio", "je7remy", source="sherlock",
                               source_site="steamcommunity.com",
                               confidence="high")
        self.assertEqual(self.db.get_account(aid)["confidence"], "low",
                         "upsert_account no reaplica confidence")

    def test_por_defecto_no_escribe_nada(self):
        aid = self._danada("je7remy", "duolingo.com",
                           ["tramo_distintivo", "bump_path",
                            "canario_indiscriminado"])
        res = self.c.reparar_confianza_2b()
        self.assertFalse(res["aplicado"])
        self.assertEqual(res["filas"], 1)
        self.assertEqual(res["plan"][0]["a"], "high")
        self.assertEqual(self.db.get_account(aid)["confidence"], "low",
                         "sin --aplicar no se toca la DB")

    def test_restaura_el_tramo_desde_los_motivos(self):
        casos = {
            # (motivos)                                    -> tramo esperado
            ("tramo_distintivo", "bump_path"): "high",
            ("tramo_distintivo",): "high",
            ("tramo_corto", "bump_path"): "high",
            ("tramo_corto",): "medium",
            ("tramo_muy_corto", "bump_subdominio"): "medium",
            ("tramo_corto", "corrob_cruzada"): "high",
        }
        ids = {}
        for i, (codes, _) in enumerate(casos.items()):
            ids[codes] = self._danada(
                f"u{i}", f"s{i}.com", list(codes) + ["canario_indiscriminado"])

        res = self.c.reparar_confianza_2b(aplicar=True)
        self.assertTrue(res["aplicado"])
        for codes, esperado in casos.items():
            with self.subTest(motivos=codes):
                self.assertEqual(
                    self.db.get_account(ids[codes])["confidence"], esperado)

    def test_el_tramo_reconstruido_coincide_con_la_funcion_real(self):
        """La tabla `_TRAMO_POR_MOTIVO` no es una escala nueva: es la de
        `discovery._sherlock_confidence` leída al revés. Si allí cambia el
        mapeo motivo→tramo, este test se cae."""
        from rastrillo import discovery
        for username, url in (("je7remy", "https://s.com/u/je7remy"),
                              ("ana", "https://s.com/u/ana"),
                              ("jeremias", None),
                              ("carlos", "https://carlos.tumblr.com/")):
            tramo, motivos = discovery._sherlock_confidence(username, {"url": url})
            codes = [m["code"] for m in motivos]
            base = next(self.c._TRAMO_POR_MOTIVO[c] for c in codes
                        if c in self.c._TRAMO_POR_MOTIVO)
            for c in codes:
                if c in self.c._MOTIVOS_QUE_SUBEN:
                    base = discovery._subir_tramo(base)
            with self.subTest(username=username, url=url):
                self.assertEqual(base, tramo)

    def test_conserva_el_motivo_del_canario(self):
        """Sigue siendo evidencia válida: lo que cambia es que ya no mueve el
        tramo."""
        aid = self._danada("je7remy", "duolingo.com",
                           ["tramo_distintivo", "bump_path",
                            "canario_indiscriminado"])
        self.c.reparar_confianza_2b(aplicar=True)
        codes = {m["code"] for m in
                 self.db.parse_reasons(self.db.get_account(aid)["confidence_reasons"])}
        self.assertIn("canario_indiscriminado", codes)

    def test_es_idempotente(self):
        aid = self._danada("je7remy", "duolingo.com",
                           ["tramo_distintivo", "bump_path",
                            "canario_indiscriminado"])
        primera = self.c.reparar_confianza_2b(aplicar=True)
        self.assertEqual(primera["filas"], 1)
        segunda = self.c.reparar_confianza_2b(aplicar=True)
        self.assertEqual(segunda["filas"], 0, "tras la primera pasada, nada")
        self.assertEqual(self.db.get_account(aid)["confidence"], "high")

    def test_no_toca_lo_que_no_degrado_el_canario(self):
        low_legitima = self._danada("ana", "corto.com", ["tramo_muy_corto"])
        low_de_otro = self._danada("bob", "otro.com",
                                   ["tramo_muy_corto", "canario_discrimina"])
        holehe = self.db.upsert_account(
            "foro", "yo@example.com", source="holehe", source_site="foro.com",
            status="found", confidence="low",
            confidence_reasons=self.db.dump_reasons(
                [{"code": "canario_indiscriminado", "desc": "x"}]))
        alta = self._danada("carlos", "alta.com",
                            ["tramo_distintivo", "canario_indiscriminado"],
                            conf="high")

        res = self.c.reparar_confianza_2b(aplicar=True)
        self.assertEqual(res["filas"], 0)
        for aid in (low_legitima, low_de_otro, holehe):
            with self.subTest(fila=aid):
                self.assertEqual(self.db.get_account(aid)["confidence"], "low")
        self.assertEqual(self.db.get_account(alta)["confidence"], "high")

    def test_hace_snapshot_antes_de_escribir(self):
        self._danada("je7remy", "duolingo.com",
                     ["tramo_distintivo", "canario_indiscriminado"])
        backups = self.HOME / "backups"
        self.c.reparar_confianza_2b(aplicar=True)
        self.assertTrue(backups.exists())
        self.assertTrue(list(backups.glob("rastrillo_*.db")),
                        "debe quedar un snapshot antes de tocar datos")

    def test_db_vacia_no_revienta(self):
        res = self.c.reparar_confianza_2b(aplicar=True)
        self.assertEqual(res["filas"], 0)

    def test_el_cli_expone_el_subcomando(self):
        import cli as cli_mod
        args = cli_mod.build_parser().parse_args(["reparar-confianza"])
        self.assertFalse(args.aplicar)
        args = cli_mod.build_parser().parse_args(["reparar-confianza", "--aplicar"])
        self.assertTrue(args.aplicar)


if __name__ == "__main__":
    import unittest
    unittest.main()
