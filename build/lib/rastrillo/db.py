"""Persistencia en SQLite. Guarda el estado de cada cuenta para poder reanudar
aunque cierres la app a la mitad.

Estados:
  found        -> detectada por discovery, sin tocar
  queued       -> en cola para procesar
  in_progress  -> el motor está trabajando en ella ahora
  awaiting_user-> pausada esperando que resuelvas CAPTCHA/2FA/confirmación
  deleted      -> borrada (verificada)
  anonymized   -> no se pudo borrar; perfil anonimizado
  user_done    -> el usuario marcó como tramitada (mark-sent)
  semi_auto    -> link directo, 1 clic del usuario
  email_draft  -> borrador GDPR listo para enviar
  manual       -> requiere acción manual; revisa el log
  failed       -> error; revisa el log
  skipped      -> tú decidiste saltarla (o está en KEEP_PLATFORMS)
  not_mine     -> el usuario descartó como falso positivo (triage)
  dry_run      -> simulación: se mostró el plan pero no se ejecutó

Identidad de una cuenta:
  - `platform` es un slug interno usado para casar con recetas (engine.get_recipe).
  - `source_site` es el host/dominio real que reportó el escáner (e.g.
    "old.reddit.com", "spotify.com"). Es lo que distingue dos hallazgos
    distintos que casaron con la misma receta — antes UNIQUE(platform,
    identifier) los colapsaba en una sola fila, ahora no.
  La unicidad la maneja `upsert_account` por (source_site, identifier) cuando
  hay source_site; fallback a (platform, identifier) para inserts manuales sin
  source_site.
"""
import logging
import shutil
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Optional
from . import config

_log = logging.getLogger("rastrillo.db")

# SQL: la tabla `accounts` NO lleva UNIQUE constraint sobre (platform, identifier)
# para no colapsar sitios distintos que comparten slug de receta. La unicidad
# vive en `upsert_account` y usa `source_site` cuando está disponible.
#
# Importante: los índices van SEPARADOS del CREATE TABLE. Si una DB vieja ya
# existe sin `source_site`, el CREATE INDEX ON accounts(source_site, ...)
# explotaría antes de que la migración (ALTER TABLE ADD COLUMN) tenga ocasión
# de añadir la columna. Por eso primero creamos/migramos la tabla y al final
# aplicamos los índices.
SCHEMA_TABLES = """
CREATE TABLE IF NOT EXISTS accounts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    platform TEXT NOT NULL,
    display_name TEXT,
    profile_url TEXT,
    identifier TEXT,            -- username o email con el que se detectó
    source TEXT,                -- sherlock | holehe | manual
    source_site TEXT,           -- host/dominio real del sitio detectado
    status TEXT NOT NULL DEFAULT 'found',
    deletion_type TEXT,         -- full | anonymize | manual | unknown
    difficulty TEXT,
    current_step INTEGER DEFAULT 0,
    last_message TEXT,
    updated_at REAL,
    recipe_hash TEXT,
    action_meta TEXT,           -- JSON con la Resolution del resolver (link, email, etc.)
    confidence TEXT,            -- high | medium | low (sherlock genera falsos positivos)
    owned INTEGER DEFAULT 0,    -- 1 = el usuario confirmó "es mía"; 0 = sin confirmar
    sent_at REAL                -- timestamp UNIX de cuándo se envió la solicitud GDPR
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id INTEGER,
    ts REAL,
    level TEXT,
    message TEXT
);
"""

SCHEMA_INDEXES = """
CREATE INDEX IF NOT EXISTS idx_accounts_lookup
    ON accounts(source_site, identifier);
CREATE INDEX IF NOT EXISTS idx_accounts_platform
    ON accounts(platform, identifier);
"""

# Migración para DB anteriores que tenían UNIQUE(platform, identifier): se
# detecta leyendo `sqlite_master.sql` y, si está, recreamos la tabla sin
# UNIQUE preservando todas las filas existentes.
_REBUILD_WITHOUT_UNIQUE = """
CREATE TABLE accounts__new (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    platform TEXT NOT NULL,
    display_name TEXT,
    profile_url TEXT,
    identifier TEXT,
    source TEXT,
    source_site TEXT,
    status TEXT NOT NULL DEFAULT 'found',
    deletion_type TEXT,
    difficulty TEXT,
    current_step INTEGER DEFAULT 0,
    last_message TEXT,
    updated_at REAL,
    recipe_hash TEXT,
    action_meta TEXT,
    confidence TEXT,
    owned INTEGER DEFAULT 0
);
INSERT INTO accounts__new
    (id, platform, display_name, profile_url, identifier, source, source_site,
     status, deletion_type, difficulty, current_step, last_message,
     updated_at, recipe_hash, action_meta, confidence, owned)
SELECT id, platform, display_name, profile_url, identifier, source, source_site,
       status, deletion_type, difficulty, current_step, last_message,
       updated_at, recipe_hash, action_meta, confidence, owned
FROM accounts;
DROP TABLE accounts;
ALTER TABLE accounts__new RENAME TO accounts;
"""


