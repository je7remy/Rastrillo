"""Canario a nivel de sitio (Paso 2B, Entrega 2), 100% offline.

Todo con primitivas de red inyectadas por parámetro (`http_get`, `url_segura`),
igual que `_whois_query`/`_dns_query` en domain_intel. Ni un socket.

Cubre:
  - veredicto `indiscriminado` (dos 200 idénticos sin marcadores) y su efecto:
    las filas del sitio bajan a `low` con su motivo.
  - `discrimina` por status 404 y por marcador de "no existe" en el cuerpo
    (español y otro idioma) — este último es el que evita el falso veredicto.
  - `indeterminado` por timeout/excepción: confianza intacta y SIN cachear.
  - caché: fresca → cero peticiones; vencida por TTL → se repite.
  - 3 hits del mismo host → 2 peticiones, no 6.
  - hits sin `url` (holehe, hibp) → cero peticiones.
  - guarda de red: http:// y host privado no se piden, sin excepción.
  - el canario NUNCA sube la confianza.
  - los tokens falsos generados son plausibles.
"""
import json
import time
from .helpers import IsolatedTestCase


# --- Cuerpos de ejemplo -----------------------------------------------------
_PLANTILLA_GENERICA = (
    "<html><head><title>Perfil</title></head><body>"
    "<nav>Inicio Buscar Ayuda</nav>"
    "<div class='perfil'><h1>{u}</h1><p>Sin publicaciones todavia.</p></div>"
    "<footer>(c) 2026 Sitio Generico. Terminos y condiciones.</footer>"
    "</body></html>"
)
_CUERPO_404_ES = (
    "<html><body><h1>Vaya</h1>"
    "<p>El usuario no encontrado en este sitio.</p></body></html>"
)
_CUERPO_404_DE = (
    "<html><body><h1>Hoppla</h1>"
    "<p>Benutzer nicht gefunden.</p></body></html>"
)


class _Espia:
    """http_get inyectable que cuenta llamadas y responde por guion."""

    def __init__(self, responder):
        self.responder = responder
        self.urls = []

    def __call__(self, url):
        self.urls.append(url)
        return self.responder(url)

    @property
    def n(self):
        return len(self.urls)


def _siempre(status, cuerpo):
    return lambda url: (status, url, cuerpo)


def _generico(url):
    """Sitio que responde 200 con la misma plantilla a cualquier usuario:
    el caso de HudsonRock / Periscope."""
    usuario = url.rstrip("/").rsplit("/", 1)[-1]
    return 200, url, _PLANTILLA_GENERICA.format(u=usuario)


