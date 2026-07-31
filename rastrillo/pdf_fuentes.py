"""Elección de fuente del informe PDF y degradación honesta de Unicode.

El problema
-----------
Rastrillo soporta 6 idiomas, incluido el ruso, y en una DB real hay hosts `.ru`
con borradores GDPR en cirílico. Pero:

  - Las base-14 de PostScript (Helvetica, Times, Courier) van con
    WinAnsiEncoding: cubren Latin-1 y poco más. Cero cirílico.
  - Las TTF que empaqueta reportlab 4.5.1 son la familia Bitstream Vera
    (`Vera.ttf` y compañía): 283 codepoints, Latin-1 completo, 23/128 de Latin
    Extended-A y **0/96 de cirílico**. Medido, no supuesto.

Y reportlab **no avisa**: pintar un carácter sin glifo no lanza ninguna
excepción, simplemente no dibuja nada. Un nombre en ruso desaparecería del
informe en silencio, que es el peor fallo posible en un documento que el
usuario archiva como prueba de lo que hizo.

La solución (sin binarios nuevos en el repo)
--------------------------------------------
Tres escalones, en orden:

  1. **Fuente del sistema.** Se buscan familias conocidas con cobertura
     cirílica en las rutas habituales de Linux, macOS y Windows (DejaVu,
     Liberation, Noto, Arial, Segoe UI…). En el Arch del proyecto DejaVu está
     casi siempre; en Windows, Arial.
  2. **Vera**, la que viene dentro de reportlab. No añade nada al repo ni
     licencia nueva porque ya es parte de una dependencia instalada.
  3. **Helvetica** base-14, si hasta el registro de TTF falla.

En los escalones 2 y 3 el cirílico no se puede representar. Entonces NO se
calla: `sanear()` sustituye cada carácter sin glifo por su codepoint visible
(`[U+0412]`) y lleva la cuenta, y el informe imprime en el colofón con qué
fuente se compuso y cuántos caracteres no pudo representar. Es feo a propósito:
un `[U+0412]` se ve y se puede investigar; un hueco en blanco, no.

Contrapartida asumida (decisión del usuario, Paso 4): el PDF no es
byte-idéntico entre máquinas, porque depende de qué fuente haya instalada.
"""
from __future__ import annotations

import logging
import os
import sys
import unicodedata
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Sequence, Tuple

_log = logging.getLogger("rastrillo.pdf_fuentes")

# Muestra con la que se decide si una familia "sirve": si cubre esto, cubre lo
# que el informe necesita de verdad. Cirílico incluido a propósito — es el
# criterio que separa el escalón 1 del 2.
_MUESTRA_LATINA = "áéíóúüñçàèÁÉÍÑ—…“”·"
_MUESTRA_CIRILICA = "ВКонтактеЁйщ"


@dataclass
class Familia:
    """Los cuatro estilos que usa el documento, ya registrados en reportlab."""

    regular: str
    negrita: str
    cursiva: str
    negrita_cursiva: str
    origen: str                      # de dónde salió, para el colofón
    cubre_cirilico: bool
    _codepoints: Optional[frozenset] = field(default=None, repr=False)

    def cubre(self, ch: str) -> bool:
        """¿Hay glifo para este carácter?

        Con TTF se mira el cmap real. Con las base-14 se aproxima por
        cp1252, que es lo que WinAnsiEncoding puede codificar.
        """
        if self._codepoints is not None:
            return ord(ch) in self._codepoints
        try:
            ch.encode("cp1252")
            return True
        except UnicodeEncodeError:
            return False


# ── Catálogo de familias del sistema ────────────────────────────────────────
# (nombre lógico, regular, negrita, cursiva, negrita+cursiva). Los nombres de
# fichero son los reales de cada distribución/SO.
_CANDIDATAS: Sequence[Tuple[str, str, str, str, str]] = (
    ("DejaVuSans", "DejaVuSans.ttf", "DejaVuSans-Bold.ttf",
     "DejaVuSans-Oblique.ttf", "DejaVuSans-BoldOblique.ttf"),
    ("LiberationSans", "LiberationSans-Regular.ttf", "LiberationSans-Bold.ttf",
     "LiberationSans-Italic.ttf", "LiberationSans-BoldItalic.ttf"),
    ("NotoSans", "NotoSans-Regular.ttf", "NotoSans-Bold.ttf",
     "NotoSans-Italic.ttf", "NotoSans-BoldItalic.ttf"),
    ("FreeSans", "FreeSans.ttf", "FreeSansBold.ttf",
     "FreeSansOblique.ttf", "FreeSansBoldOblique.ttf"),
    ("Arial", "arial.ttf", "arialbd.ttf", "ariali.ttf", "arialbi.ttf"),
    ("Arial", "Arial.ttf", "Arial Bold.ttf", "Arial Italic.ttf",
     "Arial Bold Italic.ttf"),
    ("SegoeUI", "segoeui.ttf", "segoeuib.ttf", "segoeuii.ttf", "segoeuiz.ttf"),
    ("Tahoma", "tahoma.ttf", "tahomabd.ttf", "tahoma.ttf", "tahomabd.ttf"),
)


