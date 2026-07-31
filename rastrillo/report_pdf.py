"""Informe PDF: un documento, no un volcado de la tabla.

Arquitectura (Paso 4)
---------------------
Cuatro partes, en este orden:

  1. **Portada** — título, cuándo se generó, alcance (qué identificadores hay
     en el informe), los totales grandes y la advertencia de que el documento
     lleva datos personales y se generó en local.
  2. **Resumen** — distribución por estado y por confianza, con barras
     dibujadas a mano (rectángulos del canvas: cero dependencias nuevas), y el
     recuento del audit log.
  3. **Detalle por cuenta** — una entrada por cuenta, agrupadas por estado. La
     URL del PERFIL y la de BAJA van como dos campos distintos y etiquetados:
     confundirlas fue un bug real de la UI y no se reintroduce en papel.
  4. **Anexo** — qué significan los tramos de confianza y los veredictos del
     canario, con los textos de los tooltips del dashboard (ver `glosario.py`:
     se leen de `app.js`, no se copian).

Criterios de diseño, explícitos para que sean auditables
--------------------------------------------------------
- **Retícula.** A4 con márgenes de 24 mm a los lados; la caja de texto mide
  162 mm, que a 9,5 pt son ~88 caracteres por línea.
- **Tipografía.** Una sola familia (ver `pdf_fuentes.py`) y cuatro tamaños de
  contenido: 24 / 14 / 10,5 / 9,5 pt, más 7,5 pt para cabecera y pie, que son
  cromo y no contenido. La jerarquía la hacen el tamaño y el peso.
- **Color.** Fondo blanco y un único acento, usado en la barra de la portada,
  el filete bajo los títulos de sección y el relleno de las barras.
  **Ningún dato depende del color**: el estado, la confianza y la
  verificabilidad se imprimen siempre como palabras, y cada barra lleva su
  número al lado. En blanco y negro el informe se lee igual.
- **Tablas.** Sin rejilla. Filetes finos solo donde separan de verdad y
  cabeceras en negrita, nunca en bloque de color saturado.
- **Saltos.** Cada entrada de cuenta va en un `KeepTogether`, así que no se
  parte entre páginas; los títulos llevan `keepWithNext`, así que no se quedan
  solos al final de una página.

Privacidad: igual que antes, el CUERPO de los correos GDPR no se imprime
(puede llevar PII); destinatario y asunto sí.
"""
from __future__ import annotations

import io
import json
import time as _t
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT, TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfgen import canvas as _canvas
from reportlab.platypus import (
    Flowable, KeepTogether, PageBreak, Paragraph, SimpleDocTemplate, Spacer,
    Table, TableStyle,
)

from . import db, glosario, pdf_fuentes

# ── Retícula ────────────────────────────────────────────────────────────────
MARGEN_LATERAL = 24 * mm
MARGEN_SUPERIOR = 22 * mm      # deja sitio a la cabecera, que va en 13 mm
MARGEN_INFERIOR = 20 * mm      # deja sitio al pie, que va en 12 mm
ANCHO_TEXTO = A4[0] - 2 * MARGEN_LATERAL

# ── Escala tipográfica ──────────────────────────────────────────────────────
T_PORTADA = 24
T_SECCION = 14
T_ENTRADA = 10.5
T_CUERPO = 9.5
T_CROMO = 7.5                  # cabecera y pie: no es contenido

# ── Paleta ──────────────────────────────────────────────────────────────────
TINTA = colors.HexColor("#1A1A1A")
TINTA_SUAVE = colors.HexColor("#5C5C5C")
TINTA_TENUE = colors.HexColor("#8A8A8A")
FILETE = colors.HexColor("#D9D9D9")
ACENTO = colors.HexColor("#0F6E56")      # el verde del proyecto, legible en papel
PISTA = colors.HexColor("#F1F1F1")       # fondo de la barra: da la escala

# Orden narrativo del detalle: primero lo que ya está hecho, luego lo que
# espera al usuario, y al final lo que se decidió no tocar.
ORDEN_ESTADOS: Sequence[str] = (
    "deleted", "anonymized", "user_done", "dry_run",
    "pending_deletion", "email_draft", "semi_auto", "awaiting_user",
    "in_progress", "queued", "manual", "found",
    "failed", "skipped", "not_mine",
)

