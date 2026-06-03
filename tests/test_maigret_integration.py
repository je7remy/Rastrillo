"""Nivel 8 Parte 2: maigret como motor adicional de discovery.

Cubre:
  - sin binario instalado → skipped, sin hits, sin crash
  - mock con JSON sintético → solo entradas Claimed entran como hits
  - dos schemas de status (dict y string) en el mismo run
  - discover(): raw_counts incluye maigret/maigret_saved
  - discover(): KEEP_PLATFORMS no entran como found
  - discover(): dedup por (source_site, identifier) funciona
  - discover(): si maigret revienta, sherlock/holehe siguen
"""
import json
import subprocess
import os
from pathlib import Path
from unittest.mock import patch
from .helpers import IsolatedTestCase


def _fake_proc(cmd, **_kw):
    return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")


def _write_maigret_json(folder, username, payload):
    Path(folder, f"{username}.json").write_text(
        json.dumps(payload), encoding="utf-8")


class TestRunMaigretSinBinario(IsolatedTestCase):

    def setUp(self):
        super().setUp()
        from rastrillo import discovery
        self.discovery = discovery

    def test_skipped_si_no_instalado(self):
        with patch.object(self.discovery.shutil, "which", return_value=None):
            r = self.discovery.run_maigret("alice")
        self.assertTrue(r["skipped"])
        self.assertEqual(r["hits"], [])
        self.assertEqual(r["raw_count"], 0)
        self.assertIsNone(r["error"])
        self.assertFalse(r["incomplete"])

    def test_maigret_available_helper(self):
        with patch.object(self.discovery.shutil, "which", return_value=None):
            self.assertFalse(self.discovery.maigret_available())
        with patch.object(self.discovery.shutil, "which",
                          return_value="/usr/bin/maigret"):
            self.assertTrue(self.discovery.maigret_available())


class TestRunMaigretConJSON(IsolatedTestCase):

    def setUp(self):
        super().setUp()
        from rastrillo import discovery
        self.discovery = discovery

    def _run_with_payload(self, payload, username="alice"):
        """Invoca run_maigret simulando que el binario existe y produce
        el JSON `payload` en el folderoutput."""
        def _run(cmd, **kw):
            # extraer --folderoutput
            tmp = cmd[cmd.index("--folderoutput") + 1]
            _write_maigret_json(tmp, username, payload)
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

        with patch.object(self.discovery.shutil, "which",
                          return_value="/usr/bin/maigret"), \
             patch.object(self.discovery.subprocess, "run", side_effect=_run):
            return self.discovery.run_maigret(username)

    def test_3_claimed_1_available_solo_3_hits(self):
        r = self._run_with_payload({
            "Reddit":  {"status": "Claimed",
                        "url_user": "https://reddit.com/user/alice"},
            "GitHub":  {"status": "Claimed",
                        "url_user": "https://github.com/alice"},
            "Twitter": {"status": "Claimed",
                        "url_user": "https://twitter.com/alice"},
            "MySpace": {"status": "Available",
                        "url_user": "https://myspace.com/alice"},
        })
        self.assertFalse(r["skipped"])
        self.assertIsNone(r["error"])
        self.assertEqual(r["raw_count"], 3)
        sites = {h["source_site"] for h in r["hits"]}
        self.assertEqual(sites, {"reddit.com", "github.com", "twitter.com"})

    def test_schema_status_dict(self):
        """Maigret recientes empaqueta el status en un sub-dict."""
        r = self._run_with_payload({
            "Reddit": {
                "url_user": "https://reddit.com/user/alice",
                "status":   {"status": "Claimed", "http_status": 200},
            },
        })
        self.assertEqual(r["raw_count"], 1)
        self.assertEqual(r["hits"][0]["source_site"], "reddit.com")

    def test_no_json_es_error_no_crash(self):
        """Maigret corre y deja la carpeta vacía → error visible, sin crash."""
        def _run(cmd, **kw):
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        with patch.object(self.discovery.shutil, "which",
                          return_value="/usr/bin/maigret"), \
             patch.object(self.discovery.subprocess, "run", side_effect=_run):
            r = self.discovery.run_maigret("alice")
        self.assertFalse(r["skipped"])
        self.assertEqual(r["hits"], [])
        self.assertIsNotNone(r["error"])
        self.assertIn("JSON", r["error"])

    def test_json_invalido_no_propaga(self):
        """JSON corrupto en el output → error en hit con _error, no crash."""
        def _run(cmd, **kw):
            tmp = cmd[cmd.index("--folderoutput") + 1]
            Path(tmp, "alice.json").write_text("no es json {", encoding="utf-8")
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        with patch.object(self.discovery.shutil, "which",
                          return_value="/usr/bin/maigret"), \
             patch.object(self.discovery.subprocess, "run", side_effect=_run):
            r = self.discovery.run_maigret("alice")
        # No crash. El hit con _error sí entra (lo recoge _register para
        # registrarlo como error en discover()).
        self.assertEqual(len(r["hits"]), 1)
        self.assertIn("_error", r["hits"][0])

    def test_timeout_es_incomplete_no_crash(self):
        def _boom(cmd, **kw):
            raise subprocess.TimeoutExpired(cmd, kw.get("timeout", 1))
        with patch.object(self.discovery.shutil, "which",
                          return_value="/usr/bin/maigret"), \
             patch.object(self.discovery.subprocess, "run", side_effect=_boom):
            r = self.discovery.run_maigret("alice")
        self.assertTrue(r["incomplete"])
        self.assertIn("timeout", (r["error"] or "").lower())
        self.assertEqual(r["hits"], [])


