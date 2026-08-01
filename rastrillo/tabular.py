"""Capa compartida de las exportaciones tabulares (CSV y XLSX).

Por qué existe este módulo
--------------------------
El Paso 6 arregló el CSV y añadió el XLSX. Son dos formatos con capacidades muy
distintas —uno es texto plano para que lo lea otro programa, el otro puede tener
tipos, anchos y filtros— pero **enseñan exactamente los mismos datos**. Si cada
uno decidiera por su cuenta qué columnas hay, cómo se llaman y cómo se traduce
un `not_mine`, las dos exportaciones divergirían con el primer retoque y el
usuario tendría dos versiones distintas de su propio informe.

Así que aquí vive UNA definición (`COLUMNAS`) y UNA proyección (`proyectar`), y
los dos renderizadores se limitan a pintar lo que salga. Hay un test que exige
que las columnas de la hoja «Cuentas» del XLSX sean idénticas a las del CSV.

Las etiquetas legibles salen SIEMPRE de `glosario.py`, que es de donde salen las
del dashboard y las del anexo del PDF. No se escribe aquí un segundo juego de
traducciones: es la misma razón por la que el anexo del PDF lee los tooltips de
`app.js` en vez de tener su propia copia.

Qué NO decide este módulo
-------------------------
El formato final de cada valor. `proyectar` devuelve valores **tipados de
Python** (`datetime`/`date` para fechas, `int` para números, `str` o `None` para
texto); el CSV los pasa a texto y el XLSX los escribe con su tipo, que es lo que
permite que `pwn_count` sea sumable en Excel y que una fecha se ordene como
fecha. Meter aquí un `strftime` habría obligado al XLSX a re-parsearlo.

Inyección de fórmulas
---------------------
`display_name` y el nombre del sitio vienen de páginas ajenas: son datos que no
controlamos. Si una celda empieza por `=`, `+`, `-` o `@`, Excel y LibreOffice la
interpretan como fórmula al abrir el fichero, así que un nombre de perfil
malicioso se ejecuta en la máquina de quien abre su propio informe.

La DETECCIÓN es única (`empieza_por_formula`), el remedio es el que cada formato
permite:

  - **XLSX**: se fuerza `celda.data_type = "s"`. El valor queda byte-idéntico y
    Excel lo muestra como texto. Cero alteración del dato.
  - **CSV**: no hay tipos, así que algo tiene que cambiar. Se prefija un
    apóstrofo, que es la convención entendida por Excel y LibreOffice, se ve en
    cualquier editor de texto, y el round-trip lo recupera quitando el `'`
    inicial. Nunca se altera en silencio: misma política que `pdf_fuentes.sanear`.

Recortes
--------
Una celda de Excel admite 32.767 caracteres. Medido: openpyxl **no avisa ni
falla** al escribir más — guarda el fichero y es Excel quien lo rechaza al abrir.
Por eso recortamos nosotros, y de forma VISIBLE (`recortar`): un aviso que dice
cuántos caracteres faltan, no un final truncado que parezca el dato entero.
"""
from __future__ import annotations

import json
import re
from datetime import date, datetime, timezone
from typing import Any, Dict, List, NamedTuple, Optional, Sequence

from . import db, glosario

# ── Límites ─────────────────────────────────────────────────────────────────
# Excel admite 32.767 caracteres por celda. Nos quedamos por debajo para que
# quepa el aviso de recorte sin rozar el techo.
MAX_CELDA = 32_000

# Nombres de hoja: 31 caracteres y sin `[ ] : * ? / \`. openpyxl lanza
# ValueError ante los caracteres prohibidos, pero ante un título largo solo
# emite un UserWarning y lo conserva — es decir, no protege.
MAX_HOJA = 31
_PROHIBIDOS_HOJA = re.compile(r"[\[\]:*?/\\]")

# Los caracteres que openpyxl rechaza con IllegalCharacterError. Es exactamente
# su propia expresión: \t, \n y \r NO están y se conservan.
_CONTROL = re.compile(r"[\000-\010]|[\013-\014]|[\016-\037]")

# Prefijos que Excel y LibreOffice interpretan como fórmula.
_PREFIJOS_FORMULA = ("=", "+", "-", "@")

# Lo que puede preceder al prefijo sin desactivarlo: los lectores saltan el
# espacio en blanco antes de decidir si la celda es una fórmula, así que
# `\t=1+1` es tan peligroso como `=1+1`.
_ANTES_DEL_PREFIJO = " \t\r\n\v\f\x00"

SEPARADOR_LISTA = " · "     # el mismo que usa el PDF para las señales


