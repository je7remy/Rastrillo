"""Paso 4: el informe PDF como documento.

Sin dependencias nuevas no se puede parsear un PDF, así que aquí se comprueba
lo que sí es comprobable sin lector:

  - que la salida sea un PDF bien formado (cabecera `%PDF`, marcador `%%EOF`),
  - el número de páginas de un conjunto conocido, contando objetos de página
    en el fichero,
  - que una DB vacía produzca un PDF válido y no una excepción,
  - que cadenas patológicas (URL de 500 caracteres, cirílico, campos `None`)
    no rompan nada y no impriman "None",
  - que 300 filas terminen en un tiempo razonable,
  - y EL test del enunciado: que los textos del anexo salgan de la MISMA
    fuente que los tooltips del dashboard, con un parser independiente del que
    usa `glosario.py` para que una divergencia se note.

Los formatos json y csv no se tocan; sus tests viven donde siempre.
"""
from __future__ import annotations

import re
import time
from pathlib import Path

from .helpers import IsolatedTestCase

_RAIZ = Path(__file__).resolve().parents[1]
_APP_JS = _RAIZ / "rastrillo" / "static" / "app.js"

# Un objeto de página en un PDF de reportlab se escribe sin comprimir como
# `/Type /Page` (con `/Pages` para el árbol, de ahí el `[^s]`).
_RE_PAGINA = re.compile(rb"/Type\s*/Page[^s]")


def _paginas(pdf: bytes) -> int:
    return len(_RE_PAGINA.findall(pdf))


class _BasePDF(IsolatedTestCase):
    """Importa el paquete DESPUÉS del sandbox y expone los módulos."""

    def setUp(self):
        super().setUp()
        from rastrillo import db, glosario, pdf_fuentes, report_pdf, reports
        self.db = db
        self.glosario = glosario
        self.pdf_fuentes = pdf_fuentes
        self.report_pdf = report_pdf
        self.reports = reports
        self.db.init()

    def _pdf(self):
        contenido, media, nombre = self.reports.build_report("pdf")
        self.assertEqual(media, "application/pdf")
        self.assertTrue(nombre.endswith(".pdf"))
        self.assertIsInstance(contenido, bytes)
        return contenido

    def _alta(self, **campos):
        """Inserta una cuenta con valores por defecto razonables."""
        base = dict(platform="ejemplo", identifier="usuario",
                    source_site="ejemplo.com", source="sherlock",
                    status="found", confidence="high")
        base.update(campos)
        return self.db.upsert_account(
            base.pop("platform"), base.pop("identifier"), **base)


class FormatoTest(_BasePDF):
    """1) La salida es un PDF bien formado."""

    def test_empieza_por_pdf_y_acaba_en_eof(self):
        self._alta()
        pdf = self._pdf()
        self.assertTrue(pdf.startswith(b"%PDF-"),
                        f"no empieza por %PDF: {pdf[:16]!r}")
        self.assertTrue(pdf.rstrip().endswith(b"%%EOF"),
                        f"no acaba en %%EOF: {pdf[-32:]!r}")
        # Un PDF sin tabla de referencias cruzadas no lo abre ningún visor.
        self.assertIn(b"startxref", pdf)
        self.assertIn(b"/Type /Catalog", pdf)

    def test_metadatos_del_documento(self):
        """El título del PDF es lo que ve el visor en la barra de ventana."""
        self._alta()
        pdf = self._pdf()
        self.assertIn(b"Rastrillo", pdf)


