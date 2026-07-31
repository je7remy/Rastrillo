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

Confianza y verificabilidad son DOS EJES DISTINTOS (Paso 2C, Entrega 1):

  `confidence`    -> ¿qué tan probable es que esta cuenta sea del usuario?
                     La deciden `discovery._register` y la corroboración de 2A.
  `verifiability` -> ¿la respuesta de ESE SITIO sirve para comprobar algo?
                     La decide el canario (`canario.py`) y NO dice nada sobre
                     la propiedad de la cuenta.

Meterlos en la misma columna encadenaba "inverificable -> low -> descartar ->
no es mía", que es afirmar algo sobre la propiedad a partir de una señal que
no habla de propiedad. Por eso `verifiability` es columna aparte y el canario
no toca `confidence` en ninguna dirección.

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
import json
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
# Columnas de `accounts` como DATO y no como texto SQL suelto. Es la ÚNICA
# fuente de verdad del esquema: de aquí salen tanto el CREATE TABLE inicial
# como la tabla temporal del rebuild de DBs legacy.
#
# Antes había dos listas: este CREATE y una copia literal dentro de
# `_REBUILD_WITHOUT_UNIQUE`. La copia se quedó atrás y dejó fuera `sent_at`,
# `deletion_eta` y `deletion_started_at`, así que migrar una DB legacy con
# UNIQUE PERDÍA esos tres campos en silencio. Con una sola lista esa deriva ya
# no puede ocurrir: si añades una columna aquí, el rebuild la conoce.
_ACCOUNTS_COLUMNS = [
    ("id", "INTEGER PRIMARY KEY AUTOINCREMENT", ""),
    ("platform", "TEXT NOT NULL", ""),
    ("display_name", "TEXT", ""),
    ("profile_url", "TEXT", ""),
    ("identifier", "TEXT", "username o email con el que se detectó"),
    ("source", "TEXT", "sherlock | holehe | manual"),
    ("source_site", "TEXT", "host/dominio real del sitio detectado"),
    ("status", "TEXT NOT NULL DEFAULT 'found'", ""),
    ("deletion_type", "TEXT", "full | anonymize | manual | unknown"),
    ("difficulty", "TEXT", ""),
    ("current_step", "INTEGER DEFAULT 0", ""),
    ("last_message", "TEXT", ""),
    ("updated_at", "REAL", ""),
    ("recipe_hash", "TEXT", ""),
    ("action_meta", "TEXT", "JSON con la Resolution del resolver (link, email…)"),
    ("confidence", "TEXT", "high | medium | low (sherlock genera falsos positivos)"),
    ("confidence_reasons", "TEXT", "JSON [{code, desc}] con el POR QUÉ de la confianza"),
    ("verifiability", "TEXT", "veredicto del canario: ¿se puede verificar este sitio?"),
    ("owned", "INTEGER DEFAULT 0", "1 = el usuario confirmó 'es mía'; 0 = sin confirmar"),
    ("sent_at", "REAL", "timestamp UNIX de cuándo se envió la solicitud GDPR"),
    ("deletion_eta", "REAL", "timestamp UNIX objetivo: cuándo se elimina según la plataforma"),
    ("deletion_started_at", "REAL", "timestamp UNIX de cuándo el usuario fijó el plazo"),
]

ACCOUNTS_COLUMN_NAMES = tuple(nombre for nombre, _, _ in _ACCOUNTS_COLUMNS)


def _ddl_accounts(tabla: str = "accounts", if_not_exists: bool = False) -> str:
    """CREATE TABLE de `accounts` con el nombre que se le pida.

    El rebuild necesita exactamente este esquema para la tabla temporal; que lo
    genere la misma función es lo que garantiza que no vuelva a divergir.
    """
    prefijo = "CREATE TABLE IF NOT EXISTS" if if_not_exists else "CREATE TABLE"
    lineas = [(f"    {nombre} {tipo}", comentario)
              for nombre, tipo, comentario in _ACCOUNTS_COLUMNS]
    ancho = max(len(l) for l, _ in lineas) + 1        # +1 por la coma
    partes = []
    for i, (linea, comentario) in enumerate(lineas):
        coma = "," if i < len(lineas) - 1 else ""
        texto = (linea + coma).ljust(ancho)
        partes.append((texto + (f"  -- {comentario}" if comentario else "")).rstrip())
    return f"{prefijo} {tabla} (\n" + "\n".join(partes) + "\n)"


