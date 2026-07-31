"""Canario a nivel de sitio: ¿este sitio sabe decir que un usuario NO existe?

Problema que resuelve. Un escaneo real con un username distintivo devolvió 9
cuentas, todas en `high`, y varias no eran cuentas: HudsonRock (que responde a
una consulta de exposición de credenciales, no a un perfil), Periscope (cerrado
desde marzo de 2021), baby.ru. Sherlock las consulta como si fueran sitios con
perfiles y ellas responden 200 a cualquier cosa. La heurística de
`discovery._sherlock_confidence` no puede verlo: mira el username y la URL, no
lo que el sitio contesta.

La idea, y por qué así. **No comparamos el perfil real contra un canario:
comparamos dos canarios entre sí.** Pedimos DOS usernames falsos distintos al
mismo sitio y miramos qué devuelve:

  - ambos 200, cuerpos casi idénticos y SIN marcadores de "no existe" → el
    sitio responde lo mismo para cualquiera; no discrimina, así que ningún hit
    suyo tiene valor probatorio → `indiscriminado`.
  - alguno 4xx, o marcador de "usuario no encontrado" en el cuerpo → el sitio
    sí distingue → `discrimina`, y el hit se respeta.
  - red caída, timeout, no se puede construir la URL → `indeterminado`.

Tres consecuencias de comparar canarios entre sí y no contra el perfil real:

  1. El veredicto NO depende del identificador, así que es del SITIO y se
     cachea (con TTL, patrón de `directory.py`). Escaneos posteriores del
     mismo host cuestan cero peticiones.
  2. Nunca pedimos la URL del perfil real. No le anunciamos a un sitio ajeno
     que alguien está mirando ese username concreto — encaja con el modelo de
     privacidad del proyecto.
  3. El chequeo de marcadores evita el falso veredicto en sitios cuya
     detección de "no existe" es por mensaje y no por status: si el sitio nos
     está diciendo que el falso no existe, es que discrimina bien.

EL INVARIANTE (Paso 2C, Entrega 1): **el canario no modifica `confidence` en
ninguna circunstancia.** Informa, no decide.

Hasta 2B, un `indiscriminado` bajaba la fila a `low`. Eso metía dos conceptos
distintos en la misma variable: `low` significa "evidencia débil de que esta
cuenta sea tuya", mientras que `indiscriminado` significa "la respuesta de este
sitio no vale nada, no puedo verificar ni a favor ni en contra". Como
`discard-low` marca `not_mine`, la cadena quedaba en **inverificable → low →
descartar → "no es mía"**: una afirmación sobre la PROPIEDAD de la cuenta
derivada de una señal que no habla de propiedad. En una herramienta que borra
cuentas eso puede hacerte marcar como ajena una cuenta que sí es tuya (pasó con
Steam y Duolingo, dos sitios donde el dueño sí podía tener cuenta).

Hoy el veredicto vive en su propia columna, `accounts.verifiability`, y
`confidence` vuelve a depender SOLO de `discovery._register` más la
corroboración de 2A.

Reglas de aplicación (`_aplicar_a_fila`):

  - El veredicto se escribe en `verifiability` para TODAS las filas del sitio:
    describe al SITIO, así que es cierto para cualquier fila suya (mismo
    criterio que `corrob_cruzada` en `_corroborar_entre_fuentes`).
  - El motivo se sigue anotando en `confidence_reasons` porque es evidencia
    útil en el triage, pero **no mueve el tramo**.
  - `discard-low` sigue barriendo por `confidence=='low'` y por tanto ya NO
    recoge inverificables. Nada se marca `not_mine` automáticamente.
  - `indeterminado` NO se cachea (se reintenta en el próximo escaneo), pero sí
    se registra en `verifiability`: "lo intenté y no pude concluir" es distinto
    de "no lo he mirado" (que es NULL). Cuando sabemos POR QUÉ no concluimos
    —el sitio nos bloquea (403/429) frente a no hubo respuesta— también se
    anota (Entrega 3): son dos situaciones distintas y el informe tiene que
    poder decir cuál fue. Nos identificamos honestamente y aceptamos el
    rechazo; no cambiamos cabeceras ni intentamos esquivar el bloqueo.

Alcance y red:

  - Solo hits CON `url`. Eso excluye estructuralmente holehe y hibp, que nunca
    la traen: ni una petición para esas dos fuentes.
  - Exactamente 2 peticiones por sitio no cacheado. Ninguna al perfil real.
  - La guarda anti-SSRF es la de `resolver._is_safe_url` (precedente
    cross-module en `engine.py`); el throttle por dominio y el timeout salen
    de `resolver._http_get`, o sea `RASTRILLO_PROBE_DELAY`.
  - Las dos primitivas de red (`http_get`, `url_segura`) son inyectables por
    parámetro, como `_whois_query`/`_dns_query` en `domain_intel.py`: los
    tests no tocan internet.
  - Estilo `discovery.py`: errores visibles, un fallo no aborta el lote.

Sin dependencias nuevas: `difflib` y `random` son stdlib.

API pública:
  generar_tokens(n=2)                        -> [str, ...] usernames falsos
  url_canario(url, identificador, token)     -> str | None
  analizar_sitio(url, identificador, ...)    -> dict(veredicto, evidencia, ...)
  veredicto_de(host, ...)                    -> dict (usa/rellena la caché)
  run_canario(...)                           -> dict resumen del pase completo
"""
from __future__ import annotations
import difflib
import json
import logging
import os
import random
import re
import string
import time
import urllib.parse
from typing import Optional