# Recortes. Las URL se parten en varias líneas en vez de recortarse (el
# usuario tiene que poder usarlas), pero con un techo: por encima de
# MAX_URL una sola URL ocuparía media página y no aporta nada.
MAX_SITIO = 60
MAX_IDENT = 60
MAX_NOTA = 220
MAX_URL = 500
# Identificadores que se listan en el «Alcance» de la portada. Sin techo, un
# informe de 300 cuentas genera un párrafo más alto que la página y reportlab
# aborta con LayoutError; se vio generando la muestra de estrés del Paso 4.
MAX_ALCANCE = 12


def _texto_corto(valor, limite: int) -> str:
    """Recorta con elipsis. `None` y vacíos salen como cadena vacía."""
    if valor is None:
        return ""
    s = str(valor).strip()
    if len(s) <= limite:
        return s
    return s[: limite - 1].rstrip() + "…"


def _lista_resumida(items: Sequence[str], maximo: int) -> str:
    """`a, b, c y 297 más`. Enumerar 300 no informa y desborda la página."""
    items = list(items)
    if len(items) <= maximo:
        return ", ".join(items)
    resto = len(items) - maximo
    return (", ".join(items[:maximo])
            + f" y {resto} más")


def deletion_url_de(action_meta, profile_url) -> Optional[str]:
    """URL de baja guardada por el resolver, o None.

    Mismo criterio que `server._deletion_url`, del que se importa: si el JSON
    está corrupto o la URL coincide con la del hit, no hay segundo enlace.
    """
    from .reports import deletion_url
    return deletion_url(action_meta, profile_url)


# ── Canvas con "página X de Y" ──────────────────────────────────────────────
def _hacer_canvas(titulo: str, subtitulo: str, familia: pdf_fuentes.Familia):
    """Fábrica del canvas: necesita datos que `SimpleDocTemplate` no pasa.

    El total de páginas no se conoce hasta el final, así que se guardan los
    estados de página, se cuentan y se pinta el cromo en una segunda pasada.
    Un informe sin "de Y" parece truncado.
    """

    class _CanvasNumerado(_canvas.Canvas):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self._estados: List[dict] = []

        def showPage(self):
            self._estados.append(dict(self.__dict__))
            self._startPage()

        def save(self):
            total = len(self._estados)
            for estado in self._estados:
                self.__dict__.update(estado)
                self._cromo(total)
                super().showPage()
            super().save()

        def _cromo(self, total: int):
            self.saveState()
            ancho = A4[0]
            # Cabecera: nombre del informe a la izquierda, fecha a la derecha.
            self.setFont(familia.regular, T_CROMO)
            self.setFillColor(TINTA_TENUE)
            self.drawString(MARGEN_LATERAL, A4[1] - 13 * mm, titulo)
            self.drawRightString(ancho - MARGEN_LATERAL, A4[1] - 13 * mm,
                                 subtitulo)
            self.setStrokeColor(FILETE)
            self.setLineWidth(0.4)
            self.line(MARGEN_LATERAL, A4[1] - 15 * mm,
                      ancho - MARGEN_LATERAL, A4[1] - 15 * mm)
            # Pie: recordatorio de qué es esto + paginación con total.
            self.line(MARGEN_LATERAL, 15 * mm, ancho - MARGEN_LATERAL, 15 * mm)
            self.drawString(MARGEN_LATERAL, 12 * mm,
                            "Documento local · contiene datos personales")
            self.drawRightString(ancho - MARGEN_LATERAL, 12 * mm,
                                 f"Página {self._pageNumber} de {total}")
            self.restoreState()

    return _CanvasNumerado


