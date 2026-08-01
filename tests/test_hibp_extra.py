"""Paso 5, Entrega 3: el detalle de la brecha HIBP deja de tirarse.

HIBP devuelve por cada brecha la fecha, cuánta gente afectó y qué tipos de dato
se expusieron. `discovery._register` lo recibía en `hit["extra"]` y lo
descartaba: era el único dato del proyecto que llegaba y se perdía.

Ahora se persiste en `accounts.breach_meta` y se muestra en la pestaña
"Brechas". Lo que se fija aquí:

  - round-trip de los campos por la ruta real (discovery → DB → API),
  - los casos degenerados: sin `data_classes`, `pwn_count` ausente vs cero,
    JSON corrupto, `extra` vacío,
  - la migración idempotente, incluida una DB anterior al Paso 5,
  - y lo más importante: que **nada de esto toca la semántica de HIBP**. Sigue
    siendo exposición en brecha, no cuenta activa; sigue entrando `medium` por
    política; sigue fuera de la corroboración. Es contexto, no una señal.
"""
from __future__ import annotations

import json
import sqlite3

from .helpers import IsolatedTestCase, auth_client


def _brecha(**kw):
    """Una brecha con la forma que devuelve la API de HIBP."""
    base = {
        "Name": "Adobe", "Title": "Adobe", "Domain": "adobe.com",
        "BreachDate": "2013-10-04", "PwnCount": 152445165,
        "DataClasses": ["Email addresses", "Password hints", "Passwords",
                        "Usernames"],
        "IsVerified": True, "IsSpamList": False, "IsFabricated": False,
    }
    base.update(kw)
    return base


class _Base(IsolatedTestCase):
    def setUp(self):
        super().setUp()
        from rastrillo import db, discovery, hibp
        self.db = db
        self.discovery = discovery
        self.hibp = hibp
        self.db.init()

    def _fila(self, source_site="adobe.com"):
        for r in self.db.list_accounts():
            if r["source_site"] == source_site:
                return r
        return None


class TestColumnaYHelpers(_Base):
    """La columna, su migración y los helpers de (de)serialización."""

    def test_la_columna_existe(self):
        with self.db.connect() as con:
            cols = {r[1] for r in con.execute("PRAGMA table_info(accounts)")}
        self.assertIn("breach_meta", cols)

    def test_esta_en_ACCOUNTS_COLUMNS(self):
        """Si no está, el rebuild de una DB legacy la perdería en silencio."""
        self.assertIn("breach_meta", self.db.ACCOUNTS_COLUMN_NAMES)

    def test_migracion_idempotente(self):
        for _ in range(3):
            self.db.init()
        with self.db.connect() as con:
            cols = [r[1] for r in con.execute("PRAGMA table_info(accounts)")]
        self.assertEqual(cols.count("breach_meta"), 1)

    def test_db_anterior_al_paso5(self):
        """Una DB sin la columna la gana sin perder datos."""
        with self.db.connect() as con:
            con.execute("DROP TABLE accounts")
            con.execute("""CREATE TABLE accounts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                platform TEXT NOT NULL, display_name TEXT, profile_url TEXT,
                identifier TEXT, source TEXT, source_site TEXT,
                status TEXT NOT NULL DEFAULT 'found', deletion_type TEXT,
                difficulty TEXT, current_step INTEGER DEFAULT 0,
                last_message TEXT, updated_at REAL)""")
            con.execute("INSERT INTO accounts (platform, identifier, source_site,"
                        " status) VALUES ('x','yo','viejo.com','found')")
        self.db.init()
        with self.db.connect() as con:
            cols = {r[1] for r in con.execute("PRAGMA table_info(accounts)")}
            fila = con.execute("SELECT * FROM accounts WHERE identifier='yo'"
                               ).fetchone()
        self.assertIn("breach_meta", cols)
        self.assertEqual(fila["source_site"], "viejo.com")
        self.assertIsNone(fila["breach_meta"])

    def test_round_trip_de_los_helpers(self):
        meta = {"breach_date": "2013-10-04", "pwn_count": 152445165,
                "data_classes": ["Passwords", "Contraseñas ñ"]}
        crudo = self.db.dump_breach_meta(meta)
        self.assertEqual(self.db.parse_breach_meta(crudo), meta)

    def test_helpers_tolerantes(self):
        """Nunca lanzan: {} ante lo que sea."""
        for basura in (None, "", "{no json", "[]", '"cadena"', "123", b""):
            with self.subTest(basura=basura):
                self.assertEqual(self.db.parse_breach_meta(basura), {})


