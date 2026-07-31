"""Paso 2C, Entrega 2: calibrar el canario contra el catálogo de Sherlock.

Dos huecos reales, encontrados sondeando los sitios de un escaneo de verdad:

  1. **Redacciones que faltaban.** Steam responde 200 con
     `<h3>The specified profile could not be found.</h3>`. Ninguna frase de la
     lista casaba (todas asumían "not found" contiguo), así que el canario
     concluía `indiscriminado` sobre un sitio que SÍ discrimina.
  2. **La URL sondeada no era la que produjo el hit.** El `data.json` de
     Sherlock define `urlProbe` para los sitios que se comprueban contra una
     API. Duolingo es el caso: `url` es una SPA que devuelve el mismo HTML para
     cualquiera, y Sherlock ni la mira — consulta
     `https://www.duolingo.com/2017-06-30/users?username={}` y compara contra
     `errorMsg` = `{"users":[]}`.

Y un caso que NO hay que "arreglar": un sitio que renderiza el perfil en el
cliente y no tiene `urlProbe` seguirá dando `indiscriminado`, porque es la
verdad sobre su respuesta.

Todo offline: el catálogo se inyecta con un `data.json` de mentira salvo en los
tests marcados como de contrato, que leen el paquete instalado pero no tocan la
red.
"""
from unittest.mock import patch

from .helpers import IsolatedTestCase


# --- Catálogo de mentira, con la forma real de data.json --------------------
_CATALOGO = {
    "$schema": "no-es-un-sitio",
    "SitioSPA": {
        "url": "https://spa.test/profile/{}",
        "urlMain": "https://spa.test/",
        "urlProbe": "https://spa.test/api/users?username={}",
        "errorType": "message",
        "errorMsg": '{"users":[]}',
    },
    "SitioMensaje": {
        "url": "https://msg.test/id/{}/",
        "urlMain": "https://msg.test/",
        "errorType": "message",
        "errorMsg": "The specified profile could not be found",
    },
    "SitioStatus": {
        "url": "https://status.test/{}",
        "urlMain": "https://status.test/",
        "errorType": "status_code",
    },
    "SitioListaDeMensajes": {
        "url": "https://lista.test/{}",
        "errorType": "message",
        "errorMsg": ["no existe tal cosa", "usuario retirado"],
    },
    # Dos entradas en el MISMO host: solo la forma de la URL desambigua.
    "Doble (User)": {
        "url": "https://doble.test/id/{}",
        "errorType": "message",
        "errorMsg": "perfil de usuario ausente",
    },
    "Doble (Group)": {
        "url": "https://doble.test/groups/{}",
        "errorType": "message",
        "errorMsg": "grupo ausente",
    },
    # Probe que NO lleva el identificador: Discord/Holopin consultan por POST
    # a un endpoint fijo.
    "SitioProbeSinId": {
        "url": "https://fijo.test/{}",
        "urlProbe": "https://fijo.test/api/check",
        "errorType": "message",
        "errorMsg": "taken",
    },
}


class _CatalogoFalso:
    """Context manager: sustituye el data.json y limpia las cachés LRU."""

    def __init__(self, catalogo, data=None):
        self.catalogo = catalogo
        self.data = _CATALOGO if data is None else data

    def __enter__(self):
        self._p = patch.object(self.catalogo, "cargar",
                               return_value={k: v for k, v in self.data.items()
                                             if isinstance(v, dict)
                                             and isinstance(v.get("url"), str)})
        self._p.start()
        self.catalogo._indice_por_host.cache_clear()
        return self.catalogo

    def __exit__(self, *a):
        self._p.stop()
        self.catalogo._indice_por_host.cache_clear()
        return False