# ── Gráfico de barras (rectángulos del canvas, sin librerías) ───────────────
class BarrasHorizontales(Flowable):
    """Barras horizontales simples.

    Cada fila lleva SIEMPRE su etiqueta y su número: la barra es refuerzo
    visual, no el dato. Impreso en blanco y negro sigue leyéndose.
    """

    ALTO_FILA = 14
    ANCHO_ETIQUETA = 52 * mm
    ANCHO_VALOR = 14 * mm

    def __init__(self, items: Sequence[Tuple[str, int]], ancho: float,
                 familia: pdf_fuentes.Familia):
        super().__init__()
        self.items = list(items)
        self.ancho = ancho
        self.familia = familia

    def wrap(self, disponible_w, disponible_h):
        self.ancho = min(self.ancho, disponible_w)
        return self.ancho, self.ALTO_FILA * max(1, len(self.items))

    def draw(self):
        c = self.canv
        if not self.items:
            return
        maximo = max((v for _, v in self.items), default=0) or 1
        x_barra = self.ANCHO_ETIQUETA
        ancho_pista = self.ancho - self.ANCHO_ETIQUETA - self.ANCHO_VALOR
        y = self.ALTO_FILA * len(self.items) - self.ALTO_FILA

        for etiqueta, valor in self.items:
            c.setFont(self.familia.regular, T_CUERPO - 0.5)
            c.setFillColor(TINTA)
            c.drawString(0, y + 3.5, etiqueta)

            # Pista tenue: da la escala aunque la barra sea diminuta.
            c.setFillColor(PISTA)
            c.rect(x_barra, y + 2.5, ancho_pista, 7, stroke=0, fill=1)
            if valor > 0:
                # Mínimo visible: un 1 sobre 300 no puede desaparecer.
                largo = max(1.5, ancho_pista * (valor / maximo))
                # Un solo tono para todas: teñir la barra más alta de otro
                # color hacía que una diferencia de 1 sobre 75 pareciera una
                # distinción de categoría. Lo que compara es el largo.
                c.setFillColor(ACENTO)
                c.rect(x_barra, y + 2.5, largo, 7, stroke=0, fill=1)

            c.setFont(self.familia.negrita, T_CUERPO - 0.5)
            c.setFillColor(TINTA)
            c.drawRightString(self.ancho, y + 3.5, str(valor))
            y -= self.ALTO_FILA


class FileteAcento(Flowable):
    """Filete corto bajo los títulos de sección. Decoración, no dato."""

    def __init__(self, ancho: float = 22 * mm, grosor: float = 1.6):
        super().__init__()
        self.ancho, self.grosor = ancho, grosor

    def wrap(self, *_):
        return self.ancho, self.grosor + 4

    def draw(self):
        self.canv.setFillColor(ACENTO)
        self.canv.rect(0, 3, self.ancho, self.grosor, stroke=0, fill=1)


# ── Compositor ──────────────────────────────────────────────────────────────
class _Compositor:
    """Estilos, saneado de texto y contadores, en un sitio.

    Lleva la cuenta de los caracteres que la fuente elegida no puede
    representar para poder decirlo en el colofón (ver `pdf_fuentes`).
    """

    def __init__(self, familia: pdf_fuentes.Familia):
        self.familia = familia
        self.no_representables = 0
        self._estilos: Dict[str, ParagraphStyle] = {}
        self._construir_estilos()

    # -- texto ----------------------------------------------------------
    def limpio(self, valor) -> str:
        """Sanea (fuente) y escapa (mini-XML de reportlab). Nunca None."""
        texto, fallos = pdf_fuentes.sanear(valor, self.familia)
        self.no_representables += fallos
        return (texto.replace("&", "&amp;")
                     .replace("<", "&lt;")
                     .replace(">", "&gt;"))

    def p(self, valor, estilo: str = "cuerpo") -> Paragraph:
        return Paragraph(self.limpio(valor), self._estilos[estilo])

    def crudo(self, marcado: str, estilo: str = "cuerpo") -> Paragraph:
        """Para marcado que construimos nosotros (ya con las partes escapadas)."""
        return Paragraph(marcado, self._estilos[estilo])

    def negrita(self, valor) -> str:
        return f"<b>{self.limpio(valor)}</b>"

    def estilo(self, nombre: str) -> ParagraphStyle:
        return self._estilos[nombre]

    # -- estilos --------------------------------------------------------
    def _construir_estilos(self):
        f = self.familia

        def nuevo(nombre, **kw):
            base = dict(fontName=f.regular, fontSize=T_CUERPO,
                        leading=T_CUERPO * 1.42, textColor=TINTA,
                        alignment=TA_LEFT, spaceBefore=0, spaceAfter=0)
            base.update(kw)
            self._estilos[nombre] = ParagraphStyle(nombre, **base)

        nuevo("cuerpo")
        nuevo("portada_titulo", fontName=f.negrita, fontSize=T_PORTADA,
              leading=T_PORTADA * 1.15, spaceAfter=4)
        nuevo("portada_sub", fontSize=T_ENTRADA, leading=T_ENTRADA * 1.4,
              textColor=TINTA_SUAVE, spaceAfter=2)
        nuevo("seccion", fontName=f.negrita, fontSize=T_SECCION,
              leading=T_SECCION * 1.25, spaceBefore=6, spaceAfter=3,
              keepWithNext=1)
        nuevo("grupo", fontName=f.negrita, fontSize=T_ENTRADA,
              leading=T_ENTRADA * 1.3, spaceBefore=10, spaceAfter=4,
              keepWithNext=1)
        nuevo("entrada_sitio", fontName=f.negrita, fontSize=T_ENTRADA,
              leading=T_ENTRADA * 1.25)
        nuevo("entrada_estado", fontSize=T_CUERPO - 0.5,
              leading=T_CUERPO * 1.3, textColor=TINTA_SUAVE,
              alignment=TA_RIGHT)
        nuevo("meta", fontSize=T_CUERPO - 0.5, leading=T_CUERPO * 1.35,
              textColor=TINTA_SUAVE)
        nuevo("etiqueta", fontSize=T_CUERPO - 0.5, leading=T_CUERPO * 1.35,
              textColor=TINTA_TENUE)
        # `CJK` parte por cualquier carácter: es el modo que impide que una URL
        # kilométrica desborde el margen o pise la columna de al lado.
        nuevo("url", fontSize=T_CUERPO - 0.5, leading=T_CUERPO * 1.35,
              wordWrap="CJK")
        nuevo("nota", fontSize=T_CROMO + 0.5, leading=T_CROMO * 1.5,
              textColor=TINTA_SUAVE)
        nuevo("aviso", fontSize=T_CUERPO - 0.5, leading=T_CUERPO * 1.45,
              textColor=TINTA_SUAVE)
        nuevo("glosario_term", fontName=f.negrita, fontSize=T_CUERPO,
              leading=T_CUERPO * 1.35, spaceBefore=7, keepWithNext=1)


