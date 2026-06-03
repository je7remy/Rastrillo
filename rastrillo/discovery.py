"""Descubrimiento de cuentas. Envuelve Sherlock (por username) y Holehe (por email).

Ambos se ejecutan como subprocesos. Parseamos sus salidas CSV — no el texto
humano — para ser robustos frente a colores ANSI y traducciones.

Diseño tras el bug de paridad CLI-vs-programa:

- TIMEOUTS GENEROSOS Y CONFIGURABLES (causa principal del 24->0): Sherlock
  contra ~400 sitios no cabe en 180 s; default 900 s, override por env.
- ERRORES VISIBLES: si un escáner se cortó por timeout, exit code != 0 o no
  generó CSV, se reporta en `summary["errors"]` con detalle. NO se devuelve
  silenciosamente una lista vacía.
- IDENTIDAD POR SITIO REAL: cada hit lleva `source_site` (host del URL o
  dominio). La unicidad en la DB es por (source_site, identifier), de modo
  que dos sitios distintos que casan con la misma receta NO se colapsan.
- EL MATCH DE RECETA ES ANOTACIÓN: si hay receta, la usamos para enriquecer
  (display_name, deletion_type) y para que engine.get_recipe la encuentre.
  Pero el match nunca descarta ni fusiona hallazgos.
- KEEP_PLATFORMS SIGUEN EN LA LISTA: las cuentas profesionales se guardan
  con status='skipped', no desaparecen del dashboard.

API pública:
  run_sherlock(username)  -> {"hits":[...], "error":str|None, "incomplete":bool, "raw_count":int}
  run_holehe(email)       -> {"hits":[...], "error":str|None, "incomplete":bool, "raw_count":int}
  discover(usernames, emails) -> dict(found, kept, no_recipe, errors, raw_counts)
"""
import csv
import logging
import os
import re
import subprocess
import tempfile
from pathlib import Path

from . import db, config
from .recipes import load_recipes

log = logging.getLogger("rastrillo.discovery")


# --- Defaults configurables --------------------------------------------------
# Pensados para que Sherlock pueda terminar contra todos sus sitios sin que el
# subprocess.run lo mate. Si te quedan demasiado generosos para tu red, baja
# RASTRILLO_SHERLOCK_TIMEOUT por env.
_DEF_SHERLOCK_TIMEOUT = 900    # 15 min — Sherlock recorre ~400 sitios
_DEF_SHERLOCK_PER_SITE = 60    # antes 10s, falsos negativos abundantes
_DEF_HOLEHE_TIMEOUT = 600      # holehe tarda ~30s contra ~120 sitios


def _env_int(name, default):
    try:
        return max(10, int(os.environ.get(name, "") or default))
    except ValueError:
        return default


# --- Validación de input ----------------------------------------------------
# Permisivos pero blindados contra typos vacíos y metacaracteres de shell.
_USERNAME_RE = re.compile(r"^[A-Za-z0-9._\-]{1,64}$")
_EMAIL_RE = re.compile(r"^[^\s@]{1,64}@[^\s@]{1,255}\.[^\s@]{1,64}$")


def _valid_username(u: str) -> bool:
    return bool(u) and bool(_USERNAME_RE.match(u))


def _valid_email(e: str) -> bool:
    return bool(e) and bool(_EMAIL_RE.match(e))


# --- Score de confianza por hit ---------------------------------------------
# Sherlock genera falsos positivos por usernames cortos o muy comunes. El
# score etiqueta cada hallazgo para que la UI ofrezca un triage antes de
# permitir cualquier acción destructiva.
def _holehe_confidence() -> str:
    """Holehe consulta el sitio con el email: si confirma "esta cuenta existe",
    el identificador es único (un email). Siempre alta."""
    return "high"


def _sherlock_confidence(username: str, hit: dict) -> str:
    """Sherlock detecta por username. La confianza baja con usernames cortos o
    sin caracteres distintivos. Sube si la URL del hit contiene el username
    como segmento (e.g. /user/je7remy)."""
    u = (username or "").strip()
    if not u:
        return "low"
    has_distinct = any(c.isdigit() or c in "._-" for c in u)
    # Base por longitud / distintividad
    if len(u) >= 8 or (len(u) >= 6 and has_distinct):
        base = "high"
    elif len(u) >= 5:
        base = "medium"
    else:
        base = "low"
    # Bump si la URL del hit incluye el username literal en el path
    url = (hit.get("url") or "").lower()
    if u.lower() in url:
        if base == "low":
            base = "medium"
        elif base == "medium":
            base = "high"
    return base


