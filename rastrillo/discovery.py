"""Descubrimiento de cuentas. Envuelve Sherlock (por username) y Holehe (por email).

Ambos se ejecutan como subprocesos para no acoplarnos a sus APIs internas (que
cambian seguido). Parseamos sus *salidas CSV* (no el texto humano) para ser
robustos frente a colores ANSI, traducciones y cambios de formato.

API pública:
  run_sherlock(username) -> list[dict(name, slug_guess, url)]
  run_holehe(email)      -> list[dict(name, slug_guess, url)]
  discover(usernames, emails) -> dict(found, kept, no_recipe, errors)
"""
import csv
import re
import subprocess
import tempfile
from pathlib import Path

from . import db, config
from .recipes import load_recipes


# --- Validación de input ----------------------------------------------------
# Permisivos pero blindados contra typos vacíos y metacaracteres de shell.
_USERNAME_RE = re.compile(r"^[A-Za-z0-9._\-]{1,64}$")
_EMAIL_RE = re.compile(r"^[^\s@]{1,64}@[^\s@]{1,255}\.[^\s@]{1,64}$")


def _valid_username(u: str) -> bool:
    return bool(u) and bool(_USERNAME_RE.match(u))


def _valid_email(e: str) -> bool:
    return bool(e) and bool(_EMAIL_RE.match(e))


# --- Slugify y matching de recetas -----------------------------------------
def _slugify(name: str) -> str:
    """Normaliza 'Reddit', 'old.reddit.com' o una URL a 'reddit'."""
    s = (name or "").strip().lower()
    if "//" in s:
        s = s.split("//", 1)[1]
    s = s.split("/", 1)[0]                       # solo host
    for prefix in ("www.", "m.", "old.", "new."):
        if s.startswith(prefix):
            s = s[len(prefix):]
    s = s.split(".", 1)[0]                       # primer token del host
    return re.sub(r"[^a-z0-9]+", "", s)


def _match_recipe(name, url, recipes):
    """Devuelve (slug, recipe) o (None, None).

    Prioridad:
      1) Match exacto de slug.
      2) Match contra `aliases` declarados en la receta.
      3) Heurística suave (substring contra slug y display_name).
    """
    candidates = [c for c in (_slugify(name), _slugify(url)) if c]
    if not candidates:
        return None, None

    # 1) exacto
    for c in candidates:
        if c in recipes:
            return c, recipes[c]

    # 2) aliases declarados
    for slug, r in recipes.items():
        aliases = {_slugify(a) for a in r.get("aliases", []) if a}
        if any(c in aliases for c in candidates):
            return slug, r

    # 3) heurística
    for slug, r in recipes.items():
        dn = _slugify(r.get("display_name", ""))
        for c in candidates:
            if slug in c or c in slug or (dn and (dn in c or c in dn)):
                return slug, r
    return None, None


# --- Sherlock ---------------------------------------------------------------
def run_sherlock(username, timeout=180):
    """Devuelve lista de hits para `username` parseando el CSV de Sherlock.

    Sherlock 0.16 con `--csv --folderoutput DIR` escribe `<username>.csv`
    con columnas: username, name, url_main, url_user, exists, http_status,
    response_time_s. Filtramos `exists == "Claimed"`.

    Si el subproceso revienta o caduca, devolvemos lo que haya alcanzado a
    escribir (lectura del CSV parcial) en vez de tragarnos todo en silencio.
    """
    if not _valid_username(username):
        raise ValueError(f"username inválido: {username!r}")

    hits = []
    with tempfile.TemporaryDirectory() as tmp:
        cmd = [
            "sherlock", username,
            "--no-color", "--no-txt", "--csv",
            "--folderoutput", tmp,
            "--timeout", "10",
            "--print-found",
        ]
        try:
            subprocess.run(
                cmd, capture_output=True, text=True,
                timeout=timeout, check=False,
            )
        except FileNotFoundError:
            raise RuntimeError(
                "Sherlock no está instalado o no está en PATH. "
                "Instálalo con `pip install sherlock-project` dentro del venv."
            )
        except subprocess.TimeoutExpired:
            # No es fatal: leemos lo que haya alcanzado a escribir.
            pass

        for f in Path(tmp).glob("*.csv"):
            hits.extend(_read_sherlock_csv(f))
    return hits


def _read_sherlock_csv(path: Path):
    out = []
    try:
        with path.open("r", encoding="utf-8", errors="ignore", newline="") as fh:
            for row in csv.DictReader(fh):
                if (row.get("exists") or "").strip().lower() != "claimed":
                    continue
                name = (row.get("name") or "").strip()
                url = (row.get("url_user") or row.get("url_main") or "").strip()
                out.append({
                    "name": name,
                    "slug_guess": _slugify(name) or _slugify(url),
                    "url": url or None,
                })
    except Exception as e:
        # Marcamos el error como un "hit" especial para que el caller lo registre.
        out.append({"name": "_error", "slug_guess": "_error", "url": f"sherlock csv: {e}"})
    return out


