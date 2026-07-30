"""Paso 2A: falsos positivos sin red.

Cubre las tres piezas:
  - el bump de `_sherlock_confidence` (ahora por path o subdominio, nunca por
    aparecer en cualquier parte del host);
  - la corroboración entre fuentes (misma fila para sherlock+maigret, filas
    separadas para holehe+sherlock);
  - la persistencia de los motivos (migración idempotente + round-trip).

TODO offline: llamamos a las funciones de confianza directamente y las fuentes
externas se parchean. Ni un subproceso, ni una petición de red.
"""
from __future__ import annotations

import json
import unittest
from unittest.mock import patch

from tests.helpers import IsolatedTestCase


def _vacio():
    return {"hits": [], "error": None, "incomplete": False, "raw_count": 0}


class BumpDeUrlTest(IsolatedTestCase):
    """La regla nueva del bump. `_sherlock_confidence` devuelve (tramo, motivos)."""

    def setUp(self):
        super().setUp()
        from rastrillo import discovery
        self.d = discovery

    def _conf(self, username, url):
        tramo, motivos = self.d._sherlock_confidence(username, {"url": url})
        return tramo, {m["code"] for m in motivos}

    # --- 1. El falso positivo que arreglamos --------------------------------
    def test_substring_del_dominio_no_da_bump(self):
        """`ana` dentro de `banana.com` NO es señal: era nuestro falso positivo
        más directo (subía low → medium)."""
        tramo, codes = self._conf("ana", "https://banana.com/u/xyz")
        self.assertEqual(tramo, "low")
        self.assertNotIn("bump_path", codes)
        self.assertNotIn("bump_subdominio", codes)

    def test_substring_a_media_palabra_en_el_path_no_da_bump(self):
        """El mismo bug de `banana.com`, movido al path: `mar` dentro de
        `marca-noticias` no es señal. Exigimos frontera de segmento."""
        tramo, codes = self._conf("mar", "https://site.com/marca-noticias/123")
        self.assertEqual(tramo, "low")
        self.assertNotIn("bump_path", codes)

    # --- 2. Bump por path ---------------------------------------------------
    def test_bump_por_path(self):
        tramo, codes = self._conf("ana", "https://site.com/ana")
        self.assertEqual(tramo, "medium")        # low + bump
        self.assertIn("bump_path", codes)

    def test_bump_por_query_string(self):
        tramo, codes = self._conf("ana", "https://site.com/perfil?user=ana")
        self.assertEqual(tramo, "medium")
        self.assertIn("bump_path", codes)

    def test_frontera_de_segmento_conserva_los_legitimos(self):
        """Los separadores / - _ . = ? & delimitan segmento: el identificador
        pegado a cualquiera de ellos sigue contando."""
        legitimos = [
            "https://site.com/u/mar",              # segmento final
            "https://site.com/marca/mar",          # 2ª aparición, la buena
            "https://site.com/mar-perfil",         # guion a la derecha
            "https://site.com/mar_perfil",         # guion bajo
            "https://site.com/mar.html",           # punto
            "https://site.com/perfil?u=mar&x=1",   # entre = y &
            "https://site.com/mar",                # sin nada detrás
            "https://site.com/mar/",               # barra detrás
        ]
        for url in legitimos:
            with self.subTest(url=url):
                _, codes = self._conf("mar", url)
                self.assertIn("bump_path", codes, f"debería bumpear: {url}")

    def test_frontera_de_segmento_corta_los_falsos(self):
        falsos = [
            "https://site.com/marca-noticias/123",
            "https://site.com/smartlist/user/99",
            "https://site.com/noticias/marketing",
            "https://site.com/perfil?tema=marketing",
        ]
        for url in falsos:
            with self.subTest(url=url):
                _, codes = self._conf("mar", url)
                self.assertNotIn("bump_path", codes, f"no debería bumpear: {url}")

    def test_identificadores_de_1_y_2_caracteres(self):
        """El residual que cerramos: un identificador cortísimo ya no bumpea
        por aparecer a media palabra."""
        for u in ("a", "ab"):
            with self.subTest(u=u):
                # A media palabra: no.
                _, codes = self._conf(u, "https://site.com/pagina-de-inicio")
                self.assertNotIn("bump_path", codes)
                # Como segmento propio: sí (y sigue siendo `low` de base).
                tramo, codes = self._conf(u, f"https://site.com/u/{u}")
                self.assertIn("bump_path", codes)
                self.assertEqual(tramo, "medium")   # low + bump, techo aparte

    def test_match_con_frontera_unitario(self):
        f = self.d._match_con_frontera
        self.assertTrue(f("mar", "/u/mar"))
        self.assertTrue(f("mar", "mar"))
        self.assertTrue(f("mar", "/marca/mar?"))
        self.assertFalse(f("mar", "/marca-noticias"))
        self.assertFalse(f("mar", ""))
        self.assertFalse(f("", "/u/mar"))

    # --- 3. Bump por etiqueta izquierda del host ---------------------------
    def test_bump_por_subdominio(self):
        """`usuario.tumblr.com`: señal legítima que no debe perderse."""
        tramo, codes = self._conf("jeremy", "https://jeremy.tumblr.com/")
        self.assertEqual(tramo, "high")          # medium (6 chars) + bump
        self.assertIn("bump_subdominio", codes)

    # --- 4. Ni path ni etiqueta izquierda ----------------------------------
    def test_sin_bump_si_el_host_solo_lo_contiene(self):
        """`blog.jeremyland.com`: la etiqueta izquierda es `blog`, y `jeremy`
        no está en el path. Sin bump."""
        tramo, codes = self._conf("jeremy", "https://blog.jeremyland.com/x")
        self.assertEqual(tramo, "medium")
        self.assertNotIn("bump_path", codes)
        self.assertNotIn("bump_subdominio", codes)

    def test_subdominio_exige_igualdad_no_substring(self):
        """`ana` no casa con la etiqueta `banana`."""
        tramo, codes = self._conf("ana", "https://banana.tumblr.com/")
        self.assertEqual(tramo, "low")
        self.assertNotIn("bump_subdominio", codes)

    # --- 5. url=None (holehe, hibp) ----------------------------------------
    def test_url_none_no_revienta_y_no_bumpea(self):
        tramo, codes = self._conf("jeremy", None)
        self.assertEqual(tramo, "medium")
        self.assertFalse({"bump_path", "bump_subdominio"} & codes)
        # Y sin `url` en el dict, ni con hit vacío.
        self.assertEqual(self.d._sherlock_confidence("jeremy", {})[0], "medium")
        self.assertEqual(self.d._sherlock_confidence("jeremy", None)[0], "medium")

    # --- 6. Identificador vacío --------------------------------------------
    def test_identificador_vacio_es_low(self):
        for vacio in ("", "   ", None):
            tramo, motivos = self.d._sherlock_confidence(
                vacio, {"url": "https://site.com/x"})
            self.assertEqual(tramo, "low")
            self.assertEqual([m["code"] for m in motivos], ["id_vacio"])

    # --- 7. La escala base no cambia ---------------------------------------
    def test_escala_base_intacta(self):
        """Los cuatro tramos, con URL que no aporta bump, dan lo de siempre.

        La URL de control tiene un path con letras a propósito: con la frontera
        de segmento, ni un identificador de 1 carácter bumpea por casualidad.
        """
        sin_bump = "https://otrositio.com/pagina"
        casos = [
            ("abcdefgh", "high"),    # len>=8
            ("abc.de", "high"),      # len>=6 + caracter distintivo
            ("abcdef", "medium"),    # len 6 sin distintivo → cae a len>=5
            ("abcde", "medium"),     # len>=5
            ("abcd", "low"),         # resto
            ("a", "low"),
        ]
        for username, esperado in casos:
            with self.subTest(username=username):
                self.assertEqual(self._conf(username, sin_bump)[0], esperado)

    def test_bump_nunca_pasa_de_high(self):
        tramo, codes = self._conf("usuariolargodistintivo1", "https://s.com/usuariolargodistintivo1")
        self.assertIn("bump_path", codes)
        self.assertEqual(tramo, "high")

    # --- Helper de host aislado -------------------------------------------
    def test_identificador_en_url_casos_raros(self):
        f = self.d._identificador_en_url
        self.assertIsNone(f("jeremy", ""))
        self.assertIsNone(f("jeremy", None))
        self.assertIsNone(f("", "https://jeremy.tumblr.com"))
        # Sin esquema seguimos parseando el host, no lo tomamos como path.
        self.assertEqual(f("jeremy", "jeremy.tumblr.com/x"), "subdominio")
        # Puerto y credenciales no estorban.
        self.assertEqual(f("jeremy", "https://jeremy.site.com:8443/"), "subdominio")
        self.assertEqual(f("jeremy", "https://u:p@jeremy.site.com/"), "subdominio")