class TestCatalogo(IsolatedTestCase):

    def setUp(self):
        super().setUp()
        from rastrillo import catalogo
        self.cat = catalogo

    # --- Emparejar fila ↔ entrada -----------------------------------------
    def test_empareja_por_forma_de_la_url(self):
        with _CatalogoFalso(self.cat) as cat:
            nombre, _ = cat.buscar("https://msg.test/id/jeremy/", "jeremy")
            self.assertEqual(nombre, "SitioMensaje")

    def test_desambigua_dos_entradas_del_mismo_host(self):
        """`steamcommunity.com` tiene "(User)" y "(Group)": por host solo,
        elegiríamos a cara o cruz."""
        with _CatalogoFalso(self.cat) as cat:
            self.assertEqual(cat.buscar("https://doble.test/id/ana", "ana")[0],
                             "Doble (User)")
            self.assertEqual(cat.buscar("https://doble.test/groups/ana", "ana")[0],
                             "Doble (Group)")

    def test_tolera_caja_y_barra_final(self):
        with _CatalogoFalso(self.cat) as cat:
            self.assertIsNotNone(cat.buscar("https://msg.test/id/JEREMY", "JEREMY"))
            self.assertIsNotNone(cat.buscar("https://msg.test/id/jeremy", "jeremy"))

    def test_sin_entrada_devuelve_none(self):
        with _CatalogoFalso(self.cat) as cat:
            self.assertIsNone(cat.buscar("https://desconocido.test/u/ana", "ana"))
            self.assertIsNone(cat.buscar("", "ana"))
            self.assertIsNone(cat.buscar("https://msg.test/id/ana", ""))
            # Mismo host pero otra forma de URL: no es la misma entrada.
            self.assertIsNone(cat.buscar("https://msg.test/otra/ana", "ana"))

    def test_ignora_las_claves_que_no_son_sitios(self):
        """`data.json` mezcla metadatos ($schema) con entradas de sitio."""
        with _CatalogoFalso(self.cat) as cat:
            self.assertNotIn("$schema", cat.cargar())

    # --- Marcadores --------------------------------------------------------
    def test_marcadores_solo_con_errortype_message(self):
        with _CatalogoFalso(self.cat) as cat:
            self.assertEqual(cat.marcadores(cat.cargar()["SitioMensaje"]),
                             ["The specified profile could not be found"])
            self.assertEqual(cat.marcadores(cat.cargar()["SitioStatus"]), [],
                             "errorType=status_code no aporta texto del cuerpo")

    def test_marcadores_acepta_lista(self):
        with _CatalogoFalso(self.cat) as cat:
            self.assertEqual(cat.marcadores(cat.cargar()["SitioListaDeMensajes"]),
                             ["no existe tal cosa", "usuario retirado"])

    def test_marcadores_de_basura_no_revienta(self):
        with _CatalogoFalso(self.cat) as cat:
            for basura in (None, {}, {"errorType": "message"},
                           {"errorType": "message", "errorMsg": 42},
                           {"errorType": "message", "errorMsg": ["", "  "]}):
                with self.subTest(basura=basura):
                    self.assertEqual(cat.marcadores(basura), [])

    # --- Plantilla de sonda ------------------------------------------------
    def test_urlprobe_gana_a_la_url_visible(self):
        with _CatalogoFalso(self.cat) as cat:
            s = cat.plantilla_sonda("https://spa.test/profile/jeremy", "jeremy")
            self.assertEqual(s["url"], "https://spa.test/api/users?username=jeremy")
            self.assertEqual(s["origen"], "urlProbe")
            self.assertEqual(s["marcadores"], ['{"users":[]}'])

    def test_sin_urlprobe_se_usa_la_del_hit(self):
        with _CatalogoFalso(self.cat) as cat:
            s = cat.plantilla_sonda("https://msg.test/id/jeremy/", "jeremy")
            self.assertEqual(s["url"], "https://msg.test/id/jeremy/")
            self.assertEqual(s["origen"], "url")

    def test_sin_catalogo_degrada_a_la_url_del_hit(self):
        """Hits de maigret, sitios retirados del catálogo, sherlock ausente."""
        with _CatalogoFalso(self.cat) as cat:
            s = cat.plantilla_sonda("https://desconocido.test/u/ana", "ana")
            self.assertEqual(s["url"], "https://desconocido.test/u/ana")
            self.assertEqual(s["origen"], "sin_catalogo")
            self.assertEqual(s["marcadores"], [])
            self.assertIsNone(s["sitio"])

    def test_catalogo_ilegible_no_rompe_nada(self):
        """Si sherlock-project no está instalado o cambió de layout."""
        with patch.object(self.cat, "cargar", return_value={}):
            self.cat._indice_por_host.cache_clear()
            try:
                s = self.cat.plantilla_sonda("https://x.test/u/ana", "ana")
                self.assertEqual(s["origen"], "sin_catalogo")
                self.assertEqual(s["url"], "https://x.test/u/ana")
            finally:
                self.cat._indice_por_host.cache_clear()

    def test_plantilla_con_llaves_literales_no_revienta(self):
        """`replace` y no `format`: hay plantillas con `{` que no es el hueco."""
        data = {"Raro": {"url": "https://raro.test/{}?f={x}",
                         "errorType": "status_code"}}
        with _CatalogoFalso(self.cat, data) as cat:
            s = cat.plantilla_sonda("https://raro.test/ana?f={x}", "ana")
            self.assertEqual(s["sitio"], "Raro")