class PaginacionTest(_BasePDF):
    """2) Número de páginas para conjuntos conocidos."""

    def test_documento_minimo_tiene_las_cuatro_partes(self):
        """Portada + resumen + detalle + anexo: nunca menos de 4 páginas.

        La portada y el resumen terminan en PageBreak explícito, así que el
        suelo es estructural y no depende de cuántas cuentas haya.
        """
        self._alta()
        self.assertGreaterEqual(_paginas(self._pdf()), 4)

    def test_una_cuenta_cabe_en_el_mismo_numero_de_paginas_que_tres(self):
        """Tres entradas cortas no desbordan la página de detalle."""
        self._alta(identifier="uno")
        una = _paginas(self._pdf())
        self._alta(identifier="dos")
        self._alta(identifier="tres")
        self.assertEqual(_paginas(self._pdf()), una)

    def test_mas_cuentas_es_mas_paginas(self):
        """Monotonía: 120 entradas ocupan más que 1. Fija que el detalle se
        pagina de verdad y no se recorta en silencio."""
        self._alta(identifier="solo")
        pocas = _paginas(self._pdf())
        for i in range(120):
            self._alta(identifier=f"usuario-{i:03d}",
                       source_site=f"sitio-{i:03d}.com")
        self.assertGreater(_paginas(self._pdf()), pocas)


class DBVaciaTest(_BasePDF):
    """3) DB sin cuentas → PDF válido que lo dice, no una excepción."""

    def test_pdf_valido_sin_cuentas(self):
        self.assertEqual(self.db.list_accounts(), [])
        pdf = self._pdf()
        self.assertTrue(pdf.startswith(b"%PDF-"))
        self.assertTrue(pdf.rstrip().endswith(b"%%EOF"))
        self.assertGreaterEqual(_paginas(pdf), 4)
        self.assertGreater(len(pdf), 1000)

    def test_render_directo_sin_resumen(self):
        """`render_pdf` tolera un summary vacío y accounts a None."""
        pdf = self.report_pdf.render_pdf(None, {}, None, 1770000000.0)
        self.assertTrue(pdf.startswith(b"%PDF-"))


class CadenasPatologicasTest(_BasePDF):
    """4) Nada de lo raro rompe el informe."""

    URL_LARGA = "https://ejemplo.com/" + "segmento-larguisimo" * 26  # >500

    def test_url_de_500_caracteres_no_rompe(self):
        self.assertGreater(len(self.URL_LARGA), 500)
        self._alta(profile_url=self.URL_LARGA)
        pdf = self._pdf()
        self.assertTrue(pdf.startswith(b"%PDF-"))
        self.assertGreater(len(pdf), 1000)

    def test_cirilico_y_nombres_largos(self):
        self._alta(
            platform="ЖЖ",
            display_name="Живой Журнал — очень длинное название сайта",
            identifier="пользователь@пример.рф",
            source_site="живойжурнал.рф",
            profile_url="https://живойжурнал.рф/users/пользователь",
            last_message="Аккаунт удалён — подтверждение отправлено")
        pdf = self._pdf()
        self.assertTrue(pdf.startswith(b"%PDF-"))
        self.assertGreater(len(pdf), 1000)

    def test_campos_nulos_no_imprimen_None(self):
        """`None` en la página es el error clásico. No puede salir nunca."""
        self._alta(identifier="sin-datos", profile_url=None,
                   confidence=None, verifiability=None,
                   confidence_reasons=None, action_meta=None,
                   last_message=None, display_name=None)
        pdf = self._pdf()
        self.assertTrue(pdf.startswith(b"%PDF-"))
        # El texto va comprimido dentro del PDF, así que se comprueba sobre
        # los flowables: es donde estaría el "None" si lo hubiera.
        for texto in self._textos_de_la_entrada():
            self.assertNotIn("None", texto,
                             f"un campo nulo se imprimió como None: {texto!r}")
            self.assertNotIn("null", texto)

    def _textos_de_la_entrada(self):
        """Texto plano de todos los Paragraph de la entrada de una cuenta."""
        from reportlab.platypus import Paragraph
        fila = dict(self.db.list_accounts()[0])
        fila.setdefault("days_since_sent", None)
        comp = self.report_pdf._Compositor(self.pdf_fuentes.elegir_familia())
        entrada = self.report_pdf._entrada(comp, fila)
        salida = []
        pendientes = list(entrada._content)
        while pendientes:
            f = pendientes.pop()
            if isinstance(f, Paragraph):
                salida.append(f.text)
            for attr in ("_cellvalues",):
                if hasattr(f, attr):
                    for fila_tabla in getattr(f, attr):
                        pendientes.extend(fila_tabla)
        return salida

    def test_confianza_nula_dice_de_que_eje_habla(self):
        """"sin evaluar" a secas se confunde con la verificabilidad."""
        self._alta(identifier="sin-tramo", confidence=None)
        textos = " ".join(self._textos_de_la_entrada())
        self.assertIn("confianza sin evaluar", textos)

    def test_verificabilidad_nula_se_dice_con_palabras(self):
        """NULL no es un veredicto: es que el canario no miró. Se dice así."""
        self._alta(identifier="sin-canario", verifiability=None)
        textos = " ".join(self._textos_de_la_entrada())
        self.assertIn("no evaluada", textos)

    def test_sin_urls_dice_no_disponible(self):
        self._alta(identifier="holehe-like", profile_url=None)
        textos = " ".join(self._textos_de_la_entrada())
        self.assertIn("Perfil detectado", textos)
        self.assertIn("Cómo darse de baja", textos)
        self.assertIn("no disponible", textos)


