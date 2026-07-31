"""Lectura del catálogo de Sherlock (`data.json`) para el canario.

Por qué existe (Paso 2C, Entrega 2). El canario sondeaba SIEMPRE la URL de
perfil visible del hit. Pero el `data.json` de Sherlock define `urlProbe` para
los sitios cuya URL de sondeo difiere de la visible —típicamente los que se
comprueban contra una API— y define `errorMsg` con el texto EXACTO que ese
sitio devuelve cuando el usuario no existe. Ignorar las dos cosas producía
veredictos que no eran comparables con lo que produjo el hit:

  - Duolingo. `url` es `https://www.duolingo.com/profile/{}`, una SPA que
    devuelve el mismo HTML para cualquiera. Sherlock no la mira: usa
    `urlProbe` = `https://www.duolingo.com/2017-06-30/users?username={}` y
    compara contra `errorMsg` = `{"users":[]}`. El canario sondeaba la SPA y
    concluía `indiscriminado` sobre una página que Sherlock nunca consultó.
  - Steam y HudsonRock. No tienen `urlProbe` (el canario sondeaba bien), pero
    su forma de decir "no existe" —"The specified profile could not be found",
    "This username is not associated with a computer infected by an
    info-stealer"— no estaba en la lista genérica multi-idioma. Con `errorMsg`
    sí se detectan, y sin hardcodear ninguna plataforma en el código: la
    convención del proyecto es que lo específico de cada sitio viva en recetas
    o en catálogos, nunca en el motor.

Ojo con lo que esto NO arregla, y es correcto que no lo arregle: un sitio que
renderiza el perfil en el cliente y no tiene `urlProbe` seguirá dando
`indiscriminado`, porque es la verdad — su respuesta no distingue a nadie.

Emparejar una fila de la DB con una entrada del catálogo. Se hace por FORMA DE
LA URL, no por nombre: `display_name` puede venir de una receta en vez de del
catálogo, y el host solo no basta (`steamcommunity.com` tiene dos entradas,
"Steam Community (User)" y "(Group)"). Sustituir el identificador en la
plantilla y comparar con la `profile_url` del hit desambigua exactamente.

Limitaciones conocidas:
  - Los hits de **Maigret** no se cubren: Maigret trae su propio catálogo, con
    otro esquema, y no lo leemos. Sus filas siguen sondeando la URL visible y
    apoyándose en la lista genérica de marcadores. Es una degradación
    silenciosa y aceptable (el veredicto solo informa; ya no toca `confidence`).
  - Algunos `urlProbe` no llevan el identificador en la URL (Discord, Holopin
    consultan por POST a un endpoint fijo). Ahí `canario.url_canario` no puede
    construir la sonda y el sitio queda `indeterminado`, que es lo honesto.

Sin dependencias nuevas: `sherlock-project` ya es requisito del proyecto y el
fichero se lee con `importlib.resources` del paquete instalado — no copiamos la
tabla ni la versionamos aquí.
"""
from __future__ import annotations
import functools
import json
import logging
import urllib.parse
from typing import Optional

log = logging.getLogger("rastrillo.catalogo")

_RECURSO = "resources/data.json"


@functools.lru_cache(maxsize=1)
def cargar() -> dict:
    """El `data.json` del paquete instalado, o `{}` si no se puede leer.

    Cacheado: son ~400 entradas y el canario lo consulta una vez por sitio.
    Nunca lanza — si `sherlock-project` no está o cambió de layout, el canario
    sigue funcionando con la lista genérica de marcadores.
    """
    try:
        import importlib.resources as ir
        raw = ir.files("sherlock_project").joinpath(_RECURSO).read_text(
            encoding="utf-8")
        data = json.loads(raw)
    except Exception as e:
        log.info("no pude leer el catálogo de sherlock (%s: %s); "
                 "el canario usará solo los marcadores genéricos",
                 type(e).__name__, e)
        return {}
    # El fichero mezcla entradas de sitio con claves de metadatos ($schema).
    return {k: v for k, v in data.items()
            if isinstance(v, dict) and isinstance(v.get("url"), str)}