class TestMarcadoresNuevos(IsolatedTestCase):
    """Una redacción por test: cada frase añadida se justifica sola."""

    def setUp(self):
        super().setUp()
        from rastrillo import canario
        self.c = canario

    def test_steam_la_redaccion_que_faltaba(self):
        """El cuerpo real que devolvió steamcommunity.com para un falso."""
        cuerpo = ("<h1>Sorry!</h1><p>An error was encountered while processing "
                  "your request:</p><h3>The specified profile could not be "
                  "found.</h3>")
        self.assertTrue(self.c._marcadores_en(cuerpo))

    def test_could_not_be_found_en_los_seis_idiomas(self):
        muestras = {
            "en": "The specified profile could not be found.",
            "en-apostrofe": "That user couldn't be found.",
            "es": "El usuario no se ha podido encontrar.",
            "es-2": "No pudimos encontrar ese perfil.",
            "ru": "Не удалось найти пользователя.",
            "pt-BR": "Não foi possível encontrar este perfil.",
            "fr": "Impossible de trouver cet utilisateur.",
            "de": "Das Profil konnte nicht gefunden werden.",
        }
        for etiqueta, texto in muestras.items():
            with self.subTest(idioma=etiqueta):
                self.assertTrue(self.c._marcadores_en(texto),
                                f"sin marcador para {etiqueta}: {texto!r}")

    def test_los_marcadores_viejos_siguen_valiendo(self):
        """La Entrega 2 solo AÑADE: nada de lo que ya se detectaba se pierde."""
        for texto in ("Sorry, user not found here.", "El usuario no encontrado.",
                      "Пользователь не найден.", "Usuário não encontrado.",
                      "Utilisateur introuvable.", "Benutzer nicht gefunden."):
            with self.subTest(texto=texto):
                self.assertTrue(self.c._marcadores_en(texto))

    def test_una_pagina_de_perfil_normal_no_dispara_marcadores(self):
        """Las frases nuevas no pueden aparecer en un perfil que sí existe."""
        for cuerpo in (
            "<h1>jeremy</h1><p>Miembro desde 2019. 42 publicaciones.</p>",
            "<h1>jeremy</h1><p>Nivel 12 · Racha de 300 días</p>",
            "<title>jeremy (@jeremy) · Perfil</title><p>Seguidores: 128</p>",
        ):
            with self.subTest(cuerpo=cuerpo[:40]):
                self.assertEqual(self.c._marcadores_en(cuerpo), [])

    # --- Marcadores del catálogo -------------------------------------------
    def test_marcador_del_catalogo_se_etiqueta_como_sherlock(self):
        hallados = self.c._marcadores_en('{"users":[]}',
                                         extra=['{"users":[]}'])
        self.assertIn('sherlock:{"users":[]}', hallados)

    def test_el_catalogo_no_sustituye_a_la_lista_generica(self):
        """Se buscan LOS DOS: si el catálogo no acierta, la lista sigue ahí."""
        hallados = self.c._marcadores_en("User not found.", extra=["otra cosa"])
        self.assertTrue(hallados)
        self.assertFalse([h for h in hallados if h.startswith("sherlock:")])

    def test_sin_extra_se_comporta_como_antes(self):
        self.assertEqual(self.c._marcadores_en("perfil normal", extra=None), [])
        self.assertEqual(self.c._marcadores_en("", extra=["x"]), [])