class SaltosDePaginaTest(_BasePDF):
    """Una entrada no se parte y un título no se queda solo."""

    def _alto_de_entrada(self, fila) -> float:
        from reportlab.platypus.flowables import _listWrapOn
        comp = self.report_pdf._Compositor(self.pdf_fuentes.elegir_familia())
        fila.setdefault("days_since_sent", None)
        entrada = self.report_pdf._entrada(comp, fila)
        _w, alto = _listWrapOn(entrada._content, self._ancho(), None)
        return alto

    def _ancho(self):
        return self.report_pdf.ANCHO_TEXTO

    def _alto_util(self) -> float:
        import io
        from reportlab.lib.pagesizes import A4
        from reportlab.platypus import SimpleDocTemplate
        rp = self.report_pdf
        return SimpleDocTemplate(
            io.BytesIO(), pagesize=A4,
            leftMargin=rp.MARGEN_LATERAL, rightMargin=rp.MARGEN_LATERAL,
            topMargin=rp.MARGEN_SUPERIOR, bottomMargin=rp.MARGEN_INFERIOR).height

    def test_la_entrada_va_en_un_KeepTogether(self):
        from reportlab.platypus import KeepTogether
        comp = self.report_pdf._Compositor(self.pdf_fuentes.elegir_familia())
        entrada = self.report_pdf._entrada(
            comp, {"source_site": "e.com", "identifier": "u", "status": "found",
                   "days_since_sent": None})
        self.assertIsInstance(entrada, KeepTogether)

    def test_una_entrada_normal_cabe_de_sobra_en_una_pagina(self):
        """`KeepTogether` solo puede mantener unido lo que quepa entero: si
        una entrada superara la altura del frame, reportlab la partiría."""
        alto = self._alto_de_entrada({
            "source_site": "ejemplo.com", "identifier": "usuario",
            "status": "found", "confidence": "high",
            "profile_url": "https://ejemplo.com/u/usuario",
            "action_meta": '{"url":"https://ejemplo.com/close"}',
            "confidence_reasons": '[{"code":"tramo_distintivo"}]',
            "last_message": "una nota normal", "days_since_sent": 3,
            "email_to": "privacy@ejemplo.com"})
        self.assertLess(alto, self._alto_util() / 3)

    def test_hasta_la_entrada_peor_cabe_en_una_pagina(self):
        """Con los topes (URL 500, nota 220) el caso peor sigue por debajo de
        la altura del frame. Si alguien sube un tope, esto avisa."""
        alto = self._alto_de_entrada({
            "source_site": "ejemplo.com",
            "display_name": "N" * self.report_pdf.MAX_SITIO,
            "identifier": "i" * self.report_pdf.MAX_IDENT,
            "status": "found", "confidence": "high",
            "verifiability": "indiscriminado",
            "profile_url": "https://e.com/" + "a" * 600,
            "action_meta": '{"url":"https://e.com/x/' + "b" * 600 + '"}',
            "confidence_reasons": '[{"code":"fuente_holehe"},'
                                  '{"code":"corrob_cruzada"}]',
            "last_message": "—" * 400, "days_since_sent": 41,
            "email_to": "dpo@ejemplo.com"})
        self.assertLess(alto, self._alto_util(),
                        "la entrada peor no cabe en una página: KeepTogether "
                        "no podrá evitar que se parta")

    def test_los_titulos_no_se_quedan_solos(self):
        """`keepWithNext` en secciones y grupos: sin él, una cabecera puede
        quedarse al pie de una página con su contenido en la siguiente."""
        comp = self.report_pdf._Compositor(self.pdf_fuentes.elegir_familia())
        for nombre in ("seccion", "grupo", "glosario_term"):
            self.assertTrue(comp.estilo(nombre).keepWithNext,
                            f"el estilo `{nombre}` no lleva keepWithNext")