@contextmanager
def connect():
    config.ensure_dirs()
    # timeout=30: el resolver tiene un ThreadPoolExecutor (varios hilos
    # escribiendo a la vez en `discovered.json` y, vía jobs, en la DB).
    # Sin timeout, SQLite levanta "database is locked" al primer
    # solapamiento; con 30 s damos margen para que el lock libere.
    con = sqlite3.connect(config.DB_PATH, timeout=30)
    con.row_factory = sqlite3.Row
    # PRAGMA synchronous es POR CONEXIÓN (no persiste como journal_mode):
    # aplicarlo solo en init() no surte efecto a runtime. Lo seteamos aquí
    # para que cada conexión use NORMAL (durabilidad razonable con WAL
    # activo) en vez del FULL por defecto.
    try:
        con.execute("PRAGMA synchronous=NORMAL")
    except sqlite3.Error:
        # Si el pragma falla por algún motivo (DB corrupta, etc.) seguimos:
        # peor caso es durabilidad FULL, no rompemos nada.
        pass
    try:
        yield con
        con.commit()
    finally:
        con.close()


def init():
    with connect() as con:
        # 0) PRAGMAs de durabilidad/concurrencia. journal_mode=WAL es
        # PERSISTENTE por archivo de DB (sobrevive a reconexiones), así que
        # ejecutarlo en init() basta. synchronous=NORMAL es por conexión,
        # pero combinado con WAL da la durabilidad razonable para nuestro
        # uso (writes frecuentes desde el pool del resolver) sin sufrir
        # bloqueos de fsync agresivos. Crea ficheros -wal y -shm junto
        # al .db; snapshot_db() los consolida con wal_checkpoint(TRUNCATE)
        # antes de copiar para no llevarse un .db a medias.
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA synchronous=NORMAL")

        # 1) Tablas (CREATE IF NOT EXISTS): respeta DBs viejas sin tocarlas.
        con.executescript(SCHEMA_TABLES)

        # 2) Migración idempotente de columnas que falten en DBs viejas.
        cols = {r["name"] for r in con.execute("PRAGMA table_info(accounts)").fetchall()}
        if "recipe_hash" not in cols:
            con.execute("ALTER TABLE accounts ADD COLUMN recipe_hash TEXT")
        if "source_site" not in cols:
            con.execute("ALTER TABLE accounts ADD COLUMN source_site TEXT")
        if "action_meta" not in cols:
            con.execute("ALTER TABLE accounts ADD COLUMN action_meta TEXT")
        if "confidence" not in cols:
            con.execute("ALTER TABLE accounts ADD COLUMN confidence TEXT")
        if "owned" not in cols:
            con.execute("ALTER TABLE accounts ADD COLUMN owned INTEGER DEFAULT 0")
        if "sent_at" not in cols:
            con.execute("ALTER TABLE accounts ADD COLUMN sent_at REAL")

        # 3) Si la tabla aún tiene UNIQUE(platform, identifier) heredada, la
        # recreamos sin esa constraint (causaba colapso de hallazgos).
        ddl_row = con.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='accounts'"
        ).fetchone()
        ddl = (ddl_row["sql"] if ddl_row else "") or ""
        if "UNIQUE" in ddl.upper() and "platform" in ddl.lower():
            con.executescript(_REBUILD_WITHOUT_UNIQUE)

        # 4) Índices al final: aquí ya estamos seguros de que source_site existe.
        con.executescript(SCHEMA_INDEXES)