SCHEMA_TABLES = _ddl_accounts("accounts", if_not_exists=True) + """;
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id INTEGER,
    ts REAL,
    level TEXT,
    message TEXT
);
-- Domain Intelligence: informe WHOIS+DNS+correlación por dominio. Guardamos
-- un solo informe por dominio (el último); `save_domain_report` borra el
-- previo antes de insertar. `report` es el JSON de domain_intel.analyze().
CREATE TABLE IF NOT EXISTS domain_reports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    domain TEXT NOT NULL,
    report TEXT,                -- JSON del analyze()
    created_at REAL
);
-- Paso 3: memoria de descartes. Guarda las decisiones de triage del usuario
-- ("esto no es mío") para que SOBREVIVAN a clear_accounts() y no haya que
-- volver a triar los mismos falsos positivos en cada escaneo.
--
-- La clave es el PAR (source_site, identifier), no el sitio suelto: que `mar`
-- no sea mío en un sitio no dice nada sobre `je7remy` en ese mismo sitio.
--
-- Solo tres datos: el par, cuándo lo decidí y por qué. Nada más. Esta tabla es
-- memoria de MIS decisiones, no un perfil.
CREATE TABLE IF NOT EXISTS discard_memory (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_site TEXT NOT NULL,
    identifier TEXT NOT NULL,
    reason TEXT,                -- motivo textual, si lo hubo
    created_at REAL
);
"""

SCHEMA_INDEXES = """
CREATE INDEX IF NOT EXISTS idx_accounts_lookup
    ON accounts(source_site, identifier);
CREATE INDEX IF NOT EXISTS idx_accounts_platform
    ON accounts(platform, identifier);
CREATE INDEX IF NOT EXISTS idx_domain_reports_domain
    ON domain_reports(domain);
-- UNIQUE de verdad aquí (a diferencia de `accounts`): una decisión por par.
-- Volver a descartar el mismo par refresca la fila, no crea otra.
CREATE UNIQUE INDEX IF NOT EXISTS idx_discard_memory_par
    ON discard_memory(source_site, identifier);
"""

def _tiene_unique_legacy(con) -> bool:
    """¿La tabla `accounts` arrastra el UNIQUE(platform, identifier) viejo?

    Se lee del DDL guardado en `sqlite_master`, que es donde SQLite conserva el
    CREATE TABLE literal con el que se creó.
    """
    fila = con.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='accounts'"
    ).fetchone()
    ddl = (fila["sql"] if fila else "") or ""
    return "UNIQUE" in ddl.upper() and "platform" in ddl.lower()


