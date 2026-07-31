"""Textos compartidos: etiquetas de estado, de confianza y de verificabilidad.

Por qué existe este módulo
--------------------------
El informe PDF (Paso 4) tiene un anexo que explica qué significan los tramos de
confianza y los veredictos del canario. Esos mismos textos ya existían: son los
tooltips del dashboard, escritos con cuidado en el Paso 3 para decir qué NO
significa cada señal (`low` es "evidencia débil de que sea tuya", no "no es
tuya"). Reescribirlos aquí habría creado dos redacciones que se desvían con el
primer retoque, y de las dos la del papel es la que el usuario archiva.

Así que el anexo **lee los textos de `static/app.js`**, que es donde viven. No
son una copia sincronizada por un test: son literalmente la misma cadena, leída
en tiempo de render. Si alguien retoca un tooltip, el PDF siguiente lo dice
igual sin que nadie tenga que acordarse de nada.

El parseo es deliberadamente tonto (contar llaves, juntar literales separados
por `+`), como el de `tests/test_ui_explicita.py`: `app.js` está escrito a mano
y esos objetos son literales planos. Si algún día dejan de serlo, el fallo es
visible — `cargar_textos()` devuelve un glosario vacío y el PDF imprime que no
pudo leerlo, en vez de inventarse las frases.

`STATUS_META` y `VERIFIABILITY_META` sí viven AQUÍ (antes estaban en
`server.py`). Se movieron porque el PDF los necesita y no puede importar
`server`: eso arrastraría FastAPI y, por `jobs`, la cadena de Playwright, para
generar un fichero. `server` los reexporta con su nombre de siempre.
"""
from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Dict, List, Tuple

_log = logging.getLogger("rastrillo.glosario")

_APP_JS = Path(__file__).parent / "static" / "app.js"


# ── Metadatos de estado ─────────────────────────────────────────────────────
# (etiqueta legible, color). El color es para la UI web; el PDF usa SOLO la
# etiqueta — en papel ningún dato puede depender del color (ver report_pdf).
STATUS_META: Dict[str, Tuple[str, str]] = {
    "found":         ("Pendiente",        "#888780"),
    "queued":        ("En cola",          "#185FA5"),
    "in_progress":   ("En proceso",       "#BA7517"),
    "awaiting_user": ("Esperándote",      "#D85A30"),
    "deleted":       ("Eliminada",        "#0F6E56"),
    "anonymized":    ("Anonimizada",      "#534AB7"),
    "user_done":     ("Tramitada",        "#0F6E56"),
    "semi_auto":     ("Acción: 1 clic",   "#0F6E56"),
    "email_draft":   ("Solicitud correo", "#534AB7"),
    "pending_deletion": ("Plazo de eliminación", "#BA7517"),
    "manual":        ("Revisar",          "#854F0B"),
    "skipped":       ("Conservada",       "#5F5E5A"),
    "failed":        ("Error",            "#A32D2D"),
    "not_mine":      ("Descartada",       "#5F5E5A"),
    "dry_run":       ("Simulada",         "#534AB7"),
}

# Verificabilidad (Paso 2C, Entrega 1): eje SEPARADO de la confianza. Dice si
# la respuesta del sitio sirve para comprobar algo, nunca si la cuenta es del
# usuario. La ausencia de entrada (verifiability NULL) significa "no evaluado"
# y la UI no pinta nada: no es un juicio, es que el canario no ha mirado.
VERIFIABILITY_META: Dict[str, Tuple[str, str]] = {
    "indiscriminado": ("No verificable",  "#854F0B"),
    "discrimina":     ("Verificable",     "#0F6E56"),
    "indeterminado":  ("Sin veredicto",   "#5F5E5A"),
}

# Orden en que se presentan los tramos y los veredictos en el anexo. Explícito
# para que no dependa del orden de inserción de un dict parseado.
ORDEN_CONFIANZA = ("high", "medium", "low")
ORDEN_VERIFICABILIDAD = ("discrimina", "indiscriminado", "indeterminado")

# Objetos de `app.js` que componen el glosario del anexo.
_OBJETOS = ("CONF_LABEL", "CONF_TIP", "REASON_LABEL", "REASON_TIP")


# ── Parseo de los literales de app.js ───────────────────────────────────────
# Un literal de cadena JS con comillas dobles, respetando escapes.
_RE_CADENA = re.compile(r'"((?:[^"\\]|\\.)*)"')

_ESCAPES = {
    "n": "\n", "t": "\t", "r": "\r",
    '"': '"', "'": "'", "\\": "\\", "/": "/",
}


def _desescapar(bruto: str) -> str:
    """Aplica los escapes de una cadena JS. `\\uXXXX` incluido."""
    out: List[str] = []
    i = 0
    while i < len(bruto):
        c = bruto[i]
        if c != "\\" or i + 1 >= len(bruto):
            out.append(c)
            i += 1
            continue
        sig = bruto[i + 1]
        if sig == "u" and i + 5 < len(bruto) + 1:
            try:
                out.append(chr(int(bruto[i + 2:i + 6], 16)))
                i += 6
                continue
            except ValueError:
                pass
        out.append(_ESCAPES.get(sig, sig))
        i += 2
    return "".join(out)