class TestCanarioUsaElCatalogo(IsolatedTestCase):
    """El pase completo: qué URL se pide y qué veredicto sale."""

    def setUp(self):
        super().setUp()
        from rastrillo import canario, catalogo, db
        self.c = canario
        self.cat = catalogo
        self.db = db
        db.init()

    def _fila(self, site, url, ident="jeremy", source="sherlock"):
        return self.db.upsert_account(
            site, ident, source=source, source_site=site, status="found",
            confidence="high", profile_url=url, display_name=site)

    def _run(self, responder):
        urls = []

        def _get(u):
            urls.append(u)
            return responder(u)

        res = self.c.run_canario(http_get=_get, url_segura=lambda u: True,
                                 tokens=["abcdefghij", "klmnopqrst"])
        return res, urls

    def test_el_caso_duolingo_se_sondea_la_api_y_sale_discrimina(self):
        """Antes: sondeaba la SPA, dos cuerpos idénticos, `indiscriminado`.
        Ahora: sondea `urlProbe` y el `errorMsg` lo resuelve."""
        self._fila("spa.test", "https://spa.test/profile/jeremy")
        with _CatalogoFalso(self.cat):
            res, urls = self._run(lambda u: (200, u, '{"users":[]}'))
        self.assertEqual(res["veredictos"]["spa.test"], "discrimina")
        for u in urls:
            self.assertIn("/api/users?username=", u,
                          "debe sondear la urlProbe, no la SPA")
            self.assertNotIn("/profile/", u)

    def test_el_caso_steam_mismo_url_pero_con_marcador(self):
        self._fila("msg.test", "https://msg.test/id/jeremy/")
        cuerpo = "<h3>The specified profile could not be found.</h3>"
        with _CatalogoFalso(self.cat):
            res, urls = self._run(lambda u: (200, u, cuerpo))
        self.assertEqual(res["veredictos"]["msg.test"], "discrimina")
        for u in urls:
            self.assertIn("/id/", u, "sin urlProbe se sondea la URL del hit")

    def test_una_spa_de_verdad_sigue_siendo_indiscriminado(self):
        """El caso legítimo: sin `urlProbe` y sin mensaje, `indiscriminado` es
        la respuesta CORRECTA. No se fuerza un `discrimina`."""
        self._fila("status.test", "https://status.test/jeremy")
        html = "<html><body><div id=app></div></body></html>"
        with _CatalogoFalso(self.cat):
            res, _ = self._run(lambda u: (200, u, html))
        self.assertEqual(res["veredictos"]["status.test"], "indiscriminado")

    def test_probe_sin_identificador_queda_indeterminado_sin_peticiones(self):
        """Discord/Holopin: el probe es un endpoint fijo, no hay plantilla."""
        self._fila("fijo.test", "https://fijo.test/jeremy")
        with _CatalogoFalso(self.cat):
            res, urls = self._run(lambda u: (200, u, "lo que sea"))
        self.assertEqual(res["veredictos"]["fijo.test"], "indeterminado")
        self.assertEqual(urls, [], "sin plantilla no se pide nada")

    def test_maigret_sin_catalogo_sigue_funcionando(self):
        """Maigret trae su propio catálogo y no lo leemos: esas filas sondean
        la URL visible y se apoyan en la lista genérica. Degradación conocida,
        no un fallo."""
        self._fila("otro.test", "https://otro.test/u/jeremy", source="maigret")
        with _CatalogoFalso(self.cat):
            res, urls = self._run(lambda u: (200, u, "User not found"))
        self.assertEqual(res["veredictos"]["otro.test"], "discrimina")
        self.assertTrue(all("/u/" in u for u in urls))

    def test_sigue_siendo_dos_peticiones_por_sitio(self):
        self._fila("spa.test", "https://spa.test/profile/jeremy")
        with _CatalogoFalso(self.cat):
            _, urls = self._run(lambda u: (200, u, '{"users":[]}'))
        self.assertEqual(len(urls), 2)

    def test_el_veredicto_sigue_sin_tocar_la_confianza(self):
        """El invariante de la Entrega 1 no se afloja por calibrar."""
        aid = self._fila("status.test", "https://status.test/jeremy")
        with _CatalogoFalso(self.cat):
            self._run(lambda u: (200, u, "<div id=app></div>"))
        row = self.db.get_account(aid)
        self.assertEqual(row["confidence"], "high")
        self.assertEqual(row["verifiability"], "indiscriminado")