def _directorios_de_fuentes() -> List[str]:
    """Rutas donde buscar, por SO. Se filtran las que no existen."""
    dirs: List[str] = []
    if sys.platform.startswith("win"):
        raiz = os.environ.get("SystemRoot", r"C:\Windows")
        dirs += [os.path.join(raiz, "Fonts")]
        local = os.environ.get("LOCALAPPDATA")
        if local:
            dirs.append(os.path.join(local, "Microsoft", "Windows", "Fonts"))
    elif sys.platform == "darwin":
        dirs += ["/System/Library/Fonts", "/System/Library/Fonts/Supplemental",
                 "/Library/Fonts", os.path.expanduser("~/Library/Fonts")]
    else:
        dirs += [
            "/usr/share/fonts", "/usr/share/fonts/TTF",
            "/usr/share/fonts/truetype", "/usr/share/fonts/truetype/dejavu",
            "/usr/share/fonts/truetype/liberation",
            "/usr/share/fonts/dejavu", "/usr/share/fonts/liberation",
            "/usr/share/fonts/noto", "/usr/share/fonts/gnu-free",
            "/usr/local/share/fonts", os.path.expanduser("~/.local/share/fonts"),
            os.path.expanduser("~/.fonts"),
        ]
    return [d for d in dirs if os.path.isdir(d)]


def _buscar(nombre_fichero: str, dirs: Sequence[str]) -> Optional[str]:
    """Ruta del fichero en el primer directorio que lo tenga.

    Un nivel de subdirectorios en Linux (`/usr/share/fonts/<familia>/x.ttf`),
    que es como lo reparten casi todas las distribuciones. No se recorre el
    árbol entero: generar un informe no debe costar un `walk` del disco.
    """
    for d in dirs:
        directo = os.path.join(d, nombre_fichero)
        if os.path.isfile(directo):
            return directo
        try:
            subdirs = [e.path for e in os.scandir(d) if e.is_dir()]
        except OSError:
            continue
        for sd in subdirs:
            cand = os.path.join(sd, nombre_fichero)
            if os.path.isfile(cand):
                return cand
    return None


def _codepoints(ruta: str) -> frozenset:
    """Cmap del TTF, leído con el parser del propio reportlab (sin deps)."""
    from reportlab.pdfbase.ttfonts import TTFont
    return frozenset(TTFont("sonda", ruta).face.charToGlyph.keys())


def _registrar_ttf(alias: str, ruta: str) -> bool:
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    try:
        pdfmetrics.registerFont(TTFont(alias, ruta))
        return True
    except Exception as e:  # noqa: BLE001
        _log.debug("no pude registrar %s (%s): %s", alias, ruta, e)
        return False


def _familia_desde_ficheros(nombre: str, rutas: Sequence[Optional[str]],
                            origen: str) -> Optional[Familia]:
    """Registra los cuatro estilos. Los que falten caen al regular.

    Que falte la cursiva no invalida la familia: el documento la usa poco y
    una cursiva sintética sería peor que usar la redonda.
    """
    from reportlab.pdfbase import pdfmetrics

    reg = rutas[0]
    if not reg:
        return None
    try:
        cps = _codepoints(reg)
    except Exception as e:  # noqa: BLE001
        _log.debug("cmap ilegible en %s: %s", reg, e)
        return None

    alias = {}
    for sufijo, ruta in zip(("", "-Bold", "-Italic", "-BoldItalic"), rutas):
        destino = f"RS{nombre}{sufijo}"
        if ruta and _registrar_ttf(destino, ruta):
            alias[sufijo] = destino
        else:
            alias[sufijo] = alias.get("", f"RS{nombre}")
    if "" not in alias:
        return None

    fam = Familia(
        regular=alias[""],
        negrita=alias["-Bold"],
        cursiva=alias["-Italic"],
        negrita_cursiva=alias["-BoldItalic"],
        origen=origen,
        cubre_cirilico=all(ord(c) in cps for c in _MUESTRA_CIRILICA),
        _codepoints=cps,
    )
    try:
        pdfmetrics.registerFontFamily(
            fam.regular, normal=fam.regular, bold=fam.negrita,
            italic=fam.cursiva, boldItalic=fam.negrita_cursiva)
    except Exception:  # noqa: BLE001 — la familia sirve igual sin el alias
        pass
    return fam