def _cuerpo_del_objeto(texto: str, nombre: str) -> str:
    """Contenido entre las llaves de `const NOMBRE={...}`.

    Lanza `ValueError` si no está o no cierra: el caller lo trata como
    "glosario no disponible" y el PDF lo dice.
    """
    ancla = f"const {nombre}={{"
    inicio = texto.find(ancla)
    if inicio < 0:
        raise ValueError(f"no encuentro `{ancla}` en app.js")
    i = texto.index("{", inicio)
    profundidad = 0
    en_cadena = False
    escapado = False
    for j in range(i, len(texto)):
        c = texto[j]
        if en_cadena:
            if escapado:
                escapado = False
            elif c == "\\":
                escapado = True
            elif c == '"':
                en_cadena = False
            continue
        if c == '"':
            en_cadena = True
        elif c == "{":
            profundidad += 1
        elif c == "}":
            profundidad -= 1
            if profundidad == 0:
                return texto[i + 1:j]
    raise ValueError(f"el objeto `{nombre}` no cierra en app.js")


def _trocear_entradas(cuerpo: str) -> List[str]:
    """Parte el cuerpo por las comas de nivel 0 (fuera de cadenas y llaves)."""
    partes: List[str] = []
    actual: List[str] = []
    profundidad = 0
    en_cadena = False
    escapado = False
    for c in cuerpo:
        if en_cadena:
            actual.append(c)
            if escapado:
                escapado = False
            elif c == "\\":
                escapado = True
            elif c == '"':
                en_cadena = False
            continue
        if c == '"':
            en_cadena = True
            actual.append(c)
        elif c in "{[(":
            profundidad += 1
            actual.append(c)
        elif c in "}])":
            profundidad -= 1
            actual.append(c)
        elif c == "," and profundidad == 0:
            partes.append("".join(actual))
            actual = []
        else:
            actual.append(c)
    partes.append("".join(actual))
    return [p for p in partes if p.strip()]


def _parsear_objeto(texto: str, nombre: str) -> Dict[str, str]:
    """`const NOMBRE={clave:"a" +"b", ...}` → `{"clave": "ab"}`.

    Los valores de estos cuatro objetos son siempre concatenaciones de
    literales de cadena, así que juntar todos los literales que aparecen en el
    valor es exacto (no aproximado) para la forma que tienen hoy.
    """
    fuera: Dict[str, str] = {}
    for entrada in _trocear_entradas(_cuerpo_del_objeto(texto, nombre)):
        # La clave es lo que hay antes del primer `:` fuera de cadena. Como las
        # claves son identificadores planos, basta con un regex anclado.
        m = re.match(r'\s*([A-Za-z_$][\w$]*)\s*:', entrada)
        if not m:
            continue
        valor = entrada[m.end():]
        trozos = _RE_CADENA.findall(valor)
        if not trozos:
            continue
        fuera[m.group(1)] = "".join(_desescapar(t) for t in trozos)
    return fuera


# ── API pública ─────────────────────────────────────────────────────────────
_cache: Dict[str, Dict[str, str]] | None = None
_error: str | None = None


def cargar_textos(force: bool = False) -> Dict[str, Dict[str, str]]:
    """Glosario leído de `app.js`, cacheado.

    Devuelve `{"CONF_TIP": {...}, ...}`. Si `app.js` no se puede leer o
    parsear devuelve `{}` y deja el motivo en `error_textos()`: el anexo lo
    imprime en vez de callarse o de inventar frases.
    """
    global _cache, _error
    if _cache is not None and not force:
        return _cache
    try:
        fuente = _APP_JS.read_text(encoding="utf-8")
        datos = {n: _parsear_objeto(fuente, n) for n in _OBJETOS}
        vacios = [n for n, d in datos.items() if not d]
        if vacios:
            raise ValueError(f"objetos vacíos tras parsear: {', '.join(vacios)}")
        _cache, _error = datos, None
    except Exception as e:  # noqa: BLE001 — degradación visible, no silenciosa
        _log.warning("no pude leer el glosario de app.js: %s", e)
        _cache, _error = {}, f"{type(e).__name__}: {e}"
    return _cache


def error_textos() -> str | None:
    """Motivo por el que el glosario no se pudo leer, o None si fue bien."""
    cargar_textos()
    return _error


def _de(objeto: str, clave: str, defecto: str = "") -> str:
    return cargar_textos().get(objeto, {}).get(clave, defecto)


def etiqueta_confianza(nivel: str) -> str:
    """"Confianza alta" / "media" / "baja".

    Sin tramo se dice "Confianza sin evaluar" y no solo "sin evaluar": en la
    línea de metadatos del PDF, al lado de "verificabilidad: no evaluada", un
    "sin evaluar" suelto no deja claro de qué eje habla.
    """
    etiqueta = _de("CONF_LABEL", nivel or "", "")
    if etiqueta:
        return etiqueta
    return f"Confianza {nivel}" if nivel else "Confianza sin evaluar"


def explicacion_confianza(nivel: str) -> str:
    return _de("CONF_TIP", nivel or "")


def etiqueta_motivo(code: str) -> str:
    return _de("REASON_LABEL", code or "", "") or (code or "")


def explicacion_motivo(code: str) -> str:
    return _de("REASON_TIP", code or "")


def etiqueta_estado(status: str) -> str:
    meta = STATUS_META.get(status or "")
    return meta[0] if meta else (status or "sin estado")


def etiqueta_verificabilidad(veredicto) -> str:
    """Etiqueta del veredicto del canario.

    `None` NO es un veredicto: significa que el canario no miró ese sitio. Se
    dice con esas palabras, que es distinto de "sin veredicto"
    (`indeterminado`: lo intentó y no pudo concluir).
    """
    if not veredicto:
        return "no evaluada"
    meta = VERIFIABILITY_META.get(veredicto)
    return meta[0] if meta else str(veredicto)


# Motivos que produce el canario: el anexo los agrupa aparte porque hablan del
# SITIO y no de la cuenta, que es justo lo que la gente confunde.
MOTIVOS_CANARIO = (
    "canario_indiscriminado",
    "canario_discrimina",
    "canario_bloqueado",
    "canario_sin_respuesta",
)
