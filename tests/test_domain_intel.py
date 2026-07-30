"""Tests del módulo Domain Intelligence (WHOIS + DNS + correlación).

Todo OFFLINE: las dos primitivas de red (`_whois_query` y `_dns_query`) se
parchean con fixtures fijos. Ninguna parte del test toca la red real.
"""
from __future__ import annotations

import json
import socket
import unittest
from unittest import mock

from tests.helpers import IsolatedTestCase, auth_client


# --- Fixtures WHOIS (respuestas crudas tipo .com con cadena de referencias) --
_IANA_RESP = """% IANA WHOIS server
domain:       COM
refer:        whois.verisign-grs.com
"""

_VERISIGN_RESP = """Domain Name: EXAMPLE.COM
Registrar WHOIS Server: whois.exampleregistrar.com
Registrar: Example Registrar, Inc.
Name Server: NS1.EXAMPLEDNS.COM
Domain Status: clientTransferProhibited https://icann.org/epp#clientTransferProhibited
"""

_REGISTRAR_RESP = """Domain Name: example.com
Registrar: Example Registrar, Inc.
Creation Date: 1995-08-14T04:00:00Z
Registry Expiry Date: 2026-08-13T04:00:00Z
Updated Date: 2024-08-14T07:01:44Z
Registrant Organization: Example Org
Name Server: ns1.exampledns.com
Name Server: ns2.exampledns.com
Domain Status: clientDeleteProhibited
"""


def _fake_whois_query(server, query, timeout):
    """Simula la cadena IANA → registro → registrar."""
    mapping = {
        "whois.iana.org": _IANA_RESP,
        "whois.verisign-grs.com": _VERISIGN_RESP,
        "whois.exampleregistrar.com": _REGISTRAR_RESP,
    }
    if server in mapping:
        return mapping[server]
    raise OSError(f"servidor inesperado en el test: {server}")


def _fake_dns_query(name, rtype, timeout):
    """Fixture DNS fijo para example.com."""
    data = {
        "A": ["93.184.216.34"],
        "MX": ["10 aspmx.l.google.com."],
        "NS": ["ns1.cloudflare.com.", "ns2.cloudflare.com."],
        "TXT": [
            '"v=spf1 include:_spf.google.com include:amazonses.com ~all"',
            '"google-site-verification=abc123def456"',
        ],
    }
    return data.get(rtype, [])


class WhoisParseTest(IsolatedTestCase):
    def test_recursive_lookup_and_parse(self):
        from rastrillo import domain_intel
        with mock.patch.object(domain_intel, "_whois_query", _fake_whois_query):
            w = domain_intel.lookup_whois("example.com")
        self.assertIsNone(w["error"])
        self.assertEqual(w["registrar"], "Example Registrar, Inc.")
        self.assertTrue(w["created"].startswith("1995"))
        self.assertTrue(w["expires"].startswith("2026"))
        self.assertTrue(w["updated"].startswith("2024"))
        self.assertEqual(w["registrant"], "Example Org")
        self.assertIn("ns1.exampledns.com", w["nameservers"])
        self.assertIn("ns2.exampledns.com", w["nameservers"])
        self.assertTrue(w["status"])           # al menos un estado
        self.assertTrue(w["raw"])              # raw recortado presente

    def test_whois_timeout_is_visible_not_crash(self):
        from rastrillo import domain_intel

        def _boom(server, query, timeout):
            raise socket.timeout("timed out")

        with mock.patch.object(domain_intel, "_whois_query", _boom):
            w = domain_intel.lookup_whois("example.com")
        self.assertIsNotNone(w["error"])       # error VISIBLE
        self.assertIsNone(w["registrar"])      # sin datos, pero no crashea

    def test_invalid_domain_returns_error_dict(self):
        from rastrillo import domain_intel
        w = domain_intel.lookup_whois("no es un dominio")
        self.assertIn("inválido", w["error"])