class VolumenTest(_BasePDF):
    """5) 300 filas: no lanza y no tarda una eternidad."""

    LIMITE_SEGUNDOS = 60

    def test_trescientas_filas(self):
        for i in range(300):
            self._alta(platform=f"sitio{i}", identifier=f"usuario-{i:03d}",
                       source_site=f"sitio-{i:03d}.example",
                       profile_url=f"https://sitio-{i:03d}.example/u/usuario",
                       status="found" if i % 3 else "deleted",
                       confidence=("high", "medium", "low")[i % 3])
        self.assertEqual(len(self.db.list_accounts()), 300)
        t0 = time.monotonic()
        pdf = self._pdf()
        tardanza = time.monotonic() - t0
        self.assertTrue(pdf.startswith(b"%PDF-"))
        self.assertTrue(pdf.rstrip().endswith(b"%%EOF"))
        self.assertGreater(_paginas(pdf), 10)
        self.assertLess(tardanza, self.LIMITE_SEGUNDOS,
                        f"300 filas tardaron {tardanza:.1f}s")

    def test_muchos_identificadores_no_desbordan_la_portada(self):
        """Regresión del Paso 4.

        El «Alcance» de la portada enumeraba TODOS los identificadores. Con
        300 cuentas de nombres largos y dos filas (usuarios + correos) el
        párrafo superaba la altura del frame y reportlab abortaba con
        `LayoutError` — el informe entero se perdía por la portada.

        Con nombres cortos el caso quedaba a un 5% del límite y pasaba
        desapercibido, así que aquí se usan identificadores largos y de los
        dos tipos, que es como se destapó generando las muestras.
        """
        for i in range(300):
            largo = f"identificador-bastante-largo-numero-{i:03d}"
            self._alta(platform=f"sitio{i}",
                       identifier=largo if i % 2 else f"{largo}@ejemplo.com",
                       source_site=f"sitio-{i:03d}.example")
        pdf = self._pdf()          # antes: LayoutError
        self.assertTrue(pdf.startswith(b"%PDF-"))

    def test_el_alcance_se_resume(self):
        """Enumerar 300 identificadores no informa; decir cuántos hay, sí."""
        filas = [{"identifier": f"usuario-{i:03d}"} for i in range(300)]
        _correos, usuarios = self.report_pdf._alcance(filas)
        self.assertEqual(len(usuarios), 300)
        resumen = self.report_pdf._lista_resumida(
            usuarios, self.report_pdf.MAX_ALCANCE)
        self.assertIn("usuario-000", resumen)
        self.assertIn("288 más", resumen)
        self.assertNotIn("usuario-299", resumen)

    def test_lista_resumida_no_miente_cuando_cabe_entera(self):
        r = self.report_pdf._lista_resumida(["a", "b", "c"], 12)
        self.assertEqual(r, "a, b, c")
        self.assertNotIn("más", r)


