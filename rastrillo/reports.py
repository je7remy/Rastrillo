"""Generación de informes en JSON / CSV / XLSX / PDF.

Antes la lógica vivía dentro del endpoint `/api/report` en `server.py`;
Tarea 11 la sacó aquí para que tanto el endpoint como el CLI (`rastrillo
report --format ... --out FILE`) compartan el mismo código y produzcan
informes idénticos.

`build_report(fmt)` devuelve:
  - (bytes,      "text/csv; charset=utf-8",            "rastrillo-<ts>.csv")
  - (bytes,      "application/vnd.openxmlformats-...", "rastrillo-<ts>.xlsx")
  - (bytes,      "application/pdf",                    "rastrillo-<ts>.pdf")
  - (dict,       "application/json",                   "rastrillo-<ts>.json")

El caller decide qué hacer con el contenido (devolverlo por HTTP o
escribirlo a un fichero).

Los cuatro formatos NO son cuatro vistas distintas de los datos: `json` es el
volcado crudo, `pdf` es el documento para archivar, y `csv`/`xlsx` comparten
columnas, orden y traducciones porque los dos salen de `tabular.py`. Ver la
cabecera de ese módulo para el porqué.

Reparto de papeles entre csv y xlsx (Paso 6): el **CSV es para procesar** —RFC
4180, coma, sin adornos, que lo lea otro programa— y el **XLSX es para mirar**,
con tipos, anchos, filtros y una hoja de resumen. Intentar que el CSV fuera las
dos cosas es lo que lo tenía ilegible.
"""
from __future__ import annotations
import codecs
import csv as _csv
import io
import json
import time as _t
from datetime import date, datetime
from typing import Any, Dict, List, Optional, Tuple, Union

from . import audit, config, db, tabular

# Formatos que acepta `build_report`. En un solo sitio para que el endpoint, el
# CLI y el mensaje de error digan lo mismo.
FORMATOS = ("json", "csv", "xlsx", "pdf")

MEDIA_XLSX = ("application/vnd.openxmlformats-officedocument"
              ".spreadsheetml.sheet")


def deletion_url(action_meta, profile_url) -> Any:
    """URL de BAJA que guardó el resolver en `action_meta`, o None.

    Vive aquí (y no en `server.py`, de donde se movió en el Paso 4) porque la
    necesitan dos consumidores: el endpoint `/api/accounts` y el informe PDF.
    `report_pdf` no puede importar `server` —arrastraría FastAPI y, vía
    `jobs`, la cadena de Playwright— así que la lógica baja a un módulo que
    ambos pueden importar. `server._deletion_url` sigue existiendo como alias.

    Devuelve None si no hay Resolution, si su JSON está corrupto (misma
    tolerancia que `db.parse_reasons`: mejor sin enlace que una vista rota) o
    si coincide con la del hit, para no pintar el mismo enlace dos veces.
    """
    if not action_meta:
        return None
    try:
        meta = json.loads(action_meta)
    except (ValueError, TypeError):
        return None
    if not isinstance(meta, dict):
        return None
    url = (meta.get("url") or "").strip() or None
    if url and url == (profile_url or "").strip():
        return None
    return url