class Columna(NamedTuple):
    """Una columna de la exportación.

    `tipo` decide cómo la escribe cada formato:
      texto  → cadena (pasa por la guarda de fórmulas)
      url    → cadena; en XLSX además es hipervínculo
      fecha  → `datetime` o `date`; en XLSX va con formato de fecha
      entero → `int`; en XLSX va con separador de millares y es sumable
    """
    clave: str
    titulo: str
    tipo: str


# ── La definición ÚNICA de columnas ─────────────────────────────────────────
# Orden pensado para LEERSE, no el orden en que están en la tabla: primero lo
# que identifica la cuenta, luego en qué estado está, luego el detalle, y al
# final el contexto de la brecha (que casi siempre está vacío).
#
# «Perfil detectado» y «Cómo darse de baja» son dos columnas distintas y
# etiquetadas a propósito: es el mismo error que se arregló en la UI y en el
# PDF, y no vuelve por aquí.
COLUMNAS: Sequence[Columna] = (
    # Identificación
    Columna("id",             "id",                          "entero"),
    Columna("sitio",          "Sitio",                       "texto"),
    Columna("dominio",        "Dominio",                     "texto"),
    Columna("plataforma",     "Plataforma",                  "texto"),
    Columna("identificador",  "Identificador",               "texto"),
    Columna("detectado_por",  "Detectado por",               "texto"),
    # Estado
    Columna("estado",         "Estado",                      "texto"),
    Columna("confianza",      "Confianza",                   "texto"),
    Columna("senales",        "Señales",                     "texto"),
    Columna("verificabilidad", "Verificabilidad",            "texto"),
    Columna("es_mia",         "Confirmada como mía",         "texto"),
    # Detalle
    Columna("perfil_url",     "Perfil detectado",            "url"),
    Columna("baja_url",       "Cómo darse de baja",          "url"),
    Columna("tipo_baja",      "Tipo de baja",                "texto"),
    Columna("dificultad",     "Dificultad",                  "texto"),
    Columna("capa",           "Capa que lo resolvió",        "texto"),
    Columna("via",            "Vía de baja",                 "texto"),
    Columna("solicitud_a",    "Solicitud enviada a",         "texto"),
    Columna("solicitud_asunto", "Asunto de la solicitud",    "texto"),
    Columna("enviada_el",     "Solicitud enviada el",        "fecha"),
    Columna("dias_desde_envio", "Días desde el envío",       "entero"),
    Columna("plazo_inicio",   "Plazo iniciado el",           "fecha"),
    Columna("plazo_limite",   "Fecha límite de eliminación", "fecha"),
    Columna("actualizada_el", "Última actualización",        "fecha"),
    Columna("nota",           "Última nota",                 "texto"),
    # Contexto de la brecha (HIBP). Columnas propias, nunca el JSON en una celda.
    Columna("brecha_fecha",   "Brecha: fecha",               "fecha"),
    Columna("brecha_cuentas", "Brecha: cuentas expuestas",   "entero"),
    Columna("brecha_datos",   "Brecha: datos expuestos",     "texto"),
    Columna("brecha_avisos",  "Brecha: advertencias",        "texto"),
)

TITULOS: Sequence[str] = tuple(c.titulo for c in COLUMNAS)


class Incidencias:
    """Cuenta lo que la exportación tuvo que tocar.

    No es decorativo: el XLSX lo imprime en la hoja de resumen. Un recorte o una
    neutralización que nadie cuenta es una alteración silenciosa del dato, que
    es justo lo que este proyecto no hace (ver `pdf_fuentes.sanear`).
    """

    def __init__(self) -> None:
        self.recortados = 0
        self.neutralizados = 0

    def __repr__(self) -> str:      # pragma: no cover — solo para depurar
        return (f"<Incidencias recortados={self.recortados} "
                f"neutralizados={self.neutralizados}>")


# ── Saneado y guardas ───────────────────────────────────────────────────────
def limpiar_control(texto: str) -> str:
    """Quita los caracteres de control que openpyxl rechaza.

    `\\t`, `\\n` y `\\r` se conservan: son legítimos dentro de una celda. Ojo
    con `\\r`, que Excel normaliza a `\\n` al guardar (medido); no lo
    "arreglamos" porque el dato sigue siendo el mismo salto de línea.
    """
    return _CONTROL.sub("", texto)


def empieza_por_formula(valor: Any) -> bool:
    """¿Excel interpretaría esta celda como una fórmula?

    Mira el primer carácter significativo, saltando el espacio en blanco y los
    nulos iniciales: los lectores los ignoran antes de decidir, así que
    `"\\t=1+1"` y `"\\r@SUM(A1)"` son tan peligrosos como sin el prefijo.
    """
    if not isinstance(valor, str):
        return False          # los tipados (fecha, entero) nunca son fórmula
    limpio = valor.lstrip(_ANTES_DEL_PREFIJO)
    return limpio.startswith(_PREFIJOS_FORMULA)