# ── 6) El anexo y los tooltips, la misma fuente ─────────────────────────────
# Parser INDEPENDIENTE del de `glosario.py`. Está escrito de otra manera a
# propósito —por líneas, no contando llaves— para que si el de producción se
# desvía (o si alguien deja de leer app.js y hardcodea las frases) se note.
_OBJETOS = ("CONF_LABEL", "CONF_TIP", "REASON_LABEL", "REASON_TIP")
_RE_ENTRADA = re.compile(r'(\w+)\s*:\s*((?:"(?:[^"\\]|\\.)*"\s*\+?\s*)+)', re.S)
_RE_LITERAL = re.compile(r'"((?:[^"\\]|\\.)*)"')


def _cuerpo_por_lineas(lineas, nombre: str) -> str:
    """Texto entre las llaves de `const NOMBRE={`, cerrando en la línea que
    acaba en `};` (la convención de escritura de app.js)."""
    for i, linea in enumerate(lineas):
        if not re.match(rf"\s*const {nombre}\s*=\s*\{{", linea):
            continue
        trozos = [linea[linea.index("{") + 1:]]
        j = i
        while not lineas[j].rstrip().endswith("};"):
            j += 1
            if j >= len(lineas):
                raise AssertionError(f"`const {nombre}` no cierra en app.js")
            trozos.append(lineas[j])
        return "\n".join(trozos)
    raise AssertionError(f"no encuentro `const {nombre}` en app.js")


def _objetos_de_app_js() -> dict:
    lineas = _APP_JS.read_text(encoding="utf-8").splitlines()
    fuera = {}
    for nombre in _OBJETOS:
        entradas = {}
        for clave, valor in _RE_ENTRADA.findall(_cuerpo_por_lineas(lineas, nombre)):
            entradas[clave] = "".join(
                t.replace('\\"', '"').replace("\\\\", "\\")
                for t in _RE_LITERAL.findall(valor))
        fuera[nombre] = entradas
    return fuera


