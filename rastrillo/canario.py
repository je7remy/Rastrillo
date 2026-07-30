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

Reglas de aplicación (`aplicar_veredictos`):

  - `indiscriminado` BAJA a `low` las filas HEURÍSTICAS de ese sitio
    (sherlock, maigret) y anota el motivo en todas, de modo que "Descartar
    dudosas" (`discard-low`) las recoja sin tocar su lógica. Nada se marca
    `not_mine` automáticamente. Las fuentes de confianza POLÍTICA (holehe
    `high`, hibp `medium`) reciben el motivo pero no cambian de tramo: misma
    línea que trazó el paso 2A, y ver `_aplicar_a_fila` para el porqué.
  - `discrimina` NO sube nada. El canario solo puede penalizar; el techo de
    confianza lo siguen fijando `discovery._register` y la corroboración.
  - `indeterminado` no penaliza, no anota y NO se cachea (para reintentar en
    el próximo escaneo).

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
# Fuente única de verdad de qué confianzas son ESTIMACIÓN y cuáles POLÍTICA.
# La fijó el paso 2A y el canario se rige por la misma línea (ver
# `_aplicar_a_fila`). Importar de `discovery` no cierra ciclo: `discovery` no
# sabe nada de este módulo.
from .discovery import _FUENTES_HEURISTICAS

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
_MARCADORES_NO_EXISTE = {
    "en": [
        "user not found", "page not found", "profile not found",
        "account not found", "no such user", "user does not exist",
        "doesn't exist", "does not exist", "sorry, this page isn",
        "nobody on", "not found on", "404 not found",
    ],
    "es": [
        "usuario no encontrado", "no se ha encontrado", "no se encontr",
        "no existe", "página no encontrada", "pagina no encontrada",
        "perfil no encontrado", "usuario no existe",
    ],
    "ru": [
        "пользователь не найден", "страница не найдена", "не существует",
        "не найден", "ничего не найдено",
    ],
    "pt-BR": [
        "usuário não encontrado", "usuario nao encontrado",
        "página não encontrada", "pagina nao encontrada", "não existe",
        "nao existe", "não encontrado",
    ],
    "fr": [
        "utilisateur introuvable", "page introuvable", "n'existe pas",
        "compte introuvable", "aucun utilisateur",
    ],
    "de": [
        "benutzer nicht gefunden", "seite nicht gefunden", "existiert nicht",
        "nicht gefunden", "kein benutzer",
    ],
}


def _marcadores_en(cuerpo: str) -> list:
    """Marcadores de "no existe" presentes en el cuerpo. Lista de
    `"idioma:frase"` para que la evidencia sea legible."""
    if not cuerpo:
        return []
    bajo = cuerpo.lower()
    return [f"{lang}:{frase}"
            for lang, frases in _MARCADORES_NO_EXISTE.items()
            for frase in frases if frase in bajo]


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
    """GET real. Import diferido para no arrastrar `resolver` (y con él
    `ai_assist`) cuando el canario se usa desde tests o desde el CLI."""
    from . import resolver
    return resolver._http_get(url)


def _url_segura_por_defecto(url: str) -> bool:
    from . import resolver
    return resolver._is_safe_url(url)


def analizar_sitio(url: str, identificador: str, tokens=None,
                   http_get=None, url_segura=None) -> dict:
    """Corre el canario contra UN sitio. Nunca lanza.

    Devuelve `{veredicto, tokens, evidencia}`, donde `evidencia` lleva las dos
    URLs pedidas, su status, la similitud y los marcadores encontrados — es lo
    que imprime `rastrillo canario` para poder juzgar el mecanismo a mano.
    """
    get = http_get or _http_get_por_defecto
    segura = url_segura or _url_segura_por_defecto
    toks = list(tokens or generar_tokens(2))

    ev = {"url_origen": url, "identificador": identificador,
          "tokens": toks, "sondas": [], "similitud": None, "motivo": None}

    urls = [url_canario(url, identificador, t) for t in toks]
    if not all(urls):
        ev["motivo"] = ("no puedo construir la URL del canario: el "
                        "identificador no aparece en la URL del hit")
        return {"veredicto": "indeterminado", "tokens": toks, "evidencia": ev}

    respuestas = []
    for u in urls:
        # La guarda va ANTES del GET y sin excepción: un `http://` o un host
        # que resuelva a IP privada no se pide, punto.
        if not segura(u):
            ev["sondas"].append({"url": u, "status": None,
                                 "error": "bloqueada por la guarda de red"})
            ev["motivo"] = "URL no permitida por la guarda anti-SSRF"
            return {"veredicto": "indeterminado", "tokens": toks, "evidencia": ev}
        try:
            r = get(u)
        except Exception as e:
            # Un fallo no aborta el lote: este sitio queda indeterminado.
            log.info("canario(%s) excepción: %s: %s", u, type(e).__name__, e)
            ev["sondas"].append({"url": u, "status": None,
                                 "error": f"{type(e).__name__}: {e}"})
            ev["motivo"] = "excepción durante la petición"
            return {"veredicto": "indeterminado", "tokens": toks, "evidencia": ev}
        if not r:
            ev["sondas"].append({"url": u, "status": None,
                                 "error": "sin respuesta (timeout, 403/429 o red caída)"})
            ev["motivo"] = "sin respuesta"
            return {"veredicto": "indeterminado", "tokens": toks, "evidencia": ev}
        status, final_url, cuerpo = r
        marcadores = _marcadores_en(cuerpo)
        ev["sondas"].append({
            "url": u, "final_url": final_url, "status": status,
            "bytes": len(cuerpo or ""), "marcadores": marcadores,
        })
        respuestas.append((status, cuerpo, marcadores))

    (st1, c1, m1), (st2, c2, m2) = respuestas

    # 1) Status de error en cualquiera de los dos: el sitio sabe decir que no.
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
        "desc": ("el sitio responde igual a usuarios inventados: no puede "
                 "confirmar que la cuenta exista"),
    },
    "discrimina": {
        "code": "canario_discrimina",
        "desc": "el sitio sí distingue usuarios que no existen",
    },
}