class TestPersistenciaDesdeDiscovery(_Base):
    """La ruta real: un hit de HIBP acaba con su detalle en la fila."""

    def _registrar(self, *brechas):
        """Corre `discover()` con las otras tres fuentes en vacío y HIBP
        devolviendo estas brechas ya convertidas a hits."""
        from unittest import mock
        vacio = {"hits": [], "error": None, "incomplete": False, "raw_count": 0}
        with mock.patch.object(self.discovery, "run_sherlock", return_value=vacio), \
             mock.patch.object(self.discovery, "run_holehe", return_value=vacio), \
             mock.patch.object(self.discovery, "run_maigret",
                               return_value={**vacio, "skipped": True}), \
             mock.patch.object(self.discovery.hibp, "run_hibp", return_value={
                 "hits": self._construir(*brechas), "error": None,
                 "incomplete": False, "raw_count": len(brechas),
                 "skipped": False}):
            self.discovery.discover([], ["yo@example.com"])

    def _construir(self, *brechas):
        """Replica lo que hace hibp.check_email con la respuesta cruda."""
        hits = []
        for b in brechas:
            dominio = (b.get("Domain") or "").strip().lower()
            if not dominio:
                continue
            hits.append({
                "name": b.get("Title") or dominio,
                "url": None,
                "source_site": dominio,
                "no_site": bool(b.get("IsSpamList") or b.get("IsFabricated")
                                or not b.get("IsVerified", True)),
                "extra": {
                    "breach_date": b.get("BreachDate"),
                    "pwn_count": b.get("PwnCount"),
                    "data_classes": b.get("DataClasses") or [],
                    "is_verified": b.get("IsVerified"),
                    "is_spam_list": b.get("IsSpamList"),
                    "is_fabricated": b.get("IsFabricated"),
                },
            })
        return hits

    def test_los_tres_campos_llegan_a_la_fila(self):
        self._registrar(_brecha())
        fila = self._fila()
        self.assertIsNotNone(fila, "no se registró la cuenta de adobe.com")
        meta = self.db.parse_breach_meta(fila["breach_meta"])
        self.assertEqual(meta["breach_date"], "2013-10-04")
        self.assertEqual(meta["pwn_count"], 152445165)
        self.assertEqual(meta["data_classes"],
                         ["Email addresses", "Password hints", "Passwords",
                          "Usernames"])

    def test_se_guardan_tambien_las_banderas(self):
        """El `extra` trae seis campos, no tres. No se recorta al guardar."""
        self._registrar(_brecha())
        meta = self.db.parse_breach_meta(self._fila()["breach_meta"])
        for k in ("is_verified", "is_spam_list", "is_fabricated"):
            self.assertIn(k, meta)

    def test_sin_data_classes(self):
        self._registrar(_brecha(DataClasses=[]))
        meta = self.db.parse_breach_meta(self._fila()["breach_meta"])
        self.assertEqual(meta["data_classes"], [])

    def test_data_classes_ausente(self):
        b = _brecha()
        del b["DataClasses"]
        self._registrar(b)
        meta = self.db.parse_breach_meta(self._fila()["breach_meta"])
        self.assertEqual(meta["data_classes"], [])

    def test_pwn_count_cero_no_es_lo_mismo_que_ausente(self):
        self._registrar(_brecha(PwnCount=0))
        meta = self.db.parse_breach_meta(self._fila()["breach_meta"])
        self.assertIn("pwn_count", meta)
        self.assertEqual(meta["pwn_count"], 0)

    def test_pwn_count_ausente(self):
        b = _brecha()
        del b["PwnCount"]
        self._registrar(b)
        meta = self.db.parse_breach_meta(self._fila()["breach_meta"])
        self.assertIsNone(meta["pwn_count"])

    def test_filas_que_no_son_hibp_no_tienen_breach_meta(self):
        self.db.upsert_account("ejemplo", "yo", source="sherlock",
                               source_site="ejemplo.com", status="found")
        fila = self._fila("ejemplo.com")
        self.assertIsNone(fila["breach_meta"])


class TestNoCambiaLaSemanticaDeHIBP(_Base):
    """El invariante de la entrega: esto es contexto, no una señal."""

    def _registrar_una(self, **kw):
        from unittest import mock
        hit = {
            "name": "Adobe", "url": None, "source_site": "adobe.com",
            "no_site": False,
            "extra": {"breach_date": "2013-10-04", "pwn_count": 1000,
                      "data_classes": ["Passwords"]},
        }
        hit.update(kw)
        vacio = {"hits": [], "error": None, "incomplete": False, "raw_count": 0}
        with mock.patch.object(self.discovery, "run_sherlock", return_value=vacio), \
             mock.patch.object(self.discovery, "run_holehe", return_value=vacio), \
             mock.patch.object(self.discovery, "run_maigret",
                               return_value={**vacio, "skipped": True}), \
             mock.patch.object(self.discovery.hibp, "run_hibp", return_value={
                 "hits": [hit], "error": None, "incomplete": False,
                 "raw_count": 1, "skipped": False}):
            self.discovery.discover([], ["yo@example.com"])

    def test_la_confianza_sigue_siendo_medium(self):
        self._registrar_una()
        self.assertEqual(self._fila()["confidence"], "medium")

    def test_el_motivo_sigue_siendo_el_de_siempre(self):
        self._registrar_una()
        codigos = {r["code"] for r in
                   self.db.parse_reasons(self._fila()["confidence_reasons"])}
        self.assertIn("fuente_hibp", codigos)

    def test_no_hay_motivo_nuevo_por_el_detalle(self):
        """Guardar el detalle no puede inventarse una señal de confianza."""
        self._registrar_una()
        codigos = {r["code"] for r in
                   self.db.parse_reasons(self._fila()["confidence_reasons"])}
        for sospechoso in ("breach_meta", "brecha_detalle", "data_classes",
                           "pwn_count", "breach_date"):
            self.assertNotIn(sospechoso, codigos)

    def test_no_site_sigue_marcando(self):
        self._registrar_una(no_site=True)
        codigos = {r["code"] for r in
                   self.db.parse_reasons(self._fila()["confidence_reasons"])}
        self.assertIn("hibp_no_sitio", codigos)
        self.assertEqual(self._fila()["confidence"], "medium")

    def test_el_estado_sigue_siendo_found(self):
        self._registrar_una()
        self.assertEqual(self._fila()["status"], "found")