class CorroboracionTest(IsolatedTestCase):
    """Corroboración entre fuentes, con `discover()` y las fuentes parcheadas."""

    def setUp(self):
        super().setUp()
        from rastrillo import config, db, discovery
        self.config = config
        self.db = db
        self.d = discovery
        db.init()

    def _reasons(self, row):
        return {r["code"] for r in self.db.parse_reasons(row["confidence_reasons"])}

    # --- 8. holehe (email) + sherlock (username) sobre el mismo sitio -------
    def test_email_mas_username_es_senal_fuerte_y_no_fusiona(self):
        sherlock = {"hits": [{"name": "Foro", "url": "https://foro.com/u/ana",
                              "source_site": "foro.com"}],
                    "error": None, "incomplete": False, "raw_count": 1}
        holehe = {"hits": [{"name": "Foro", "url": None,
                            "source_site": "foro.com"}],
                  "error": None, "incomplete": False, "raw_count": 1}
        with patch.object(self.d, "run_sherlock", return_value=sherlock), \
             patch.object(self.d, "run_holehe", return_value=holehe), \
             patch.object(self.d, "run_maigret",
                          return_value={**_vacio(), "skipped": True}):
            summary = self.d.discover(["ana"], ["ana@example.com"])

        filas = [r for r in self.db.list_accounts()
                 if (r["source_site"] or "") == "foro.com"]
        # DOS filas: el identificador difiere (username vs email). No se fusionan.
        self.assertEqual(len(filas), 2, "holehe y sherlock deben ser filas distintas")
        self.assertEqual({r["identifier"] for r in filas},
                         {"ana", "ana@example.com"})
        # La señal queda registrada en ambas.
        for r in filas:
            self.assertIn("corrob_cruzada", self._reasons(r))
        # Y la fila heurística (sherlock) sube de tramo: 'ana' es low de base,
        # medium por el bump de path (/u/ana) y high al corroborar holehe.
        sher = next(r for r in filas if r["source"] == "sherlock")
        self.assertEqual(sher["confidence"], "high")
        self.assertEqual(summary["corroborated"], 1)

    def test_holehe_sigue_high_y_no_baja_por_corroborar(self):
        """La confianza de holehe es política (siempre high), no estimación:
        la corroboración le anota el motivo pero no le mueve el tramo."""
        sherlock = {"hits": [{"name": "Foro", "url": "https://foro.com/u/ana",
                              "source_site": "foro.com"}],
                    "error": None, "incomplete": False, "raw_count": 1}
        holehe = {"hits": [{"name": "Foro", "url": None,
                            "source_site": "foro.com"}],
                  "error": None, "incomplete": False, "raw_count": 1}
        with patch.object(self.d, "run_sherlock", return_value=sherlock), \
             patch.object(self.d, "run_holehe", return_value=holehe), \
             patch.object(self.d, "run_maigret",
                          return_value={**_vacio(), "skipped": True}):
            self.d.discover(["ana"], ["ana@example.com"])
        hol = next(r for r in self.db.list_accounts() if r["source"] == "holehe")
        self.assertEqual(hol["confidence"], "high")
        self.assertIn("corrob_cruzada", self._reasons(hol))

    # --- 9. sherlock + maigret: una sola fila, señal débil registrada ------
    def test_sherlock_mas_maigret_una_fila_y_motivo_registrado(self):
        hit = {"name": "Foro", "url": "https://foro.com/u/ana",
               "source_site": "foro.com"}
        uno = {"hits": [hit], "error": None, "incomplete": False, "raw_count": 1}
        with patch.object(self.d, "run_sherlock", return_value=uno), \
             patch.object(self.d, "run_holehe", return_value=_vacio()), \
             patch.object(self.d, "run_maigret", return_value={**uno, "skipped": False}):
            self.d.discover(["ana"], [])

        filas = [r for r in self.db.list_accounts()
                 if (r["source_site"] or "") == "foro.com"]
        # El dedup por (source_site, identifier) sigue intacto: UNA fila.
        self.assertEqual(len(filas), 1)
        fila = filas[0]
        # El "lo han visto dos herramientas" queda anotado en el upsert.
        self.assertIn("corrob_misma_fila", self._reasons(fila))
        # Débil a propósito: catálogos que se solapan, así que NO mueve tramo.
        # 'ana' con bump por path = medium.
        self.assertEqual(fila["confidence"], "medium")

    # --- 10. Nunca por encima de high --------------------------------------
    def test_corroboracion_no_pasa_de_high(self):
        """Un username ya `high` corroborado por holehe se queda en `high`."""
        largo = "usuariolargo1"
        sherlock = {"hits": [{"name": "Foro", "url": f"https://foro.com/u/{largo}",
                              "source_site": "foro.com"}],
                    "error": None, "incomplete": False, "raw_count": 1}
        holehe = {"hits": [{"name": "Foro", "url": None,
                            "source_site": "foro.com"}],
                  "error": None, "incomplete": False, "raw_count": 1}
        with patch.object(self.d, "run_sherlock", return_value=sherlock), \
             patch.object(self.d, "run_holehe", return_value=holehe), \
             patch.object(self.d, "run_maigret",
                          return_value={**_vacio(), "skipped": True}):
            summary = self.d.discover([largo], ["x@example.com"])
        for r in self.db.list_accounts():
            self.assertIn(r["confidence"], ("high", "medium", "low"))
            self.assertEqual(r["confidence"], "high")
        self.assertEqual(summary["corroborated"], 0)   # ya estaba arriba

    def test_sitios_distintos_no_se_corroboran(self):
        sherlock = {"hits": [{"name": "A", "url": "https://a.com/u/ana",
                              "source_site": "a.com"}],
                    "error": None, "incomplete": False, "raw_count": 1}
        holehe = {"hits": [{"name": "B", "url": None, "source_site": "b.com"}],
                  "error": None, "incomplete": False, "raw_count": 1}
        with patch.object(self.d, "run_sherlock", return_value=sherlock), \
             patch.object(self.d, "run_holehe", return_value=holehe), \
             patch.object(self.d, "run_maigret",
                          return_value={**_vacio(), "skipped": True}):
            summary = self.d.discover(["ana"], ["ana@example.com"])
        self.assertEqual(summary["corroborated"], 0)
        for r in self.db.list_accounts():
            self.assertNotIn("corrob_cruzada", self._reasons(r))