class TestCanarioVeredictos(IsolatedTestCase):

    def setUp(self):
        super().setUp()
        from rastrillo import canario, db
        self.c = canario
        self.db = db
        db.init()

    def _analizar(self, get, url="https://sitio.com/u/jeremy", ident="jeremy"):
        return self.c.analizar_sitio(
            url, ident, tokens=["abcdefghij", "klmnopqrst"],
            http_get=get, url_segura=lambda u: True)

    # --- 5. Indiscriminado --------------------------------------------------
    def test_dos_doscientos_identicos_sin_marcadores_es_indiscriminado(self):
        espia = _Espia(_generico)
        res = self._analizar(espia)
        self.assertEqual(res["veredicto"], "indiscriminado")
        self.assertEqual(espia.n, 2, "exactamente 2 peticiones por sitio")
        # Ninguna de las dos toca el perfil real.
        for u in espia.urls:
            self.assertNotIn("jeremy", u)
        self.assertGreaterEqual(res["evidencia"]["similitud"],
                                self.c.UMBRAL_SIMILITUD)

    # --- 6. Discrimina por status ------------------------------------------
    def test_dos_cuatrocientoscuatro_es_discrimina(self):
        espia = _Espia(_siempre(404, "<html><body>nope</body></html>"))
        res = self._analizar(espia)
        self.assertEqual(res["veredicto"], "discrimina")
        self.assertEqual(espia.n, 2)

    # --- 7. Discrimina por MARCADOR en un 200 (el falso veredicto) ---------
    def test_doscientos_con_marcador_es_discrimina_es(self):
        """200 pero el cuerpo dice "usuario no encontrado": el sitio SÍ sabe
        distinguir. Sin este chequeo lo daríamos por indiscriminado, porque
        los dos cuerpos son idénticos entre sí."""
        espia = _Espia(_siempre(200, _CUERPO_404_ES))
        res = self._analizar(espia)
        self.assertEqual(res["veredicto"], "discrimina")
        self.assertIn("no encontrado", res["evidencia"]["motivo"])

    def test_doscientos_con_marcador_es_discrimina_otro_idioma(self):
        espia = _Espia(_siempre(200, _CUERPO_404_DE))
        res = self._analizar(espia)
        self.assertEqual(res["veredicto"], "discrimina")

    def test_marcadores_cubren_los_seis_idiomas(self):
        muestras = {
            "en": "Sorry, user not found here.",
            "es": "El usuario no encontrado.",
            "ru": "Пользователь не найден.",
            "pt-BR": "Usuário não encontrado.",
            "fr": "Utilisateur introuvable.",
            "de": "Benutzer nicht gefunden.",
        }
        for lang, texto in muestras.items():
            with self.subTest(lang=lang):
                self.assertTrue(self.c._marcadores_en(texto),
                                f"sin marcador para {lang}")

    # --- 8. Indeterminado: timeout / excepción -----------------------------
    def test_sin_respuesta_es_indeterminado(self):
        espia = _Espia(lambda url: None)          # timeout / 403 / red caída
        res = self._analizar(espia)
        self.assertEqual(res["veredicto"], "indeterminado")

    def test_excepcion_es_indeterminado_y_no_propaga(self):
        def _boom(url):
            raise RuntimeError("BOOM")
        res = self._analizar(_Espia(_boom))
        self.assertEqual(res["veredicto"], "indeterminado")
        self.assertIn("RuntimeError", str(res["evidencia"]))

    def test_indeterminado_no_se_cachea(self):
        self.db.upsert_account("sitio", "jeremy", source="sherlock",
                               source_site="sitio.com", status="found",
                               confidence="high",
                               profile_url="https://sitio.com/u/jeremy")
        espia = _Espia(lambda url: None)
        res = self.c.run_canario(http_get=espia, url_segura=lambda u: True,
                                 tokens=["abcdefghij", "klmnopqrst"])
        self.assertEqual(res["veredictos"]["sitio.com"], "indeterminado")
        self.assertEqual(self.c._leer_cache(), {},
                         "un indeterminado NO debe cachearse")
        # Confianza intacta. El motivo de la CAUSA sí se anota desde la
        # Entrega 3 (aquí, fallo de red), pero nunca uno de veredicto.
        row = self.db.get_account(1)
        self.assertEqual(row["confidence"], "high")
        codes = {r["code"] for r in self.db.parse_reasons(row["confidence_reasons"])}
        self.assertIn("canario_sin_respuesta", codes)
        self.assertNotIn("canario_indiscriminado", codes)
        self.assertNotIn("canario_discrimina", codes)

    # --- 12. Guarda de red --------------------------------------------------
    def test_guarda_de_red_no_pide_http_ni_ips_privadas(self):
        from rastrillo import resolver
        for url in ("http://sitio.com/u/jeremy",       # esquema no https
                    "https://localhost/u/jeremy",      # resuelve a 127.0.0.1
                    "https://127.0.0.1/u/jeremy"):
            with self.subTest(url=url):
                espia = _Espia(_generico)
                # Guarda REAL: es lo que estamos probando.
                res = self.c.analizar_sitio(
                    url, "jeremy", tokens=["abcdefghij", "klmnopqrst"],
                    http_get=espia, url_segura=resolver._is_safe_url)
                self.assertEqual(res["veredicto"], "indeterminado")
                self.assertEqual(espia.n, 0, "no debe pedirse nada")

    # --- 14. Tokens plausibles ---------------------------------------------
    def test_tokens_generados_son_plausibles(self):
        import random
        for semilla in range(40):
            toks = self.c.generar_tokens(2, rng=random.Random(semilla))
            self.assertEqual(len(toks), 2)
            self.assertNotEqual(toks[0], toks[1], "deben ser distintos")
            for t in toks:
                with self.subTest(t=t):
                    self.assertTrue(t.islower() or t[-1].isdigit())
                    self.assertRegex(t, r"^[a-z][a-z0-9]+$",
                                     "solo minúsculas y dígitos, empieza por letra")
                    self.assertGreaterEqual(len(t), 9)
                    self.assertLessEqual(len(t), 13)
                    self.assertTrue(self.c.token_plausible(t))

    def test_token_plausible_rechaza_lo_raro(self):
        for malo in ("", "abc", "MiUsuario", "con-guion", "con espacio",
                     "a" * 40, None):
            with self.subTest(malo=malo):
                self.assertFalse(self.c.token_plausible(malo))

    # --- Construcción de la URL del canario --------------------------------
    def test_url_canario_sustituye_en_path_query_y_subdominio(self):
        f = self.c.url_canario
        self.assertEqual(f("https://s.com/u/jeremy", "jeremy", "tok"),
                         "https://s.com/u/tok")
        self.assertEqual(f("https://s.com/@jeremy", "jeremy", "tok"),
                         "https://s.com/@tok")
        self.assertEqual(f("https://s.com/x?username=jeremy", "jeremy", "tok"),
                         "https://s.com/x?username=tok")
        self.assertEqual(f("https://jeremy.tumblr.com/", "jeremy", "tok"),
                         "https://tok.tumblr.com/")
        # Caja distinta: sherlock normaliza a veces.
        self.assertEqual(f("https://s.com/u/JEREMY", "jeremy", "tok"),
                         "https://s.com/u/tok")

    def test_url_canario_none_si_el_identificador_no_esta(self):
        f = self.c.url_canario
        self.assertIsNone(f("https://s.com/inicio", "jeremy", "tok"))
        self.assertIsNone(f("", "jeremy", "tok"))
        self.assertIsNone(f("https://s.com/u/jeremy", "", "tok"))
        # En el dominio (no en la etiqueta izquierda) NO se sustituye: sería
        # el mismo falso positivo de `banana.com` que cerró el paso 2A.
        self.assertIsNone(f("https://banana.com/u/x", "ana", "tok"))

    def test_sin_plantilla_es_indeterminado_sin_peticiones(self):
        espia = _Espia(_generico)
        res = self._analizar(espia, url="https://s.com/inicio")
        self.assertEqual(res["veredicto"], "indeterminado")
        self.assertEqual(espia.n, 0)