class TestAPI(_Base):
    """El endpoint lo entrega ya parseado, como `confidence_reasons`."""

    def setUp(self):
        super().setUp()
        self.cli = auth_client()

    def test_la_api_lo_devuelve_parseado(self):
        meta = {"breach_date": "2013-10-04", "pwn_count": 152445165,
                "data_classes": ["Passwords", "Usernames"]}
        self.db.upsert_account("adobe", "yo@example.com", source="hibp",
                               source_site="adobe.com", status="found",
                               confidence="medium",
                               breach_meta=self.db.dump_breach_meta(meta))
        r = self.cli.get("/api/accounts", headers=self.hdr())
        self.assertEqual(r.status_code, 200)
        fila = r.json()["accounts"][0]
        self.assertEqual(fila["breach_meta"], meta)
        self.assertIsInstance(fila["breach_meta"], dict)

    def test_fila_sin_brecha_devuelve_dict_vacio(self):
        self.db.upsert_account("ejemplo", "yo", source="sherlock",
                               source_site="ejemplo.com", status="found")
        r = self.cli.get("/api/accounts", headers=self.hdr())
        self.assertEqual(r.json()["accounts"][0]["breach_meta"], {})

    def test_breach_meta_corrupto_no_rompe_la_api(self):
        """Una fila con JSON basura no puede tumbar la vista entera."""
        self.db.upsert_account("adobe", "yo@example.com", source="hibp",
                               source_site="adobe.com", status="found")
        with self.db.connect() as con:
            con.execute("UPDATE accounts SET breach_meta='{roto'")
        r = self.cli.get("/api/accounts", headers=self.hdr())
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["accounts"][0]["breach_meta"], {})


class TestUIExplicita(IsolatedTestCase):
    """La UI muestra los tres campos, y sin traducciones inventadas."""

    def setUp(self):
        super().setUp()
        from pathlib import Path
        raiz = Path(__file__).resolve().parents[1]
        self.js = (raiz / "rastrillo" / "static" / "app.js").read_text(
            encoding="utf-8")

    def test_hay_render_de_la_brecha(self):
        self.assertIn("breachDetail", self.js)
        self.assertIn("breach_meta", self.js)

    def test_los_tres_campos_se_pintan(self):
        for campo in ("breach_date", "pwn_count", "data_classes"):
            with self.subTest(campo=campo):
                self.assertIn(campo, self.js)

    def test_magnitud_con_separador_de_millares(self):
        self.assertIn("toLocaleString", self.js)

    def test_las_categorias_van_como_chips(self):
        self.assertIn("chip-data", self.js)

    def test_traduccion_con_passthrough(self):
        """Lo que no esté en la tabla se muestra en inglés, no se inventa."""
        self.assertIn("DATA_CLASSES_ES", self.js)
        self.assertRegex(self.js, r"DATA_CLASSES_ES\[c\]\s*\|\|\s*c")

    def test_la_tabla_cubre_las_categorias_frecuentes(self):
        for c in ("Email addresses", "Passwords", "Usernames",
                  "IP addresses", "Names", "Phone numbers",
                  "Physical addresses", "Dates of birth"):
            with self.subTest(categoria=c):
                self.assertIn(f'"{c}"', self.js)

    def test_el_detalle_se_escapa(self):
        """Los valores vienen de una API externa: nada sin escapar."""
        import re
        m = re.search(r"function breachDetail\(a\)\{.*?\n\}", self.js, re.S)
        self.assertIsNotNone(m)
        cuerpo = m.group(0)
        # Toda interpolación de dato va envuelta en escapeHtml/escapeAttr.
        for crudo in ("${m.breach_date}", "${c}", "${m.pwn_count}"):
            self.assertNotIn(crudo, cuerpo)
