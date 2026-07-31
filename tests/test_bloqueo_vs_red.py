"""Paso 2C, Entrega 3: distinguir "el sitio nos bloquea" de "no hubo respuesta".

`resolver._http_get` colapsaba 403/429, timeout, DNS y fallo de conexión en un
único `None`. Un sitio que nos RECHAZA y un sitio CAÍDO se veían idénticos, así
que el canario no podía reportar honestamente qué había medido: baby.ru
devuelve 403 con una página "Security Check" y el informe decía "sin respuesta
(timeout, 403/429 o red caída)", que vale para todo y no informa de nada.

Los dos siguen dando `indeterminado` y ninguno se cachea. Lo que cambia es que
ahora se sabe cuál fue.

Criterio que NO se toca: nos identificamos con nuestro User-Agent y aceptamos
el rechazo. Ser bloqueado es información válida sobre el sitio; disfrazarse
para esquivarlo cruzaría la línea del proyecto.
"""
import urllib.error
from unittest.mock import patch

from .helpers import IsolatedTestCase


class _RespuestaFalsa:
    """Sustituto de lo que devuelve `urlopen` como context manager."""

    def __init__(self, status, url, body):
        self.status = status
        self._url = url
        self._body = body.encode()

    def read(self, n=None):
        return self._body

    def geturl(self):
        return self._url

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class TestHttpGetDetallado(IsolatedTestCase):
    """La primitiva. El contrato de `_http_get` NO cambia."""

    def setUp(self):
        super().setUp()
        from rastrillo import resolver
        self.r = resolver

    def _con_urlopen(self, efecto):
        # `_is_safe_url` haría DNS real; lo cortamos, no es lo que se prueba.
        return (patch.object(self.r, "_is_safe_url", return_value=True),
                patch.object(self.r.urllib.request, "urlopen", side_effect=efecto))

    def _llamar(self, efecto, detallado=True):
        p1, p2 = self._con_urlopen(efecto)
        with p1, p2:
            if detallado:
                return self.r._http_get_detallado("https://sitio.test/u/x")
            return self.r._http_get("https://sitio.test/u/x")

    def _http_error(self, code):
        def _efecto(*a, **kw):
            raise urllib.error.HTTPError("https://sitio.test/u/x", code,
                                         "nope", {}, None)
        return _efecto

    # --- Bloqueo -----------------------------------------------------------
    def test_403_es_bloqueo(self):
        resultado, motivo = self._llamar(self._http_error(403))
        self.assertIsNone(resultado)
        self.assertEqual(motivo, self.r.MOTIVO_BLOQUEADO)

    def test_429_es_bloqueo(self):
        resultado, motivo = self._llamar(self._http_error(429))
        self.assertIsNone(resultado)
        self.assertEqual(motivo, self.r.MOTIVO_BLOQUEADO)

    # --- Fallo de transporte -----------------------------------------------
    def test_timeout_es_red(self):
        def _efecto(*a, **kw):
            raise TimeoutError("timed out")
        resultado, motivo = self._llamar(_efecto)
        self.assertIsNone(resultado)
        self.assertEqual(motivo, self.r.MOTIVO_RED)

    def test_dns_es_red(self):
        def _efecto(*a, **kw):
            raise urllib.error.URLError("Name or service not known")
        resultado, motivo = self._llamar(_efecto)
        self.assertIsNone(resultado)
        self.assertEqual(motivo, self.r.MOTIVO_RED)

    def test_conexion_rechazada_es_red(self):
        def _efecto(*a, **kw):
            raise ConnectionRefusedError("refused")
        resultado, motivo = self._llamar(_efecto)
        self.assertIsNone(resultado)
        self.assertEqual(motivo, self.r.MOTIVO_RED)

    # --- Lo que NO cambia --------------------------------------------------
    def test_500_sigue_siendo_respuesta_con_cuerpo(self):
        """Semántica de siempre: cualquier código que no sea 403/429 vuelve
        como respuesta, porque hay páginas que responden 4xx/5xx con
        información útil. La Entrega 3 no inventa semántica nueva aquí."""
        def _efecto(*a, **kw):
            e = urllib.error.HTTPError("https://sitio.test/u/x", 500,
                                       "boom", {}, None)
            e.read = lambda n=None: b"<html>error interno</html>"
            raise e
        resultado, motivo = self._llamar(_efecto)
        self.assertIsNone(motivo)
        self.assertEqual(resultado[0], 500)
        self.assertIn("error interno", resultado[2])

    def test_404_sigue_siendo_respuesta_con_cuerpo(self):
        def _efecto(*a, **kw):
            e = urllib.error.HTTPError("https://sitio.test/u/x", 404,
                                       "nope", {}, None)
            e.read = lambda n=None: b"user not found"
            raise e
        resultado, motivo = self._llamar(_efecto)
        self.assertIsNone(motivo)
        self.assertEqual(resultado[0], 404)

    def test_exito(self):
        def _efecto(*a, **kw):
            return _RespuestaFalsa(200, "https://sitio.test/u/x", "hola")
        resultado, motivo = self._llamar(_efecto)
        self.assertIsNone(motivo)
        self.assertEqual(resultado[0], 200)

    def test_ssrf_tiene_su_propio_motivo(self):
        with patch.object(self.r, "_is_safe_url", return_value=False):
            resultado, motivo = self.r._http_get_detallado("http://127.0.0.1/")
        self.assertIsNone(resultado)
        self.assertEqual(motivo, self.r.MOTIVO_SSRF)

    # --- El envoltorio conserva el contrato viejo --------------------------
    def test_http_get_sigue_devolviendo_none_en_todos_los_fallos(self):
        """Los tres callers de `resolver` solo distinguen "hubo respuesta" de
        "no la hubo". Si esto cambiara, habría que revisarlos uno a uno."""
        for efecto in (self._http_error(403), self._http_error(429)):
            with self.subTest(efecto=efecto):
                self.assertIsNone(self._llamar(efecto, detallado=False))

        def _timeout(*a, **kw):
            raise TimeoutError("t")
        self.assertIsNone(self._llamar(_timeout, detallado=False))

    def test_http_get_sigue_devolviendo_la_tupla_en_exito(self):
        def _efecto(*a, **kw):
            return _RespuestaFalsa(200, "https://sitio.test/u/x", "hola")
        r = self._llamar(_efecto, detallado=False)
        self.assertEqual(r, (200, "https://sitio.test/u/x", "hola"))