def _familia_sistema() -> Optional[Familia]:
    dirs = _directorios_de_fuentes()
    if not dirs:
        return None
    for nombre, reg, bd, it, bi in _CANDIDATAS:
        ruta_reg = _buscar(reg, dirs)
        if not ruta_reg:
            continue
        try:
            if not all(ord(c) in _codepoints(ruta_reg)
                       for c in _MUESTRA_CIRILICA + _MUESTRA_LATINA):
                continue          # existe pero no cubre: sigue buscando
        except Exception:  # noqa: BLE001
            continue
        fam = _familia_desde_ficheros(
            nombre,
            [ruta_reg, _buscar(bd, dirs), _buscar(it, dirs), _buscar(bi, dirs)],
            f"{nombre} (fuente del sistema)")
        if fam:
            return fam
    return None


def _familia_vera() -> Optional[Familia]:
    """Bitstream Vera, empaquetada DENTRO de reportlab. Sin cirílico."""
    try:
        import reportlab
        base = os.path.join(os.path.dirname(reportlab.__file__), "fonts")
    except Exception:  # noqa: BLE001
        return None
    rutas = [os.path.join(base, n) for n in
             ("Vera.ttf", "VeraBd.ttf", "VeraIt.ttf", "VeraBI.ttf")]
    return _familia_desde_ficheros(
        "Vera", [r if os.path.isfile(r) else None for r in rutas],
        "Bitstream Vera (incluida en reportlab)")


def _familia_base14() -> Familia:
    return Familia(
        regular="Helvetica", negrita="Helvetica-Bold",
        cursiva="Helvetica-Oblique", negrita_cursiva="Helvetica-BoldOblique",
        origen="Helvetica base-14 (sin fuente TrueType disponible)",
        cubre_cirilico=False, _codepoints=None,
    )


_familia_cache: Optional[Familia] = None


def elegir_familia(force: bool = False) -> Familia:
    """La mejor familia disponible. Cacheada: registrar TTF no es gratis."""
    global _familia_cache
    if _familia_cache is not None and not force:
        return _familia_cache
    fam = None
    try:
        fam = _familia_sistema()
    except Exception as e:  # noqa: BLE001 — nunca romper por buscar fuentes
        _log.warning("fallo buscando fuentes del sistema: %s", e)
    if fam is None:
        try:
            fam = _familia_vera()
        except Exception as e:  # noqa: BLE001
            _log.warning("fallo registrando Vera: %s", e)
    if fam is None:
        fam = _familia_base14()
    _log.info("PDF: componiendo con %s", fam.origen)
    _familia_cache = fam
    return fam


# ── Saneado del texto ───────────────────────────────────────────────────────
# Equivalencias tipográficas seguras: no son transliteración (no cambian el
# idioma de nada), solo variantes de puntuación que cualquier fuente latina
# tiene. Evitan un `[U+2192]` donde basta una flecha ASCII.
_EQUIVALENTES = {
    "\u2192": "->", "\u2190": "<-", "\u2713": "OK", "\u2717": "x",
    "\u2022": "\u00b7", "\u2043": "-", "\u200b": "", "\u00a0": " ",
    "\ufeff": "", "\u2011": "-", "\u2012": "-", "\u2015": "\u2014",
}


def sanear(texto, familia: Familia) -> Tuple[str, int]:
    """Devuelve `(texto representable, nº de caracteres sustituidos)`.

    Pasos, en orden:
      1. Normaliza a NFC (une acentos combinantes, que muchas fuentes solo
         tienen precompuestos).
      2. Aplica equivalencias tipográficas seguras.
      3. Lo que aún no tenga glifo se sustituye por `[U+XXXX]`, visible.

    Nunca lanza y nunca devuelve None: un informe no se rompe por un nombre
    raro.
    """
    if texto is None:
        return "", 0
    if not isinstance(texto, str):
        texto = str(texto)
    if not texto:
        return "", 0

    texto = unicodedata.normalize("NFC", texto)
    salida: List[str] = []
    sustituidos = 0
    for ch in texto:
        if ch in ("\n", "\t"):
            salida.append(ch)
            continue
        if familia.cubre(ch):
            salida.append(ch)
            continue
        alt = _EQUIVALENTES.get(ch)
        if alt is not None and all(familia.cubre(c) for c in alt):
            salida.append(alt)
            continue
        # Último recurso: decirlo. Un hueco en blanco sería peor.
        salida.append(f"[U+{ord(ch):04X}]")
        sustituidos += 1
    return "".join(salida), sustituidos