# ── Datos derivados ─────────────────────────────────────────────────────────
def _alcance(accounts: Sequence[dict]) -> Tuple[List[str], List[str]]:
    """Identificadores del informe, separados en correos y usernames.

    No hay tabla de "qué se escaneó": se deriva de lo que hay. Es honesto —
    el informe declara su propio contenido, no una intención pasada.
    """
    correos, usuarios = set(), set()
    for a in accounts:
        ident = _texto_corto(a.get("identifier"), MAX_IDENT)
        if not ident:
            continue
        (correos if "@" in ident else usuarios).add(ident)
    return sorted(correos), sorted(usuarios)


def _totales(summary: Dict) -> List[Tuple[str, int]]:
    por_estado = summary.get("by_status") or {}

    def n(*claves) -> int:
        return sum(int(por_estado.get(k, 0) or 0) for k in claves)

    return [
        ("Detectadas", int(summary.get("total", 0) or 0)),
        ("Completadas", n("deleted", "anonymized", "user_done")),
        ("Pendientes de ti", n("email_draft", "semi_auto", "awaiting_user",
                               "pending_deletion", "manual")),
        ("Conservadas", n("skipped")),
        ("Descartadas", n("not_mine")),
    ]


def _reparto_confianza(accounts: Sequence[dict]) -> List[Tuple[str, int]]:
    cuenta = {"high": 0, "medium": 0, "low": 0}
    sin = 0
    for a in accounts:
        c = a.get("confidence")
        if c in cuenta:
            cuenta[c] += 1
        else:
            sin += 1
    filas = [(glosario.etiqueta_confianza(k), cuenta[k])
             for k in glosario.ORDEN_CONFIANZA]
    if sin:
        filas.append(("Sin evaluar", sin))
    return filas


def _reparto_verificabilidad(accounts: Sequence[dict]) -> List[Tuple[str, int]]:
    cuenta: Dict[str, int] = {}
    for a in accounts:
        cuenta[a.get("verifiability") or ""] = (
            cuenta.get(a.get("verifiability") or "", 0) + 1)
    filas = [(glosario.etiqueta_verificabilidad(k), cuenta[k])
             for k in glosario.ORDEN_VERIFICABILIDAD if cuenta.get(k)]
    if cuenta.get(""):
        filas.append(("No evaluada", cuenta[""]))
    return filas


