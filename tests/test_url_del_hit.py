"""Paso 2B, Entrega 1: los dos arreglos que salieron del escaneo real.

1.1 — `@ ~ #` faltaban en `_SEPARADORES_URL`. La fila de TikTok
     (`https://www.tiktok.com/@usuario`) no obtenía el motivo "coincide en la
     ruta" porque el `@` previo no validaba como frontera de segmento. Con un
     username corto eso cuesta un tramo entero.

1.2 — El triage enseñaba, bajo la etiqueta "perfil", la URL de BORRADO que
     había escrito el resolver encima de `profile_url`. Los chips de motivo se
     calcularon sobre la URL del HIT, así que a ojo parecía que el chip mentía.
     Ahora `profile_url` es la del hit y la de borrado va aparte
     (`deletion_url`, computada desde `action_meta`).
"""
import json
from unittest.mock import patch
from .helpers import IsolatedTestCase, auth_client


class TestSeparadoresDeFrontera(IsolatedTestCase):

    def setUp(self):
        super().setUp()
        from rastrillo import discovery
        self.d = discovery

    def _codes(self, username, url):
        _, motivos = self.d._sherlock_confidence(username, {"url": url})
        return {m["code"] for m in motivos}

    # --- 1. `@` como frontera (el caso de TikTok) --------------------------
    def test_arroba_es_frontera(self):
        codes = self._codes("je7remy", "https://www.tiktok.com/@je7remy")
        self.assertIn("bump_path", codes)

    def test_arroba_con_username_corto_sube_de_tramo(self):
        """Donde de verdad importa: con `ana` (low de base) el bump es la
        diferencia entre `low` y `medium`."""
        tramo, motivos = self.d._sherlock_confidence(
            "ana", {"url": "https://www.tiktok.com/@ana"})
        self.assertEqual(tramo, "medium")
        self.assertIn("bump_path", {m["code"] for m in motivos})

    # --- 2. `#` como frontera (fragmento) ----------------------------------
    def test_almohadilla_es_frontera_y_el_fragmento_cuenta(self):
        """`urlsplit` saca el fragmento del path; antes se perdía entero. Hay
        SPAs que publican el perfil ahí."""
        codes = self._codes("jeremy", "https://site.com/#/user/jeremy")
        self.assertIn("bump_path", codes)
        codes = self._codes("jeremy", "https://site.com/perfil#jeremy")
        self.assertIn("bump_path", codes)

    def test_almohadilla_no_afloja_la_frontera(self):
        """El fragmento entra al haystack, pero con las mismas reglas: a media
        palabra sigue sin contar."""
        codes = self._codes("mar", "https://site.com/x#marketing-2026")
        self.assertNotIn("bump_path", codes)

    # --- 3. `~` (hostings personales clásicos) -----------------------------
    def test_tilde_es_frontera(self):
        codes = self._codes("jeremy", "https://site.edu/~jeremy/")
        self.assertIn("bump_path", codes)

    # --- 4. Los casos de 2A siguen verdes ----------------------------------
    def test_los_falsos_positivos_de_2a_siguen_cortados(self):
        """Los separadores nuevos no aflojan nada de lo que cerró el paso 2A."""
        for u, url in (("ana", "https://banana.com/u/xyz"),
                       ("mar", "https://site.com/marca-noticias/123"),
                       ("ana", "https://banana.tumblr.com/"),
                       ("mar", "https://site.com/smartlist/user/99")):
            with self.subTest(u=u, url=url):
                codes = self._codes(u, url)
                self.assertNotIn("bump_path", codes)
                self.assertNotIn("bump_subdominio", codes)