class TestDiscoverIntegracionMaigret(IsolatedTestCase):

    def setUp(self):
        super().setUp()
        from rastrillo import db, discovery
        self.db = db
        self.discovery = discovery
        db.init()

    def _silent_others(self):
        """Stubs vacíos para sherlock/holehe/hibp para aislar maigret."""
        from rastrillo import hibp
        empty = {"hits": [], "error": None, "incomplete": False, "raw_count": 0}
        empty_hibp = {**empty, "skipped": False}
        return [
            patch.object(self.discovery, "run_sherlock", return_value=empty),
            patch.object(self.discovery, "run_holehe",   return_value=empty),
            patch.object(hibp, "run_hibp",
                         return_value={**empty, "skipped": True}),
        ]

    def test_raw_counts_y_persistencia(self):
        """maigret devuelve 4 hits (uno KEEP) → 4 raw, 4 saved (uno como
        skipped); KEEP no cuenta como found."""
        maigret_payload = {
            "hits": [
                {"name": "Reddit", "url": "https://reddit.com/u/alice",
                 "source_site": "reddit.com"},
                {"name": "GitHub", "url": "https://github.com/alice",
                 "source_site": "github.com"},     # KEEP_PLATFORMS → skipped
                {"name": "Twitter", "url": "https://twitter.com/alice",
                 "source_site": "twitter.com"},
                {"name": "Steam", "url": "https://steamcommunity.com/id/alice",
                 "source_site": "steamcommunity.com"},
            ],
            "error": None, "incomplete": False,
            "raw_count": 4, "skipped": False,
        }
        ctxs = self._silent_others()
        ctxs.append(patch.object(self.discovery, "run_maigret",
                                 return_value=maigret_payload))
        for c in ctxs:
            c.__enter__()
        try:
            summary = self.discovery.discover(["alice"], [])
        finally:
            for c in ctxs:
                c.__exit__(None, None, None)

        self.assertEqual(summary["raw_counts"]["maigret"], 4)
        self.assertEqual(summary["raw_counts"]["maigret_saved"], 4)
        # 4 cuentas persistidas con source=maigret
        rows = [dict(r) for r in self.db.list_accounts()]
        maigret_rows = [r for r in rows if r["source"] == "maigret"]
        self.assertEqual(len(maigret_rows), 4)
        for r in maigret_rows:
            self.assertEqual(r["confidence"], "high")    # 'alice' = 5 chars
                                                          # + match en URL → high
        # GitHub debe estar como skipped (KEEP_PLATFORMS)
        github = next(r for r in maigret_rows if r["source_site"] == "github.com")
        self.assertEqual(github["status"], "skipped")
        # Reddit/Twitter/Steam como found (no KEEP, no recetas para
        # twitter/steamcommunity)
        for site in ("reddit.com", "twitter.com", "steamcommunity.com"):
            row = next(r for r in maigret_rows if r["source_site"] == site)
            self.assertIn(row["status"], ("found", "skipped"))
        # summary["kept"] cuenta al menos github
        self.assertGreaterEqual(summary["kept"], 1)

    def test_dedup_maigret_y_sherlock_mismo_host(self):
        """Maigret y Sherlock encuentran el mismo sitio para el mismo username:
        la unicidad por (source_site, identifier) los colapsa en 1 fila."""
        sherlock_payload = {
            "hits": [{"name": "Reddit", "url": "https://reddit.com/u/alice",
                      "source_site": "reddit.com"}],
            "error": None, "incomplete": False, "raw_count": 1,
        }
        maigret_payload = {
            "hits": [{"name": "Reddit", "url": "https://reddit.com/u/alice",
                      "source_site": "reddit.com"}],
            "error": None, "incomplete": False, "raw_count": 1, "skipped": False,
        }
        empty = {"hits": [], "error": None, "incomplete": False, "raw_count": 0}
        ctxs = [
            patch.object(self.discovery, "run_sherlock",
                         return_value=sherlock_payload),
            patch.object(self.discovery, "run_maigret",
                         return_value=maigret_payload),
            patch.object(self.discovery, "run_holehe", return_value=empty),
            patch("rastrillo.hibp.run_hibp",
                  return_value={**empty, "skipped": True}),
        ]
        for c in ctxs: c.__enter__()
        try:
            self.discovery.discover(["alice"], [])
        finally:
            for c in ctxs: c.__exit__(None, None, None)
        # Una sola fila en la DB para reddit.com con identifier alice
        rows = [r for r in self.db.list_accounts() if r["source_site"] == "reddit.com"]
        self.assertEqual(len(rows), 1)
        # La primera fuente en escribir gana; sherlock corre antes que maigret
        self.assertEqual(rows[0]["source"], "sherlock")

    def test_maigret_excepcion_no_aborta_lote(self):
        """Si run_maigret levanta RuntimeError, sherlock/holehe siguen y
        el error queda registrado en summary['errors']."""
        sherlock_payload = {
            "hits": [{"name": "Tumblr", "url": "https://tumblr.com/u/alice",
                      "source_site": "tumblr.com"}],
            "error": None, "incomplete": False, "raw_count": 1,
        }
        holehe_payload = {
            "hits": [{"name": "spotify", "url": None,
                      "source_site": "spotify.com"}],
            "error": None, "incomplete": False, "raw_count": 1,
        }
        empty_hibp = {"hits": [], "error": None, "incomplete": False,
                      "raw_count": 0, "skipped": True}
        ctxs = [
            patch.object(self.discovery, "run_sherlock",
                         return_value=sherlock_payload),
            patch.object(self.discovery, "run_holehe", return_value=holehe_payload),
            patch("rastrillo.hibp.run_hibp", return_value=empty_hibp),
            patch.object(self.discovery, "run_maigret",
                         side_effect=RuntimeError("simulado")),
        ]
        for c in ctxs: c.__enter__()
        try:
            summary = self.discovery.discover(["alice"], ["alice@example.com"])
        finally:
            for c in ctxs: c.__exit__(None, None, None)
        # Sherlock y Holehe pasaron a la DB
        rows = [r for r in self.db.list_accounts()]
        sources = sorted({r["source"] for r in rows})
        self.assertIn("sherlock", sources)
        self.assertIn("holehe", sources)
        # El error de maigret está registrado
        maigret_errs = [e for e in summary["errors"] if e["source"] == "maigret"]
        self.assertEqual(len(maigret_errs), 1)
        self.assertIn("simulado", maigret_errs[0]["error"])

    def test_sin_maigret_instalado_no_afecta(self):
        """Si maigret no está instalado: skipped=True silencioso, 0 hits.
        raw_counts.maigret = 0, sin entrada de error."""
        empty = {"hits": [], "error": None, "incomplete": False, "raw_count": 0}
        empty_hibp = {**empty, "skipped": True}
        sherlock_payload = {
            "hits": [{"name": "Reddit", "url": "https://reddit.com/u/alice",
                      "source_site": "reddit.com"}],
            "error": None, "incomplete": False, "raw_count": 1,
        }
        # maigret_available -> False (no instalado). El loop usa run_maigret
        # que internamente devuelve skipped.
        ctxs = [
            patch.object(self.discovery, "run_sherlock",
                         return_value=sherlock_payload),
            patch.object(self.discovery, "run_holehe", return_value=empty),
            patch("rastrillo.hibp.run_hibp", return_value=empty_hibp),
            patch.object(self.discovery.shutil, "which", return_value=None),
        ]
        for c in ctxs: c.__enter__()
        try:
            summary = self.discovery.discover(["alice"], [])
        finally:
            for c in ctxs: c.__exit__(None, None, None)
        self.assertEqual(summary["raw_counts"]["maigret"], 0)
        self.assertEqual(summary["raw_counts"]["maigret_saved"], 0)
        # Sin errores de maigret
        maigret_errs = [e for e in summary["errors"] if e["source"] == "maigret"]
        self.assertEqual(len(maigret_errs), 0)
        # Sherlock siguió como siempre
        rows = list(self.db.list_accounts())
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["source"], "sherlock")