def _agrupar(accounts: Sequence[dict]) -> List[Tuple[str, List[dict]]]:
    """Cuentas por estado, en `ORDEN_ESTADOS`; los desconocidos al final."""
    por_estado: Dict[str, List[dict]] = {}
    for a in accounts:
        por_estado.setdefault(a.get("status") or "", []).append(a)
    for filas in por_estado.values():
        filas.sort(key=lambda r: ((r.get("source_site") or "").lower(),
                                  (r.get("identifier") or "").lower()))
    ordenados = [(e, por_estado.pop(e)) for e in ORDEN_ESTADOS if e in por_estado]
    ordenados += sorted(por_estado.items())
    return ordenados


# ── Piezas del documento ────────────────────────────────────────────────────
def _tabla_simple(filas: Sequence[Sequence[Any]], comp: _Compositor,
                  anchos: Sequence[float], cabecera: bool = False) -> Table:
    """Tabla sin rejilla: filete fino bajo la cabecera y nada más."""
    t = Table(list(filas), colWidths=list(anchos), hAlign="LEFT")
    estilo = [
        ("FONTNAME", (0, 0), (-1, -1), comp.familia.regular),
        ("FONTSIZE", (0, 0), (-1, -1), T_CUERPO),
        ("TEXTCOLOR", (0, 0), (-1, -1), TINTA),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("ALIGN", (1, 0), (-1, -1), "RIGHT"),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 0),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
    ]
    if cabecera:
        estilo += [
            ("FONTNAME", (0, 0), (-1, 0), comp.familia.negrita),
            ("LINEBELOW", (0, 0), (-1, 0), 0.6, FILETE),
        ]
    t.setStyle(TableStyle(estilo))
    return t


def _portada(comp: _Compositor, accounts: Sequence[dict], summary: Dict,
             ts_legible: str) -> List:
    elems: List = [Spacer(1, 26 * mm)]
    # Barra de acento: el único gesto gráfico de la portada.
    elems.append(FileteAcento(ancho=34 * mm, grosor=3))
    elems.append(Spacer(1, 7 * mm))
    elems.append(comp.p("Informe de cuentas", "portada_titulo"))
    elems.append(comp.p("Rastrillo · rastreo y baja de cuentas propias",
                        "portada_sub"))
    elems.append(Spacer(1, 3 * mm))
    elems.append(comp.crudo(f"Generado el {comp.limpio(ts_legible)}", "meta"))

    # Alcance.
    correos, usuarios = _alcance(accounts)
    elems.append(Spacer(1, 9 * mm))
    elems.append(comp.p("Alcance", "grupo"))
    if not correos and not usuarios:
        elems.append(comp.p("El informe no contiene ninguna cuenta.", "cuerpo"))
    else:
        filas = []
        if usuarios:
            filas.append(("Nombres de usuario",
                          _lista_resumida(usuarios, MAX_ALCANCE)))
        if correos:
            filas.append(("Correos", _lista_resumida(correos, MAX_ALCANCE)))
        datos = [[comp.p(k, "etiqueta"), comp.p(v, "cuerpo")] for k, v in filas]
        t = Table(datos, colWidths=[38 * mm, ANCHO_TEXTO - 38 * mm],
                  hAlign="LEFT")
        t.setStyle(TableStyle([
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("LEFTPADDING", (0, 0), (-1, -1), 0),
            ("RIGHTPADDING", (0, 0), (-1, -1), 0),
            ("TOPPADDING", (0, 0), (-1, -1), 2),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ]))
        elems.append(t)

    # Totales grandes.
    elems.append(Spacer(1, 10 * mm))
    totales = _totales(summary)
    ancho_col = ANCHO_TEXTO / len(totales)
    numeros = [Paragraph(
        f'<font size="{T_SECCION + 4}">{comp.limpio(str(v))}</font>',
        ParagraphStyle("n", parent=comp.estilo("cuerpo"),
                       fontName=comp.familia.negrita,
                       leading=(T_SECCION + 4) * 1.2))
        for _, v in totales]
    rotulos = [comp.p(k, "etiqueta") for k, _ in totales]
    t = Table([numeros, rotulos], colWidths=[ancho_col] * len(totales),
              hAlign="LEFT")
    t.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "BOTTOM"),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 6),
        ("TOPPADDING", (0, 0), (-1, -1), 0),
        ("BOTTOMPADDING", (0, 0), (0, 0), 1),
        ("LINEBELOW", (0, 1), (-1, 1), 0.6, FILETE),
    ]))
    elems.append(t)

    # Advertencia.
    elems.append(Spacer(1, 12 * mm))
    elems.append(comp.p(
        "Este documento contiene datos personales: los identificadores que "
        "escaneaste y los sitios donde aparecieron. Se generó en tu equipo y "
        "no se envió a ningún servidor. Guárdalo o destrúyelo con el mismo "
        "cuidado que tendrías con la lista de tus cuentas, porque eso es.",
        "aviso"))
    elems.append(PageBreak())
    return elems