class HibpCorroboracionTest(IsolatedTestCase):
    """HIBP corrobora solo cuando la brecha es de un sitio real.

    `Domain` vacío ya se descarta en hibp.py (nunca llega a ser hit). Aquí
    cubrimos las que SÍ traen dominio pero no son evidencia de una cuenta:
    volcados agregados, listas de spam, brechas sin verificar.
    """

    def setUp(self):
        super().setUp()
        from rastrillo import config, db, discovery, hibp
        self.config = config
        self.db = db
        self.d = discovery
        self.hibp = hibp
        db.init()

    def _breach(self, **over):
        b = {"Name": "Foro", "Title": "Foro", "Domain": "foro.com",
             "BreachDate": "2015-01-01", "PwnCount": 10,
             "IsVerified": True, "IsSpamList": False, "IsFabricated": False}
        b.update(over)
        return b

    def _fake_urlopen(self, payload):
        class _Resp:
            def read(self_): return json.dumps(payload).encode()
            def __enter__(self_): return self_
            def __exit__(self_, *a): pass
        return lambda *a, **k: _Resp()

    def _discover_con(self, breaches):
        """sherlock ve el mismo sitio por username; hibp lo ve por email."""
        sherlock = {"hits": [{"name": "Foro", "url": "https://foro.com/u/ana",
                              "source_site": "foro.com"}],
                    "error": None, "incomplete": False, "raw_count": 1}
        self.config.HIBP_API_KEY = "fake"
        with patch.object(self.d, "run_sherlock", return_value=sherlock), \
             patch.object(self.d, "run_holehe", return_value=_vacio()), \
             patch.object(self.d, "run_maigret",
                          return_value={**_vacio(), "skipped": True}), \
             patch("rastrillo.hibp.urllib.request.urlopen",
                   side_effect=self._fake_urlopen(breaches)):
            return self.d.discover(["ana"], ["ana@example.com"])

    def _reasons(self, row):
        return {r["code"] for r in self.db.parse_reasons(row["confidence_reasons"])}

    # --- Domain vacío: ni siquiera es hit ----------------------------------
    def test_domain_vacio_no_entra_al_discovery(self):
        """Un volcado agregado tipo "Collection #1" viene con Domain vacío:
        hibp.py lo salta, así que nunca hay fila que pueda corroborar."""
        self.config.HIBP_API_KEY = "fake"
        payload = [self._breach(Title="Collection #1", Domain="",
                                IsVerified=False)]
        with patch("rastrillo.hibp.urllib.request.urlopen",
                   side_effect=self._fake_urlopen(payload)):
            res = self.hibp.run_hibp("ana@example.com")
        self.assertEqual(res["hits"], [])
        self.assertEqual(res["raw_count"], 0)

    # --- Con dominio, pero no es un sitio real -----------------------------
    def test_brecha_normal_si_corrobora(self):
        summary = self._discover_con([self._breach()])
        sher = next(r for r in self.db.list_accounts() if r["source"] == "sherlock")
        self.assertIn("corrob_cruzada", self._reasons(sher))
        self.assertEqual(summary["corroborated"], 1)

    def test_spam_list_no_corrobora(self):
        summary = self._discover_con([self._breach(IsSpamList=True)])
        self._assert_sin_corroborar(summary)

    def test_fabricada_no_corrobora(self):
        summary = self._discover_con([self._breach(IsFabricated=True)])
        self._assert_sin_corroborar(summary)

    def test_sin_verificar_no_corrobora(self):
        summary = self._discover_con([self._breach(IsVerified=False)])
        self._assert_sin_corroborar(summary)

    def _assert_sin_corroborar(self, summary):
        filas = self.db.list_accounts()
        hib = next(r for r in filas if r["source"] == "hibp")
        sher = next(r for r in filas if r["source"] == "sherlock")
        # La fila de hibp SIGUE en el discovery (no la escondemos), marcada.
        self.assertIn("hibp_no_sitio", self._reasons(hib))
        self.assertEqual(hib["confidence"], "medium")
        # Pero no corrobora: sherlock no sube ni recibe el motivo.
        self.assertNotIn("corrob_cruzada", self._reasons(sher))
        self.assertEqual(summary["corroborated"], 0)
        # 'ana' + /u/ana = low + bump de path = medium. Sin corroborar, ahí queda.
        self.assertEqual(sher["confidence"], "medium")