class TestContratoConSherlockReal(IsolatedTestCase):
    """Contra el `data.json` del paquete instalado. Sin red.

    Si `sherlock-project` cambia de layout o retira estas entradas, estos tests
    avisan en vez de que el canario degrade en silencio.
    """

    def setUp(self):
        super().setUp()
        from rastrillo import catalogo
        self.cat = catalogo

    def test_el_catalogo_se_lee_y_tiene_sitios(self):
        data = self.cat.cargar()
        if not data:
            self.skipTest("sherlock-project no instalado")
        self.assertGreater(len(data), 100)
        self.assertTrue(all(isinstance(v, dict) for v in data.values()))

    def test_duolingo_tiene_urlprobe_y_lo_usamos(self):
        if not self.cat.cargar():
            self.skipTest("sherlock-project no instalado")
        s = self.cat.plantilla_sonda("https://www.duolingo.com/profile/je7remy",
                                     "je7remy")
        self.assertEqual(s["sitio"], "Duolingo")
        self.assertEqual(s["origen"], "urlProbe")
        self.assertIn("username=je7remy", s["url"])
        self.assertNotIn("/profile/", s["url"])
        self.assertTrue(s["marcadores"])

    def test_steam_no_tiene_urlprobe_pero_si_marcador(self):
        if not self.cat.cargar():
            self.skipTest("sherlock-project no instalado")
        s = self.cat.plantilla_sonda("https://steamcommunity.com/id/je7remy/",
                                     "je7remy")
        self.assertEqual(s["sitio"], "Steam Community (User)")
        self.assertEqual(s["origen"], "url")
        self.assertTrue(s["marcadores"])
        # Y ese marcador es justo el que faltaba en la lista genérica.
        self.assertTrue(any("could not be found" in m.lower()
                            for m in s["marcadores"]))

    def test_hudsonrock_el_caso_que_motivo_el_modulo(self):
        """Sondeaba bien (su `url` YA es la API) pero su forma de decir "no
        existe" no estaba en la lista: era un `indiscriminado` falso."""
        if not self.cat.cargar():
            self.skipTest("sherlock-project no instalado")
        s = self.cat.plantilla_sonda(
            "https://cavalier.hudsonrock.com/api/json/v2/osint-tools/"
            "search-by-username?username=je7remy", "je7remy")
        self.assertEqual(s["sitio"], "HudsonRock")
        self.assertTrue(s["marcadores"])


if __name__ == "__main__":
    import unittest
    unittest.main()
