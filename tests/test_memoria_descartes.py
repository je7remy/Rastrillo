"""Paso 3, Entrega 1: la memoria de decisiones de triage.

El canario ataca los falsos positivos de sitios que responden igual para
cualquiera. Los dos casos confirmados del usuario NO son de esa clase:
Periscope cerró en 2021 (discrimina perfectamente, pero no puede haber cuenta
activa) y HudsonRock es una consulta de exposición a infostealers (discrimina
bien, pero no es un sitio donde se tengan cuentas). Ninguna heurística los
resuelve; lo que los resuelve es que el usuario lo diga UNA vez y la
herramienta lo recuerde.

Hasta ahora la decisión vivía solo en la fila de `accounts` y "Limpiar todas
las cuentas" se la llevaba por delante. Aquí se fija que:

  - marcar `not_mine` deja entrada en `discard_memory`,
  - `clear_accounts()` NO la toca (ese es el punto entero),
  - un escaneo posterior mete el hallazgo ya descartado y CON MOTIVO,
  - la clave es el PAR (sitio, identificador), no el sitio suelto,
  - deshacer borra la entrada y devuelve el hallazgo al triage,
  - la migración es idempotente.
"""
import time
from unittest.mock import patch

from .helpers import IsolatedTestCase, auth_client


def _vacio():
    return {"hits": [], "error": None, "incomplete": False, "raw_count": 0}


def _sherlock(hits):
    return {"hits": hits, "error": None, "incomplete": False,
            "raw_count": len(hits)}


class MemoriaDescartesDBTest(IsolatedTestCase):
    """La tabla y sus helpers, sin servidor de por medio."""

    def setUp(self):
        super().setUp()
        from rastrillo import db
        self.db = db
        db.init()

    def _tablas(self):
        with self.db.connect() as con:
            return {r["name"] for r in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}

    # ── esquema ──
    def test_la_tabla_existe_en_una_db_nueva(self):
        self.assertIn("discard_memory", self._tablas())

    def test_migracion_idempotente(self):
        """`init()` varias veces no falla, no duplica la tabla ni el índice."""
        for _ in range(3):
            self.db.init()
        self.assertIn("discard_memory", self._tablas())
        with self.db.connect() as con:
            idx = [r["name"] for r in con.execute(
                "SELECT name FROM sqlite_master WHERE type='index' "
                "AND tbl_name='discard_memory'").fetchall()]
        self.assertEqual(idx.count("idx_discard_memory_par"), 1)

    def test_migracion_sobre_db_anterior_al_paso_3(self):
        """Una DB que no conoce la tabla la gana sin perder nada."""
        with self.db.connect() as con:
            con.execute("DROP TABLE discard_memory")
            con.execute("INSERT INTO accounts (platform, identifier, status) "
                        "VALUES ('viejo','ana','found')")
        self.assertNotIn("discard_memory", self._tablas())
        self.db.init()
        self.assertIn("discard_memory", self._tablas())
        self.assertEqual(len(self.db.list_accounts()), 1)

    # ── helpers ──
    def test_remember_y_get(self):
        self.assertTrue(self.db.remember_discard("periscope.tv", "je7remy",
                                                 reason="cerró en 2021"))
        fila = self.db.get_discard("periscope.tv", "je7remy")
        self.assertIsNotNone(fila)
        self.assertEqual(fila["reason"], "cerró en 2021")
        self.assertGreater(fila["created_at"], 0)

    def test_es_por_par_no_por_sitio(self):
        """Que `mar` no sea mío en un sitio no dice nada sobre `je7remy` allí."""
        self.db.remember_discard("foro.com", "mar")
        self.assertIsNotNone(self.db.get_discard("foro.com", "mar"))
        self.assertIsNone(self.db.get_discard("foro.com", "je7remy"))

    def test_repetir_no_duplica_y_refresca(self):
        self.db.remember_discard("foro.com", "ana", reason="uno")
        primero = self.db.get_discard("foro.com", "ana")["created_at"]
        time.sleep(0.01)
        self.db.remember_discard("foro.com", "ana", reason="dos")
        self.assertEqual(len(self.db.list_discards()), 1)
        fila = self.db.get_discard("foro.com", "ana")
        self.assertEqual(fila["reason"], "dos")
        self.assertGreaterEqual(fila["created_at"], primero)

    def test_normaliza_mayusculas_y_espacios(self):
        self.db.remember_discard("Foro.COM", " Ana ")
        self.assertIsNotNone(self.db.get_discard("foro.com", "ana"))

    def test_par_incompleto_no_se_guarda(self):
        """Sin sitio o sin identificador no hay decisión que recordar."""
        self.assertFalse(self.db.remember_discard(None, "ana"))
        self.assertFalse(self.db.remember_discard("foro.com", ""))
        self.assertEqual(self.db.list_discards(), [])
        # Y consultarlo tampoco revienta.
        self.assertIsNone(self.db.get_discard(None, "ana"))
        self.assertFalse(self.db.forget_discard("foro.com", None))

    def test_forget_borra_y_dice_si_existia(self):
        self.db.remember_discard("foro.com", "ana")
        self.assertTrue(self.db.forget_discard("foro.com", "ana"))
        self.assertIsNone(self.db.get_discard("foro.com", "ana"))
        # Segunda vez: ya no estaba.
        self.assertFalse(self.db.forget_discard("foro.com", "ana"))

    def test_clear_discard_memory_vacia_y_hace_snapshot(self):
        self.db.remember_discard("a.com", "ana")
        self.db.remember_discard("b.com", "ana")
        n = self.db.clear_discard_memory()
        self.assertEqual(n, 2)
        self.assertEqual(self.db.list_discards(), [])
        backups = list((self.HOME / "backups").glob("rastrillo_*.db"))
        self.assertTrue(backups)

    # ── EL punto del paso: clear_accounts no la toca ──
    def test_clear_accounts_no_borra_la_memoria(self):
        aid = self.db.upsert_account("periscope", "je7remy",
                                     source="sherlock",
                                     source_site="periscope.tv",
                                     status="found")
        self.db.remember_discard("periscope.tv", "je7remy", reason="triage")
        self.assertEqual(len(self.db.list_accounts()), 1)

        self.db.clear_accounts()

        # Las cuentas se van...
        self.assertEqual(self.db.list_accounts(), [])
        # ...y la memoria queda.
        self.assertEqual(len(self.db.list_discards()), 1)
        self.assertIsNotNone(self.db.get_discard("periscope.tv", "je7remy"))
        self.assertIsNotNone(aid)