class DnsLookupTest(IsolatedTestCase):
    def test_dns_records_parsed(self):
        from rastrillo import domain_intel
        with mock.patch.object(domain_intel, "_dns_query", _fake_dns_query):
            d = domain_intel.lookup_dns("example.com")
        self.assertIsNone(d["error"])
        self.assertFalse(d["incomplete"])
        self.assertIn("93.184.216.34", d["a"])
        self.assertEqual(d["mx"], ["10 aspmx.l.google.com."])
        self.assertIn("ns1.cloudflare.com", d["ns"])
        # TXT: comillas eliminadas por _clean_txt
        self.assertTrue(any(t.startswith("v=spf1") for t in d["txt"]))
        self.assertTrue(any("google-site-verification" in t for t in d["txt"]))

    def test_nxdomain_is_global_error(self):
        from rastrillo import domain_intel
        import dns.resolver

        def _nx(name, rtype, timeout):
            raise dns.resolver.NXDOMAIN()

        with mock.patch.object(domain_intel, "_dns_query", _nx):
            d = domain_intel.lookup_dns("noexiste.example")
        self.assertIn("NXDOMAIN", d["error"])
        self.assertTrue(d["incomplete"])

    def test_per_type_timeout_marks_incomplete(self):
        from rastrillo import domain_intel
        import dns.exception

        def _partial(name, rtype, timeout):
            if rtype == "TXT":
                raise dns.exception.Timeout()
            return _fake_dns_query(name, rtype, timeout)

        with mock.patch.object(domain_intel, "_dns_query", _partial):
            d = domain_intel.lookup_dns("example.com")
        self.assertTrue(d["incomplete"])
        self.assertIsNotNone(d["error"])
        self.assertIn("TXT", d["error"])
        # los otros tipos sí se resolvieron — no abortó el lote
        self.assertTrue(d["a"])
        self.assertTrue(d["mx"])


class CorrelationTest(IsolatedTestCase):
    def test_mx_google(self):
        from rastrillo import domain_intel
        cor = domain_intel.correlate({"mx": ["10 aspmx.l.google.com."]})
        self.assertTrue(any(c["proveedor"] == "Google Workspace"
                            and c["confidence"] == "high" for c in cor))

    def test_ns_cloudflare(self):
        from rastrillo import domain_intel
        cor = domain_intel.correlate({"ns": ["ns1.cloudflare.com"]})
        self.assertTrue(any(c["proveedor"] == "Cloudflare"
                            and c["tipo"] == "dns" for c in cor))

    def test_spf_includes(self):
        from rastrillo import domain_intel
        cor = domain_intel.correlate({
            "txt": ["v=spf1 include:_spf.google.com include:amazonses.com ~all"],
        })
        provs = {c["proveedor"] for c in cor}
        self.assertIn("Google Workspace (SPF)", provs)
        self.assertIn("Amazon SES", provs)
        # confianza medium para autorizaciones SPF
        for c in cor:
            self.assertEqual(c["confidence"], "medium")

    def test_txt_verification_token_low_confidence(self):
        from rastrillo import domain_intel
        cor = domain_intel.correlate({"txt": ["google-site-verification=xyz"]})
        self.assertTrue(any(c["tipo"] == "verificación"
                            and c["confidence"] == "low" for c in cor))

    def test_no_network_calls_in_correlate(self):
        """correlate() es PURO: no debe tocar ninguna primitiva de red."""
        from rastrillo import domain_intel
        with mock.patch.object(domain_intel, "_whois_query") as mw, \
                mock.patch.object(domain_intel, "_dns_query") as md:
            domain_intel.correlate({"mx": ["10 aspmx.l.google.com."],
                                    "ns": ["ns1.cloudflare.com"]})
            mw.assert_not_called()
            md.assert_not_called()

    def test_dedup_same_provider(self):
        from rastrillo import domain_intel
        cor = domain_intel.correlate({
            "mx": ["10 aspmx.l.google.com.", "20 alt1.aspmx.l.google.com."],
        })
        provs = [c["proveedor"] for c in cor]
        self.assertEqual(provs.count("Google Workspace"), 1)


