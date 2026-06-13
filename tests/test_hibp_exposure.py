"""Parte 1 del Nivel 6: HIBP como 'exposición en brecha', no cuenta activa.

  - confidence de HIBP es 'medium', no 'high'
  - auto-resolver salta las filas source='hibp'
  - process-all-auto salta las filas source='hibp'
  - endpoint confirm-account convierte la fila a source='hibp_confirmed'
    y la hace candidata normal
  - UI: filtro 'exposure', helper isExposure, botón "Sí tengo cuenta",
    banner explicativo
"""
import json
from unittest.mock import patch
from .helpers import IsolatedTestCase, auth_client


def _fake_breach(*args, **kwargs):
    """Mock de urllib.request.urlopen — acepta cualquier firma."""
    payload = [{"Name":"LinkedIn","Title":"LinkedIn","Domain":"linkedin.com",
                "BreachDate":"2012-05-05","PwnCount":1}]
    class _Resp:
        def read(self_): return json.dumps(payload).encode()
        def __enter__(self_): return self_
        def __exit__(self_, *a): pass
    return _Resp()


class TestHIBPConfidence(IsolatedTestCase):
    def setUp(self):
        super().setUp()
        from rastrillo import config, db, discovery, hibp
        self.config = config
        self.db = db
        self.discovery = discovery
        self.hibp = hibp
        db.init()

    def test_confidence_es_medium(self):
        """Sembramos un hit de HIBP vía discover() con un mock; la cuenta
        creada debe tener confidence='medium', no 'high'."""
        self.config.HIBP_API_KEY = "fake"
        empty = {"hits":[], "error":None, "incomplete":False, "raw_count":0}
        with patch.object(self.discovery, "run_sherlock", return_value=empty), \
             patch.object(self.discovery, "run_holehe",   return_value=empty), \
             patch.object(self.discovery, "run_maigret",  return_value={**empty,"skipped":True}), \
             patch("rastrillo.hibp.urllib.request.urlopen", side_effect=_fake_breach):
            self.discovery.discover([], ["alice@example.com"])
        rows = [r for r in self.db.list_accounts() if r["source"]=="hibp"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["confidence"], "medium",
            f"HIBP debe ser medium, no {rows[0]['confidence']!r}")


class TestHIBPExcluidoDeAutoResolver(IsolatedTestCase):
    def setUp(self):
        super().setUp()
        from rastrillo import db, jobs
        self.db = db
        self.jobs = jobs
        db.init()

    def test_candidates_excluye_hibp(self):
        """`_candidates_to_resolve` no devuelve cuentas con source='hibp'."""
        self.db.upsert_account("linkedin", "alice@example.com", source="hibp",
            source_site="linkedin.com", display_name="LinkedIn",
            status="found", confidence="medium")
        # Usamos un slug sin receta del paquete: el helper excluye las que
        # tienen receta determinista (no necesitan resolver).
        self.db.upsert_account("fakesite", "alice", source="sherlock",
            source_site="fakesite.example", display_name="FakeSite",
            status="found", confidence="medium")
        cands = self.jobs._candidates_to_resolve()
        platforms = [c["platform"] for c in cands]
        self.assertNotIn("linkedin", platforms,
            "HIBP no debe ser candidato a auto-resolver")
        self.assertIn("fakesite", platforms)

    def test_auto_resolver_no_toca_hibp(self):
        """Ejecutar _auto_resolve_pending no debe llamar a resolver para HIBP."""
        from rastrillo.resolver import Resolution
        self.db.upsert_account("linkedin", "alice@example.com", source="hibp",
            source_site="linkedin.com", display_name="LinkedIn",
            status="found", confidence="medium")
        called = []
        def _fake(host, ident, force_refresh=False):
            called.append(host)
            return Resolution(kind="semi_auto", layer="probe",
                              title=host, url=f"https://{host}/", language="en")
        with patch("rastrillo.resolver.resolve", side_effect=_fake):
            self.jobs._auto_resolve_pending()
        self.assertEqual(called, [],
            f"resolver no debería llamarse para HIBP, pero se llamó con {called}")
        # action_meta de la fila HIBP sigue None
        row = self.db.get_account(1)
        self.assertIsNone(row["action_meta"])