# --- Slugify, host y matching de recetas ------------------------------------
def _slugify(name: str) -> str:
    """Normaliza 'Reddit', 'old.reddit.com' o una URL a 'reddit'."""
    s = (name or "").strip().lower()
    if "//" in s:
        s = s.split("//", 1)[1]
    s = s.split("/", 1)[0]
    for prefix in ("www.", "m.", "old.", "new."):
        if s.startswith(prefix):
            s = s[len(prefix):]
    s = s.split(".", 1)[0]
    return re.sub(r"[^a-z0-9]+", "", s)


def _host_from_url(url: str) -> str:
    """Devuelve el host limpio de una URL ('reddit.com', 'old.reddit.com')."""
    if not url:
        return ""
    s = url.strip().lower()
    if "//" in s:
        s = s.split("//", 1)[1]
    s = s.split("/", 1)[0].split("?", 1)[0]
    if s.startswith("www."):
        s = s[4:]
    return s


def _match_recipe(name, url, recipes):
    """Devuelve (slug, recipe) o (None, None). Solo anotación; nunca descarta hits."""
    candidates = [c for c in (_slugify(name), _slugify(url)) if c]
    if not candidates:
        return None, None

    # 1) match exacto por slug
    for c in candidates:
        if c in recipes:
            return c, recipes[c]

    # 2) aliases declarados
    for slug, r in recipes.items():
        aliases = {_slugify(a) for a in r.get("aliases", []) if a}
        if any(c in aliases for c in candidates):
            return slug, r

    # 3) heurística suave
    for slug, r in recipes.items():
        dn = _slugify(r.get("display_name", ""))
        for c in candidates:
            if slug in c or c in slug or (dn and (dn in c or c in dn)):
                return slug, r
    return None, None


# --- Sherlock ---------------------------------------------------------------
def run_sherlock(username, timeout=None, per_site_timeout=None):
    """Devuelve dict con hits y diagnóstico:
        {hits: [{name, url, source_site}], error: str|None, incomplete: bool, raw_count: int}

    No tira excepción salvo binario ausente. El caller decide cómo presentar
    los errores (timeouts, exit !=0, CSV ausente).
    """
    if not _valid_username(username):
        return {"hits": [], "error": f"username inválido: {username!r}",
                "incomplete": False, "raw_count": 0}

    timeout = timeout or _env_int("RASTRILLO_SHERLOCK_TIMEOUT", _DEF_SHERLOCK_TIMEOUT)
    per_site = per_site_timeout or _env_int(
        "RASTRILLO_SHERLOCK_SITE_TIMEOUT", _DEF_SHERLOCK_PER_SITE,
    )

    hits = []
    error = None
    incomplete = False

    with tempfile.TemporaryDirectory() as tmp:
        cmd = [
            "sherlock", username,
            "--no-color", "--no-txt", "--csv",
            "--folderoutput", tmp,
            "--timeout", str(per_site),
            "--print-found",
        ]
        try:
            proc = subprocess.run(
                cmd, capture_output=True, text=True,
                timeout=timeout, check=False,
            )
            if proc.returncode != 0:
                # No es fatal — Sherlock a veces devuelve !=0 pero ya escribió el CSV.
                tail = (proc.stderr or "").strip().splitlines()[-3:]
                error = f"sherlock exit={proc.returncode}: {' | '.join(tail) or '(sin stderr)'}"
                log.warning("sherlock(%s) exit=%s: %s", username, proc.returncode, tail)
        except FileNotFoundError:
            return {"hits": [], "error": "Sherlock no instalado (pip install sherlock-project)",
                    "incomplete": True, "raw_count": 0}
        except subprocess.TimeoutExpired:
            incomplete = True
            error = (f"sherlock cortado por timeout global ({timeout}s); "
                     f"resultados PARCIALES — sube RASTRILLO_SHERLOCK_TIMEOUT")
            log.warning("sherlock(%s) timeout %ss; leyendo CSV parcial", username, timeout)

        csvs = list(Path(tmp).glob("*.csv"))
        if not csvs:
            if not error:
                error = "sherlock no escribió CSV (¿versión cambió de flags?)"
            log.error("sherlock(%s) sin CSV en %s", username, tmp)
        for f in csvs:
            hits.extend(_read_sherlock_csv(f))

    return {"hits": hits, "error": error, "incomplete": incomplete, "raw_count": len(hits)}