def neutralizar_csv(valor: str, incidencias: Optional[Incidencias] = None) -> str:
    """Prefija un apóstrofo si la celda pasaría por fórmula.

    En CSV no hay tipos de celda, así que el dato TIENE que cambiar. El
    apóstrofo es la convención que entienden Excel y LibreOffice, se ve en
    cualquier editor, y `valor.lstrip("'")` devuelve el original: el round-trip
    recupera el dato reconocible.
    """
    if not empieza_por_formula(valor):
        return valor
    if incidencias is not None:
        incidencias.neutralizados += 1
    return "'" + valor


def recortar(valor: Any, limite: int = MAX_CELDA,
             incidencias: Optional[Incidencias] = None) -> str:
    """Recorta a `limite` caracteres dejando dicho cuántos faltan.

    Se ve que está recortado, que es el requisito: un final truncado a secas
    parece el dato entero. El aviso entra DENTRO del límite, así que el
    resultado nunca lo supera.
    """
    s = str(valor)
    if len(s) <= limite:
        return s
    # El tamaño del aviso depende del número de omitidos, y el número de
    # omitidos del tamaño del aviso. Converge en dos vueltas; iteramos por si
    # acaso y nos quedamos con el último corte estable.
    corte = limite
    for _ in range(4):
        aviso = f"…[recortado, {len(s) - corte} caracteres omitidos]"
        nuevo = max(0, limite - len(aviso))
        if nuevo == corte:
            break
        corte = nuevo
    aviso = f"…[recortado, {len(s) - corte} caracteres omitidos]"
    if incidencias is not None:
        incidencias.recortados += 1
    return s[:corte] + aviso


def nombre_hoja(bruto: Any, defecto: str = "Hoja") -> str:
    """Nombre de hoja válido para Excel.

    openpyxl lanza `ValueError` ante `[ ] : * ? / \\`, pero ante un título de
    más de 31 caracteres solo emite un `UserWarning` y lo conserva — es decir,
    te deja generar un fichero que Excel abrirá mal. Saneamos nosotros.

    Existe aunque hoy las tres hojas tengan nombre fijo: en cuanto alguien
    derive un nombre de un estado o de un sitio (una hoja por estado, por
    ejemplo) lo va a necesitar, y sin esto el fallo es silencioso.
    """
    s = limpiar_control(str(bruto or "")).replace("\n", " ").replace("\r", " ")
    s = _PROHIBIDOS_HOJA.sub("", s)
    s = re.sub(r"\s+", " ", s).strip().strip("'")
    if not s:
        return defecto
    if len(s) > MAX_HOJA:
        s = s[:MAX_HOJA - 1].rstrip() + "…"
    return s


# ── Conversiones ────────────────────────────────────────────────────────────
def _fecha(ts: Any) -> Optional[datetime]:
    """Timestamp UNIX → `datetime` naive en UTC.

    Naive y en UTC a propósito: Excel no tiene zonas horarias, y el resto del
    proyecto (nombre del fichero, `generated_at_iso` del JSON) ya habla en UTC.
    Mezclar la hora local aquí haría que dos máquinas exportaran fechas
    distintas para la misma fila.
    """
    if ts in (None, ""):
        return None
    try:
        return datetime.fromtimestamp(float(ts), tz=timezone.utc).replace(tzinfo=None)
    except (TypeError, ValueError, OSError, OverflowError):
        return None


