"""Paso 3, Entrega 2: señal agregada por sitio, INFORMATIVA.

Si el usuario descarta el mismo sitio con varios identificadores distintos,
eso es evidencia de que el sitio genera ruido. Pero es una inferencia sobre
una muestra minúscula, así que se calcula y se enseña — y nada más.

Lo que este archivo fija:
  - el umbral (mínimo 2 identificadores DISTINTOS),
  - que 2 descartes del MISMO identificador no son señal,
  - que la señal NO mueve `confidence` en ningún caso,
  - que no existe ningún endpoint que actúe sobre ella.

El precedente que justifica la cautela está en el canario: se construyó sobre
una hipótesis razonable y sus tres detecciones resultaron ser errores suyos.
No se conecta una señal sin medir a algo que escribe en la DB.
"""
from .helpers import IsolatedTestCase, auth_client


class SenalSitioTest(IsolatedTestCase):

    def setUp(self):
        super().setUp()
        from rastrillo import db, server
        self.db = db
        self.server = server
        db.init()
        self.client = auth_client()

    def _alta(self, host, ident, conf="high"):
        return self.db.upsert_account(host.split(".")[0], ident,
                                      source="sherlock", source_site=host,
                                      display_name=host, status="found",
                                      confidence=conf)

    def _payload(self, aid):
        r = self.client.get("/api/accounts", headers=self.hdr())
        self.assertEqual(r.status_code, 200)
        return next(a for a in r.json()["accounts"] if a["id"] == aid)

    # ── umbral ──
    def test_un_identificador_sin_senal(self):
        aid = self._alta("foro.com", "je7remy")
        self.db.remember_discard("foro.com", "mar")
        self.assertIsNone(self._payload(aid)["site_discards"])

    def test_dos_identificadores_distintos_hay_senal(self):
        aid = self._alta("foro.com", "je7remy")
        self.db.remember_discard("foro.com", "mar")
        self.db.remember_discard("foro.com", "ana")
        self.assertEqual(self._payload(aid)["site_discards"], 2)

    def test_el_mismo_identificador_dos_veces_no_es_senal(self):
        """El UNIQUE del par ya lo garantiza; aquí se fija como contrato."""
        aid = self._alta("foro.com", "je7remy")
        self.db.remember_discard("foro.com", "mar", reason="uno")
        self.db.remember_discard("foro.com", "mar", reason="dos")
        self.assertEqual(len(self.db.list_discards()), 1)
        self.assertIsNone(self._payload(aid)["site_discards"])

    def test_la_senal_es_por_sitio_no_se_contagia(self):
        propia = self._alta("foro.com", "je7remy")
        ajena = self._alta("otro.com", "je7remy")
        self.db.remember_discard("foro.com", "mar")
        self.db.remember_discard("foro.com", "ana")
        self.assertEqual(self._payload(propia)["site_discards"], 2)
        self.assertIsNone(self._payload(ajena)["site_discards"])

    def test_sin_memoria_ninguna_fila_trae_senal(self):
        aid = self._alta("foro.com", "je7remy")
        self.assertIsNone(self._payload(aid)["site_discards"])

    def test_deshacer_baja_la_senal(self):
        """No se persiste en la fila: se recalcula, así que el deshacer la baja."""
        aid = self._alta("foro.com", "je7remy")
        self.db.remember_discard("foro.com", "mar")
        self.db.remember_discard("foro.com", "ana")
        self.assertEqual(self._payload(aid)["site_discards"], 2)
        self.db.forget_discard("foro.com", "ana")
        self.assertIsNone(self._payload(aid)["site_discards"])

    def test_umbral_declarado_en_un_solo_sitio(self):
        self.assertEqual(self.server._UMBRAL_SENAL_SITIO, 2)

    # ── la regla dura: no mueve nada ──
    def test_la_senal_no_mueve_confidence_en_ningun_caso(self):
        """Barrido: para cada tramo y cada nivel de señal, `confidence` es el
        mismo antes y después. Ni sube ni baja."""
        casos = []
        for i, tramo in enumerate(("high", "medium", "low")):
            for j, descartes in enumerate((0, 1, 2, 5)):
                host = f"s{i}{j}.com"
                aid = self._alta(host, "je7remy", conf=tramo)
                for k in range(descartes):
                    self.db.remember_discard(host, f"ruido{k}")
                casos.append((aid, host, tramo, descartes))

        r = self.client.get("/api/accounts", headers=self.hdr())
        payload = {a["id"]: a for a in r.json()["accounts"]}
        for aid, host, tramo, descartes in casos:
            fila = self.db.get_account(aid)
            self.assertEqual(fila["confidence"], tramo,
                             f"{host}: la señal movió confidence en la DB")
            self.assertEqual(payload[aid]["confidence"], tramo,
                             f"{host}: la señal movió confidence en la API")
            esperado = descartes if descartes >= 2 else None
            self.assertEqual(payload[aid]["site_discards"], esperado, host)

    def test_la_senal_no_toca_el_estado_ni_verifiability(self):
        aid = self._alta("foro.com", "je7remy")
        self.db.remember_discard("foro.com", "mar")
        self.db.remember_discard("foro.com", "ana")
        fila = self._payload(aid)
        self.assertEqual(fila["status"], "found")
        self.assertIsNone(fila["verifiability"])
        self.assertEqual(fila["owned"], 0)

    def test_no_existe_endpoint_que_actue_sobre_la_senal(self):
        """Ninguna ruta registrada menciona la señal agregada. Si algún día se
        añade una acción sobre ella, este test obliga a pasar por aquí."""
        rutas = {getattr(r, "path", "") for r in self.server.app.routes}
        for r in rutas:
            self.assertNotIn("site-discard", r)
            self.assertNotIn("noisy-site", r)
            self.assertNotIn("discard-site", r)

    def test_la_senal_no_persiste_en_confidence_reasons(self):
        """Se calcula al vuelo; no ensucia los motivos guardados de la fila."""
        aid = self._alta("foro.com", "je7remy")
        self.db.remember_discard("foro.com", "mar")
        self.db.remember_discard("foro.com", "ana")
        self.client.get("/api/accounts", headers=self.hdr())
        motivos = self.db.parse_reasons(
            self.db.get_account(aid)["confidence_reasons"])
        self.assertEqual([m.get("code") for m in motivos], [])
