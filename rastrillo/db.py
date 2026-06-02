"""Persistencia en SQLite. Guarda el estado de cada cuenta para poder reanudar
aunque cierres la app a la mitad.

Estados:
  found        -> detectada por discovery, sin tocar
  queued       -> en cola para procesar
  in_progress  -> el motor está trabajando en ella ahora
  awaiting_user-> pausada esperando que resuelvas CAPTCHA/2FA/confirmación
  deleted      -> borrada (verificada)
  anonymized   -> no se pudo borrar; perfil anonimizado
  manual       -> requiere acción manual (soporte, etc.); marca el link
  failed       -> error; revisa el log
  skipped      -> tú decidiste saltarla (o está en KEEP_PLATFORMS)
"""
import sqlite3
import time
from contextlib import contextmanager
from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    platform TEXT NOT NULL,
    display_name TEXT,
    profile_url TEXT,
    identifier TEXT,            -- username o email con el que se detectó
    source TEXT,                -- sherlock | holehe | manual
    status TEXT NOT NULL DEFAULT 'found',
    deletion_type TEXT,         -- full | anonymize | manual | unknown
    difficulty TEXT,
    current_step INTEGER DEFAULT 0,
    last_message TEXT,
    updated_at REAL,
    UNIQUE(platform, identifier)
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id INTEGER,
    ts REAL,
    level TEXT,
    message TEXT
);
"""


@contextmanager
def connect():
    config.ensure_dirs()
    con = sqlite3.connect(config.DB_PATH)
    con.row_factory = sqlite3.Row
    try:
        yield con
        con.commit()
    finally:
        con.close()


def init():
    with connect() as con:
        con.executescript(SCHEMA)
        # Migración idempotente: añadimos recipe_hash si la DB es anterior al
        # cambio de resumibilidad. Detectar cambios en la receta entre runs
        # permite resetear current_step cuando los pasos ya no coinciden.
        cols = {r["name"] for r in con.execute("PRAGMA table_info(accounts)").fetchall()}
        if "recipe_hash" not in cols:
            con.execute("ALTER TABLE accounts ADD COLUMN recipe_hash TEXT")


def upsert_account(platform, identifier, **fields):
    """Inserta una cuenta detectada sin pisar su progreso si ya existe."""
    with connect() as con:
        cur = con.execute(
            "SELECT id FROM accounts WHERE platform=? AND identifier=?",
            (platform, identifier),
        )
        row = cur.fetchone()
        now = time.time()
        if row:
            return row["id"]
        cols = {"platform": platform, "identifier": identifier, "updated_at": now}
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
            return con.execute("SELECT * FROM accounts WHERE status=? ORDER BY platform", (status,)).fetchall()
        return con.execute("SELECT * FROM accounts ORDER BY status, platform").fetchall()


def stats():
    with connect() as con:
        rows = con.execute("SELECT status, COUNT(*) c FROM accounts GROUP BY status").fetchall()
        return {r["status"]: r["c"] for r in rows}