def _fecha_iso(valor: Any) -> Optional[date]:
    """`"YYYY-MM-DD"` (lo que manda HIBP) → `date`. None si no parsea."""
    if not valor:
        return None
    try:
        return datetime.strptime(str(valor)[:10], "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return None


def _entero(valor: Any) -> Optional[int]:
    """A `int` de verdad, para que Excel lo pueda sumar. None si no es número."""
    if valor is None or valor == "":
        return None
    try:
        return int(valor)
    except (TypeError, ValueError):
        return None


def _texto(valor: Any) -> Optional[str]:
    """Texto limpio, o None. `None` nunca se imprime: vacío es vacío."""
    if valor is None:
        return None
    s = limpiar_control(str(valor)).strip()
    return s or None


def _breach(raw: Any) -> Dict[str, Any]:
    """Detalle de la brecha desde `breach_meta`. Tolerante como `parse_reasons`."""
    if not raw:
        return {}
    try:
        meta = json.loads(raw)
    except (ValueError, TypeError):
        return {}
    return meta if isinstance(meta, dict) else {}


def _senales(raw: Any) -> Optional[str]:
    """`confidence_reasons` → etiquetas legibles, nunca el JSON.

    Mismo camino que el PDF: `db.parse_reasons` (que ya tolera basura) y
    `glosario.etiqueta_motivo`, que es de donde salen los chips del dashboard.
    """
    etiquetas = []
    for m in db.parse_reasons(raw):
        if isinstance(m, dict) and m.get("code"):
            e = glosario.etiqueta_motivo(m["code"])
            if e:
                etiquetas.append(e)
    return SEPARADOR_LISTA.join(etiquetas) or None


def _avisos_brecha(meta: Dict[str, Any]) -> Optional[str]:
    """Las banderas de HIBP en palabras. Son contexto, no una señal."""
    avisos = []
    if meta.get("is_spam_list"):
        avisos.append("lista de spam")
    if meta.get("is_fabricated"):
        avisos.append("posiblemente fabricada")
    if meta.get("is_verified") is False:
        avisos.append("sin verificar")
    return SEPARADOR_LISTA.join(avisos) or None


# ── Proyección ──────────────────────────────────────────────────────────────
def proyectar(cuentas: Sequence[dict],
              incidencias: Optional[Incidencias] = None) -> List[Dict[str, Any]]:
    """Filas de la exportación: valores tipados, traducidos y ya recortados.

    Entra lo que devuelve `reports._enrich_rows()` y sale una lista de dicts
    con las claves de `COLUMNAS`. Los dos formatos consumen esto y nada más.
    """
    # Import local: `reports` importa este módulo, así que a nivel de módulo
    # sería un ciclo. `deletion_url` es una función pura y ya resuelve el caso
    # de que la URL de baja coincida con la del perfil (entonces devuelve None,
    # para no pintar el mismo enlace en las dos columnas).
    from .reports import deletion_url

    filas: List[Dict[str, Any]] = []
    for a in cuentas:
        brecha = _breach(a.get("breach_meta"))
        clases = [glosario.etiqueta_clase_datos(c)
                  for c in (brecha.get("data_classes") or [])
                  if c]
        fila = {
            "id":             _entero(a.get("id")),
            "sitio":          _texto(a.get("display_name") or a.get("source_site")
                                     or a.get("platform")),
            "dominio":        _texto(a.get("source_site")),
            "plataforma":     _texto(a.get("platform")),
            "identificador":  _texto(a.get("identifier")),
            "detectado_por":  _texto(a.get("source")),

            "estado":         glosario.etiqueta_estado(a.get("status")),
            "confianza":      glosario.etiqueta_confianza(a.get("confidence")),
            "senales":        _senales(a.get("confidence_reasons")),
            "verificabilidad": glosario.etiqueta_verificabilidad(
                                   a.get("verifiability")),
            # El título de la columna es «Confirmada como mía», así que un "No"
            # se lee como "no confirmada" y no como "no es tuya" — que es una
            # afirmación que Rastrillo no hace nunca por su cuenta.
            "es_mia":         "Sí" if a.get("owned") else "No",

            "perfil_url":     _texto(a.get("profile_url")),
            "baja_url":       _texto(deletion_url(a.get("action_meta"),
                                                  a.get("profile_url"))),
            "tipo_baja":      glosario.etiqueta_tipo_baja(a.get("deletion_type")),
            "dificultad":     glosario.etiqueta_dificultad(a.get("difficulty")),
            "capa":           _texto(a.get("resolver_layer")),
            "via":            glosario.etiqueta_via(a.get("resolver_kind")),
            "solicitud_a":    _texto(a.get("email_to")),
            "solicitud_asunto": _texto(a.get("email_subject")),
            "enviada_el":     _fecha(a.get("sent_at")),
            "dias_desde_envio": _entero(a.get("days_since_sent")),
            "plazo_inicio":   _fecha(a.get("deletion_started_at")),
            "plazo_limite":   _fecha(a.get("deletion_eta")),
            "actualizada_el": _fecha(a.get("updated_at")),
            "nota":           _texto(a.get("last_message")),

            "brecha_fecha":   _fecha_iso(brecha.get("breach_date")),
            "brecha_cuentas": _entero(brecha.get("pwn_count")),
            "brecha_datos":   SEPARADOR_LISTA.join(clases) or None,
            "brecha_avisos":  _avisos_brecha(brecha),
        }
        # Recorte al final y en un solo sitio: cualquier columna de texto puede
        # traer un valor patológico (una URL de 500+, un borrador GDPR largo).
        for col in COLUMNAS:
            if col.tipo in ("texto", "url") and isinstance(fila[col.clave], str):
                fila[col.clave] = recortar(fila[col.clave],
                                           incidencias=incidencias)
        filas.append(fila)
    return filas