class MemoriaEnDiscoveryTest(IsolatedTestCase):
    """Qué pasa en el siguiente escaneo. Sin red: las fuentes van parcheadas."""

    def setUp(self):
        super().setUp()
        from rastrillo import db, discovery
        self.db = db
        self.d = discovery
        db.init()

    def _escanear(self, hits, usernames):
        with patch.object(self.d, "run_sherlock", return_value=_sherlock(hits)), \
             patch.object(self.d, "run_holehe", return_value=_vacio()), \
             patch.object(self.d, "run_maigret",
                          return_value={**_vacio(), "skipped": True}), \
             patch.object(self.d, "_corroborar_entre_fuentes", return_value=None):
            return self.d.discover(usernames, [])

    def _fila(self, host):
        return next((r for r in self.db.list_accounts()
                     if (r["source_site"] or "") == host), None)

    def _codigos(self, fila):
        return {r["code"] for r in self.db.parse_reasons(fila["confidence_reasons"])}

    def test_hallazgo_recordado_entra_descartado_y_con_motivo(self):
        self.db.remember_discard("periscope.tv", "je7remy", reason="triage")
        hit = {"name": "Periscope", "url": "https://periscope.tv/je7remy",
               "source_site": "periscope.tv"}
        summary = self._escanear([hit], ["je7remy"])

        fila = self._fila("periscope.tv")
        self.assertIsNotNone(fila)
        self.assertEqual(fila["status"], "not_mine")
        # No en silencio: el motivo tiene que estar y decir que ya lo descartó.
        self.assertIn("descartado_antes", self._codigos(fila))
        self.assertEqual(summary["remembered_discards"], 1)

    def test_mismo_sitio_identificador_distinto_entra_normal(self):
        """La clave es el PAR. Descartar `mar` no descarta `je7remy`."""
        self.db.remember_discard("periscope.tv", "mar")
        hit = {"name": "Periscope", "url": "https://periscope.tv/je7remy",
               "source_site": "periscope.tv"}
        summary = self._escanear([hit], ["je7remy"])

        fila = self._fila("periscope.tv")
        self.assertEqual(fila["status"], "found")
        self.assertNotIn("descartado_antes", self._codigos(fila))
        self.assertEqual(summary["remembered_discards"], 0)

    def test_sin_memoria_todo_sigue_igual(self):
        hit = {"name": "Foro", "url": "https://foro.com/u/je7remy",
               "source_site": "foro.com"}
        self._escanear([hit], ["je7remy"])
        fila = self._fila("foro.com")
        self.assertEqual(fila["status"], "found")
        self.assertNotIn("descartado_antes", self._codigos(fila))

    def test_keep_platforms_manda_sobre_la_memoria(self):
        """Invariante 5: KEEP siempre a `skipped`, aunque hubiera decisión."""
        self.db.remember_discard("github.com", "je7remy")
        hit = {"name": "GitHub", "url": "https://github.com/je7remy",
               "source_site": "github.com"}
        self._escanear([hit], ["je7remy"])
        self.assertEqual(self._fila("github.com")["status"], "skipped")

    def test_el_ciclo_completo_limpiar_y_reescanear(self):
        """El caso real: descarto, limpio todo, reescaneo y no vuelve a triage."""
        hit = {"name": "HudsonRock",
               "url": "https://cavalier.hudsonrock.com/je7remy",
               "source_site": "cavalier.hudsonrock.com"}
        self._escanear([hit], ["je7remy"])
        self.assertEqual(self._fila("cavalier.hudsonrock.com")["status"], "found")

        # El usuario lo descarta (lo que hace el endpoint de triage).
        self.db.remember_discard("cavalier.hudsonrock.com", "je7remy",
                                 reason="triage")
        self.db.clear_accounts()
        self._escanear([hit], ["je7remy"])

        fila = self._fila("cavalier.hudsonrock.com")
        self.assertEqual(fila["status"], "not_mine")
        self.assertIn("descartado_antes", self._codigos(fila))