class TestCanarioPaseSobreLaDB(IsolatedTestCase):

    def setUp(self):
        super().setUp()
        from rastrillo import canario, db
        self.c = canario
        self.db = db
        db.init()

    def _fila(self, platform, ident, site, url, conf="high", source="sherlock"):
        return self.db.upsert_account(
            platform, ident, source=source, source_site=site, status="found",
            confidence=conf, profile_url=url,
            display_name=platform)

    def _run(self, get, **kw):
        return self.c.run_canario(http_get=get, url_segura=lambda u: True,
                                  tokens=["abcdefghij", "klmnopqrst"], **kw)

    def _codes(self, aid):
        row = self.db.get_account(aid)
        return {r["code"] for r in self.db.parse_reasons(row["confidence_reasons"])}

    # --- E1. indiscriminado marca verificabilidad, NO confianza ------------
    def test_indiscriminado_marca_verificabilidad_y_deja_la_confianza(self):
        a1 = self._fila("hudsonrock", "jeremy", "cavalier.hudsonrock.com",
                        "https://cavalier.hudsonrock.com/api?username=jeremy")
        a2 = self._fila("bueno", "jeremy", "bueno.com",
                        "https://bueno.com/u/jeremy")

        def _mixto(url):
            if "hudsonrock" in url:
                return _generico(url)
            return 404, url, "<html>no such user</html>"

        res = self._run(_Espia(_mixto))
        self.assertEqual(res["veredictos"]["cavalier.hudsonrock.com"],
                         "indiscriminado")
        self.assertEqual(res["veredictos"]["bueno.com"], "discrimina")
        self.assertEqual(res["marcadas"], 2)

        # El veredicto va a su propia columna...
        self.assertEqual(self.db.get_account(a1)["verifiability"],
                         "indiscriminado")
        self.assertEqual(self.db.get_account(a2)["verifiability"], "discrimina")
        # ...y la confianza NO se mueve en ninguno de los dos casos.
        self.assertEqual(self.db.get_account(a1)["confidence"], "high")
        self.assertEqual(self.db.get_account(a2)["confidence"], "high")
        # El motivo se sigue anotando: es evidencia, pero no mueve el tramo.
        self.assertIn("canario_indiscriminado", self._codes(a1))
        self.assertIn("canario_discrimina", self._codes(a2))

    def test_discard_low_no_barre_inverificables(self):
        """La cadena "inverificable -> low -> descartar -> no es mía" es lo que
        cierra la Entrega 1: `discard-low` sigue mirando SOLO `confidence`."""
        from .helpers import auth_client
        aid = self._fila("malo", "jeremy", "malo.com",
                         "https://malo.com/u/jeremy", conf="high")
        self._run(_Espia(_generico))
        row = self.db.get_account(aid)
        self.assertEqual(row["verifiability"], "indiscriminado")
        self.assertEqual(row["confidence"], "high", "la confianza no se toca")

        cli = auth_client()
        r = cli.post("/api/accounts/discard-low", json={}, headers=self.hdr())
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["discarded"], 0,
                         "una fila high+indiscriminado NO se descarta")
        self.assertEqual(self.db.get_account(aid)["status"], "found")

    def test_discard_low_sigue_barriendo_una_low_normal(self):
        """El comportamiento de siempre para el ruido real de Sherlock."""
        from .helpers import auth_client
        aid = self._fila("corto", "ana", "corto.com",
                         "https://corto.com/u/ana", conf="low")
        cli = auth_client()
        r = cli.post("/api/accounts/discard-low", json={}, headers=self.hdr())
        self.assertEqual(r.json()["discarded"], 1)
        self.assertEqual(self.db.get_account(aid)["status"], "not_mine")

    # --- El veredicto describe al SITIO: se marca en todas sus filas -------
    def test_el_veredicto_se_marca_en_todas_las_filas_del_sitio(self):
        """Incluidas las de holehe/hibp, que no aportan plantilla. Describe al
        SITIO, así que es cierto para cualquier fila suya — mismo criterio que
        `corrob_cruzada` en 2A. Y ninguna cambia de tramo."""
        sher = self._fila("foro", "jeremy", "foro.com",
                          "https://foro.com/u/jeremy")
        hole = self.db.upsert_account(
            "foro", "yo@example.com", source="holehe", source_site="foro.com",
            status="found", confidence="high", display_name="Foro")

        espia = _Espia(_generico)
        res = self._run(espia)
        self.assertEqual(res["veredictos"]["foro.com"], "indiscriminado")
        # Una sola petición doble: la fila de holehe no aporta plantilla.
        self.assertEqual(espia.n, 2)

        for aid in (sher, hole):
            with self.subTest(fila=aid):
                row = self.db.get_account(aid)
                self.assertEqual(row["verifiability"], "indiscriminado")
                self.assertEqual(row["confidence"], "high")
                self.assertIn("canario_indiscriminado", self._codes(aid))

    def test_indiscriminado_no_toca_hibp_ni_hibp_confirmed(self):
        ids = {}
        for fuente, conf in (("hibp", "medium"), ("hibp_confirmed", "medium")):
            ids[fuente] = self.db.upsert_account(
                "brecha", f"{fuente}@example.com", source=fuente,
                source_site="brecha.com", status="found", confidence=conf,
                display_name="Brecha")
        self._fila("brecha", "jeremy", "brecha.com",
                   "https://brecha.com/u/jeremy")

        res = self._run(_Espia(_generico))
        self.assertEqual(res["veredictos"]["brecha.com"], "indiscriminado")
        for fuente, aid in ids.items():
            with self.subTest(fuente=fuente):
                self.assertEqual(self.db.get_account(aid)["confidence"], "medium")

    def test_maigret_tampoco_se_degrada(self):
        aid = self._fila("sitio", "jeremy", "sitio.com",
                         "https://sitio.com/u/jeremy", source="maigret")
        self._run(_Espia(_generico))
        row = self.db.get_account(aid)
        self.assertEqual(row["confidence"], "high",
                         "maigret era heurística y en 2B bajaba; ya no")
        self.assertEqual(row["verifiability"], "indiscriminado")

    # --- 13. Nunca sube la confianza ---------------------------------------
    def test_discrimina_no_sube_una_fila_low(self):
        aid = self._fila("corto", "ana", "corto.com",
                         "https://corto.com/u/ana", conf="low")
        res = self._run(_Espia(_siempre(404, "not found")))
        self.assertEqual(res["veredictos"]["corto.com"], "discrimina")
        self.assertEqual(self.db.get_account(aid)["confidence"], "low",
                         "el canario nunca sube el tramo")
        self.assertIn("canario_discrimina", self._codes(aid))

    # --- indeterminado: se registra, pero no se cachea ---------------------
    def test_indeterminado_se_registra_en_verificabilidad(self):
        """"Lo intenté y no pude concluir" es distinto de "no lo he mirado"
        (NULL). El veredicto se registra aunque no se cachee, y con la Entrega
        3 se anota además la causa (aquí, fallo de red)."""
        aid = self._fila("mudo", "jeremy", "mudo.com",
                         "https://mudo.com/u/jeremy")
        res = self._run(_Espia(lambda url: None))
        self.assertEqual(res["veredictos"]["mudo.com"], "indeterminado")
        row = self.db.get_account(aid)
        self.assertEqual(row["verifiability"], "indeterminado")
        self.assertEqual(row["confidence"], "high")
        self.assertIn("canario_sin_respuesta", self._codes(aid))
        self.assertEqual(self.c._leer_cache(), {}, "no se cachea")

    # --- 10. Tres hits del mismo host → 2 peticiones -----------------------
    def test_tres_hits_del_mismo_host_dos_peticiones(self):
        self._fila("sitio", "jeremy", "sitio.com", "https://sitio.com/u/jeremy")
        self._fila("sitio", "jeremy2", "sitio.com", "https://sitio.com/u/jeremy2")
        self._fila("sitio", "jeremy3", "sitio.com", "https://sitio.com/u/jeremy3")
        espia = _Espia(_generico)
        res = self._run(espia)
        self.assertEqual(res["sitios"], 1)
        self.assertEqual(espia.n, 2, "2 peticiones por SITIO, no por fila")
        # Pero las 3 filas quedan marcadas con la verificabilidad del sitio.
        self.assertEqual(res["marcadas"], 3)

    # --- 11. Hits sin url (holehe, hibp) → cero peticiones -----------------
    def test_hits_sin_url_no_generan_peticiones(self):
        self.db.upsert_account("foro", "yo@example.com", source="holehe",
                               source_site="foro.com", status="found",
                               confidence="high")
        self.db.upsert_account("brecha", "yo@example.com", source="hibp",
                               source_site="brecha.com", status="found",
                               confidence="medium")
        espia = _Espia(_generico)
        res = self._run(espia)
        self.assertEqual(res["sitios"], 0)
        self.assertEqual(espia.n, 0, "holehe/hibp no traen url: ni una petición")
        # Y su confianza no se toca.
        self.assertEqual(self.db.get_account(1)["confidence"], "high")
        self.assertEqual(self.db.get_account(2)["confidence"], "medium")

    # --- 9. Caché ----------------------------------------------------------
    def test_cache_fresca_cero_peticiones(self):
        self._fila("sitio", "jeremy", "sitio.com", "https://sitio.com/u/jeremy")
        primera = _Espia(_generico)
        self._run(primera)
        self.assertEqual(primera.n, 2)
        self.assertTrue(self.c.CACHE_PATH.exists())

        segunda = _Espia(_generico)
        res = self._run(segunda)
        self.assertEqual(segunda.n, 0, "con caché fresca no se toca la red")
        self.assertEqual(res["cacheados"], 1)
        self.assertEqual(res["probados"], 0)
        self.assertEqual(res["veredictos"]["sitio.com"], "indiscriminado")

    def test_cache_vencida_por_ttl_se_repite(self):
        self._fila("sitio", "jeremy", "sitio.com", "https://sitio.com/u/jeremy")
        self._run(_Espia(_generico))
        # Envejecemos la entrada más allá del TTL por defecto (30 días).
        cache = json.loads(self.c.CACHE_PATH.read_text(encoding="utf-8"))
        cache["sitio.com"]["fecha"] = time.time() - 31 * 86400
        self.c.CACHE_PATH.write_text(json.dumps(cache), encoding="utf-8")

        espia = _Espia(_generico)
        res = self._run(espia)
        self.assertEqual(espia.n, 2, "caché vencida: hay que volver a probar")
        self.assertEqual(res["cacheados"], 0)

    def test_ttl_configurable_por_env(self):
        import os
        self._fila("sitio", "jeremy", "sitio.com", "https://sitio.com/u/jeremy")
        self._run(_Espia(_generico))
        cache = json.loads(self.c.CACHE_PATH.read_text(encoding="utf-8"))
        cache["sitio.com"]["fecha"] = time.time() - 2 * 86400   # 2 días
        self.c.CACHE_PATH.write_text(json.dumps(cache), encoding="utf-8")
        os.environ["RASTRILLO_CANARIO_MAX_AGE_DAYS"] = "1"
        try:
            espia = _Espia(_generico)
            self._run(espia)
            self.assertEqual(espia.n, 2, "TTL de 1 día: 2 días es viejo")
        finally:
            os.environ.pop("RASTRILLO_CANARIO_MAX_AGE_DAYS", None)

    def test_cache_corrupta_no_revienta(self):
        self.c.CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        self.c.CACHE_PATH.write_text("{no es json", encoding="utf-8")
        self._fila("sitio", "jeremy", "sitio.com", "https://sitio.com/u/jeremy")
        espia = _Espia(_generico)
        res = self._run(espia)
        self.assertEqual(res["veredictos"]["sitio.com"], "indiscriminado")

    # --- Robustez ----------------------------------------------------------
    def test_un_sitio_que_explota_no_aborta_el_lote(self):
        self._fila("malo", "jeremy", "malo.com", "https://malo.com/u/jeremy")
        self._fila("bueno", "jeremy", "bueno.com", "https://bueno.com/u/jeremy")

        def _flaky(url):
            if "malo.com" in url:
                raise RuntimeError("BOOM")
            return _generico(url)

        res = self._run(_Espia(_flaky))
        self.assertEqual(res["veredictos"]["malo.com"], "indeterminado")
        self.assertEqual(res["veredictos"]["bueno.com"], "indiscriminado")
        self.assertEqual(len(res["errores"]), 1)

    def test_db_vacia_no_hace_nada(self):
        espia = _Espia(_generico)
        res = self._run(espia)
        self.assertEqual(res["sitios"], 0)
        self.assertEqual(espia.n, 0)

    # --- El invariante, barrido entero -------------------------------------
    def test_el_canario_nunca_modifica_la_confianza(self):
        """Barrido de (fuente × tramo × veredicto): 54 filas, y en NINGUNA
        cambia `confidence` — ni hacia arriba ni hacia abajo, para ninguna
        fuente y ningún tramo. Lo que sí cambia es `verifiability`, que es el
        eje nuevo de la Entrega 1.

        Este es EL test del invariante: si alguien vuelve a meter una escritura
        de `confidence` en `canario.py`, aquí se cae."""
        fuentes = ("sherlock", "maigret", "holehe", "hibp", "hibp_confirmed",
                   "manual")
        tramos = ("high", "medium", "low")
        # Un host por veredicto, cada uno con una fila por (fuente, tramo).
        guion = {"indis.com": _generico,
                 "discri.com": lambda u: (404, u, "not found"),
                 "indeter.com": lambda u: None}
        veredicto_de = {"indis.com": "indiscriminado",
                        "discri.com": "discrimina",
                        "indeter.com": "indeterminado"}
        esperado, sembradas = {}, []
        for host in guion:
            for fuente in fuentes:
                for tramo in tramos:
                    aid = self.db.upsert_account(
                        f"{host}-{fuente}", f"{fuente}-{tramo}",
                        source=fuente, source_site=host, status="found",
                        confidence=tramo, display_name=host,
                        profile_url=f"https://{host}/u/{fuente}-{tramo}")
                    sembradas.append(aid)
                    # El tramo sembrado es el tramo esperado. Siempre.
                    esperado[aid] = (tramo, veredicto_de[host])

        def _mixto(url):
            for host, fn in guion.items():
                if host in url:
                    return fn(url)
            raise AssertionError(f"host inesperado: {url}")

        self.assertEqual(len(sembradas), 54, "el barrido cubre 54 filas")
        res = self._run(_Espia(_mixto))
        self.assertEqual(res["veredictos"], veredicto_de)
        for aid in sembradas:
            row = self.db.get_account(aid)
            tramo, veredicto = esperado[aid]
            with self.subTest(fila=row["display_name"], src=row["source"],
                              tramo=tramo):
                self.assertEqual(row["confidence"], tramo,
                                 "el canario no modifica confidence, nunca")
                self.assertEqual(row["verifiability"], veredicto)
        # Y ninguna fila cambió de estado: nada se marca `not_mine` solo.
        self.assertEqual({self.db.get_account(a)["status"] for a in sembradas},
                         {"found"})
