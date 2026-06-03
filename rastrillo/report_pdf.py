"""Generación del informe en PDF (reportlab).

Mantiene la misma estructura conceptual que el JSON/CSV: cabecera con
fecha, resumen agregado (counts por estado, fuentes, auditoría) y tabla
detallada de cuentas. El cuerpo de los correos GDPR NO se incluye (puede
contener PII); sí destinatario y asunto.

Diseño:
- A4 vertical con márgenes generosos.
- `SimpleDocTemplate` paginado automáticamente.
- Tabla con `repeatRows=1` para que la cabecera salga en cada página.
- Tonos de estado coordinados con la paleta del dashboard (más sobrios
  para impresión: pasteles desaturados).
"""
from __future__ import annotations
import io
import time as _t
from typing import Dict, List, Optional

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import mm
from reportlab.platypus import (
    SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer, PageBreak,
)


_STATUS_COLOR = {
    "deleted":       colors.HexColor("#e6f7ef"),
    "anonymized":    colors.HexColor("#eef0fc"),
    "user_done":     colors.HexColor("#e6f7ef"),
    "semi_auto":     colors.HexColor("#eef0fc"),
    "email_draft":   colors.HexColor("#eef0fc"),
    "awaiting_user": colors.HexColor("#fff4e0"),
    "manual":        colors.HexColor("#fff4e0"),
    "failed":        colors.HexColor("#fdecec"),
    "skipped":       colors.HexColor("#f1f1f1"),
    "not_mine":      colors.HexColor("#f1f1f1"),
    "queued":        colors.HexColor("#ecf1fc"),
    "in_progress":   colors.HexColor("#fff4e0"),
    "found":         colors.HexColor("#fafafa"),
    "dry_run":       colors.HexColor("#eef0fc"),
}


def _short(s, n: int = 38) -> str:
    if not s:
        return ""
    s = str(s)
    return s if len(s) <= n else s[: n - 1] + "…"