def upsert_account(platform, identifier, **fields):
    """Inserta una cuenta detectada sin pisar su progreso si ya existe.

    Unicidad:
      - Si `source_site` viene en fields → la unicidad es (source_site, identifier).
        Esto permite que dos hallazgos distintos que casen con la misma receta
        (p.ej. "Reddit" y "RedditGifts") convivan como filas separadas.
      - Si no viene source_site (inserts manuales / tests) → fallback al
        comportamiento previo (platform, identifier).
    """
    source_site = fields.get("source_site")
    with connect() as con:
        if source_site:
            cur = con.execute(
                "SELECT id FROM accounts WHERE source_site=? AND identifier=?",
                (source_site, identifier),
            )
        else:
            cur = con.execute(
                "SELECT id FROM accounts WHERE platform=? AND identifier=?",
                (platform, identifier),
            )
        row = cur.fetchone()
        if row:
            return row["id"]
        cols = {"platform": platform, "identifier": identifier, "updated_at": time.time()}
        cols.update(fields)
        keys = ",".join(cols)
        qs = ",".join("?" * len(cols))
        cur = con.execute(f"INSERT INTO accounts ({keys}) VALUES ({qs})", tuple(cols.values()))
        return cur.lastrowid


def update_account(account_id, **fields):
    if not fields:
        return
    fields["updated_at"] = time.time()
    sets = ",".join(f"{k}=?" for k in fields)
    with connect() as con:
        con.execute(f"UPDATE accounts SET {sets} WHERE id=?", (*fields.values(), account_id))


def set_status(account_id, status, message=None):
    update_account(account_id, status=status, last_message=message)
    log(account_id, "info", f"status -> {status}" + (f": {message}" if message else ""))


def log(account_id, level, message):
    with connect() as con:
        con.execute(
            "INSERT INTO events (account_id, ts, level, message) VALUES (?,?,?,?)",
            (account_id, time.time(), level, message),
        )


def get_account(account_id):
    with connect() as con:
        return con.execute("SELECT * FROM accounts WHERE id=?", (account_id,)).fetchone()


def list_accounts(status=None):
    with connect() as con:
        if status:
            return con.execute(
                "SELECT * FROM accounts WHERE status=? ORDER BY platform",
                (status,),
            ).fetchall()
        return con.execute(
            "SELECT * FROM accounts ORDER BY status, platform"
        ).fetchall()


def stats():
    with connect() as con:
        rows = con.execute(
            "SELECT status, COUNT(*) c FROM accounts GROUP BY status"
        ).fetchall()
        return {r["status"]: r["c"] for r in rows}


def snapshot_db() -> Optional[Path]:
    """Tier 3.2: copia rastrillo.db a ~/.rastrillo/backups/rastrillo_<ts>.db
    antes de un borrado masivo. Devuelve la ruta del snapshot o None si la
    DB todavía no existía (nada que respaldar).

    Los backups NO se borran automáticamente: el usuario decide cuándo limpiar
    la carpeta. Esto es la red de seguridad por si `clear_accounts` se
    dispara por error.
    """
    if not config.DB_PATH.exists():
        return None
    backup_dir = config.BASE_DIR / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    ts = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
    target = backup_dir / f"rastrillo_{ts}.db"
    # Con WAL activo, los writes recientes viven en `<db>-wal` y solo se
    # consolidan al rotar el log. Copiar el .db a pelo dejaría fuera esos
    # cambios. wal_checkpoint(TRUNCATE) fuerza el merge y vacía el WAL.
    try:
        with connect() as con:
            con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    except sqlite3.Error as e:
        # No abortamos: si la consolidación falla seguimos copiando lo que
        # haya; mejor un backup parcial que ninguno.
        _log.warning("wal_checkpoint falló antes del snapshot: %s", e)
    # copy2 preserva metadatos y respeta locks de SQLite mejor que copyfile
    # en Windows; aún así, idealmente nadie está escribiendo en este momento.
    shutil.copy2(config.DB_PATH, target)
    _log.info("snapshot DB -> %s", target)
    return target


def clear_accounts():
    """Vacía la tabla de cuentas y su historial de eventos.

    Pensado para el botón "Limpiar todo" del dashboard: el usuario quiere
    re-escanear desde cero sin acumular hallazgos previos. No toca:
      - el directorio cacheado (~/.rastrillo/directory.json),
      - los hallazgos del resolver (~/.rastrillo/discovered.json),
      - el perfil persistente de Chromium.

    Antes de tocar nada, hace un snapshot en ~/.rastrillo/backups/.
    """
    try:
        snapshot_db()
    except Exception as e:
        # No abortamos por un fallo de copia (disco lleno, permisos, etc.);
        # solo lo dejamos en log. La operación destructiva sigue.
        _log.warning("snapshot DB falló (sigo con clear): %s", e)
    with connect() as con:
        con.execute("DELETE FROM events")
        con.execute("DELETE FROM accounts")
        # Reseteamos el AUTOINCREMENT para que los próximos IDs empiecen en 1.
        con.execute(
            "DELETE FROM sqlite_sequence WHERE name IN ('accounts','events')"
        )
