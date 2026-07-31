"""Paso 3, Entrega 3: que la UI diga qué va a pasar.

Los chips explicaban de dónde salía la confianza, pero nada explicaba qué hace
cada botón ni qué concluir de cada señal. Aquí se fija:

  - que cada motivo REGISTRADO en el backend tenga etiqueta Y tooltip en el
    front (el test barre el código fuente: añadir un motivo nuevo sin su texto
    rompe la suite),
  - que el tooltip de `low` diga que es evidencia DÉBIL de que sea tuya y no
    "no es tuya",
  - que el descarte masivo pueda decir el número EXACTO de filas antes de
    escribir nada, y que ese número salga del mismo criterio que el endpoint
    que escribe,
  - que ningún tooltip lleve HTML crudo.
"""
import re
from pathlib import Path

from .helpers import IsolatedTestCase, auth_client

_RAIZ = Path(__file__).resolve().parents[1]
_APP_JS = _RAIZ / "rastrillo" / "static" / "app.js"

# Archivos que registran motivos de confianza. `server.py` queda fuera a
# propósito: sus `"code"` son códigos de error de la API (p.ej.
# `needs_ownership_confirmation`), no motivos de confianza.
_FUENTES_DE_MOTIVOS = ("discovery.py", "canario.py", "db.py")

# Dos formas de registrar un motivo en el código: el helper `_motivo("x", ...)`
# de discovery y el literal `{"code": "x", ...}` de canario/db.
_PAT_MOTIVO = re.compile(r'_motivo\(\s*"([a-z0-9_]+)"')
_PAT_CODE = re.compile(r'"code":\s*"([a-z0-9_]+)"')


def _motivos_registrados() -> set:
    codes = set()
    for nombre in _FUENTES_DE_MOTIVOS:
        texto = (_RAIZ / "rastrillo" / nombre).read_text(encoding="utf-8")
        codes |= set(_PAT_MOTIVO.findall(texto))
        codes |= set(_PAT_CODE.findall(texto))
    return codes


def _claves_de_objeto_js(texto: str, nombre: str) -> set:
    """Claves de un objeto literal `const NOMBRE={...}` de app.js.

    Parseo deliberadamente tonto (contamos llaves hasta cerrar) porque no hay
    dependencias nuevas y el objeto es un literal plano escrito a mano.
    """
    inicio = texto.index(f"const {nombre}={{")
    i = texto.index("{", inicio)
    profundidad, fin = 0, None
    for j in range(i, len(texto)):
        if texto[j] == "{":
            profundidad += 1
        elif texto[j] == "}":
            profundidad -= 1
            if profundidad == 0:
                fin = j
                break
    cuerpo = texto[i + 1:fin]
    # Claves a nivel 0 del literal: `clave:` al principio de línea o tras coma.
    return set(re.findall(r'(?:^|[,{])\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*:', cuerpo))


class TextosDeMotivosTest(IsolatedTestCase):
    """Cobertura de textos. No necesita servidor: lee los fuentes."""

    def setUp(self):
        super().setUp()
        self.js = _APP_JS.read_text(encoding="utf-8")

    def test_hay_motivos_que_barrer(self):
        """Guarda del propio barrido: si el regex deja de casar, que se note
        aquí y no como un 'todo verde' silencioso."""
        codes = _motivos_registrados()
        self.assertGreaterEqual(len(codes), 15)
        # Anclas conocidas de cada fuente.
        for esperado in ("tramo_distintivo", "corrob_misma_fila",
                         "canario_indiscriminado", "descartado_antes"):
            self.assertIn(esperado, codes)

    def test_cada_motivo_tiene_etiqueta(self):
        faltan = _motivos_registrados() - _claves_de_objeto_js(self.js, "REASON_LABEL")
        self.assertEqual(faltan, set(),
                         f"motivos sin etiqueta en REASON_LABEL: {sorted(faltan)}")

    def test_cada_motivo_tiene_tooltip(self):
        """EL test del enunciado: añadir un motivo sin su frase rompe aquí."""
        faltan = _motivos_registrados() - _claves_de_objeto_js(self.js, "REASON_TIP")
        self.assertEqual(faltan, set(),
                         f"motivos sin tooltip en REASON_TIP: {sorted(faltan)}")

    def test_no_sobran_textos_de_motivos_inexistentes(self):
        """Al revés: un tooltip para un motivo que ya nadie emite es ruido."""
        sobran = _claves_de_objeto_js(self.js, "REASON_TIP") - _motivos_registrados()
        self.assertEqual(sobran, set(),
                         f"REASON_TIP tiene motivos que ya no se emiten: {sorted(sobran)}")

    def test_los_tres_tramos_tienen_tooltip(self):
        claves = _claves_de_objeto_js(self.js, "CONF_TIP")
        self.assertEqual(claves, {"high", "medium", "low"})

    def test_el_tooltip_de_low_no_dice_que_no_sea_tuya(self):
        """`low` = evidencia débil de que sea tuya, NO "no es tuya". La etiqueta
        roja se lee al revés y ese malentendido es el que encadenaba descartes
        indebidos."""
        bloque = self.js[self.js.index("const CONF_TIP={"):]
        bloque = bloque[:bloque.index("\n};")].lower()
        self.assertIn("débil", bloque)
        self.assertIn("no significa que no sea tuya", bloque)

    def test_ningun_tooltip_lleva_html(self):
        """Los textos van al atributo `title` y el render los escapa; aun así,
        que no haya etiquetas en el origen."""
        for nombre in ("REASON_TIP", "CONF_TIP"):
            bloque = self.js[self.js.index(f"const {nombre}={{"):]
            bloque = bloque[:bloque.index("\n};")]
            self.assertNotRegex(bloque, r"<[a-zA-Z/]", f"{nombre} lleva HTML crudo")

    def test_showconfirm_escapa_antes_de_romper_lineas(self):
        """El cuerpo del modal es texto plano: se escapa ENTERO y solo después
        se convierten los `\\n`. Invertir el orden dejaría pasar HTML."""
        cuerpo = self.js[self.js.index("function confirmBodyHtml("):]
        cuerpo = cuerpo[:cuerpo.index("\n}")]
        self.assertLess(cuerpo.index("escapeHtml("), cuerpo.index('replace(/\\n/g'))