from . import config, db

log = logging.getLogger("rastrillo.canario")

CACHE_PATH = config.BASE_DIR / "canario.json"

VEREDICTOS = ("indiscriminado", "discrimina", "indeterminado")

# Por encima de esto, dos respuestas normalizadas se consideran "el mismo
# cuerpo". No es 1.0 a propósito: casi cualquier página real varía en un token
# CSRF, un timestamp o un anuncio.
UMBRAL_SIMILITUD = 0.95

# Cuánto cuerpo comparamos. Suficiente para ver la plantilla de la página sin
# que SequenceMatcher (cuadrático en el peor caso) se dispare.
_MAX_CUERPO = 4000

# Edad máxima de un veredicto cacheado. Mismo patrón que
# RASTRILLO_DIR_MAX_AGE_DAYS en `jobs._dir_max_age_seconds`.
_DEF_MAX_AGE_DAYS = 30.0


def _max_age_seconds() -> float:
    try:
        days = float(os.environ.get("RASTRILLO_CANARIO_MAX_AGE_DAYS",
                                    _DEF_MAX_AGE_DAYS))
    except ValueError:
        days = _DEF_MAX_AGE_DAYS
    return max(1.0, days) * 86400.0


def _workers() -> int:
    """Pool para el pase. Reusa RASTRILLO_RESOLVER_WORKERS (misma naturaleza:
    peticiones HTTP cortas) en vez de inventar una var nueva. Duplicamos las
    cuatro líneas de `jobs._resolver_workers` a propósito: importar `jobs`
    desde aquí cerraría el ciclo (jobs → canario → jobs)."""
    try:
        n = int(os.environ.get("RASTRILLO_RESOLVER_WORKERS", "5"))
    except ValueError:
        n = 5
    return max(1, min(n, 16))


# --- Marcadores de "este usuario no existe" ---------------------------------
# Los 6 idiomas que ya soporta el proyecto (en, es, ru, pt-BR, fr, de). Se
# buscan en el cuerpo en minúsculas; basta uno para concluir que el sitio SÍ
# sabe decir que no existe.
#
# La familia "could not be found" (2C, Entrega 2) entró de un escaneo real:
# Steam responde 200 con `<h3>The specified profile could not be found.</h3>` y
# ninguna de las frases anteriores casaba, porque todas asumían "not found"
# contiguo. El sitio SÍ discrimina y lo dábamos por indiscriminado. Se añade en
# los 6 idiomas por simetría con el resto de la tabla.
_MARCADORES_NO_EXISTE = {
    "en": [
        "user not found", "page not found", "profile not found",
        "account not found", "no such user", "user does not exist",
        "doesn't exist", "does not exist", "sorry, this page isn",
        "nobody on", "not found on", "404 not found",
        "could not be found", "couldn't be found",
    ],
    "es": [
        "usuario no encontrado", "no se ha encontrado", "no se encontr",
        "no existe", "página no encontrada", "pagina no encontrada",
        "perfil no encontrado", "usuario no existe",
        "no se ha podido encontrar", "no pudimos encontrar",
    ],
    "ru": [
        "пользователь не найден", "страница не найдена", "не существует",
        "не найден", "ничего не найдено", "не удалось найти",
    ],
    "pt-BR": [
        "usuário não encontrado", "usuario nao encontrado",
        "página não encontrada", "pagina nao encontrada", "não existe",
        "nao existe", "não encontrado", "não foi possível encontrar",
    ],
    "fr": [
        "utilisateur introuvable", "page introuvable", "n'existe pas",
        "compte introuvable", "aucun utilisateur",
        "impossible de trouver",
    ],
    "de": [
        "benutzer nicht gefunden", "seite nicht gefunden", "existiert nicht",
        "nicht gefunden", "kein benutzer", "konnte nicht gefunden werden",
    ],
}