def _rebuild_sin_unique(con) -> None:
    """Recrea `accounts` sin el UNIQUE(platform, identifier) heredado, que
    colapsaba hallazgos de sitios distintos con el mismo slug de receta.

    Copia TODAS las columnas que la tabla vieja y el esquema actual tienen en
    común, calculadas en tiempo de ejecución. La versión anterior llevaba la
    lista escrita a mano y se había quedado atrás: dejaba fuera `sent_at`,
    `deletion_eta` y `deletion_started_at`, de modo que migrar una DB legacy
    con UNIQUE borraba en silencio los plazos de eliminación y la fecha de
    envío de las solicitudes GDPR. Con la intersección dinámica el problema no
    puede repetirse aunque el esquema siga creciendo.

    Corre ANTES de los ALTER TABLE de `init()`: la tabla nueva ya nace con el
    esquema completo, así que los ALTERs posteriores son no-ops sobre ella y
    rellenan solo lo que falte en el camino que no pasa por aquí.
    """
    actuales = {r["name"] for r in
                con.execute("PRAGMA table_info(accounts)").fetchall()}
    comunes = [c for c in ACCOUNTS_COLUMN_NAMES if c in actuales]
    perdidas = actuales - set(ACCOUNTS_COLUMN_NAMES)
    if perdidas:
        # No abortamos —la columna no la conoce el código— pero que se vea.
        _log.warning("rebuild de accounts: columnas desconocidas que no se "
                     "copian: %s", sorted(perdidas))
    lista = ", ".join(comunes)
    con.execute("DROP TABLE IF EXISTS accounts__new")
    con.execute(_ddl_accounts("accounts__new"))
    con.execute(f"INSERT INTO accounts__new ({lista}) "
                f"SELECT {lista} FROM accounts")
    con.execute("DROP TABLE accounts")
    con.execute("ALTER TABLE accounts__new RENAME TO accounts")
    _log.info("accounts recreada sin UNIQUE; columnas preservadas: %s",
              len(comunes))


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

        # 2) Si la tabla aún tiene UNIQUE(platform, identifier) heredada, la
        # recreamos sin esa constraint (colapsaba hallazgos distintos).
        #
        # Va ANTES de los ALTER TABLE del paso 3, y el orden importa: la tabla
        # nueva nace con el esquema completo, así que el paso 3 no tiene nada
        # que añadirle. Cuando el rebuild iba DESPUÉS, copiaba una lista de
        # columnas escrita a mano que se había quedado corta y tiraba
        # `sent_at`, `deletion_eta` y `deletion_started_at` — justo los campos
        # que los ALTERs acababan de crear. Ahora la copia es dinámica (ver
        # `_rebuild_sin_unique`), así que el orden es defensa en profundidad y
        # no la única barrera.
        if _tiene_unique_legacy(con):
            _rebuild_sin_unique(con)

        # 3) Migración idempotente de columnas que falten en DBs viejas.
        cols = {r["name"] for r in con.execute("PRAGMA table_info(accounts)").fetchall()}
        if "recipe_hash" not in cols:
            con.execute("ALTER TABLE accounts ADD COLUMN recipe_hash TEXT")
        if "source_site" not in cols:
            con.execute("ALTER TABLE accounts ADD COLUMN source_site TEXT")
        if "action_meta" not in cols:
            con.execute("ALTER TABLE accounts ADD COLUMN action_meta TEXT")
        if "confidence" not in cols:
            con.execute("ALTER TABLE accounts ADD COLUMN confidence TEXT")
        if "confidence_reasons" not in cols:
            con.execute("ALTER TABLE accounts ADD COLUMN confidence_reasons TEXT")
        if "owned" not in cols:
            con.execute("ALTER TABLE accounts ADD COLUMN owned INTEGER DEFAULT 0")
        if "sent_at" not in cols:
            con.execute("ALTER TABLE accounts ADD COLUMN sent_at REAL")
        if "deletion_eta" not in cols:
            con.execute("ALTER TABLE accounts ADD COLUMN deletion_eta REAL")
        if "deletion_started_at" not in cols:
            con.execute("ALTER TABLE accounts ADD COLUMN deletion_started_at REAL")
        if "verifiability" not in cols:
            con.execute("ALTER TABLE accounts ADD COLUMN verifiability TEXT")

        # 4) Índices al final: aquí ya estamos seguros de que source_site existe.
        con.executescript(SCHEMA_INDEXES)


# --- Verificabilidad (eje separado de la confianza) -------------------------
# Valores que puede tomar `accounts.verifiability`. El "no evaluado" es NULL a
# propósito: es lo que deja la migración en las filas viejas y lo que tiene una
# fila que el canario todavía no ha mirado (holehe/hibp nunca lo hacen, porque
# no traen `profile_url`). Un `indeterminado` NO es lo mismo: ahí sí probamos y
# no pudimos concluir.
VERIFICABILIDAD = ("indiscriminado", "discrimina", "indeterminado")
VERIFICABILIDAD_NO_EVALUADA = None


# --- Motivos de confianza ---------------------------------------------------
# Se serializan igual que `action_meta`: JSON en una columna TEXT, con
# `ensure_ascii=False` al escribir y `json.loads` tolerante al leer (una fila
# vieja o corrupta devuelve [] en vez de reventar la vista).
def parse_reasons(raw) -> list:
    """Lee la columna `confidence_reasons`. Nunca lanza: devuelve [] si no hay
    nada o si el JSON está corrupto."""
    if not raw:
        return []
    try:
        val = json.loads(raw)
    except (ValueError, TypeError):
        _log.warning("confidence_reasons corrupto; lo ignoro")
        return []
    return val if isinstance(val, list) else []