class SsrfGuardTest(IsolatedTestCase):
    def test_whois_query_rejects_private_server(self):
        """El guard del socket WHOIS rechaza servidores que resuelven a IPs
        no públicas (mismo criterio que resolver._is_safe_url)."""
        from rastrillo import domain_intel
        # _whois_query SIN parchear: debe rechazar loopback/privadas antes de
        # abrir el socket (no toca la red).
        with self.assertRaises(OSError):
            domain_intel._whois_query("127.0.0.1", "example.com", 1.0)
        with self.assertRaises(OSError):
            domain_intel._whois_query("10.0.0.1", "example.com", 1.0)

    def test_host_resolves_public_helper(self):
        from rastrillo import domain_intel
        self.assertFalse(domain_intel._host_resolves_public("127.0.0.1"))
        self.assertFalse(domain_intel._host_resolves_public("192.168.1.1"))
        self.assertFalse(domain_intel._host_resolves_public(""))


class AnalyzeTest(IsolatedTestCase):
    def test_analyze_invalid_domain_raises(self):
        from rastrillo import domain_intel
        with self.assertRaises(ValueError):
            domain_intel.analyze("not a domain !!")

    def test_analyze_full_report(self):
        from rastrillo import domain_intel
        with mock.patch.object(domain_intel, "_whois_query", _fake_whois_query), \
                mock.patch.object(domain_intel, "_dns_query", _fake_dns_query):
            rep = domain_intel.analyze("example.com")
        self.assertEqual(rep["domain"], "example.com")
        self.assertEqual(rep["registrar"], "Example Registrar, Inc.")
        self.assertTrue(rep["dns"]["mx"])
        self.assertTrue(rep["correlations"])
        self.assertIsInstance(rep["errors"], list)


class EndpointTest(IsolatedTestCase):
    def test_analyze_requires_token(self):
        client = auth_client()
        # Sin header de token → 401 (middleware de auth).
        r = client.post("/api/domain/analyze", json={"domain": "example.com"})
        self.assertEqual(r.status_code, 401)

    def test_analyze_rejects_invalid_domain(self):
        client = auth_client()
        r = client.post("/api/domain/analyze",
                        json={"domain": "no válido"}, headers=self.hdr())
        self.assertEqual(r.status_code, 400)

    def test_analyze_happy_path_and_persist(self):
        from rastrillo import domain_intel
        client = auth_client()
        with mock.patch.object(domain_intel, "_whois_query", _fake_whois_query), \
                mock.patch.object(domain_intel, "_dns_query", _fake_dns_query):
            r = client.post("/api/domain/analyze",
                            json={"domain": "example.com"}, headers=self.hdr())
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["registrar"], "Example Registrar, Inc.")
        # Persistencia: el GET devuelve el informe guardado.
        r2 = client.get("/api/domain/report?domain=example.com",
                        headers=self.hdr())
        self.assertEqual(r2.status_code, 200)
        saved = r2.json()
        self.assertIsNotNone(saved["report"])
        self.assertEqual(saved["report"]["domain"], "example.com")

    def test_report_unknown_domain_returns_null(self):
        client = auth_client()
        r = client.get("/api/domain/report?domain=desconocido.com",
                       headers=self.hdr())
        self.assertEqual(r.status_code, 200)
        self.assertIsNone(r.json()["report"])

    def test_history_requires_token(self):
        client = auth_client()
        r = client.get("/api/domain/history")
        self.assertEqual(r.status_code, 401)

    def test_history_lists_analyzed_domains(self):
        from rastrillo import domain_intel
        client = auth_client()
        with mock.patch.object(domain_intel, "_whois_query", _fake_whois_query), \
                mock.patch.object(domain_intel, "_dns_query", _fake_dns_query):
            client.post("/api/domain/analyze",
                        json={"domain": "example.com"}, headers=self.hdr())
            client.post("/api/domain/analyze",
                        json={"domain": "example.org"}, headers=self.hdr())
        r = client.get("/api/domain/history", headers=self.hdr())
        self.assertEqual(r.status_code, 200)
        domains = r.json()["domains"]
        names = {d["domain"] for d in domains}
        self.assertIn("example.com", names)
        self.assertIn("example.org", names)
        # cada entrada lleva domain + created_at (lo que la cabecera de cada
        # tarjeta del histórico necesita para pintarse plegada)
        self.assertTrue(all("created_at" in d for d in domains))