class PreviewDescarteMasivoTest(IsolatedTestCase):
    """El conteo exacto que alimenta el modal de confirmación."""

    def setUp(self):
        super().setUp()
        from rastrillo import db, server
        self.db = db
        self.server = server
        db.init()
        self.client = auth_client()

    def _alta(self, host, ident, conf, owned=0, status="found"):
        aid = self.db.upsert_account(host.split(".")[0], ident, source="sherlock",
                                     source_site=host, display_name=host.upper(),
                                     status=status, confidence=conf)
        if owned:
            self.db.update_account(aid, owned=1)
        return aid

    def test_el_preview_da_el_conteo_exacto(self):
        self._alta("a.com", "mar", "low")
        self._alta("b.com", "mar", "low")
        self._alta("c.com", "mar", "high")
        r = self.client.get("/api/accounts/discard-low/preview", headers=self.hdr())
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["count"], 2)
        sitios = {a["source_site"] for a in r.json()["accounts"]}
        self.assertEqual(sitios, {"a.com", "b.com"})

    def test_el_preview_dice_que_es_reversible(self):
        self._alta("a.com", "mar", "low")
        r = self.client.get("/api/accounts/discard-low/preview", headers=self.hdr())
        self.assertTrue(r.json()["reversible"])

    def test_el_preview_no_escribe_nada(self):
        aid = self._alta("a.com", "mar", "low")
        for _ in range(3):
            self.client.get("/api/accounts/discard-low/preview", headers=self.hdr())
        self.assertEqual(self.db.get_account(aid)["status"], "found")
        self.assertEqual(self.db.list_discards(), [])

    def test_el_conteo_coincide_con_lo_que_escribe_el_endpoint(self):
        """El preview y el POST comparten criterio: el número prometido es el
        que ocurre. Metemos casos de borde en los dos sentidos."""
        self._alta("a.com", "mar", "low")
        self._alta("b.com", "mar", "low")
        self._alta("c.com", "mar", "low", owned=1)        # confirmada como mía
        self._alta("d.com", "mar", "low", status="manual")  # no está en 'found'
        self._alta("e.com", "mar", None)                   # sin confianza

        prometido = self.client.get("/api/accounts/discard-low/preview",
                                    headers=self.hdr()).json()["count"]
        real = self.client.post("/api/accounts/discard-low", json={},
                                headers=self.hdr()).json()["discarded"]
        self.assertEqual(prometido, 2)
        self.assertEqual(prometido, real)

    def test_preview_vacio_cuando_no_hay_nada_que_barrer(self):
        self._alta("a.com", "mar", "high")
        r = self.client.get("/api/accounts/discard-low/preview", headers=self.hdr())
        self.assertEqual(r.json()["count"], 0)
        self.assertEqual(r.json()["accounts"], [])

    def test_el_preview_pide_token(self):
        r = self.client.get("/api/accounts/discard-low/preview")
        self.assertEqual(r.status_code, 401)

    def test_el_modal_usa_el_conteo_del_preview(self):
        """El front tiene que pedir el preview ANTES de abrir la confirmación, y
        el texto tiene que llevar el número y la reversibilidad."""
        js = _APP_JS.read_text(encoding="utf-8")
        bloque = js[js.index("async function discardLowConfidence()"):]
        bloque = bloque[:bloque.index("\nfunction askProcessAllAuto")]
        self.assertIn("/api/accounts/discard-low/preview", bloque)
        # El preview se pide antes de mostrar el modal.
        self.assertLess(bloque.index("discard-low/preview"),
                        bloque.index("showConfirm("))
        # Y el cuerpo dice cuántas y que se deshace.
        self.assertIn("${n}", bloque)
        self.assertIn("REVERSIBLE", bloque)