def _sitios_a_probar(filas) -> dict:
    """host → URL del hit que sirve de plantilla.

    Solo filas CON `profile_url`: holehe y hibp nunca la traen, así que
    quedan fuera por construcción y no generan ni una petición. Un host con
    varios hits se prueba UNA vez (de ahí las 2 peticiones por sitio, no 2
    por fila).
    """
    out = {}
    for row in filas:
        host = (row["source_site"] or "").strip().lower()
        url = (row["profile_url"] or "").strip()
        if not host or not url:
            continue
        out.setdefault(host, {"url": url, "identifier": row["identifier"] or ""})
    return out


def _aplicar_a_fila(row, veredicto: str) -> bool:
    """Anota el motivo y, si procede, baja el tramo. Devuelve True si bajó.

    Dos límites, y son los mismos que fijó el paso 2A:

    1. El canario NUNCA sube la confianza. `discrimina` solo deja constancia;
       bajar a `low` ante un `indiscriminado` es la ÚNICA vía por la que este
       módulo toca `confidence`. Tampoco hay tramo intermedio de castigo: el
       veredicto invalida el hit entero, no lo debilita a medias.

    2. Solo se degradan las fuentes HEURÍSTICAS (`sherlock`, `maigret`). La
       señal ajusta estimaciones, nunca políticas: el `high` de holehe no es
       una estimación sobre la página de perfil sino una política basada en
       que el email es identificador único —y holehe ni siquiera consulta esa
       página, usa flujos de recuperación—, así que el veredicto del canario
       no es evidencia sobre esa fila. Igual con el `medium` de hibp. El
       motivo sí se anota en todas: describe al SITIO, es cierto para
       cualquier fila suya, y así el triage ve el contexto completo (mismo
       criterio que `corrob_cruzada` en `_corroborar_entre_fuentes`).
    """
    motivo = _MOTIVOS.get(veredicto)
    if not motivo:
        return False
    motivos = db.merge_reasons(db.parse_reasons(row["confidence_reasons"]),
                              [dict(motivo)])
    campos = {"confidence_reasons": db.dump_reasons(motivos)}
    bajo = False
    fuente = (row["source"] or "").strip()
    if (veredicto == "indiscriminado"
            and fuente in _FUENTES_HEURISTICAS
            and (row["confidence"] or "") != "low"):
        campos["confidence"] = "low"
        bajo = True
    db.update_account(row["id"], **campos)
    return bajo


def run_canario(http_get=None, url_segura=None, tokens=None,
                usar_cache: bool = True) -> dict:
    """Pase de canario sobre lo que hay en la DB. Sin excepciones al caller.

    Corre DESPUÉS del discovery y ANTES del auto-resolver (ver
    `jobs.scan_async`): así el auto-resolver no gasta peticiones resolviendo
    sitios que el canario acaba de invalidar, y `profile_url` todavía es la
    URL del hit, que es la plantilla que necesitamos.

    Devuelve `{sitios, cacheados, probados, indiscriminados, degradadas,
    errores, veredictos}`.
    """
    import concurrent.futures

    filas = list(db.list_accounts())
    sitios = _sitios_a_probar(filas)
    resumen = {"sitios": len(sitios), "cacheados": 0, "probados": 0,
               "indiscriminados": 0, "degradadas": 0, "errores": [],
               "veredictos": {}}
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
            return host, analizar_sitio(info["url"], info["identifier"],
                                        tokens=tokens, http_get=http_get,
                                        url_segura=url_segura)
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
                if res["veredicto"] == "indeterminado":
                    # No se cachea: reintentamos en el próximo escaneo.
                    resumen["errores"].append(
                        {"host": host,
                         "error": (res.get("evidencia") or {}).get("motivo")})
                    continue
                cache[host] = {"veredicto": res["veredicto"],
                               "fecha": time.time(),
                               "evidencia": res.get("evidencia") or {}}
        if usar_cache:
            _escribir_cache(cache)

    for row in filas:
        host = (row["source_site"] or "").strip().lower()
        veredicto = resumen["veredictos"].get(host)
        if veredicto in ("indiscriminado", "discrimina"):
            if _aplicar_a_fila(row, veredicto):
                resumen["degradadas"] += 1
    resumen["indiscriminados"] = sum(
        1 for v in resumen["veredictos"].values() if v == "indiscriminado")

    log.info("canario: %s sitios (%s cacheados, %s probados) | "
             "%s indiscriminados | %s filas degradadas | %s errores",
             resumen["sitios"], resumen["cacheados"], resumen["probados"],
             resumen["indiscriminados"], resumen["degradadas"],
             len(resumen["errores"]))
    print(f"[canario] sitios {resumen['sitios']} "
          f"(cache {resumen['cacheados']}, red {resumen['probados']}) | "
          f"indiscriminados {resumen['indiscriminados']} | "
          f"filas a low {resumen['degradadas']} | "
          f"errores {len(resumen['errores'])}")
    return resumen