class HistoryCleanupTest(IsolatedTestCase):
    """Borrado de UN informe y limpieza del histórico completo.

    Igual que el resto del archivo: primitivas de red parcheadas, cero
    tráfico real. Cada test corre con su propio RASTRILLO_HOME (IsolatedTestCase).
    """

    def _analyze(self, client, *domains):
        """Persiste un informe por cada dominio, offline."""
        from rastrillo import domain_intel
        with mock.patch.object(domain_intel, "_whois_query", _fake_whois_query), \
                mock.patch.object(domain_intel, "_dns_query", _fake_dns_query):
            for d in domains:
                r = client.post("/api/domain/analyze",
                                json={"domain": d}, headers=self.hdr())
                self.assertEqual(r.status_code, 200, f"analyze({d}) falló")

    def _history(self, client):
        r = client.get("/api/domain/history", headers=self.hdr())
        self.assertEqual(r.status_code, 200)
        return {d["domain"] for d in r.json()["domains"]}

    # --- Borrar un informe concreto -----------------------------------------
    def test_delete_existing_report_disappears_from_history(self):
        from rastrillo import db
        client = auth_client()
        self._analyze(client, "example.com", "example.org")

        r = client.post("/api/domain/report/delete",
                        json={"domain": "example.com"}, headers=self.hdr())
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["ok"])

        # Fuera del histórico (endpoint y capa db) y el otro intacto.
        self.assertEqual(self._history(client), {"example.org"})
        self.assertEqual({row["domain"] for row in db.list_domain_reports()},
                         {"example.org"})
        self.assertIsNone(db.get_domain_report("example.com"))

    def test_delete_unknown_domain_404_and_history_intact(self):
        client = auth_client()
        self._analyze(client, "example.com")

        r = client.post("/api/domain/report/delete",
                        json={"domain": "nunca-analizado.com"},
                        headers=self.hdr())
        self.assertEqual(r.status_code, 404)
        # Mensaje accionable, no un 404 pelado.
        self.assertIn("nunca-analizado.com", r.json()["detail"])
        # El resto del histórico no se ha tocado.
        self.assertEqual(self._history(client), {"example.com"})

    def test_delete_rejects_invalid_domain(self):
        client = auth_client()
        r = client.post("/api/domain/report/delete",
                        json={"domain": "no válido"}, headers=self.hdr())
        self.assertEqual(r.status_code, 400)

    def test_delete_requires_token(self):
        client = auth_client()
        r = client.post("/api/domain/report/delete", json={"domain": "example.com"})
        self.assertEqual(r.status_code, 401)

    # --- Limpiar todo el histórico ------------------------------------------
    def test_clear_history_empties_and_snapshots_db(self):
        from rastrillo import config, db
        client = auth_client()
        self._analyze(client, "example.com", "example.org")

        backups = config.BASE_DIR / "backups"
        antes = len(list(backups.glob("rastrillo_*.db"))) if backups.exists() else 0

        r = client.post("/api/domain/history/clear", json={}, headers=self.hdr())
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["deleted"], 2)

        # Histórico vacío.
        self.assertEqual(self._history(client), set())
        self.assertEqual(db.list_domain_reports(), [])
        # Y se creó el snapshot de la DB antes de borrar (patrón clear_accounts).
        self.assertTrue(backups.exists(), "no se creó ~/.rastrillo/backups/")
        self.assertGreater(len(list(backups.glob("rastrillo_*.db"))), antes)

    def test_clear_history_does_not_touch_accounts(self):
        """El histórico de dominios y las cuentas son datos independientes."""
        from rastrillo import db
        client = auth_client()
        db.init()
        db.upsert_account("reddit", "yo", source_site="reddit",
                          profile_url="https://reddit.com/u/yo",
                          source="sherlock")
        self._analyze(client, "example.com")

        r = client.post("/api/domain/history/clear", json={}, headers=self.hdr())
        self.assertEqual(r.status_code, 200)
        self.assertEqual(db.list_domain_reports(), [])
        self.assertEqual(len(db.list_accounts()), 1)

    def test_clear_history_requires_token(self):
        client = auth_client()
        r = client.post("/api/domain/history/clear", json={})
        self.assertEqual(r.status_code, 401)

    def test_clear_empty_history_is_noop(self):
        """Sin informes: 200 con deleted=0 (idempotente, no revienta)."""
        client = auth_client()
        r = client.post("/api/domain/history/clear", json={}, headers=self.hdr())
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["deleted"], 0)


if __name__ == "__main__":
    unittest.main()