def _marcadores_en(cuerpo: str, extra=None) -> list:
    """Marcadores de "no existe" presentes en el cuerpo. Lista de
    `"idioma:frase"` para que la evidencia sea legible.

    `extra` son los marcadores que declara el catálogo de Sherlock para ESE
    sitio (`catalogo.marcadores`). Van etiquetados como `sherlock:` porque son
    de otra naturaleza: no son una heurística multi-idioma nuestra, es el texto
    exacto contra el que Sherlock decidió que el usuario no existía.
    """
    if not cuerpo:
        return []
    bajo = cuerpo.lower()
    hallados = [f"sherlock:{frase}" for frase in (extra or [])
                if frase and frase.lower() in bajo]
    hallados += [f"{lang}:{frase}"
                 for lang, frases in _MARCADORES_NO_EXISTE.items()
                 for frase in frases if frase in bajo]
    return hallados


# --- Tokens falsos ----------------------------------------------------------
# Plausibles a propósito: longitud de username normal, minúsculas y dígitos,
# sin caracteres raros. Un token que PAREZCA inválido puede ser rechazado por
# formato, y concluiríamos mal que el sitio discrimina.
_ALFABETO_TOKEN = string.ascii_lowercase + string.digits
_LARGO_TOKEN = (9, 13)
_TOKEN_RE = re.compile(r"^[a-z0-9]+$")


def generar_tokens(n: int = 2, rng=None) -> list:
    """`n` usernames falsos distintos, plausibles y sin colisión entre ellos.

    Empiezan por letra porque hay sitios que rechazan un username que abre con
    dígito, y ese rechazo por formato es justo el falso veredicto que
    queremos evitar.
    """
    rnd = rng or random.Random()
    out: list = []
    while len(out) < n:
        largo = rnd.randint(*_LARGO_TOKEN)
        tok = rnd.choice(string.ascii_lowercase) + "".join(
            rnd.choice(_ALFABETO_TOKEN) for _ in range(largo - 1))
        if tok not in out:
            out.append(tok)
    return out


def token_plausible(tok: str) -> bool:
    """Contrato de `generar_tokens`, expuesto para tests y para el subcomando
    de debug (que acepta tokens del usuario)."""
    return bool(tok) and bool(_TOKEN_RE.match(tok)) and 4 <= len(tok) <= 32


# --- Construcción de la URL del canario -------------------------------------
def url_canario(url: str, identificador: str, token: str) -> Optional[str]:
    """Sustituye `identificador` por `token` en la URL del hit.

    Solo toca los sitios donde `discovery._identificador_en_url` concede
    señal: path, query, fragmento y etiqueta más a la izquierda del host. Si
    el identificador no aparece en ninguno, devolvemos None: sin plantilla no
    hay canario, y el sitio queda `indeterminado` (no penalizado).
    """
    ident = (identificador or "").strip().lower()
    if not url or not ident or not token:
        return None
    try:
        parts = urllib.parse.urlsplit(url if "://" in url else "//" + url)
    except ValueError:
        return None

    def _sub(texto):
        # Case-insensitive: el CSV de sherlock a veces normaliza la caja.
        return re.sub(re.escape(ident), token, texto or "", flags=re.IGNORECASE)

    path, query, frag = _sub(parts.path), _sub(parts.query), _sub(parts.fragment)
    netloc = parts.netloc or ""
    host = netloc.split("@")[-1].split(":")[0]
    if host and host.split(".")[0].lower() == ident:
        etiquetas = host.split(".")
        etiquetas[0] = token
        netloc = netloc.replace(host, ".".join(etiquetas), 1)

    cambio = (path != (parts.path or "") or query != (parts.query or "")
              or frag != (parts.fragment or "") or netloc != (parts.netloc or ""))
    if not cambio:
        return None
    return urllib.parse.urlunsplit(
        (parts.scheme or "https", netloc, path, query, frag))