def _resumen(comp: _Compositor, accounts: Sequence[dict], summary: Dict,
             audit_summary: Optional[Dict[str, int]]) -> List:
    elems: List = [comp.p("Resumen", "seccion"), FileteAcento(),
                   Spacer(1, 2 * mm)]

    por_estado = summary.get("by_status") or {}
    filas_estado = [(glosario.etiqueta_estado(e), int(por_estado[e]))
                    for e in ORDEN_ESTADOS if por_estado.get(e)]
    filas_estado += [(glosario.etiqueta_estado(e), int(n))
                     for e, n in sorted(por_estado.items())
                     if e not in ORDEN_ESTADOS and n]

    elems.append(comp.p("Distribución por estado", "grupo"))
    if filas_estado:
        elems.append(BarrasHorizontales(filas_estado, ANCHO_TEXTO,
                                        comp.familia))
    else:
        elems.append(comp.p("Sin cuentas registradas.", "cuerpo"))

    elems.append(comp.p("Distribución por confianza", "grupo"))
    elems.append(comp.p(
        "La confianza dice cuánta evidencia hay de que la cuenta sea tuya. "
        "No es una comprobación; el anexo explica cada tramo.", "meta"))
    elems.append(Spacer(1, 2 * mm))
    conf = [f for f in _reparto_confianza(accounts) if f[1]]
    if conf:
        elems.append(BarrasHorizontales(conf, ANCHO_TEXTO, comp.familia))
    else:
        elems.append(comp.p("Sin cuentas registradas.", "cuerpo"))

    verif = _reparto_verificabilidad(accounts)
    if verif:
        elems.append(comp.p("Verificabilidad de los sitios", "grupo"))
        elems.append(comp.p(
            "Eje distinto del anterior: dice si la respuesta del sitio sirve "
            "para comprobar algo, nunca si la cuenta es tuya.", "meta"))
        elems.append(Spacer(1, 2 * mm))
        elems.append(BarrasHorizontales(verif, ANCHO_TEXTO, comp.familia))

    if audit_summary:
        elems.append(comp.p("Acciones registradas", "grupo"))
        filas = [[comp.p("Acción", "cuerpo"), comp.p("Veces", "cuerpo")]]
        filas += [[comp.p(k, "cuerpo"), comp.p(str(audit_summary[k]), "cuerpo")]
                  for k in sorted(audit_summary)]
        elems.append(_tabla_simple(filas, comp,
                                   [ANCHO_TEXTO - 26 * mm, 26 * mm],
                                   cabecera=True))
        elems.append(Spacer(1, 2 * mm))
        elems.append(comp.p(
            f"Total de acciones en el registro de auditoría: "
            f"{summary.get('audit_total', 0)}.", "meta"))

    elems.append(PageBreak())
    return elems