class TestCanarioDistingueLaCausa(IsolatedTestCase):

    def setUp(self):
        super().setUp()
        from rastrillo import canario, db
        self.c = canario
        self.db = db
        db.init()

    def _analizar(self, get):
        return self.c.analizar_sitio(
            "https://sitio.test/u/jeremy", "jeremy",
            tokens=["abcdefghij", "klmnopqrst"],
            http_get=get, url_segura=lambda u: True)

    def test_bloqueo_lo_dice(self):
        res = self._analizar(lambda u: (None, "bloqueado"))
        self.assertEqual(res["veredicto"], "indeterminado")
        ev = res["evidencia"]
        self.assertEqual(ev["causa"], "bloqueado")
        self.assertIn("bloquea", ev["motivo"])
        self.assertEqual(ev["sondas"][0]["causa"], "bloqueado")

    def test_red_lo_dice(self):
        res = self._analizar(lambda u: (None, "red"))
        ev = res["evidencia"]
        self.assertEqual(res["veredicto"], "indeterminado")
        self.assertEqual(ev["causa"], "red")
        self.assertIn("sin respuesta", ev["motivo"])

    def test_los_dos_motivos_son_distinguibles(self):
        bloqueo = self._analizar(lambda u: (None, "bloqueado"))["evidencia"]
        red = self._analizar(lambda u: (None, "red"))["evidencia"]
        self.assertNotEqual(bloqueo["motivo"], red["motivo"])

    # --- Compatibilidad de formas ------------------------------------------
    def test_forma_simple_sigue_valiendo(self):
        """Un `http_get` inyectado que no sabe de motivos (la mayoría de los
        tests) sigue funcionando: el fallo se lee como fallo de red."""
        res = self._analizar(lambda u: None)
        self.assertEqual(res["veredicto"], "indeterminado")
        self.assertEqual(res["evidencia"]["causa"], "red")

    def test_forma_simple_de_exito_sigue_valiendo(self):
        res = self._analizar(lambda u: (200, u, "user not found"))
        self.assertEqual(res["veredicto"], "discrimina")

    def test_forma_detallada_de_exito(self):
        res = self._analizar(lambda u: ((200, u, "user not found"), None))
        self.assertEqual(res["veredicto"], "discrimina")

    def test_forma_desconocida_no_revienta(self):
        for basura in ("texto", 42, (1, 2, 3, 4), []):
            with self.subTest(basura=basura):
                res = self._analizar(lambda u: basura)
                self.assertEqual(res["veredicto"], "indeterminado")

    def test_500_sigue_dando_discrimina(self):
        """No inventamos semántica nueva: >=400 ya era `discrimina`."""
        res = self._analizar(lambda u: (500, u, "<html>boom</html>"))
        self.assertEqual(res["veredicto"], "discrimina")