def dump_reasons(reasons) -> str:
    """Serializa la lista de motivos (mismo patrón que `action_meta`)."""
    return json.dumps(reasons or [], ensure_ascii=False)


def merge_reasons(previos, nuevos) -> list:
    """Une motivos sin repetir `code` y preservando el orden de aparición."""
    out = list(previos or [])
    vistos = {r.get("code") for r in out if isinstance(r, dict)}
    for r in nuevos or []:
        if not isinstance(r, dict):
            continue
        if r.get("code") in vistos:
            continue
        vistos.add(r.get("code"))
        out.append(r)
    return out


def upsert_account(platform, identifier, **fields):
    """Inserta una cuenta detectada sin pisar su progreso si ya existe.

    Unicidad:
      - Si `source_site` viene en fields → la unicidad es (source_site, identifier).
        Esto permite que dos hallazgos distintos que casen con la misma receta
        (p.ej. "Reddit" y "RedditGifts") convivan como filas separadas.
      - Si no viene source_site (inserts manuales / tests) → fallback al
        comportamiento previo (platform, identifier).

    Corroboración en el duplicado (Paso 2A): cuando la fila YA existe y el hit
    entrante viene de otra fuente, el upsert es un no-op y ese "lo han visto
    dos herramientas" se perdería. Aquí lo dejamos anotado en
    `confidence_reasons` antes de salir. Ojo: este caso solo puede ser
    sherlock+maigret, porque son las únicas dos fuentes que comparten tipo de
    identificador (username); holehe y hibp usan un email, así que caen en una
    FILA DISTINTA por construcción y su corroboración se calcula fuera (ver
    `discovery._corroborar_entre_fuentes`). Por eso aquí NO tocamos el tramo de
    confianza: dos buscadores de username con catálogos que se solapan no son
    señal independiente.
    """
    source_site = fields.get("source_site")
    with connect() as con:
        if source_site:
            cur = con.execute(
                "SELECT * FROM accounts WHERE source_site=? AND identifier=?",
                (source_site, identifier),
            )
        else:
            cur = con.execute(
                "SELECT * FROM accounts WHERE platform=? AND identifier=?",
                (platform, identifier),
            )
        row = cur.fetchone()
        if row:
            prev_source = (row["source"] or "").strip()
            new_source = (fields.get("source") or "").strip()
            if prev_source and new_source and prev_source != new_source:
                fuentes = " + ".join(sorted({prev_source, new_source}))
                reasons = merge_reasons(parse_reasons(row["confidence_reasons"]), [
                    {"code": "corrob_misma_fila",
                     "desc": f"visto por {fuentes} (mismo username)"},
                ])
                con.execute(
                    "UPDATE accounts SET confidence_reasons=? WHERE id=?",
                    (dump_reasons(reasons), row["id"]),
                )
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


def backfill_profile_url(account_id, url) -> bool:
    """Rellena `profile_url` SOLO si la fila todavía no tiene ninguna.

    `profile_url` es la URL DEL HIT: la que produjo el descubrimiento y sobre
    la que `discovery._identificador_en_url` calculó los motivos de confianza.
    El triage la enseña etiquetada como "perfil", así que pisarla con la URL de
    BORRADO que devuelve el resolver hacía que los chips ("coincide en la
    ruta") describieran una URL distinta de la mostrada — a ojo, parecía que
    el chip mentía. La URL de borrado ya viaja en `action_meta.url` y la UI la
    saca de ahí como enlace aparte.

    El backfill sí es útil: holehe y hibp nunca traen URL, y para esas filas la
    del resolver es la única que hay. Devuelve True si escribió.
    """
    if not url:
        return False
    row = get_account(account_id)
    if row is not None and (row["profile_url"] or "").strip():
        return False
    update_account(account_id, profile_url=url)
    return True


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
    ahora = time.time()
    ts = time.strftime("%Y%m%d-%H%M%S", time.gmtime(ahora)) + f"-{int(ahora % 1 * 1_000_000):06d}"
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
      - el perfil persistente de Chromium,
      - **la memoria de descartes** (tabla `discard_memory`).

    Lo último es el punto entero del Paso 3: la decisión "esto no es mío" la
    tomó el usuario y no es un hallazgo del escáner, así que no se va con los
    hallazgos. Si se fuera, cada limpieza obligaría a re-triar los mismos
    falsos positivos (Periscope, HudsonRock…). Para olvidarla hay dos caminos
    explícitos: deshacer una decisión concreta (`forget_discard`) o vaciar la
    memoria entera (`clear_discard_memory`).

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