# --- Normalización y similitud ----------------------------------------------
_RE_ESPACIOS = re.compile(r"\s+")
_RE_DIGITOS = re.compile(r"\d+")


def _normalizar(cuerpo: str, tokens) -> str:
    """Deja el cuerpo comparable: minúsculas, sin los tokens falsos (que el
    sitio suele reflejar en el título), sin dígitos (timestamps, contadores,
    IDs de sesión) y con espacios colapsados. Truncado al final para que el
    corte no dependa de basura ya eliminada."""
    s = (cuerpo or "").lower()
    for t in tokens or []:
        if t:
            s = s.replace(str(t).lower(), "")
    s = _RE_DIGITOS.sub("", s)
    return _RE_ESPACIOS.sub(" ", s).strip()[:_MAX_CUERPO]


def similitud(cuerpo_a: str, cuerpo_b: str, tokens=()) -> float:
    """Ratio 0..1 entre dos cuerpos normalizados (`difflib`, stdlib)."""
    a = _normalizar(cuerpo_a, tokens)
    b = _normalizar(cuerpo_b, tokens)
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    return difflib.SequenceMatcher(None, a, b).ratio()


# --- Análisis de UN sitio ---------------------------------------------------
def _http_get_por_defecto(url: str):
    """GET real, en forma DETALLADA `(resultado, motivo)`.

    Import diferido para no arrastrar `resolver` (y con él `ai_assist`) cuando
    el canario se usa desde tests o desde el CLI.
    """
    from . import resolver
    return resolver._http_get_detallado(url)


# Cómo se le explica al usuario cada motivo de fallo. Un sitio que nos rechaza
# y un sitio caído producen los dos `indeterminado`, pero no son lo mismo y el
# informe tiene que decir cuál fue (Paso 2C, Entrega 3).
_MOTIVO_HUMANO = {
    "bloqueado": "el sitio bloquea a Rastrillo (403/429)",
    "red": "sin respuesta (timeout, DNS o conexión)",
    "ssrf": "URL no permitida por la guarda anti-SSRF",
}


def _normalizar_respuesta(r) -> tuple:
    """Acepta las DOS formas que puede devolver la primitiva `http_get` y
    devuelve siempre `(resultado, motivo)`.

      - `(status, final_url, body)`  → forma simple, la que usan los tests y
        cualquier `http_get` inyectado que no sepa de motivos.
      - `(resultado_o_None, motivo)` → forma detallada de
        `resolver._http_get_detallado`.
      - `None` / falsy               → fallo sin motivo declarado; lo tratamos
        como fallo de red, que es lo que era antes de la Entrega 3.

    Se distinguen por la forma: la simple tiene 3 elementos y abre con un
    status entero; la detallada tiene 2.
    """
    if not r:
        return None, "red"
    if isinstance(r, tuple) and len(r) == 3 and isinstance(r[0], int):
        return r, None
    if isinstance(r, (tuple, list)) and len(r) == 2:
        resultado, motivo = r
        if resultado:
            return tuple(resultado), None
        return None, (motivo or "red")
    # Forma desconocida: no inventamos, lo tratamos como fallo de red.
    return None, "red"


def _url_segura_por_defecto(url: str) -> bool:
    from . import resolver
    return resolver._is_safe_url(url)