class TestCausaEnElPaseCompleto(IsolatedTestCase):

    def setUp(self):
        super().setUp()
        from rastrillo import canario, db
        self.c = canario
        self.db = db
        db.init()

    def _fila(self, site):
        return self.db.upsert_account(
            site, "jeremy", source="sherlock", source_site=site,
            status="found", confidence="high",
            profile_url=f"https://{site}/u/jeremy", display_name=site)

    def _run(self, responder):
        return self.c.run_canario(http_get=responder,
                                  url_segura=lambda u: True,
                                  tokens=["abcdefghij", "klmnopqrst"])

    def _codes(self, aid):
        row = self.db.get_account(aid)
        return {r["code"] for r in self.db.parse_reasons(row["confidence_reasons"])}

    def test_el_bloqueo_queda_anotado_en_la_fila(self):
        aid = self._fila("baby.test")
        res = self._run(lambda u: (None, "bloqueado"))
        self.assertEqual(res["veredictos"]["baby.test"], "indeterminado")
        self.assertEqual(res["causas"]["baby.test"], "bloqueado")
        self.assertEqual(res["bloqueados"], 1)
        self.assertIn("canario_bloqueado", self._codes(aid))
        self.assertNotIn("canario_sin_respuesta", self._codes(aid))

    def test_la_caida_queda_anotada_en_la_fila(self):
        aid = self._fila("caido.test")
        res = self._run(lambda u: (None, "red"))
        self.assertEqual(res["bloqueados"], 0)
        self.assertIn("canario_sin_respuesta", self._codes(aid))

    def test_ninguno_de_los_dos_se_cachea(self):
        self._fila("baby.test")
        self._run(lambda u: (None, "bloqueado"))
        self.assertEqual(self.c._leer_cache(), {})
        self._fila("caido.test")
        self._run(lambda u: (None, "red"))
        self.assertEqual(self.c._leer_cache(), {})

    def test_el_resumen_de_errores_lleva_la_causa(self):
        self._fila("baby.test")
        res = self._run(lambda u: (None, "bloqueado"))
        self.assertEqual(len(res["errores"]), 1)
        self.assertEqual(res["errores"][0]["causa"], "bloqueado")
        self.assertIn("bloquea", res["errores"][0]["error"])

    def test_la_causa_no_toca_la_confianza(self):
        """El invariante de la Entrega 1 tampoco cede aquí."""
        for causa in ("bloqueado", "red"):
            with self.subTest(causa=causa):
                aid = self._fila(f"{causa}.test")
                self._run(lambda u: (None, causa))
                row = self.db.get_account(aid)
                self.assertEqual(row["confidence"], "high")
                self.assertEqual(row["verifiability"], "indeterminado")


if __name__ == "__main__":
    import unittest
    unittest.main()