class AnexoMismaFuenteTest(_BasePDF):
    """EL test del enunciado: si el anexo y los tooltips divergen, falla."""

    def setUp(self):
        super().setUp()
        self.js = _objetos_de_app_js()

    def test_el_parser_independiente_encuentra_algo(self):
        """Guarda del propio barrido: un regex que deja de casar no puede
        pasar por 'todo verde'."""
        self.assertIn("CONF_TIP", self.js)
        self.assertIn("REASON_TIP", self.js)
        self.assertEqual(set(self.js["CONF_TIP"]), {"high", "medium", "low"})
        self.assertGreaterEqual(len(self.js["REASON_TIP"]), 15)

    def test_glosario_lee_exactamente_lo_mismo_que_app_js(self):
        textos = self.glosario.cargar_textos()
        self.assertIsNone(self.glosario.error_textos())
        for objeto in ("CONF_LABEL", "CONF_TIP", "REASON_LABEL", "REASON_TIP"):
            self.assertEqual(
                textos[objeto], self.js[objeto],
                f"{objeto} del glosario no coincide con app.js")

    def test_el_anexo_contiene_las_frases_de_los_tooltips(self):
        """No basta con que el glosario las lea: tienen que ir al documento."""
        from reportlab.platypus import Paragraph
        comp = self.report_pdf._Compositor(self.pdf_fuentes.elegir_familia())
        textos = " ".join(f.text for f in self.report_pdf._anexo(comp)
                          if isinstance(f, Paragraph))
        for nivel, frase in self.js["CONF_TIP"].items():
            self.assertIn(comp.limpio(frase), textos,
                          f"falta en el anexo la explicación de {nivel}")
        for code, frase in self.js["REASON_TIP"].items():
            self.assertIn(comp.limpio(frase), textos,
                          f"falta en el anexo la explicación de {code}")

    def test_los_veredictos_del_canario_estan_en_el_anexo(self):
        from reportlab.platypus import Paragraph
        comp = self.report_pdf._Compositor(self.pdf_fuentes.elegir_familia())
        textos = " ".join(f.text for f in self.report_pdf._anexo(comp)
                          if isinstance(f, Paragraph))
        for code in self.glosario.MOTIVOS_CANARIO:
            self.assertIn(code, self.js["REASON_TIP"],
                          f"{code} ya no tiene tooltip en app.js")
            self.assertIn(comp.limpio(self.js["REASON_TIP"][code]), textos)

    def test_low_no_se_lee_como_no_es_tuya(self):
        """El matiz del Paso 3 tiene que sobrevivir al papel."""
        frase = self.js["CONF_TIP"]["low"]
        self.assertIn("DÉBIL", frase)
        self.assertIn("No significa que no sea tuya", frase)

    def test_si_app_js_no_se_puede_leer_el_anexo_lo_dice(self):
        """Degradación visible: sin glosario, se avisa; no se inventan frases
        ni se lanza una excepción a mitad del informe."""
        from reportlab.platypus import Paragraph
        self.glosario._APP_JS = Path(self.HOME) / "no-existe.js"
        self.glosario.cargar_textos(force=True)
        self.assertIsNotNone(self.glosario.error_textos())
        comp = self.report_pdf._Compositor(self.pdf_fuentes.elegir_familia())
        textos = " ".join(f.text for f in self.report_pdf._anexo(comp)
                          if isinstance(f, Paragraph))
        self.assertIn("No se pudieron leer las definiciones", textos)
        # Y el informe entero sigue generándose.
        self._alta()
        self.assertTrue(self._pdf().startswith(b"%PDF-"))


class FuentesTest(_BasePDF):
    """La fuente elegida y el saneado de lo que no cubre."""

    def test_hay_familia_y_esta_registrada(self):
        fam = self.pdf_fuentes.elegir_familia()
        self.assertTrue(fam.regular)
        self.assertTrue(fam.origen)
        from reportlab.pdfbase import pdfmetrics
        pdfmetrics.getFont(fam.regular)      # lanza si no está registrada
        pdfmetrics.getFont(fam.negrita)

    def test_sanear_nunca_lanza_y_nunca_devuelve_none(self):
        fam = self.pdf_fuentes.elegir_familia()
        for entrada in (None, "", 0, 12.5, "texto", b"bytes".decode(),
                        "\U0001F4A5", "テスト", "​", "á"):
            texto, n = self.pdf_fuentes.sanear(entrada, fam)
            self.assertIsInstance(texto, str)
            self.assertIsInstance(n, int)
            self.assertNotIn("None", texto)

    def test_lo_no_representable_sale_visible_y_se_cuenta(self):
        """Un hueco en blanco sería peor que un código feo."""
        fam = self.pdf_fuentes.elegir_familia()
        texto, n = self.pdf_fuentes.sanear("emoji \U0001F4A5", fam)
        self.assertEqual(n, 1)
        self.assertIn("[U+1F4A5]", texto)

    def test_base14_no_cubre_cirilico_pero_lo_dice(self):
        """El escalón peor: sin TTF, el cirílico se escapa, no desaparece."""
        fam = self.pdf_fuentes._familia_base14()
        self.assertFalse(fam.cubre_cirilico)
        texto, n = self.pdf_fuentes.sanear("ВК", fam)
        self.assertEqual(n, 2)
        self.assertEqual(texto, "[U+0412][U+041A]")

    def test_vera_de_reportlab_es_usable_como_respaldo(self):
        """El escalón 2 tiene que existir de verdad en esta instalación."""
        fam = self.pdf_fuentes._familia_vera()
        self.assertIsNotNone(fam, "reportlab ya no empaqueta Vera.ttf")
        self.assertFalse(fam.cubre_cirilico)      # 0/96, medido en el Paso 4
        texto, _ = self.pdf_fuentes.sanear("café", fam)
        self.assertEqual(texto, "café")

    def test_equivalencias_tipograficas_no_gastan_escape(self):
        """Una flecha no merece un [U+2192] si `->` se lee igual."""
        fam = self.pdf_fuentes._familia_base14()
        texto, n = self.pdf_fuentes.sanear("a → b", fam)
        self.assertEqual(n, 0)
        self.assertIn("->", texto)