@functools.lru_cache(maxsize=1)
def _indice_por_host() -> dict:
    """host de la plantilla `url` → lista de (nombre, entrada).

    Estrecha la búsqueda antes de comparar plantillas; sin esto cada lookup
    recorrería las ~400 entradas.
    """
    idx: dict = {}
    for nombre, info in cargar().items():
        host = _host(info["url"].replace("{}", "x"))
        if host:
            idx.setdefault(host, []).append((nombre, info))
    return idx


def _host(url: str) -> str:
    try:
        p = urllib.parse.urlsplit(url if "://" in url else "//" + url)
    except ValueError:
        return ""
    return (p.hostname or "").strip().lower()


def _sustituir(plantilla: str, identificador: str) -> str:
    """`replace` y no `format`: hay plantillas con llaves literales y `format`
    reventaría con KeyError/IndexError sobre ellas."""
    return (plantilla or "").replace("{}", identificador or "")


def _misma_url(a: str, b: str) -> bool:
    """Compara dos URLs tolerando caja y barra final, que es donde difieren el
    CSV de sherlock y la plantilla del catálogo."""
    return (a or "").strip().lower().rstrip("/") == (b or "").strip().lower().rstrip("/")


def buscar(profile_url: str, identificador: str) -> Optional[tuple]:
    """Entrada del catálogo cuyo `url` genera exactamente `profile_url` al
    sustituir `identificador`. Devuelve `(nombre, entrada)` o None.
    """
    if not profile_url or not identificador:
        return None
    candidatos = _indice_por_host().get(_host(profile_url)) or []
    for nombre, info in candidatos:
        if _misma_url(_sustituir(info["url"], identificador), profile_url):
            return nombre, info
    return None


def marcadores(entrada: dict) -> list:
    """Textos de "este usuario no existe" que declara el catálogo.

    Solo con `errorType == "message"`: en los demás modos (`status_code`,
    `response_url`) `errorMsg` no existe o no es un texto del cuerpo. Sherlock
    admite string o lista, así que aceptamos las dos formas.
    """
    if not isinstance(entrada, dict) or entrada.get("errorType") != "message":
        return []
    msg = entrada.get("errorMsg")
    if isinstance(msg, str):
        msg = [msg]
    if not isinstance(msg, list):
        return []
    return [m.strip() for m in msg if isinstance(m, str) and m.strip()]


def plantilla_sonda(profile_url: str, identificador: str) -> dict:
    """Qué debe sondear el canario para ESTE hit, y con qué marcadores.

    Devuelve siempre un dict:
      `url`         URL a sondear (la de `urlProbe` si existe, si no la del hit)
      `marcadores`  textos de "no existe" declarados por el catálogo
      `origen`      "urlProbe" | "url" | "sin_catalogo" — para la evidencia
      `sitio`       nombre de la entrada del catálogo, o None

    Si no hay entrada (Maigret, sitio retirado del catálogo, sherlock no
    instalado) devuelve la URL del hit tal cual: el canario degrada a la lista
    genérica de marcadores en vez de dejar de funcionar.
    """
    fallback = {"url": profile_url, "marcadores": [],
                "origen": "sin_catalogo", "sitio": None}
    hallazgo = buscar(profile_url, identificador)
    if not hallazgo:
        return fallback
    nombre, info = hallazgo
    probe = info.get("urlProbe")
    if isinstance(probe, str) and probe.strip():
        return {"url": _sustituir(probe, identificador),
                "marcadores": marcadores(info),
                "origen": "urlProbe", "sitio": nombre}
    return {"url": profile_url, "marcadores": marcadores(info),
            "origen": "url", "sitio": nombre}