def analizar_sitio(url: str, identificador: str, tokens=None,
                   http_get=None, url_segura=None, marcadores_extra=None) -> dict:
    """Corre el canario contra UN sitio. Nunca lanza.

    Devuelve `{veredicto, tokens, evidencia}`, donde `evidencia` lleva las dos
    URLs pedidas, su status, la similitud y los marcadores encontrados — es lo
    que imprime `rastrillo canario` para poder juzgar el mecanismo a mano.

    `marcadores_extra` son los textos de "no existe" que declara el catálogo de
    Sherlock para este sitio (`catalogo.plantilla_sonda`). Se buscan además de
    la lista genérica multi-idioma, no en su lugar.
    """
    get = http_get or _http_get_por_defecto
    segura = url_segura or _url_segura_por_defecto
    toks = list(tokens or generar_tokens(2))
    extra = list(marcadores_extra or [])

    ev = {"url_origen": url, "identificador": identificador,
          "tokens": toks, "sondas": [], "similitud": None, "motivo": None,
          "marcadores_catalogo": extra, "causa": None}

    urls = [url_canario(url, identificador, t) for t in toks]
    if not all(urls):
        ev["motivo"] = ("no puedo construir la URL del canario: el "
                        "identificador no aparece en la URL del hit")
        ev["causa"] = "sin_plantilla"
        return {"veredicto": "indeterminado", "tokens": toks, "evidencia": ev}

    respuestas = []
    for u in urls:
        # La guarda va ANTES del GET y sin excepción: un `http://` o un host
        # que resuelva a IP privada no se pide, punto.
        if not segura(u):
            ev["sondas"].append({"url": u, "status": None,
                                 "error": "bloqueada por la guarda de red"})
            ev["motivo"] = _MOTIVO_HUMANO["ssrf"]
            ev["causa"] = "ssrf"
            return {"veredicto": "indeterminado", "tokens": toks, "evidencia": ev}
        try:
            bruto = get(u)
        except Exception as e:
            # Un fallo no aborta el lote: este sitio queda indeterminado.
            log.info("canario(%s) excepción: %s: %s", u, type(e).__name__, e)
            ev["sondas"].append({"url": u, "status": None,
                                 "error": f"{type(e).__name__}: {e}"})
            ev["motivo"] = "excepción durante la petición"
            ev["causa"] = "excepcion"
            return {"veredicto": "indeterminado", "tokens": toks, "evidencia": ev}
        r, causa = _normalizar_respuesta(bruto)
        if not r:
            # Distinguir bloqueo de caída: identificarnos y ser rechazados es
            # información válida sobre el sitio; no llegar, no lo es.
            ev["sondas"].append({
                "url": u, "status": None, "causa": causa,
                "error": _MOTIVO_HUMANO.get(causa, "sin respuesta")})
            ev["motivo"] = _MOTIVO_HUMANO.get(causa, "sin respuesta")
            ev["causa"] = causa
            return {"veredicto": "indeterminado", "tokens": toks, "evidencia": ev}
        status, final_url, cuerpo = r
        marcadores = _marcadores_en(cuerpo, extra)
        ev["sondas"].append({
            "url": u, "final_url": final_url, "status": status,
            "bytes": len(cuerpo or ""), "marcadores": marcadores,
        })
        respuestas.append((status, cuerpo, marcadores))

    (st1, c1, m1), (st2, c2, m2) = respuestas

    # 1) Status de error en cualquiera de los dos: el sitio sabe decir que no.
    #    Incluye 5xx, que estrictamente es "el sitio se rompió" y no "el
    #    usuario no existe. Es la semántica que ya tenía y no la cambiamos
    #    aquí; queda anotada como candidata a revisión.
    if (st1 or 0) >= 400 or (st2 or 0) >= 400:
        ev["motivo"] = f"status {st1}/{st2}: el sitio rechaza usuarios falsos"
        return {"veredicto": "discrimina", "tokens": toks, "evidencia": ev}

    # 2) Marcador de "no existe" en el cuerpo. Sin esto daríamos por
    #    indiscriminado a todo sitio que responda 200 con un mensaje de error
    #    — que son muchos.
    if m1 or m2:
        ev["motivo"] = ("el cuerpo dice que el usuario no existe: "
                        + ", ".join(sorted(set(m1 + m2))[:3]))
        return {"veredicto": "discrimina", "tokens": toks, "evidencia": ev}

    # 3) Dos 200 con el mismo cuerpo: el sitio no puede discriminar.
    sim = similitud(c1, c2, toks)
    ev["similitud"] = round(sim, 4)
    if st1 == st2 == 200 and sim >= UMBRAL_SIMILITUD:
        ev["motivo"] = (f"dos usernames falsos devuelven 200 con cuerpos "
                        f"{sim:.1%} idénticos y sin mensaje de 'no existe'")
        return {"veredicto": "indiscriminado", "tokens": toks, "evidencia": ev}

    # 4) Todo lo demás: no concluimos, y por tanto no penalizamos.
    ev["motivo"] = (f"respuestas distintas entre sí (similitud {sim:.1%}, "
                    f"status {st1}/{st2}) pero sin señal clara")
    ev["causa"] = "sin_senal"
    return {"veredicto": "indeterminado", "tokens": toks, "evidencia": ev}


# --- Caché por host (patrón de directory.py) --------------------------------
def _leer_cache() -> dict:
    if not CACHE_PATH.exists():
        return {}
    try:
        obj = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
    except Exception:
        log.exception("canario.json corrupto; lo ignoro")
        return {}
    return obj if isinstance(obj, dict) else {}