# --- Memoria de descartes (Paso 3) ------------------------------------------
# El usuario tria un hallazgo como "no es mía" y esa decisión se guarda aquí,
# fuera de `accounts`, para que sobreviva a `clear_accounts()`. En el siguiente
# escaneo `discovery._register` la consulta y el hallazgo entra ya descartado,
# con un motivo que lo dice.
#
# Nada escribe aquí por inferencia: solo lo hacen los dos endpoints que el
# usuario pulsa a mano (triage individual y "Descartar low"). Ver server.py.

def _clave_descarte(source_site, identifier):
    """Normaliza el par (sitio, identificador) para casar entre escaneos.

    Ambos a minúsculas y sin espacios: el host es case-insensitive por
    definición y los usernames lo son en la práctica en casi todos los sitios,
    así que `Mar` y `mar` son la misma decisión. Es una normalización que solo
    colapsa variantes de la MISMA cadena; no amplía el alcance a otros
    identificadores. Devuelve None si falta cualquiera de los dos: sin par
    completo no hay decisión que recordar.
    """
    site = (source_site or "").strip().lower()
    ident = (identifier or "").strip().lower()
    if not site or not ident:
        return None
    return site, ident


def remember_discard(source_site, identifier, reason=None) -> bool:
    """Anota que el usuario descartó este par. Idempotente: repetirlo refresca
    fecha y motivo en vez de duplicar la fila.

    Devuelve True si se guardó, False si el par estaba incompleto (sin
    `source_site` o sin `identifier` no hay nada que recordar; pasa con
    inserts manuales de tests y con filas viejas sin host).
    """
    clave = _clave_descarte(source_site, identifier)
    if clave is None:
        return False
    site, ident = clave
    with connect() as con:
        con.execute(
            "INSERT INTO discard_memory (source_site, identifier, reason, created_at) "
            "VALUES (?,?,?,?) "
            "ON CONFLICT(source_site, identifier) DO UPDATE SET "
            "reason=excluded.reason, created_at=excluded.created_at",
            (site, ident, (reason or None), time.time()),
        )
    return True


def forget_discard(source_site, identifier) -> bool:
    """Deshace una decisión: borra la entrada del par. Devuelve True si existía.

    Es la mitad obligatoria de la memoria. Sin deshacer, un clic equivocado en
    "No es mía" sería permanente y ningún reescaneo lo arreglaría.
    """
    clave = _clave_descarte(source_site, identifier)
    if clave is None:
        return False
    with connect() as con:
        cur = con.execute(
            "DELETE FROM discard_memory WHERE source_site=? AND identifier=?", clave
        )
        return cur.rowcount > 0


def get_discard(source_site, identifier):
    """Fila de la decisión previa para este par, o None si no la hay."""
    clave = _clave_descarte(source_site, identifier)
    if clave is None:
        return None
    with connect() as con:
        return con.execute(
            "SELECT * FROM discard_memory WHERE source_site=? AND identifier=?", clave
        ).fetchone()


def list_discards():
    """Todas las decisiones recordadas, recientes primero."""
    with connect() as con:
        return con.execute(
            "SELECT * FROM discard_memory ORDER BY created_at DESC"
        ).fetchall()