class TestUrlDelHitEnElTriage(IsolatedTestCase):

    def setUp(self):
        super().setUp()
        from rastrillo import db, jobs, resolver, server
        self.db = db
        self.jobs = jobs
        self.resolver = resolver
        self.server = server
        db.init()

    _URL_HIT = "https://www.freelancer.in/u/jeremy"
    _URL_BORRADO = ("https://www.freelancer.in/users/settings.php"
                    "#AccountSettings")

    def _cuenta(self, profile_url=_URL_HIT):
        return self.db.upsert_account(
            "freelancer", "jeremy", source="sherlock",
            source_site="freelancer.in", display_name="Freelancer",
            status="found", confidence="high", profile_url=profile_url)

    def _resolution(self):
        return self.resolver.Resolution(
            kind="semi_auto", layer="directory", title="Cerrar cuenta",
            notes="", url=self._URL_BORRADO, language="en")

    def _fake_resolve(self, host, identifier, force_refresh=False):
        return self._resolution()

    # --- El auto-resolver ya no pisa la URL del hit ------------------------
    def test_auto_resolver_no_pisa_la_url_del_hit(self):
        aid = self._cuenta()
        with patch.object(self.resolver, "resolve", side_effect=self._fake_resolve):
            self.jobs._auto_resolve_pending()
        row = self.db.get_account(aid)
        self.assertEqual(row["profile_url"], self._URL_HIT)
        # La de borrado no se pierde: vive en action_meta.
        self.assertEqual(json.loads(row["action_meta"])["url"], self._URL_BORRADO)

    def test_backfill_si_la_fila_no_tenia_url(self):
        """holehe y hibp nunca traen URL: para esas filas la del resolver es
        la única que hay, así que sí se rellena."""
        aid = self.db.upsert_account(
            "foro", "yo@example.com", source="holehe", source_site="foro.com",
            display_name="Foro", status="found", confidence="high")
        with patch.object(self.resolver, "resolve", side_effect=self._fake_resolve):
            self.jobs._auto_resolve_pending()
        self.assertEqual(self.db.get_account(aid)["profile_url"],
                         self._URL_BORRADO)

    def test_backfill_profile_url_unitario(self):
        aid = self._cuenta()
        self.assertFalse(self.db.backfill_profile_url(aid, "https://otra.com/x"))
        self.assertEqual(self.db.get_account(aid)["profile_url"], self._URL_HIT)
        vacia = self.db.upsert_account("x", "y", source="holehe",
                                       source_site="x.com", status="found")
        self.assertTrue(self.db.backfill_profile_url(vacia, "https://otra.com/x"))
        self.assertFalse(self.db.backfill_profile_url(vacia, None))

    # --- El payload de /api/accounts expone ambas --------------------------
    def test_el_payload_expone_la_url_del_hit_y_la_de_borrado(self):
        aid = self._cuenta()
        self.db.update_account(aid, action_meta=json.dumps(
            self._resolution().to_meta(), ensure_ascii=False))
        cli = auth_client()
        r = cli.get("/api/accounts", headers=self.hdr())
        self.assertEqual(r.status_code, 200)
        fila = next(a for a in r.json()["accounts"] if a["id"] == aid)
        self.assertEqual(fila["profile_url"], self._URL_HIT,
                         "el enlace 'perfil' debe ser la URL del hit")
        self.assertEqual(fila["deletion_url"], self._URL_BORRADO,
                         "la de borrado va aparte, con otra etiqueta")

    def test_deletion_url_none_si_coincide_o_no_hay(self):
        f = self.server._deletion_url
        self.assertIsNone(f(None, self._URL_HIT))
        self.assertIsNone(f("", self._URL_HIT))
        self.assertIsNone(f("{no es json", self._URL_HIT))
        self.assertIsNone(f('"una cadena"', self._URL_HIT))
        self.assertIsNone(f('{"kind":"email_draft"}', self._URL_HIT))
        # Misma URL en ambos sitios: no pintamos el enlace dos veces.
        self.assertIsNone(f(json.dumps({"url": self._URL_HIT}), self._URL_HIT))
        self.assertEqual(f(json.dumps({"url": self._URL_BORRADO}), self._URL_HIT),
                         self._URL_BORRADO)

    # --- Y el mismo criterio en las otras dos escrituras -------------------
    def test_apply_resolution_no_pisa_la_url_del_hit(self):
        aid = self._cuenta()
        self.server._apply_resolution(aid, self._resolution())
        row = self.db.get_account(aid)
        self.assertEqual(row["profile_url"], self._URL_HIT)
        self.assertEqual(row["status"], "semi_auto")

    def test_engine_no_pisa_la_url_del_hit(self):
        """La CUARTA escritura: `Engine.run_account` por la rama sin receta.
        Dejarla pisando era un bug latente — el resto de caminos respetan la
        URL del hit y este no. El bucle de IA no depende de `profile_url`
        (usa `res.url` directo), así que no hace falta tocar nada más.
        """
        from rastrillo import engine
        aid = self._cuenta()
        eng = engine.Engine(headless=True)
        # kind=semi_auto: `run_account` persiste y vuelve sin abrir Chromium.
        with patch.object(engine.resolver, "resolve",
                          side_effect=self._fake_resolve):
            eng.run_account(aid)
        row = self.db.get_account(aid)
        self.assertEqual(row["status"], "semi_auto")
        self.assertEqual(row["profile_url"], self._URL_HIT)
        self.assertEqual(json.loads(row["action_meta"])["url"], self._URL_BORRADO)

    def test_engine_backfill_si_la_fila_no_tenia_url(self):
        from rastrillo import engine
        aid = self.db.upsert_account(
            "foro", "yo@example.com", source="holehe", source_site="foro.com",
            display_name="Foro", status="found", confidence="high")
        eng = engine.Engine(headless=True)
        with patch.object(engine.resolver, "resolve",
                          side_effect=self._fake_resolve):
            eng.run_account(aid)
        self.assertEqual(self.db.get_account(aid)["profile_url"],
                         self._URL_BORRADO)

    def test_process_all_auto_no_pisa_la_url_del_hit(self):
        aid = self._cuenta()
        self.db.update_account(aid, owned=1)
        cli = auth_client()
        with patch.object(self.resolver, "resolve", side_effect=self._fake_resolve):
            r = cli.post("/api/accounts/process-all-auto", json={},
                         headers=self.hdr())
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.db.get_account(aid)["profile_url"], self._URL_HIT)