def _escribir_cache(data: dict) -> None:
    config.ensure_dirs()
    try:
        CACHE_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                              encoding="utf-8")
    except Exception:
        log.exception("no pude escribir canario.json")


def cache_fresca(host: str, cache=None, ahora=None) -> Optional[dict]:
    """Entrada cacheada de `host` si no ha vencido el TTL, o None."""
    c = _leer_cache() if cache is None else cache
    entrada = c.get((host or "").strip().lower())
    if not isinstance(entrada, dict):
        return None
    if entrada.get("veredicto") not in VEREDICTOS:
        return None
    fecha = entrada.get("fecha")
    if not fecha:
        return None
    ahora = time.time() if ahora is None else ahora
    if (ahora - float(fecha)) > _max_age_seconds():
        return None
    return entrada


# --- Pase completo sobre la DB ----------------------------------------------
_MOTIVOS = {
    "indiscriminado": {
        "code": "canario_indiscriminado",
        "desc": ("el sitio responde igual a usuarios inventados: no se puede "
                 "verificar aquí (no dice nada sobre si la cuenta es tuya)"),
    },
    "discrimina": {
        "code": "canario_discrimina",
        "desc": "el sitio sí distingue usuarios que no existen",
    },
}

# Motivos para los `indeterminado` cuya causa sí sabemos (Paso 2C, Entrega 3).
# Un sitio que nos bloquea y un sitio caído acababan los dos en "sin respuesta"
# y eran indistinguibles en el informe. Se anotan como cualquier otro motivo:
# el canario ya no mueve el tramo, así que dejar constancia no tiene coste.
_MOTIVOS_CAUSA = {
    "bloqueado": {
        "code": "canario_bloqueado",
        "desc": ("el sitio bloquea a Rastrillo (403/429): no pudimos "
                 "comprobar nada, pero el sitio sí está en pie"),
    },
    "red": {
        "code": "canario_sin_respuesta",
        "desc": ("no hubo respuesta (timeout, DNS o conexión): puede estar "
                 "caído o inalcanzable desde aquí"),
    },
}


def _sitios_a_probar(filas) -> dict:
    """host → qué sondear en ese sitio.

    Solo filas CON `profile_url`: holehe y hibp nunca la traen, así que
    quedan fuera por construcción y no generan ni una petición. Un host con
    varios hits se prueba UNA vez (de ahí las 2 peticiones por sitio, no 2
    por fila).

    La plantilla NO es necesariamente la URL del hit: si el catálogo de
    Sherlock define `urlProbe` para ese sitio, sondeamos ESA, que es la que
    produjo el hallazgo (ver `catalogo.plantilla_sonda`). Sondear la página de
    perfil mientras Sherlock consulta una API mide otra cosa, y el veredicto no
    sería comparable con el hit.
    """
    from . import catalogo

    out = {}
    for row in filas:
        host = (row["source_site"] or "").strip().lower()
        url = (row["profile_url"] or "").strip()
        ident = row["identifier"] or ""
        if not host or not url or host in out:
            continue
        sonda = catalogo.plantilla_sonda(url, ident)
        out[host] = {"url": sonda["url"] or url, "identifier": ident,
                     "marcadores": sonda["marcadores"],
                     "origen": sonda["origen"], "sitio": sonda["sitio"]}
    return out


def _aplicar_a_fila(row, veredicto: str, causa=None) -> bool:
    """Escribe la VERIFICABILIDAD de la fila y anota el motivo. Nunca toca
    `confidence`. Devuelve True si la verificabilidad cambió.

    Este es el punto donde vive el invariante de la Entrega 1, así que conviene
    decirlo sin rodeos: **`confidence` no aparece en `campos` en ninguna rama**,
    ni para subir ni para bajar. El veredicto del canario describe si el SITIO
    sirve para verificar; la probabilidad de que la cuenta sea del usuario la
    siguen fijando `discovery._register` y la corroboración de 2A, que son las
    únicas que miran el identificador.

    Por eso tampoco hace falta ya distinguir fuentes heurísticas de fuentes de
    confianza política (holehe `high`, hibp `medium`): esa distinción existía
    para decidir A QUIÉN degradar, y aquí no se degrada a nadie. El veredicto se
    escribe en todas las filas del sitio porque es una propiedad del sitio.
    """
    campos = {"verifiability": veredicto}
    # `causa` solo llega con `indeterminado` y solo cuando sabemos por qué
    # (bloqueo vs. red). Anotarla es lo que permite que el informe diga qué
    # midió en vez de un "sin respuesta" que valía para todo.
    motivo = _MOTIVOS.get(veredicto) or _MOTIVOS_CAUSA.get(causa or "")
    if motivo:
        motivos = db.merge_reasons(db.parse_reasons(row["confidence_reasons"]),
                                   [dict(motivo)])
        campos["confidence_reasons"] = db.dump_reasons(motivos)
    cambio = (row["verifiability"] or None) != veredicto
    db.update_account(row["id"], **campos)
    return cambio