class TestProcessAllAutoExcluyeHIBP(IsolatedTestCase):
    def setUp(self):
        super().setUp()
        from rastrillo import db
        self.db = db
        db.init()
        self.client = auth_client()

    def test_skipped_exposure(self):
        """Con 1 HIBP owned + 1 sherlock owned + receta, process-all-auto
        solo encola la sherlock; la HIBP aparece en skipped_exposure."""
        self.db.upsert_account("linkedin", "alice@example.com", source="hibp",
            source_site="linkedin.com", display_name="LinkedIn",
            status="found", owned=1, confidence="medium")
        self.db.upsert_account("reddit", "alice", source="sherlock",
            source_site="reddit.com", display_name="Reddit",
            status="found", owned=1, confidence="high")
        r = self.client.post("/api/accounts/process-all-auto", json={}, headers=self.hdr())
        s = r.json()
        self.assertEqual(s["queued"], 1)
        self.assertEqual(s.get("skipped_exposure", 0), 1)
        # La HIBP sigue como found, no encolada
        rows = {r["platform"]: r for r in self.db.list_accounts()}
        self.assertEqual(rows["linkedin"]["status"], "found")
        self.assertEqual(rows["reddit"]["status"], "queued")


class TestConfirmAccount(IsolatedTestCase):
    def setUp(self):
        super().setUp()
        from rastrillo import db
        self.db = db
        db.init()
        self.client = auth_client()

    def test_confirm_promueve_source(self):
        aid = self.db.upsert_account("linkedin", "alice@example.com",
            source="hibp", source_site="linkedin.com",
            display_name="LinkedIn", status="found", confidence="medium")
        r = self.client.post(f"/api/accounts/{aid}/confirm-account",
                             json={}, headers=self.hdr())
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["source"], "hibp_confirmed")
        row = self.db.get_account(aid)
        self.assertEqual(row["source"], "hibp_confirmed")

    def test_confirm_no_aplica_a_no_hibp(self):
        aid = self.db.upsert_account("reddit", "alice", source="sherlock",
            source_site="reddit.com", display_name="Reddit", status="found")
        r = self.client.post(f"/api/accounts/{aid}/confirm-account",
                             json={}, headers=self.hdr())
        self.assertEqual(r.status_code, 409)

    def test_tras_confirmar_pasa_a_candidato_normal(self):
        """Una vez confirmada, _candidates_to_resolve la incluye."""
        from rastrillo import jobs
        aid = self.db.upsert_account("linkedin", "alice@example.com",
            source="hibp", source_site="linkedin.com",
            display_name="LinkedIn", status="found", confidence="medium")
        self.client.post(f"/api/accounts/{aid}/confirm-account",
                         json={}, headers=self.hdr())
        cands = jobs._candidates_to_resolve()
        self.assertEqual(len(cands), 1)
        self.assertEqual(cands[0]["platform"], "linkedin")


class TestUIExposure(IsolatedTestCase):
    def setUp(self):
        super().setUp()
        self.client = auth_client()

    def test_html_tiene_filtro_y_helper(self):
        # Desde Tarea 10 el JS vive en /static/app.js; el HTML solo tiene
        # la estructura. Buscamos los marcadores donde realmente residen.
        html = self.client.get("/").text
        app_js = self.client.get("/static/app.js").text
        self.assertIn('data-f="exposure"', html)        # botón filtro: HTML
        self.assertIn("isExposure", app_js)             # helper JS
        self.assertIn("confirmAccount", app_js)         # función JS
        self.assertIn("/api/accounts/", app_js)         # rutas API que usa el JS
        self.assertIn("confirm-account", app_js)
        # Texto del banner explicativo (vive en el JS que lo inyecta).
        self.assertIn("No son cuentas confirmadas".lower(),
                      app_js.lower().replace("estas no son cuentas confirmadas",
                                             "no son cuentas confirmadas"))
        # El botón "Sí, tengo cuenta" está en el JS
        self.assertIn("tengo cuenta", app_js)