def _entrada(comp: _Compositor, a: dict) -> KeepTogether:
    """Una cuenta. Va entera o pasa a la página siguiente."""
    sitio = _texto_corto(a.get("display_name") or a.get("source_site")
                         or a.get("platform") or "sitio desconocido", MAX_SITIO)
    cabecera = Table(
        [[comp.p(sitio, "entrada_sitio"),
          comp.p(glosario.etiqueta_estado(a.get("status")), "entrada_estado")]],
        colWidths=[ANCHO_TEXTO - 40 * mm, 40 * mm], hAlign="LEFT")
    cabecera.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "BOTTOM"),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 0),
        ("TOPPADDING", (0, 0), (-1, -1), 0),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
        ("LINEBELOW", (0, 0), (-1, 0), 0.4, FILETE),
    ]))

    # Línea de metadatos. Todo en palabras: nada depende del color.
    piezas = [comp.limpio(_texto_corto(a.get("identifier"), MAX_IDENT)
                          or "sin identificador")]
    if a.get("source"):
        piezas.append(f"fuente: {comp.limpio(a.get('source'))}")
    piezas.append(comp.limpio(
        glosario.etiqueta_confianza(a.get("confidence")).lower()))
    piezas.append("verificabilidad: " + comp.limpio(
        glosario.etiqueta_verificabilidad(a.get("verifiability")).lower()))
    meta = comp.crudo(" · ".join(p for p in piezas if p), "meta")

    # Campos etiquetados. La URL del PERFIL y la de BAJA, separadas.
    perfil = _texto_corto(a.get("profile_url"), MAX_URL)
    baja = _texto_corto(
        deletion_url_de(a.get("action_meta"), a.get("profile_url")), MAX_URL)
    filas: List[Tuple[str, Paragraph]] = [
        ("Perfil detectado",
         comp.p(perfil or "no disponible", "url" if perfil else "etiqueta")),
        ("Cómo darse de baja",
         comp.p(baja or "no disponible", "url" if baja else "etiqueta")),
    ]

    motivos = db.parse_reasons(a.get("confidence_reasons"))
    etiquetas = [glosario.etiqueta_motivo(m.get("code"))
                 for m in motivos if isinstance(m, dict) and m.get("code")]
    etiquetas = [e for e in etiquetas if e]
    filas.append(("Señales",
                  comp.p(" · ".join(etiquetas) if etiquetas
                         else "sin señales registradas",
                         "cuerpo" if etiquetas else "etiqueta")))

    if a.get("email_to"):
        filas.append(("Solicitud a", comp.p(a.get("email_to"), "url")))
    if a.get("days_since_sent") is not None:
        dias = a["days_since_sent"]
        filas.append(("Enviada hace",
                      comp.p(f"{dias} día{'s' if dias != 1 else ''}", "cuerpo")))
    if a.get("last_message"):
        filas.append(("Última nota",
                      comp.p(_texto_corto(a.get("last_message"), MAX_NOTA),
                             "cuerpo")))

    datos = [[comp.p(k, "etiqueta"), v] for k, v in filas]
    tabla = Table(datos, colWidths=[38 * mm, ANCHO_TEXTO - 38 * mm],
                  hAlign="LEFT")
    tabla.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 0),
        ("TOPPADDING", (0, 0), (-1, -1), 1.5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 1.5),
    ]))

    return KeepTogether([cabecera, Spacer(1, 2), meta, Spacer(1, 3), tabla,
                         Spacer(1, 6 * mm)])


def _detalle(comp: _Compositor, accounts: Sequence[dict]) -> List:
    elems: List = [comp.p("Detalle por cuenta", "seccion"), FileteAcento(),
                   Spacer(1, 2 * mm)]
    if not accounts:
        elems.append(comp.p(
            "No hay ninguna cuenta registrada en esta sesión. Si esperabas "
            "encontrar alguna, lanza un escaneo desde el dashboard antes de "
            "generar el informe.", "cuerpo"))
        elems.append(PageBreak())
        return elems

    elems.append(comp.p(
        "«Perfil detectado» es la URL que produjo el hallazgo y sobre la que "
        "se calcularon las señales. «Cómo darse de baja» es adonde hay que ir "
        "para cerrar la cuenta. No son lo mismo y por eso van separadas.",
        "meta"))

    for estado, filas in _agrupar(accounts):
        etiqueta = glosario.etiqueta_estado(estado)
        elems.append(comp.p(f"{etiqueta} ({len(filas)})", "grupo"))
        for a in filas:
            elems.append(_entrada(comp, a))
    elems.append(PageBreak())
    return elems