# --- Reparación de un solo uso (daño del paso 2B) ---------------------------
# Tramo BASE que implica cada motivo de `discovery._sherlock_confidence`. No es
# una escala nueva: es la misma, leída al revés. Si alguna vez cambia allí,
# cambia aquí — por eso el test la fija contra la función real.
_TRAMO_POR_MOTIVO = {
    "tramo_distintivo": "high",
    "tramo_corto": "medium",
    "tramo_muy_corto": "low",
    "id_vacio": "low",
}
# Motivos que suben un tramo sobre la base. `corrob_misma_fila` NO está aquí a
# propósito: dos buscadores de username con catálogos que se solapan no son
# señal independiente (ver `db.upsert_account`).
_MOTIVOS_QUE_SUBEN = ("bump_path", "bump_subdominio", "corrob_cruzada")


def reparar_confianza_2b(aplicar: bool = False) -> dict:
    """Devuelve a su tramo las filas que el paso 2B bajó a `low` por el canario.

    Por qué hace falta. Hasta 2B un `indiscriminado` escribía `confidence='low'`
    sin guardar en ningún sitio el tramo anterior. Y un reescaneo NO lo
    recupera: `db.upsert_account` hace `return row["id"]` en cuanto la fila
    existe, así que el `confidence` que calcula `discovery._register` para un
    hit repetido **nunca llega a escribirse**.

    Cómo lo recupera sin red. El tramo es reconstruible de forma determinista a
    partir de los motivos que sí se persistieron: la escala base sale de
    `_TRAMO_POR_MOTIVO` y los bumps de `_MOTIVOS_QUE_SUBEN`, que son la misma
    escala de `discovery._sherlock_confidence` leída al revés.

    Alcance deliberadamente estrecho — solo toca filas que cumplan las tres:
      1. `confidence == 'low'`,
      2. llevan el motivo `canario_indiscriminado`,
      3. su fuente es heurística (sherlock/maigret), que son las únicas que 2B
         llegó a degradar.
    Eso la hace idempotente: tras la primera pasada ninguna fila cumple (1).

    Por defecto NO escribe: devuelve el plan (`aplicar=False`). Con
    `aplicar=True` hace `snapshot_db()` primero y luego actualiza. El motivo
    del canario se CONSERVA: sigue siendo evidencia válida, simplemente ya no
    mueve el tramo.
    """
    from .discovery import _FUENTES_HEURISTICAS, _subir_tramo

    plan = []
    for row in db.list_accounts():
        if (row["confidence"] or "") != "low":
            continue
        if (row["source"] or "").strip() not in _FUENTES_HEURISTICAS:
            continue
        codes = [m.get("code") for m in db.parse_reasons(row["confidence_reasons"])]
        if "canario_indiscriminado" not in codes:
            continue
        base = next((_TRAMO_POR_MOTIVO[c] for c in codes
                     if c in _TRAMO_POR_MOTIVO), None)
        if base is None:
            # Sin motivo de tramo base no hay nada que reconstruir: la dejamos
            # como está en vez de inventarle una confianza.
            continue
        for c in codes:
            if c in _MOTIVOS_QUE_SUBEN:
                base = _subir_tramo(base)
        if base == "low":
            continue
        plan.append({"id": row["id"], "site": row["source_site"],
                     "identifier": row["identifier"],
                     "de": "low", "a": base, "motivos": codes})

    if aplicar and plan:
        db.snapshot_db()
        for p in plan:
            db.update_account(p["id"], confidence=p["a"])
            db.log(p["id"], "info",
                   f"confianza restaurada a {p['a']} (el canario ya no degrada)")
    return {"aplicado": bool(aplicar), "filas": len(plan), "plan": plan}