class MotivosPersistenciaTest(IsolatedTestCase):
    """Migración de la columna y round-trip de los motivos."""

    def setUp(self):
        super().setUp()
        from rastrillo import db
        self.db = db

    # --- 11. Migración idempotente -----------------------------------------
    def test_migracion_idempotente(self):
        self.db.init()
        self.db.init()          # dos veces no debe fallar
        self.db.init()
        with self.db.connect() as con:
            cols = {r["name"] for r in
                    con.execute("PRAGMA table_info(accounts)").fetchall()}
        self.assertIn("confidence_reasons", cols)
        # `confidence` sigue existiendo y no se ha duplicado nada.
        self.assertIn("confidence", cols)

    def test_migracion_sobre_db_vieja_sin_la_columna(self):
        """Simula una DB anterior: creamos la tabla sin `confidence_reasons` y
        comprobamos que init() la añade sin perder filas."""
        self.db.init()
        with self.db.connect() as con:
            con.execute("ALTER TABLE accounts RENAME TO accounts_old")
            con.execute("CREATE TABLE accounts ("
                        "id INTEGER PRIMARY KEY AUTOINCREMENT,"
                        "platform TEXT NOT NULL, identifier TEXT,"
                        "status TEXT NOT NULL DEFAULT 'found', confidence TEXT)")
            con.execute("INSERT INTO accounts (platform, identifier, confidence)"
                        " VALUES ('reddit','yo','low')")
        self.db.init()
        with self.db.connect() as con:
            cols = {r["name"] for r in
                    con.execute("PRAGMA table_info(accounts)").fetchall()}
            filas = con.execute("SELECT * FROM accounts").fetchall()
        self.assertIn("confidence_reasons", cols)
        self.assertEqual(len(filas), 1)
        self.assertEqual(filas[0]["identifier"], "yo")

    # --- 12. Round-trip, igual que action_meta ----------------------------
    def test_round_trip_de_motivos(self):
        self.db.init()
        motivos = [
            {"code": "tramo_corto", "desc": "username de 5 caracteres"},
            {"code": "bump_subdominio", "desc": "es el subdominio del sitio"},
        ]
        aid = self.db.upsert_account(
            "reddit", "yo", source_site="reddit.com", source="sherlock",
            confidence="medium",
            confidence_reasons=self.db.dump_reasons(motivos))
        row = self.db.get_account(aid)
        self.assertEqual(self.db.parse_reasons(row["confidence_reasons"]), motivos)
        # La serialización es JSON, como action_meta, y no escapa el unicode.
        self.assertEqual(json.loads(row["confidence_reasons"]), motivos)

    def test_parse_reasons_tolera_basura(self):
        """Una fila vieja o corrupta no debe romper la vista."""
        self.assertEqual(self.db.parse_reasons(None), [])
        self.assertEqual(self.db.parse_reasons(""), [])
        self.assertEqual(self.db.parse_reasons("{no json"), [])
        self.assertEqual(self.db.parse_reasons('{"code":"x"}'), [])   # no es lista

    def test_dump_reasons_no_escapa_unicode(self):
        raw = self.db.dump_reasons([{"code": "x", "desc": "exposición"}])
        self.assertIn("exposición", raw)

    def test_merge_reasons_no_duplica_codigos(self):
        a = [{"code": "tramo_corto", "desc": "d1"}]
        b = [{"code": "tramo_corto", "desc": "otra"},
             {"code": "bump_path", "desc": "d2"}]
        out = self.db.merge_reasons(a, b)
        self.assertEqual([r["code"] for r in out], ["tramo_corto", "bump_path"])
        self.assertEqual(out[0]["desc"], "d1")     # gana el primero
        # Tolera basura sin romper.
        self.assertEqual(self.db.merge_reasons(None, None), [])
        self.assertEqual(self.db.merge_reasons([], ["no soy dict"]), [])


class MotivosEnLaApiTest(IsolatedTestCase):
    """El endpoint devuelve los motivos ya parseados (la UI no hace json.parse)."""

    def test_api_accounts_devuelve_lista(self):
        from tests.helpers import auth_client
        from rastrillo import db
        db.init()
        db.upsert_account("reddit", "yo", source_site="reddit.com",
                          source="sherlock", confidence="low",
                          confidence_reasons=db.dump_reasons(
                              [{"code": "tramo_muy_corto", "desc": "muy común"}]))
        client = auth_client()
        r = client.get("/api/accounts", headers=self.hdr())
        self.assertEqual(r.status_code, 200)
        acc = r.json()["accounts"][0]
        self.assertEqual(acc["confidence_reasons"],
                         [{"code": "tramo_muy_corto", "desc": "muy común"}])


if __name__ == "__main__":
    unittest.main()