def _read_sherlock_csv(path: Path):
    out = []
    try:
        with path.open("r", encoding="utf-8", errors="ignore", newline="") as fh:
            for row in csv.DictReader(fh):
                if (row.get("exists") or "").strip().lower() != "claimed":
                    continue
                name = (row.get("name") or "").strip()
                url = (row.get("url_user") or row.get("url_main") or "").strip()
                source_site = _host_from_url(url) or name.lower()
                out.append({
                    "name": name,
                    "url": url or None,
                    "source_site": source_site,
                })
    except Exception as e:
        log.exception("error leyendo CSV de sherlock %s", path)
        out.append({"_error": f"sherlock csv: {e}"})
    return out


# --- Holehe -----------------------------------------------------------------
def run_holehe(email, timeout=None):
    """Misma forma que run_sherlock. Holehe escribe el CSV en CWD; lo aislamos
    en un tempdir."""
    if not _valid_email(email):
        return {"hits": [], "error": f"email inválido: {email!r}",
                "incomplete": False, "raw_count": 0}

    timeout = timeout or _env_int("RASTRILLO_HOLEHE_TIMEOUT", _DEF_HOLEHE_TIMEOUT)

    hits = []
    error = None
    incomplete = False
    proc = None

    with tempfile.TemporaryDirectory() as tmp:
        cmd = ["holehe", email, "--only-used", "--no-color", "--no-clear", "-C"]
        try:
            proc = subprocess.run(
                cmd, capture_output=True, text=True,
                timeout=timeout, check=False, cwd=tmp,
            )
            # Holehe a veces devuelve exit=1 aún con resultados; no lo marcamos
            # como error a menos que no produzca ningún CSV ni nada parseable.
            log.info("holehe(%s) exit=%s", email, proc.returncode)
        except FileNotFoundError:
            return {"hits": [], "error": "Holehe no instalado (pip install holehe)",
                    "incomplete": True, "raw_count": 0}
        except subprocess.TimeoutExpired:
            incomplete = True
            error = f"holehe cortado por timeout {timeout}s"
            log.warning("holehe(%s) timeout %ss", email, timeout)

        csvs = list(Path(tmp).glob("*.csv"))
        if csvs:
            for f in csvs:
                hits.extend(_read_holehe_csv(f))
        else:
            # Fallback: si no hubo CSV, parseamos stdout limpiando ANSI.
            if proc is not None:
                ansi = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
                for ln in (proc.stdout or "").splitlines():
                    clean = ansi.sub("", ln).strip()
                    if clean.startswith("[+]"):
                        token = clean[3:].strip().split()[0]
                        hits.append({"name": token, "url": None, "source_site": token})
            if not hits and not error:
                error = "holehe no escribió CSV ni encontró '[+]' en stdout"

    return {"hits": hits, "error": error, "incomplete": incomplete, "raw_count": len(hits)}


def _read_holehe_csv(path: Path):
    out = []
    try:
        with path.open("r", encoding="utf-8", errors="ignore", newline="") as fh:
            for row in csv.DictReader(fh):
                exists = (row.get("exists") or "").strip().lower()
                if exists not in ("true", "1", "yes"):
                    continue
                name = (row.get("name") or "").strip()
                domain = (row.get("domain") or "").strip()
                if not (name or domain):
                    continue
                out.append({
                    "name": name or domain,
                    "url": None,
                    "source_site": (domain or name).lower(),
                })
    except Exception as e:
        log.exception("error leyendo CSV de holehe %s", path)
        out.append({"_error": f"holehe csv: {e}"})
    return out