def count_discards_by_site() -> dict:
    """{source_site: nº de identificadores DISTINTOS descartados en ese sitio}.

    Base de la señal agregada del Paso 3 / Entrega 2. Cuenta identificadores
    distintos y no filas porque el UNIQUE ya garantiza una fila por par: dos
    descartes del mismo identificador siguen siendo UNA decisión sobre UN
    identificador, y eso no es señal de que el sitio genere ruido.

    Es puramente INFORMATIVA: nadie la usa para mover `confidence` ni para
    disparar acciones (ver el comentario de `server._discard_site_counts`).
    """
    with connect() as con:
        rows = con.execute(
            "SELECT source_site, COUNT(DISTINCT identifier) c "
            "FROM discard_memory GROUP BY source_site"
        ).fetchall()
        return {r["source_site"]: r["c"] for r in rows}


def clear_discard_memory() -> int:
    """Vacía la memoria de descartes. Devuelve cuántas decisiones borró.

    No lo llama `clear_accounts` (ver su docstring). Existe para el reset total
    y para que el usuario pueda empezar de cero su triage a propósito. Snapshot
    antes de borrar, igual que los otros borrados masivos del repo.
    """
    try:
        snapshot_db()
    except Exception as e:
        _log.warning("snapshot DB falló (sigo con clear_discard_memory): %s", e)
    with connect() as con:
        n = con.execute("SELECT COUNT(*) c FROM discard_memory").fetchone()["c"]
        con.execute("DELETE FROM discard_memory")
        con.execute("DELETE FROM sqlite_sequence WHERE name='discard_memory'")
        return n


# --- Domain Intelligence ----------------------------------------------------
def save_domain_report(domain: str, report_json: str) -> int:
    """Guarda (o reemplaza) el informe de un dominio. Un informe por dominio:
    borramos el previo antes de insertar para no acumular histórico."""
    domain = (domain or "").strip().lower()
    with connect() as con:
        con.execute("DELETE FROM domain_reports WHERE domain=?", (domain,))
        cur = con.execute(
            "INSERT INTO domain_reports (domain, report, created_at) VALUES (?,?,?)",
            (domain, report_json, time.time()),
        )
        return cur.lastrowid


def get_domain_report(domain: str):
    """Devuelve la fila del informe más reciente de `domain`, o None."""
    domain = (domain or "").strip().lower()
    with connect() as con:
        return con.execute(
            "SELECT * FROM domain_reports WHERE domain=? "
            "ORDER BY created_at DESC LIMIT 1",
            (domain,),
        ).fetchone()


def list_domain_reports():
    """Lista (domain, created_at) de todos los informes guardados, recientes
    primero. La UI lo usa para ofrecer un histórico breve."""
    with connect() as con:
        return con.execute(
            "SELECT domain, created_at FROM domain_reports ORDER BY created_at DESC"
        ).fetchall()


def delete_domain_report(domain: str) -> bool:
    """Borra el informe de UN dominio. Devuelve True si existía (y se borró),
    False si no había nada que borrar.

    El caller (endpoint) usa ese booleano para distinguir 404 de éxito. No
    hacemos snapshot aquí: es un borrado puntual de un dato re-generable
    (volver a analizar el dominio lo reconstruye), no una purga masiva.
    """
    domain = (domain or "").strip().lower()
    with connect() as con:
        cur = con.execute("DELETE FROM domain_reports WHERE domain=?", (domain,))
        return cur.rowcount > 0


def clear_domain_reports() -> int:
    """Vacía el histórico de Domain Intelligence. Devuelve cuántas filas borró.

    Antes de tocar nada hace un snapshot en ~/.rastrillo/backups/, igual que
    `clear_accounts`: es el patrón del repo para cualquier operación que
    destruye datos locales en bloque. No toca la tabla de cuentas.
    """
    try:
        snapshot_db()
    except Exception as e:
        # Mismo criterio que clear_accounts: un fallo de copia (disco lleno,
        # permisos) no aborta la operación; queda en el log.
        _log.warning("snapshot DB falló (sigo con clear_domain_reports): %s", e)
    with connect() as con:
        n = con.execute("SELECT COUNT(*) c FROM domain_reports").fetchone()["c"]
        con.execute("DELETE FROM domain_reports")
        # Reseteamos el AUTOINCREMENT para que los próximos IDs empiecen en 1.
        con.execute("DELETE FROM sqlite_sequence WHERE name='domain_reports'")
        return n