def render_pdf(
    accounts: List[dict],
    summary: Dict,
    audit_summary: Optional[Dict[str, int]] = None,
    generated_at: Optional[float] = None,
) -> bytes:
    """Devuelve los bytes del PDF. Caller controla descarga / disposition."""
    generated_at = generated_at or _t.time()
    ts_iso = _t.strftime("%Y-%m-%d %H:%M UTC", _t.gmtime(generated_at))

    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=A4,
        leftMargin=18 * mm, rightMargin=18 * mm,
        topMargin=18 * mm, bottomMargin=18 * mm,
        title=f"Rastrillo · informe {ts_iso}",
        author="Rastrillo",
    )

    styles = getSampleStyleSheet()
    h1 = ParagraphStyle("h1", parent=styles["Heading1"],
                        fontSize=18, leading=22, spaceAfter=4,
                        textColor=colors.HexColor("#0a0a0a"))
    sub = ParagraphStyle("sub", parent=styles["Normal"],
                         fontSize=10, leading=12, textColor=colors.HexColor("#6f6f6f"),
                         spaceAfter=14)
    h2 = ParagraphStyle("h2", parent=styles["Heading2"],
                        fontSize=12, leading=14, spaceBefore=10, spaceAfter=6,
                        textColor=colors.HexColor("#0a0a0a"))
    body = ParagraphStyle("body", parent=styles["Normal"],
                          fontSize=9.5, leading=12.5)
    foot = ParagraphStyle("foot", parent=styles["Normal"],
                          fontSize=8, leading=10, alignment=1,
                          textColor=colors.HexColor("#9b9b9b"))

    elems = []
    elems.append(Paragraph("Informe Rastrillo", h1))
    elems.append(Paragraph(f"Generado el {ts_iso}", sub))

    # ─── Resumen ────────────────────────────────────────────────────────
    elems.append(Paragraph("Resumen", h2))
    rows = [
        ["Total de cuentas",       str(summary.get("total", 0))],
        ["Eliminadas",             str(summary.get("by_status", {}).get("deleted", 0))],
        ["Anonimizadas",           str(summary.get("by_status", {}).get("anonymized", 0))],
        ["Tramitadas por el usuario", str(summary.get("by_status", {}).get("user_done", 0))],
        ["Esperando tu acción",    str(
            (summary.get("by_status", {}).get("semi_auto", 0)) +
            (summary.get("by_status", {}).get("email_draft", 0)) +
            (summary.get("by_status", {}).get("awaiting_user", 0)))],
        ["Conservadas",            str(summary.get("by_status", {}).get("skipped", 0))],
        ["Descartadas (triage)",   str(summary.get("by_status", {}).get("not_mine", 0))],
        ["Errores",                str(summary.get("by_status", {}).get("failed", 0))],
        ["Acciones en audit log",  str(summary.get("audit_total", 0))],
    ]
    t_summary = Table(rows, colWidths=[75 * mm, 30 * mm], hAlign="LEFT")
    t_summary.setStyle(TableStyle([
        ("FONTNAME", (0, 0), (-1, -1), "Helvetica"),
        ("FONTSIZE", (0, 0), (-1, -1), 9),
        ("TEXTCOLOR", (0, 0), (0, -1), colors.HexColor("#525252")),
        ("TEXTCOLOR", (1, 0), (1, -1), colors.HexColor("#0a0a0a")),
        ("ALIGN", (1, 0), (1, -1), "RIGHT"),
        ("LINEBELOW", (0, 0), (-1, -2), 0.25, colors.HexColor("#ececec")),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
        ("RIGHTPADDING", (0, 0), (-1, -1), 6),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]))
    elems.append(t_summary)

    # ─── Audit ──────────────────────────────────────────────────────────
    if audit_summary:
        elems.append(Spacer(1, 8))
        elems.append(Paragraph("Acciones registradas en audit.json", h2))
        a_rows = [["Acción", "Veces"]]
        for k in sorted(audit_summary):
            a_rows.append([k, str(audit_summary[k])])
        t_audit = Table(a_rows, colWidths=[75 * mm, 30 * mm], hAlign="LEFT")
        t_audit.setStyle(TableStyle([
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#f4f4f4")),
            ("FONTNAME", (0, 1), (-1, -1), "Helvetica"),
            ("FONTSIZE", (0, 0), (-1, -1), 9),
            ("ALIGN", (1, 0), (1, -1), "RIGHT"),
            ("LINEBELOW", (0, 0), (-1, -1), 0.25, colors.HexColor("#ececec")),
            ("LEFTPADDING", (0, 0), (-1, -1), 6),
            ("RIGHTPADDING", (0, 0), (-1, -1), 6),
            ("TOPPADDING", (0, 0), (-1, -1), 4),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ]))
        elems.append(t_audit)

    # ─── Cuentas ────────────────────────────────────────────────────────
    elems.append(Spacer(1, 12))
    elems.append(Paragraph("Cuentas detectadas", h2))
    if not accounts:
        elems.append(Paragraph(
            "<i>Sin cuentas en la sesión.</i>", body))
    else:
        header = ["Plataforma", "Identificador", "Fuente", "Estado",
                  "Conf.", "Mía", "Días"]
        col_widths = [
            38 * mm,   # Plataforma
            48 * mm,   # Identificador
            16 * mm,   # Fuente
            26 * mm,   # Estado
            14 * mm,   # Conf
            12 * mm,   # Mía
            16 * mm,   # Días
        ]
        rows = [header]
        cell_styles = []
        for i, a in enumerate(accounts, start=1):
            plat = _short(a.get("display_name") or a.get("platform") or "?", 26)
            ident = _short(a.get("identifier") or "", 32)
            src = a.get("source") or ""
            status = a.get("status") or ""
            conf = (a.get("confidence") or "")[:1].upper()
            owned = "✓" if a.get("owned") else ""
            days = (str(a.get("days_since_sent"))
                    if a.get("days_since_sent") is not None else "")
            rows.append([plat, ident, src, status, conf, owned, days])
            tint = _STATUS_COLOR.get(status)
            if tint:
                cell_styles.append(("BACKGROUND", (3, i), (3, i), tint))

        t_acc = Table(rows, colWidths=col_widths, repeatRows=1, hAlign="LEFT")
        ts = [
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#f4f4f4")),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.HexColor("#0a0a0a")),
            ("FONTNAME", (0, 1), (-1, -1), "Helvetica"),
            ("FONTSIZE", (0, 0), (-1, -1), 8.5),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("ALIGN", (4, 1), (4, -1), "CENTER"),
            ("ALIGN", (5, 1), (5, -1), "CENTER"),
            ("ALIGN", (6, 1), (6, -1), "RIGHT"),
            ("LINEBELOW", (0, 0), (-1, -1), 0.25, colors.HexColor("#ececec")),
            ("LEFTPADDING", (0, 0), (-1, -1), 5),
            ("RIGHTPADDING", (0, 0), (-1, -1), 5),
            ("TOPPADDING", (0, 0), (-1, -1), 3),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ]
        ts.extend(cell_styles)
        t_acc.setStyle(TableStyle(ts))
        elems.append(t_acc)

    # ─── Footer ─────────────────────────────────────────────────────────
    def _on_page(canvas, doc):
        canvas.saveState()
        canvas.setFont("Helvetica", 7.5)
        canvas.setFillColor(colors.HexColor("#9b9b9b"))
        canvas.drawString(18 * mm, 10 * mm,
                          "Rastrillo · informe local · datos solo del usuario")
        canvas.drawRightString(A4[0] - 18 * mm, 10 * mm,
                               f"Página {doc.page}")
        canvas.restoreState()

    doc.build(elems, onFirstPage=_on_page, onLaterPages=_on_page)
    return buf.getvalue()
