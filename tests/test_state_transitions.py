"""Invariante: la máquina de estados respeta las reglas del Nivel 1/2/3.

  - Acciones destructivas (delete/anonymize/retry/mark-sent) sobre cuenta
    no propietaria → 412 con snapshot.
  - confirm_owned=true marca owned=1 y procede.
  - own True/False mueve a found/not_mine.
  - keep → skipped.
  - mark-sent en email_draft → user_done + sent_at.
  - dry-run no encola y deja status='dry_run'.
  - process-all-auto solo toca owned=1 y status=found.
  - not_mine no se procesa por process-all-auto.
  - Token: POST sin token → 401, con token → 200.
  - Token: GET /api/* sin token → 401, con token → 200 (Tarea 3).
  - GET / (HTML shell) sigue libre porque el navegador entra sin header.
  - Host: header Host no en config.ALLOWED_HOSTS → 403 (anti DNS-rebinding).
"""
from .helpers import IsolatedTestCase, auth_client


class TestStateTransitions(IsolatedTestCase):

    def setUp(self):
        super().setUp()
        from rastrillo import db, config
        self.db = db
        self.config = config
        db.init()
        self.client = auth_client()

    # ── auth ──
    def test_post_sin_token_es_401(self):
        r = self.client.post("/api/scan", json={"usernames":["x"]})
        self.assertEqual(r.status_code, 401)

    def test_get_api_sin_token_es_401(self):
        """Tarea 3: los GET de /api/* devuelven PII (cuentas, reports) y por
        tanto exigen token. Antes eran libres; ya no."""
        self.assertEqual(self.client.get("/api/accounts").status_code, 401)

    def test_get_api_con_token_es_200(self):
        self.assertEqual(
            self.client.get("/api/accounts", headers=self.hdr()).status_code,
            200,
        )

    def test_get_root_no_requiere_token(self):
        """GET / (HTML shell) sigue libre: el navegador entra sin header en
        la primera carga; el JS lee `?token=` y lo manda en /api/*."""
        self.assertEqual(self.client.get("/").status_code, 200)

    def test_host_no_permitido_es_403(self):
        """Anti DNS-rebinding: aunque venga el token, un Host fuera de la
        allowlist se rechaza antes."""
        r = self.client.get("/api/accounts", headers={
            **self.hdr(),
            "Host": "evil.example.com",
        })
        self.assertEqual(r.status_code, 403)

    # ── triage ──
    def test_own_true_marca_owned(self):
        aid = self.db.upsert_account("reddit", "alice",
            source="sherlock", source_site="reddit.com",
            display_name="Reddit", status="found", confidence="medium")
        r = self.client.post(f"/api/accounts/{aid}/own",
                             json={"owned": True}, headers=self.hdr())
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.db.get_account(aid)["owned"], 1)
        self.assertEqual(self.db.get_account(aid)["status"], "found")

    def test_own_false_marca_not_mine(self):
        aid = self.db.upsert_account("reddit", "alice",
            source="sherlock", source_site="reddit.com",
            display_name="Reddit", status="found", confidence="low")
        r = self.client.post(f"/api/accounts/{aid}/own",
                             json={"owned": False}, headers=self.hdr())
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.db.get_account(aid)["status"], "not_mine")

    def test_discard_low_en_lote(self):
        # 2 low + 1 high → solo se descartan las low
        self.db.upsert_account("a","u1",source="sherlock",source_site="a.com",
            display_name="A",status="found",confidence="low")
        self.db.upsert_account("b","u2",source="sherlock",source_site="b.com",
            display_name="B",status="found",confidence="low")
        self.db.upsert_account("c","u3",source="sherlock",source_site="c.com",
            display_name="C",status="found",confidence="high")
        r = self.client.post("/api/accounts/discard-low", json={}, headers=self.hdr())
        self.assertEqual(r.json()["discarded"], 2)
        statuses = sorted(row["status"] for row in self.db.list_accounts())
        self.assertEqual(statuses, ["found", "not_mine", "not_mine"])

    # ── preflight ──
    def test_action_destructiva_sin_owned_es_412(self):
        aid = self.db.upsert_account("reddit","alice",source="sherlock",
            source_site="reddit.com", display_name="Reddit", status="found")
        r = self.client.post(f"/api/accounts/{aid}/action",
                             json={"action":"delete"}, headers=self.hdr())
        self.assertEqual(r.status_code, 412)
        detail = r.json()["detail"]
        self.assertEqual(detail["code"], "needs_ownership_confirmation")
        self.assertEqual(detail["account"]["id"], aid)
        # NO encolada
        self.assertEqual(self.db.get_account(aid)["status"], "found")

    def test_action_con_confirm_owned_procede(self):
        aid = self.db.upsert_account("reddit","alice",source="sherlock",
            source_site="reddit.com", display_name="Reddit", status="found")
        r = self.client.post(f"/api/accounts/{aid}/action",
                             json={"action":"delete","confirm_owned":True},
                             headers=self.hdr())
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.db.get_account(aid)["owned"], 1)

    def test_keep_pasa_a_skipped(self):
        aid = self.db.upsert_account("reddit","alice",source="sherlock",
            source_site="reddit.com", display_name="Reddit", status="found")
        r = self.client.post(f"/api/accounts/{aid}/action",
                             json={"action":"keep"}, headers=self.hdr())
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.db.get_account(aid)["status"], "skipped")

    # ── mark-sent ──
    def test_mark_sent_desde_email_draft_guarda_sent_at(self):
        aid = self.db.upsert_account("baby","alice",source="sherlock",
            source_site="baby.ru", display_name="Baby",
            status="email_draft", owned=1)
        r = self.client.post(f"/api/accounts/{aid}/mark-sent",
                             json={"action":"mark-sent"}, headers=self.hdr())
        self.assertEqual(r.status_code, 200)
        row = self.db.get_account(aid)
        self.assertEqual(row["status"], "user_done")
        self.assertIsNotNone(row["sent_at"])

    def test_mark_sent_no_email_draft_no_guarda_sent_at(self):
        """Si la cuenta no venía de email_draft (p.ej. semi_auto), mark-sent
        la mueve a user_done pero no guarda sent_at (no es un envío GDPR)."""
        aid = self.db.upsert_account("foo","alice",source="sherlock",
            source_site="foo.com", display_name="Foo",
            status="semi_auto", owned=1)
        r = self.client.post(f"/api/accounts/{aid}/mark-sent",
                             json={"action":"mark-sent"}, headers=self.hdr())
        self.assertEqual(r.status_code, 200)
        row = self.db.get_account(aid)
        self.assertEqual(row["status"], "user_done")
        self.assertIsNone(row["sent_at"])

    def test_mark_sent_sin_owned_es_412(self):
        aid = self.db.upsert_account("baby","alice",source="sherlock",
            source_site="baby.ru", display_name="Baby", status="email_draft")
        r = self.client.post(f"/api/accounts/{aid}/mark-sent",
                             json={"action":"mark-sent"}, headers=self.hdr())
        self.assertEqual(r.status_code, 412)

    # ── dry-run ──
    def test_dry_run_no_encola(self):
        # Activamos dry-run y disparamos delete sobre cuenta con receta
        self.client.post("/api/dry-run", json={"enabled":True}, headers=self.hdr())
        aid = self.db.upsert_account("reddit","alice",source="sherlock",
            source_site="reddit.com", display_name="Reddit",
            status="found", owned=1)
        r = self.client.post(f"/api/accounts/{aid}/action",
                             json={"action":"delete"}, headers=self.hdr())
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["status"], "dry_run")
        self.assertEqual(self.db.get_account(aid)["status"], "dry_run")
        # Restauramos para no contaminar tests siguientes
        self.client.post("/api/dry-run", json={"enabled":False}, headers=self.hdr())

    # ── process-all-auto ──
    def test_process_all_auto_solo_toca_owned_found(self):
        # 1 owned+found (con receta), 1 unowned+found, 1 owned+done → solo 1 encolada
        owned_found = self.db.upsert_account("reddit","alice",source="sherlock",
            source_site="reddit.com", display_name="Reddit",
            status="found", owned=1)
        unowned = self.db.upsert_account("foo","bob",source="sherlock",
            source_site="foo.com", display_name="Foo",
            status="found", owned=0)
        owned_done = self.db.upsert_account("tumblr","carol",source="sherlock",
            source_site="tumblr.com", display_name="Tumblr",
            status="deleted", owned=1)
        r = self.client.post("/api/accounts/process-all-auto",
                             json={}, headers=self.hdr())
        s = r.json()
        self.assertEqual(s["visited"], 1)
        self.assertEqual(s["queued"], 1)
        self.assertEqual(s["skipped_unowned"], 1)
        # owned_done no aparece en visited (status != found)
        self.assertEqual(self.db.get_account(unowned)["status"], "found")
        self.assertEqual(self.db.get_account(owned_done)["status"], "deleted")
        self.assertEqual(self.db.get_account(owned_found)["status"], "queued")

    def test_process_all_auto_ignora_not_mine(self):
        self.db.upsert_account("a","u",source="sherlock",
            source_site="a.com", display_name="A",
            status="not_mine", owned=0)
        r = self.client.post("/api/accounts/process-all-auto",
                             json={}, headers=self.hdr())
        self.assertEqual(r.json()["visited"], 0)