def _anexo(comp: _Compositor) -> List:
    """Glosario. Los textos salen de `app.js` vía `glosario.py`."""
    elems: List = [comp.p("Anexo · cómo leer este informe", "seccion"),
                   FileteAcento(), Spacer(1, 2 * mm)]

    fallo = glosario.error_textos()
    if fallo:
        # Degradación visible: antes que inventar frases, decir que faltan.
        elems.append(comp.p(
            "No se pudieron leer las definiciones del glosario desde la "
            f"interfaz ({fallo}). Los términos aparecen en el informe sin su "
            "explicación; consúltalos en el dashboard.", "aviso"))
        return elems

    elems.append(comp.p("Confianza: ¿es mía esta cuenta?", "grupo"))
    elems.append(comp.p(
        "Mide cuánta evidencia hay de que la cuenta sea tuya. Ninguna de las "
        "señales comprueba nada en el sitio: la decisión sigue siendo tuya.",
        "meta"))
    for nivel in glosario.ORDEN_CONFIANZA:
        elems.append(comp.p(glosario.etiqueta_confianza(nivel),
                            "glosario_term"))
        elems.append(comp.p(glosario.explicacion_confianza(nivel), "cuerpo"))

    elems.append(comp.p("Verificabilidad: ¿sirve la respuesta del sitio?",
                        "grupo"))
    elems.append(comp.p(
        "Eje SEPARADO de la confianza. Habla del SITIO, no de tu cuenta: que "
        "un sitio no sepa distinguir usuarios inexistentes no dice nada sobre "
        "si la cuenta es tuya. Cuando no aparece un veredicto es que el "
        "canario no llegó a mirar ese sitio, que no es lo mismo que mirarlo y "
        "no poder concluir.", "meta"))
    for code in glosario.MOTIVOS_CANARIO:
        etiqueta = glosario.etiqueta_motivo(code)
        texto = glosario.explicacion_motivo(code)
        if not texto:
            continue
        elems.append(comp.p(etiqueta or code, "glosario_term"))
        elems.append(comp.p(texto, "cuerpo"))

    elems.append(comp.p("Señales de confianza", "grupo"))
    elems.append(comp.p(
        "Cada cuenta lista las señales que la llevaron a su tramo. Estas son "
        "todas las que Rastrillo sabe registrar.", "meta"))
    ya = set(glosario.MOTIVOS_CANARIO)
    textos = glosario.cargar_textos().get("REASON_TIP", {})
    for code in sorted(textos):
        if code in ya:
            continue
        elems.append(comp.p(glosario.etiqueta_motivo(code) or code,
                            "glosario_term"))
        elems.append(comp.p(textos[code], "cuerpo"))
    return elems


def _colofon(comp: _Compositor) -> List:
    """Con qué se compuso y qué no se pudo representar.

    Va al final a propósito: para entonces el contador ya ha visto todo el
    texto del documento.
    """
    elems: List = [Spacer(1, 8 * mm)]
    lineas = [f"Compuesto con {comp.familia.origen}."]
    if comp.no_representables:
        lineas.append(
            f"{comp.no_representables} carácter"
            f"{'es' if comp.no_representables != 1 else ''} del informe no "
            "tiene glifo en esa fuente y aparece como su código Unicode "
            "(por ejemplo [U+0412]). Instala una fuente con cobertura "
            "cirílica —DejaVu Sans, Noto Sans o Liberation Sans— y vuelve a "
            "generar el informe para verlos.")
    else:
        lineas.append("Todos los caracteres del informe se pudieron "
                      "representar con esa fuente.")
    elems.append(comp.p(" ".join(lineas), "nota"))
    return elems


# ── Entrada pública ─────────────────────────────────────────────────────────
def render_pdf(
    accounts: List[dict],
    summary: Dict,
    audit_summary: Optional[Dict[str, int]] = None,
    generated_at: Optional[float] = None,
) -> bytes:
    """Devuelve los bytes del PDF. Caller controla descarga / disposition.

    Firma intacta desde el Paso 4: `reports.build_report` la llama igual y el
    contrato del subcomando `report` no cambia.
    """
    generated_at = generated_at or _t.time()
    ts_iso = _t.strftime("%Y-%m-%d %H:%M UTC", _t.gmtime(generated_at))
    accounts = list(accounts or [])
    summary = summary or {}

    familia = pdf_fuentes.elegir_familia()
    comp = _Compositor(familia)

    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=A4,
        leftMargin=MARGEN_LATERAL, rightMargin=MARGEN_LATERAL,
        topMargin=MARGEN_SUPERIOR, bottomMargin=MARGEN_INFERIOR,
        title=f"Rastrillo · informe {ts_iso}",
        author="Rastrillo",
        subject="Informe de cuentas detectadas y su estado de baja",
    )

    elems: List = []
    elems += _portada(comp, accounts, summary, ts_iso)
    elems += _resumen(comp, accounts, summary, audit_summary)
    elems += _detalle(comp, accounts)
    elems += _anexo(comp)
    elems += _colofon(comp)

    doc.build(elems, canvasmaker=_hacer_canvas(
        "Informe de cuentas · Rastrillo", ts_iso, familia))
    return buf.getvalue()