def run_canario(http_get=None, url_segura=None, tokens=None,
                usar_cache: bool = True) -> dict:
    """Pase de canario sobre lo que hay en la DB. Sin excepciones al caller.

    Corre DESPUÉS del discovery y ANTES del auto-resolver (ver
    `jobs.scan_async`): así el auto-resolver no gasta peticiones resolviendo
    sitios que el canario acaba de invalidar, y `profile_url` todavía es la
    URL del hit, que es la plantilla que necesitamos.

    Devuelve `{sitios, cacheados, probados, indiscriminados, marcadas,
    errores, veredictos}`. `marcadas` cuenta filas cuya VERIFICABILIDAD cambió
    (antes se llamaba `degradadas` y contaba caídas de tramo; ya no se degrada
    nada, ver `_aplicar_a_fila`).
    """
    import concurrent.futures

    filas = list(db.list_accounts())
    sitios = _sitios_a_probar(filas)
    resumen = {"sitios": len(sitios), "cacheados": 0, "probados": 0,
               "indiscriminados": 0, "bloqueados": 0, "marcadas": 0,
               "errores": [], "veredictos": {}, "causas": {}}
    if not sitios:
        return resumen

    cache = _leer_cache() if usar_cache else {}
    pendientes = {}
    for host, info in sitios.items():
        entrada = cache_fresca(host, cache=cache) if usar_cache else None
        if entrada:
            resumen["veredictos"][host] = entrada["veredicto"]
            resumen["cacheados"] += 1
        else:
            pendientes[host] = info

    def _probar(item):
        host, info = item
        try:
            return host, analizar_sitio(
                info["url"], info["identifier"], tokens=tokens,
                http_get=http_get, url_segura=url_segura,
                marcadores_extra=info.get("marcadores"))
        except Exception as e:                       # cinturón y tirantes
            log.exception("canario(%s) falló", host)
            return host, {"veredicto": "indeterminado", "tokens": [],
                          "evidencia": {"motivo": f"{type(e).__name__}: {e}"}}

    if pendientes:
        n = min(_workers(), len(pendientes))
        with concurrent.futures.ThreadPoolExecutor(
                max_workers=n, thread_name_prefix="rastrillo-canario") as pool:
            for host, res in pool.map(_probar, list(pendientes.items())):
                resumen["probados"] += 1
                resumen["veredictos"][host] = res["veredicto"]
                ev = res.get("evidencia") or {}
                if res["veredicto"] == "indeterminado":
                    # NO se cachea, ni el bloqueo ni el fallo de red: los dos
                    # se reintentan en el próximo escaneo.
                    resumen["causas"][host] = ev.get("causa")
                    resumen["errores"].append(
                        {"host": host, "causa": ev.get("causa"),
                         "error": ev.get("motivo")})
                    continue
                cache[host] = {"veredicto": res["veredicto"],
                               "fecha": time.time(),
                               "evidencia": res.get("evidencia") or {}}
        if usar_cache:
            _escribir_cache(cache)

    for row in filas:
        host = (row["source_site"] or "").strip().lower()
        veredicto = resumen["veredictos"].get(host)
        # Los tres veredictos se registran: `indeterminado` no se cachea, pero
        # "lo intenté y no pude concluir" sigue siendo información distinta de
        # "no lo he mirado" (NULL), y el triage la quiere ver.
        if veredicto in VEREDICTOS:
            if _aplicar_a_fila(row, veredicto, resumen["causas"].get(host)):
                resumen["marcadas"] += 1
    resumen["indiscriminados"] = sum(
        1 for v in resumen["veredictos"].values() if v == "indiscriminado")
    resumen["bloqueados"] = sum(
        1 for c in resumen["causas"].values() if c == "bloqueado")

    log.info("canario: %s sitios (%s cacheados, %s probados) | "
             "%s indiscriminados | %s nos bloquean | %s filas marcadas | "
             "%s sin veredicto",
             resumen["sitios"], resumen["cacheados"], resumen["probados"],
             resumen["indiscriminados"], resumen["bloqueados"],
             resumen["marcadas"], len(resumen["errores"]))
    print(f"[canario] sitios {resumen['sitios']} "
          f"(cache {resumen['cacheados']}, red {resumen['probados']}) | "
          f"indiscriminados {resumen['indiscriminados']} | "
          f"nos bloquean {resumen['bloqueados']} | "
          f"filas marcadas {resumen['marcadas']} | "
          f"sin veredicto {len(resumen['errores'])}")
    return resumen