# --- Orquestación: discover -------------------------------------------------
def discover(usernames, emails):
    """Corre ambos escáneres, casa con recetas y puebla la DB.

    Resumen devuelto:
      found      -> filas registradas (incluye KEEP, que están como 'skipped')
      kept       -> subconjunto de `found` que está en KEEP_PLATFORMS
      no_recipe  -> slugs sin receta (sugerencia: crear receta)
      errors     -> [{source, id, error}, ...] - escáneres incompletos o que fallaron
      raw_counts -> {sherlock, holehe, sherlock_saved, holehe_saved}
    """
    db.init()
    recipes = load_recipes()
    summary = {
        "found": 0, "kept": 0, "no_recipe": [], "errors": [],
        "raw_counts": {"sherlock": 0, "holehe": 0,
                       "sherlock_saved": 0, "holehe_saved": 0},
    }

    def _register(hit, identifier, source):
        if hit.get("_error"):
            summary["errors"].append(
                {"source": source, "id": identifier, "error": hit["_error"]}
            )
            return False

        name = hit.get("name") or ""
        url = hit.get("url")
        source_site = hit.get("source_site") or _host_from_url(url) or name.lower()

        # Score de confianza (sherlock genera falsos positivos)
        if source == "holehe":
            confidence = _holehe_confidence()
        elif source == "sherlock":
            confidence = _sherlock_confidence(identifier, hit)
        else:
            confidence = "high"   # manual: el usuario lo metió a mano

        recipe_slug, recipe = _match_recipe(name, url, recipes)
        # Slug usado para 'platform' (encontrar receta + KEEP check):
        #  - Si hay receta, usamos su slug para que engine.get_recipe la encuentre.
        #  - Si no, derivamos del host (slug del source_site) y, como fallback,
        #    del nombre.
        host_slug = _slugify(source_site) or _slugify(name)
        platform = recipe_slug or host_slug or "unknown"

        is_keep = (platform in config.KEEP_PLATFORMS
                   or host_slug in config.KEEP_PLATFORMS)

        fields = dict(
            source=source,
            source_site=source_site,
            profile_url=url,
            display_name=(recipe.get("display_name") if recipe else None) or name,
            confidence=confidence,
        )
        if recipe:
            fields["deletion_type"] = recipe.get("deletion_type", "unknown")
            fields["difficulty"] = recipe.get("difficulty")
        else:
            fields["deletion_type"] = "unknown"

        if is_keep:
            fields["status"] = "skipped"
            fields["last_message"] = "conservada (profesional)"
            summary["kept"] += 1
        else:
            fields["status"] = "found"
            if not recipe and platform not in summary["no_recipe"]:
                summary["no_recipe"].append(platform)

        db.upsert_account(platform, identifier, **fields)
        summary["found"] += 1
        return True

    for u in usernames or []:
        if not _valid_username(u):
            summary["errors"].append(
                {"source": "sherlock", "id": u, "error": "username inválido"}
            )
            continue
        result = run_sherlock(u)
        summary["raw_counts"]["sherlock"] += result["raw_count"]
        if result["error"]:
            summary["errors"].append(
                {"source": "sherlock", "id": u, "error": result["error"]}
            )
        for h in result["hits"]:
            if _register(h, u, "sherlock"):
                summary["raw_counts"]["sherlock_saved"] += 1

    for e in emails or []:
        if not _valid_email(e):
            summary["errors"].append(
                {"source": "holehe", "id": e, "error": "email inválido"}
            )
            continue
        result = run_holehe(e)
        summary["raw_counts"]["holehe"] += result["raw_count"]
        if result["error"]:
            summary["errors"].append(
                {"source": "holehe", "id": e, "error": result["error"]}
            )
        for h in result["hits"]:
            if _register(h, e, "holehe"):
                summary["raw_counts"]["holehe_saved"] += 1

    rc = summary["raw_counts"]
    log.info(
        "discover: sherlock %s crudo -> %s guardado | holehe %s crudo -> %s guardado | errores %s",
        rc["sherlock"], rc["sherlock_saved"],
        rc["holehe"], rc["holehe_saved"],
        len(summary["errors"]),
    )
    print(
        f"[discover] sherlock {rc['sherlock']} crudo -> {rc['sherlock_saved']} guardado | "
        f"holehe {rc['holehe']} crudo -> {rc['holehe_saved']} guardado | "
        f"errores {len(summary['errors'])}"
    )
    return summary