def _enrich_rows() -> Tuple[List[Dict[str, Any]], float]:
    """Carga todas las cuentas y las anota con días desde el envío de la
    solicitud GDPR (si hay), la capa del resolver y el destinatario/asunto
    del correo (sin el cuerpo, que puede llevar PII)."""
    rows = [dict(r) for r in db.list_accounts()]
    now = _t.time()
    enriched: List[Dict[str, Any]] = []
    for r in rows:
        meta = None
        if r.get("action_meta"):
            try:
                meta = json.loads(r["action_meta"])
            except Exception:
                meta = None
        days_since_sent = None
        if r.get("sent_at"):
            days_since_sent = int((now - float(r["sent_at"])) // 86400)
        enriched.append({
            **r,
            "days_since_sent": days_since_sent,
            "resolver_layer": (meta or {}).get("layer"),
            "resolver_kind":  (meta or {}).get("kind"),
            "email_to":       (meta or {}).get("email_to"),
            "email_subject":  (meta or {}).get("email_subject"),
        })
    return enriched, now


def _summary(enriched: List[Dict[str, Any]]) -> Tuple[Dict[str, Any], Dict[str, int]]:
    """Resumen agregado compartido por JSON y PDF."""
    stats = db.stats()
    audit_entries = audit.read_all()
    by_action: Dict[str, int] = {}
    for a in audit_entries:
        by_action[a["action"]] = by_action.get(a["action"], 0) + 1
    return {
        "total":         len(enriched),
        "by_status":     stats,
        "audit_actions": by_action,
        "audit_total":   len(audit_entries),
    }, by_action


def _celda_csv(valor: Any, incidencias: tabular.Incidencias) -> str:
    """Un valor tipado de `tabular.proyectar` → la cadena que va al CSV.

    El CSV no tiene tipos, así que aquí es donde las fechas se vuelven texto y
    donde se aplica la guarda de fórmulas. `None` sale como cadena vacía: vacío
    es vacío, nunca la palabra "None" en una celda.
    """
    if valor is None:
        return ""
    if isinstance(valor, datetime):
        return valor.strftime("%Y-%m-%d %H:%M")
    if isinstance(valor, date):
        return valor.strftime("%Y-%m-%d")
    if isinstance(valor, bool):          # antes que int: bool ES int en Python
        return "Sí" if valor else "No"
    if isinstance(valor, int):
        return str(valor)
    return tabular.neutralizar_csv(str(valor), incidencias)


def build_csv(enriched: List[Dict[str, Any]],
              sep: Optional[str] = None) -> bytes:
    """El CSV, como CSV: RFC 4180 con BOM.

    Tres decisiones, todas medidas contra el problema real (ver `config.CSV_SEP`):

      - **BOM UTF-8**. Sin él Excel interpreta el fichero en la codificación
        ANSI del sistema y destroza acentos y cirílico. Los lectores estándar
        lo saltan solo (`encoding="utf-8-sig"`).
      - **`\\r\\n`** como terminación de línea, que es lo que manda el estándar.
      - **Coma** por defecto, con `sep` para pedir otra cosa. No se emite la
        línea `sep=;` de Microsoft: añade una fila espuria a cualquier parser
        estándar y el fichero deja de ser un CSV para todo lo que no sea Excel.

    Las columnas y las traducciones salen de `tabular`, así que son las mismas
    que las del XLSX por construcción.
    """
    delim = sep or config.CSV_SEP
    incidencias = tabular.Incidencias()
    filas = tabular.proyectar(enriched, incidencias)

    out = io.StringIO(newline="")
    w = _csv.writer(out, delimiter=delim, quotechar='"',
                    quoting=_csv.QUOTE_MINIMAL, lineterminator="\r\n")
    w.writerow(list(tabular.TITULOS))
    for fila in filas:
        w.writerow([_celda_csv(fila.get(c.clave), incidencias)
                    for c in tabular.COLUMNAS])
    return codecs.BOM_UTF8 + out.getvalue().encode("utf-8")


def build_report(fmt: str, sep: Optional[str] = None
                 ) -> Tuple[Union[bytes, dict], str, str]:
    """Construye el informe en el formato pedido.

    Devuelve `(contenido, media_type, suggested_filename)`. El contenido
    es `bytes` para csv/xlsx/pdf y `dict` para json (más fácil de manipular
    desde el caller; quien lo necesite serializado puede `json.dumps`).

    `sep` solo afecta al CSV; los demás formatos lo ignoran.
    """
    fmt = (fmt or "json").lower()
    if fmt not in FORMATOS:
        raise ValueError(f"formato no soportado: {fmt!r} "
                         f"(usa uno de: {', '.join(FORMATOS)})")

    enriched, now = _enrich_rows()
    ts = _t.strftime("%Y%m%d-%H%M%S", _t.gmtime(now))

    if fmt == "csv":
        return (
            build_csv(enriched, sep),
            "text/csv; charset=utf-8",
            f"rastrillo-{ts}.csv",
        )

    summary, by_action = _summary(enriched)

    if fmt == "xlsx":
        # Import perezoso, como el del PDF: openpyxl solo hace falta para este
        # formato, así que si faltara no puede tumbar a los otros tres.
        try:
            from . import report_xlsx
        except ImportError as e:      # pragma: no cover — depende del entorno
            raise ValueError(
                "el formato xlsx necesita openpyxl y no está instalado "
                f"({e}). Instálalo con `pip install openpyxl` o usa "
                "--format csv") from e
        data = report_xlsx.render_xlsx(
            accounts=enriched,
            summary=summary,
            audit_summary=by_action,
            generated_at=now,
        )
        return data, MEDIA_XLSX, f"rastrillo-{ts}.xlsx"

    if fmt == "pdf":
        from . import report_pdf
        data = report_pdf.render_pdf(
            accounts=enriched,
            summary=summary,
            audit_summary=by_action,
            generated_at=now,
        )
        return data, "application/pdf", f"rastrillo-{ts}.pdf"

    # JSON
    payload = {
        "generated_at": now,
        "generated_at_iso": _t.strftime("%Y-%m-%dT%H:%M:%SZ", _t.gmtime(now)),
        "summary": summary,
        "accounts": enriched,
    }
    return payload, "application/json", f"rastrillo-{ts}.json"