# --- Holehe -----------------------------------------------------------------
def run_holehe(email, timeout=180):
    """Devuelve lista de hits para `email`.

    Holehe con `-C` (CSV) escribe un archivo `<email>.csv` en el CWD.
    Lo aislamos con `cwd=tempdir` para limpiar después y para que no choque
    con otros runs en paralelo.
    """
    if not _valid_email(email):
        raise ValueError(f"email inválido: {email!r}")

    hits = []
    with tempfile.TemporaryDirectory() as tmp:
        cmd = [
            "holehe", email,
            "--only-used", "--no-color", "--no-clear", "-C",
        ]
        try:
            subprocess.run(
                cmd, capture_output=True, text=True,
                timeout=timeout, check=False, cwd=tmp,
            )
        except FileNotFoundError:
            raise RuntimeError(
                "Holehe no está instalado o no está en PATH. "
                "Instálalo con `pip install holehe` dentro del venv."
            )
        except subprocess.TimeoutExpired:
            pass

        for f in Path(tmp).glob("*.csv"):
            hits.extend(_read_holehe_csv(f))
    return hits


def _read_holehe_csv(path: Path):
    out = []
    try:
        with path.open("r", encoding="utf-8", errors="ignore", newline="") as fh:
            for row in csv.DictReader(fh):
                # 'exists' es bool textual. Algunas versiones usan otras claves;
                # somos permisivos por si Holehe cambia las columnas.
                exists = (row.get("exists") or "").strip().lower()
                if exists not in ("true", "1", "yes"):
                    continue
                name = (row.get("name") or row.get("domain") or "").strip()
                if not name:
                    continue
                out.append({
                    "name": name,
                    "slug_guess": _slugify(name),
                    "url": None,
                })
    except Exception as e:
        out.append({"name": "_error", "slug_guess": "_error", "url": f"holehe csv: {e}"})
    return out


# --- Orquestación: discover -------------------------------------------------
def discover(usernames, emails):
    """Corre ambos escáneres, casa con recetas y puebla la DB.

    Resumen devuelto:
      found      -> nº de cuentas registradas (excluidas las KEEP)
      kept       -> nº de cuentas conservadas por estar en KEEP_PLATFORMS
      no_recipe  -> lista de slugs sin receta (sugerencia: crear receta)
      errors     -> [{source, id, error}, ...] - sitios/escáneres que fallaron
    """
    db.init()
    recipes = load_recipes()
    summary = {"found": 0, "kept": 0, "no_recipe": [], "errors": []}

    def _register(hit, identifier, source):
        if hit.get("name") == "_error":
            summary["errors"].append(
                {"source": source, "id": identifier, "error": hit.get("url")}
            )
            return
        slug, recipe = _match_recipe(hit.get("name"), hit.get("url"), recipes)
        platform = slug or hit.get("slug_guess") or "unknown"
        if platform in config.KEEP_PLATFORMS:
            summary["kept"] += 1
            db.upsert_account(
                platform, identifier,
                source=source, profile_url=hit.get("url"),
                status="skipped", last_message="conservada (profesional)",
            )
            return
        fields = dict(source=source, profile_url=hit.get("url"), status="found")
        if recipe:
            fields.update(
                display_name=recipe.get("display_name"),
                deletion_type=recipe.get("deletion_type", "unknown"),
                difficulty=recipe.get("difficulty"),
            )
        else:
            fields.update(deletion_type="unknown", display_name=hit.get("name"))
            if platform not in summary["no_recipe"]:
                summary["no_recipe"].append(platform)
        db.upsert_account(platform, identifier, **fields)
        summary["found"] += 1

    for u in usernames or []:
        if not _valid_username(u):
            summary["errors"].append(
                {"source": "sherlock", "id": u, "error": "username inválido"}
            )
            continue
        try:
            for h in run_sherlock(u):
                _register(h, u, "sherlock")
        except Exception as e:
            summary["errors"].append({"source": "sherlock", "id": u, "error": str(e)})

    for e in emails or []:
        if not _valid_email(e):
            summary["errors"].append(
                {"source": "holehe", "id": e, "error": "email inválido"}
            )
            continue
        try:
            for h in run_holehe(e):
                _register(h, e, "holehe")
        except Exception as exc:
            summary["errors"].append({"source": "holehe", "id": e, "error": str(exc)})

    return summary