class SeparacionDeUrlsTest(_BasePDF):
    """El bug que se arregló en la UI no vuelve en papel."""

    def test_perfil_y_baja_son_campos_distintos(self):
        self._alta(identifier="dos-urls",
                   profile_url="https://ejemplo.com/u/dos-urls",
                   action_meta='{"url": "https://ejemplo.com/settings/close"}')
        comp = self.report_pdf._Compositor(self.pdf_fuentes.elegir_familia())
        fila = dict(self.db.list_accounts()[0])
        fila.setdefault("days_since_sent", None)
        from reportlab.platypus import Paragraph
        entrada = self.report_pdf._entrada(comp, fila)
        textos = []
        pendientes = list(entrada._content)
        while pendientes:
            f = pendientes.pop()
            if isinstance(f, Paragraph):
                textos.append(f.text)
            if hasattr(f, "_cellvalues"):
                for fila_tabla in f._cellvalues:
                    pendientes.extend(fila_tabla)
        junto = " ".join(textos)
        self.assertIn("https://ejemplo.com/u/dos-urls", junto)
        self.assertIn("https://ejemplo.com/settings/close", junto)

    def test_deletion_url_sigue_disponible_en_server(self):
        """`server._deletion_url` no se rompió al bajarla a `reports`."""
        from rastrillo import server
        self.assertIs(server._deletion_url, self.reports.deletion_url)
        self.assertIsNone(self.reports.deletion_url(None, None))
        self.assertIsNone(self.reports.deletion_url("{no json", None))
        self.assertIsNone(self.reports.deletion_url(
            '{"url": "https://a/b"}', "https://a/b"))
        self.assertEqual(self.reports.deletion_url(
            '{"url": "https://a/c"}', "https://a/b"), "https://a/c")


class CoberturaDeEstadosTest(_BasePDF):
    """Que no se pueda añadir un estado y dejarlo sin sitio en el informe."""

    def test_todos_los_estados_conocidos_tienen_orden_en_el_detalle(self):
        faltan = set(self.glosario.STATUS_META) - set(self.report_pdf.ORDEN_ESTADOS)
        self.assertEqual(faltan, set(),
                         f"estados sin posición en ORDEN_ESTADOS: {sorted(faltan)}")

    def test_todos_los_estados_de_la_db_tienen_etiqueta(self):
        """`db` y el glosario tienen que hablar del mismo conjunto.

        Los estados están documentados en la cabecera de `db.py` como
        `nombre -> qué significa`; de ahí se leen.
        """
        fuente = (_RAIZ / "rastrillo" / "db.py").read_text(encoding="utf-8")
        cabecera = fuente.split('"""')[1]
        estados = set(re.findall(r"^\s{2}(\w+)\s*->", cabecera, re.M))
        self.assertGreaterEqual(len(estados), 12,
                                "no encuentro la lista de estados en db.py")
        faltan = estados - set(self.glosario.STATUS_META)
        self.assertEqual(faltan, set(),
                         f"estados sin etiqueta en STATUS_META: {sorted(faltan)}")

    def test_estado_desconocido_no_rompe_el_informe(self):
        self._alta(identifier="raro", status="estado_inventado")
        pdf = self._pdf()
        self.assertTrue(pdf.startswith(b"%PDF-"))