class MemoriaEnEndpointsTest(IsolatedTestCase):
    """Los dos únicos puntos que escriben `not_mine`, y el deshacer."""

    def setUp(self):
        super().setUp()
        from rastrillo import db
        self.db = db
        db.init()
        self.client = auth_client()

    def _alta(self, host="periscope.tv", ident="je7remy", conf="high"):
        return self.db.upsert_account("periscope", ident, source="sherlock",
                                      source_site=host, display_name="Periscope",
                                      status="found", confidence=conf)

    def test_marcar_not_mine_deja_entrada(self):
        aid = self._alta()
        r = self.client.post(f"/api/accounts/{aid}/own",
                             json={"owned": False}, headers=self.hdr())
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["discard_memory"])
        self.assertEqual(self.db.get_account(aid)["status"], "not_mine")
        self.assertIsNotNone(self.db.get_discard("periscope.tv", "je7remy"))

    def test_discard_low_tambien_deja_entrada(self):
        """Cuenta como decisión explícita: es un botón que pulsa el usuario."""
        self._alta(host="a.com", ident="mar", conf="low")
        self._alta(host="b.com", ident="mar", conf="high")
        r = self.client.post("/api/accounts/discard-low", json={},
                             headers=self.hdr())
        self.assertEqual(r.json()["discarded"], 1)
        self.assertIsNotNone(self.db.get_discard("a.com", "mar"))
        self.assertIsNone(self.db.get_discard("b.com", "mar"))

    def test_deshacer_borra_la_entrada_y_devuelve_a_triage(self):
        aid = self._alta()
        self.client.post(f"/api/accounts/{aid}/own",
                         json={"owned": False}, headers=self.hdr())
        self.assertIsNotNone(self.db.get_discard("periscope.tv", "je7remy"))

        r = self.client.post(f"/api/accounts/{aid}/own",
                             json={"owned": True}, headers=self.hdr())
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["discard_memory_forgotten"])
        # La entrada desaparece...
        self.assertIsNone(self.db.get_discard("periscope.tv", "je7remy"))
        # ...y la fila vuelve a estar disponible en vez de quedarse descartada.
        self.assertEqual(self.db.get_account(aid)["status"], "found")

    def test_deshacer_hace_que_el_siguiente_escaneo_la_traiga_de_vuelta(self):
        from rastrillo import discovery
        aid = self._alta()
        self.client.post(f"/api/accounts/{aid}/own",
                         json={"owned": False}, headers=self.hdr())
        self.client.post(f"/api/accounts/{aid}/own",
                         json={"owned": True}, headers=self.hdr())
        self.db.clear_accounts()

        hit = {"name": "Periscope", "url": "https://periscope.tv/je7remy",
               "source_site": "periscope.tv"}
        with patch.object(discovery, "run_sherlock", return_value=_sherlock([hit])), \
             patch.object(discovery, "run_holehe", return_value=_vacio()), \
             patch.object(discovery, "run_maigret",
                          return_value={**_vacio(), "skipped": True}):
            discovery.discover(["je7remy"], [])

        fila = next(r for r in self.db.list_accounts()
                    if r["source_site"] == "periscope.tv")
        self.assertEqual(fila["status"], "found")

    def test_confirmar_es_mia_sin_descarte_previo_no_cambia_el_estado(self):
        """Regresión: "Es mía" sobre una fila normal la deja donde estaba."""
        aid = self._alta()
        r = self.client.post(f"/api/accounts/{aid}/own",
                             json={"owned": True}, headers=self.hdr())
        self.assertFalse(r.json()["discard_memory_forgotten"])
        self.assertEqual(self.db.get_account(aid)["status"], "found")
        self.assertEqual(self.db.get_account(aid)["owned"], 1)

    def test_no_se_graba_nada_por_inferencia(self):
        """Nadie escribe en la memoria salvo los dos endpoints de decisión.

        Un escaneo entero, con canario incluido, no puede inventar decisiones.
        """
        from rastrillo import discovery
        hit = {"name": "Foro", "url": "https://foro.com/u/mar",
               "source_site": "foro.com"}
        with patch.object(discovery, "run_sherlock", return_value=_sherlock([hit])), \
             patch.object(discovery, "run_holehe", return_value=_vacio()), \
             patch.object(discovery, "run_maigret",
                          return_value={**_vacio(), "skipped": True}):
            discovery.discover(["mar"], [])
        self.assertEqual(self.db.list_discards(), [])
