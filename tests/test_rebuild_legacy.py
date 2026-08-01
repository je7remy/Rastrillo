"""Paso 2C, Entrega 4: el rebuild de DBs legacy no puede perder columnas.

Las DBs anteriores llevaban `UNIQUE(platform, identifier)`, que colapsaba dos
hallazgos de sitios distintos con el mismo slug de receta (Reddit y
RedditGifts). `db.init()` las detecta y recrea la tabla sin la constraint.

El fallo: el rebuild copiaba una lista de columnas escrita a mano que se había
quedado atrás respecto al esquema. Faltaban `sent_at`, `deletion_eta` y
`deletion_started_at`, así que migrar una DB legacy con UNIQUE borraba en
silencio la fecha de envío de las solicitudes GDPR y los plazos de eliminación
en curso. Pérdida de datos, no un detalle cosmético.

Dos arreglos, y el segundo es el que cierra la clase entera de bug:
  1. El rebuild corre ANTES de los ALTER TABLE, no después.
  2. La lista de columnas se calcula en tiempo de ejecución (intersección de lo
     que hay con el esquema actual) y el esquema tiene una sola definición
     (`db._ACCOUNTS_COLUMNS`), así que no puede volver a divergir.

Esta entrega es independiente de las otras tres del paso 2C.
"""
import sqlite3

from .helpers import IsolatedTestCase


# DDL de una DB legacy: CON el UNIQUE y SIN las columnas que se añadieron
# después. Es el estado real del que hay que migrar.
_LEGACY_CON_UNIQUE = """
CREATE TABLE accounts (
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
    confidence_reasons TEXT,
    owned INTEGER DEFAULT 0,
    sent_at REAL,
    deletion_eta REAL,
    deletion_started_at REAL,
    UNIQUE(platform, identifier)
);
"""

_LEGACY_SIN_UNIQUE = _LEGACY_CON_UNIQUE.replace(
    ",\n    UNIQUE(platform, identifier)", "")


class _BaseLegacy(IsolatedTestCase):

    def setUp(self):
        super().setUp()
        from rastrillo import config, db
        self.db = db
        self.config = config
        config.ensure_dirs()

    def _crear(self, ddl, filas=()):
        """Monta una DB con el DDL dado y unas filas, SIN pasar por init()."""
        con = sqlite3.connect(self.config.DB_PATH)
        con.executescript(ddl)
        for f in filas:
            cols = ", ".join(f)
            qs = ", ".join("?" * len(f))
            con.execute(f"INSERT INTO accounts ({cols}) VALUES ({qs})",
                        tuple(f.values()))
        con.commit()
        con.close()

    def _fila(self, ident="ana"):
        return {
            "platform": "reddit", "identifier": ident, "source": "sherlock",
            "source_site": "reddit.com", "status": "email_draft",
            "confidence": "high", "profile_url": f"https://reddit.com/u/{ident}",
            # Los tres que se perdían:
            "sent_at": 1700000000.0,
            "deletion_eta": 1800000000.0,
            "deletion_started_at": 1700000001.0,
        }

    def _leer(self, ident="ana"):
        with self.db.connect() as con:
            return con.execute("SELECT * FROM accounts WHERE identifier=?",
                               (ident,)).fetchone()

    def _ddl(self):
        with self.db.connect() as con:
            r = con.execute("SELECT sql FROM sqlite_master WHERE type='table'"
                            " AND name='accounts'").fetchone()
        return (r["sql"] if r else "") or ""

    def _cols(self):
        with self.db.connect() as con:
            return [r["name"] for r in
                    con.execute("PRAGMA table_info(accounts)").fetchall()]


class TestLegacyConUnique(_BaseLegacy):

    def test_migra_sin_perder_los_tres_campos(self):
        """EL test de la entrega."""
        self._crear(_LEGACY_CON_UNIQUE, [self._fila()])
        self.db.init()

        row = self._leer()
        self.assertIsNotNone(row)
        self.assertEqual(row["sent_at"], 1700000000.0)
        self.assertEqual(row["deletion_eta"], 1800000000.0)
        self.assertEqual(row["deletion_started_at"], 1700000001.0)

    def test_migra_sin_perder_ningun_otro_dato(self):
        self._crear(_LEGACY_CON_UNIQUE, [self._fila()])
        self.db.init()
        row = self._leer()
        for campo, esperado in (
                ("platform", "reddit"), ("identifier", "ana"),
                ("source", "sherlock"), ("source_site", "reddit.com"),
                ("status", "email_draft"), ("confidence", "high"),
                ("profile_url", "https://reddit.com/u/ana")):
            with self.subTest(campo=campo):
                self.assertEqual(row[campo], esperado)

    def test_el_unique_desaparece(self):
        self._crear(_LEGACY_CON_UNIQUE, [self._fila()])
        self.db.init()
        self.assertNotIn("UNIQUE", self._ddl().upper())

    def test_tras_migrar_reddit_y_redditgifts_conviven(self):
        """Que era el motivo de quitar el UNIQUE."""
        self._crear(_LEGACY_CON_UNIQUE, [self._fila()])
        self.db.init()
        self.db.upsert_account("reddit", "ana", source_site="redditgifts.com",
                               source="sherlock")
        with self.db.connect() as con:
            n = con.execute("SELECT COUNT(*) c FROM accounts "
                            "WHERE identifier='ana'").fetchone()["c"]
        self.assertEqual(n, 2)

    def test_los_ids_se_preservan(self):
        self._crear(_LEGACY_CON_UNIQUE,
                    [self._fila("ana"), self._fila("bob")])
        antes = {}
        con = sqlite3.connect(self.config.DB_PATH)
        for r in con.execute("SELECT id, identifier FROM accounts"):
            antes[r[1]] = r[0]
        con.close()
        self.db.init()
        for ident, aid in antes.items():
            with self.subTest(ident=ident):
                self.assertEqual(self._leer(ident)["id"], aid)

    def test_la_columna_nueva_de_2c_se_anade_igual(self):
        """Rebuild primero, ALTERs después: la tabla recreada ya trae el
        esquema completo, incluido lo que se añadió en este mismo paso."""
        self._crear(_LEGACY_CON_UNIQUE, [self._fila()])
        self.db.init()
        self.assertIn("verifiability", self._cols())
        self.assertIsNone(self._leer()["verifiability"])

    def test_db_legacy_muy_vieja_sin_columnas_nuevas(self):
        """Una DB anterior incluso a `sent_at`: el rebuild copia solo lo que
        hay y los ALTERs rellenan el resto."""
        ddl = """CREATE TABLE accounts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            platform TEXT NOT NULL, display_name TEXT, profile_url TEXT,
            identifier TEXT, source TEXT, status TEXT NOT NULL DEFAULT 'found',
            deletion_type TEXT, difficulty TEXT, current_step INTEGER DEFAULT 0,
            last_message TEXT, updated_at REAL,
            UNIQUE(platform, identifier));"""
        self._crear(ddl, [{"platform": "reddit", "identifier": "ana",
                           "status": "found"}])
        self.db.init()
        self.assertNotIn("UNIQUE", self._ddl().upper())
        for c in ("source_site", "sent_at", "deletion_eta",
                  "deletion_started_at", "verifiability", "confidence"):
            with self.subTest(col=c):
                self.assertIn(c, self._cols())
        self.assertEqual(self._leer()["platform"], "reddit")

    def test_idempotente(self):
        self._crear(_LEGACY_CON_UNIQUE, [self._fila()])
        for _ in range(3):
            self.db.init()
        with self.db.connect() as con:
            n = con.execute("SELECT COUNT(*) c FROM accounts").fetchone()["c"]
        self.assertEqual(n, 1, "no duplica filas")
        self.assertEqual(self._leer()["sent_at"], 1700000000.0)
        # Y no deja la tabla temporal por ahí.
        with self.db.connect() as con:
            tablas = {r["name"] for r in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertNotIn("accounts__new", tablas)

    def test_una_columna_desconocida_no_aborta_la_migracion(self):
        """Si alguien añadió a mano una columna que el código no conoce, no se
        copia (no hay dónde ponerla) pero la migración no revienta."""
        ddl = _LEGACY_CON_UNIQUE.replace(
            "    UNIQUE(platform, identifier)",
            "    columna_rara TEXT,\n    UNIQUE(platform, identifier)")
        self._crear(ddl, [self._fila()])
        self.db.init()
        self.assertEqual(self._leer()["sent_at"], 1700000000.0)
        self.assertNotIn("columna_rara", self._cols())


class TestLegacySinUnique(_BaseLegacy):
    """Sin UNIQUE no hay rebuild: el comportamiento no cambia."""

    def test_no_se_reconstruye(self):
        """La tabla se migra por ALTER, no recreándola. Se nota en el ORDEN de
        las columnas: un rebuild las colocaría en su sitio del esquema,
        mientras que un ALTER las deja al final.

        Cuál es la última se DERIVA de `init()`, no se escribe a mano: antes
        estaba fijado el nombre de la que entonces cerraba la lista
        (`verifiability`) y al añadir una columna nueva el test fallaba sin que
        hubiera cambiado nada del comportamiento que mide. Misma técnica que
        `TestNoRecaida` más abajo.
        """
        import inspect
        import re
        self._crear(_LEGACY_SIN_UNIQUE, [self._fila()])
        antes = self._ddl()
        self.db.init()
        despues = self._ddl()
        self.assertTrue(despues.startswith(antes.strip().rstrip(")").rstrip()),
                        "el DDL original se conserva; solo se le añade")
        anadidas = re.findall(r"ADD COLUMN (\w+)",
                              inspect.getsource(self.db.init))
        self.assertEqual(self._cols()[-1], anadidas[-1],
                         "añadida por ALTER al final, no por rebuild")

    def test_los_datos_siguen_ahi(self):
        self._crear(_LEGACY_SIN_UNIQUE, [self._fila()])
        self.db.init()
        row = self._leer()
        self.assertEqual(row["sent_at"], 1700000000.0)
        self.assertEqual(row["deletion_eta"], 1800000000.0)
        self.assertEqual(row["deletion_started_at"], 1700000001.0)

    def test_los_alter_siguen_funcionando(self):
        """El camino normal de migración por columnas no se ha tocado."""
        ddl = """CREATE TABLE accounts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            platform TEXT NOT NULL, identifier TEXT, display_name TEXT,
            profile_url TEXT, source TEXT,
            status TEXT NOT NULL DEFAULT 'found', deletion_type TEXT,
            difficulty TEXT, current_step INTEGER DEFAULT 0,
            last_message TEXT, updated_at REAL);"""
        self._crear(ddl, [{"platform": "x", "identifier": "ana"}])
        self.db.init()
        for c in ("source_site", "action_meta", "confidence", "owned",
                  "sent_at", "deletion_eta", "deletion_started_at",
                  "verifiability", "recipe_hash", "confidence_reasons"):
            with self.subTest(col=c):
                self.assertIn(c, self._cols())


class TestDbNueva(_BaseLegacy):

    def test_sin_unique_y_con_todo(self):
        self.db.init()
        self.assertNotIn("UNIQUE", self._ddl().upper())
        self.assertEqual(list(self._cols()),
                         list(self.db.ACCOUNTS_COLUMN_NAMES))

    def test_init_dos_veces_no_falla(self):
        self.db.init()
        self.db.init()
        self.db.upsert_account("x", "ana", source_site="x.com")
        self.assertEqual(len(self.db.list_accounts()), 1)


class TestEsquemaUnicaFuenteDeVerdad(_BaseLegacy):
    """La causa raíz era tener el esquema escrito dos veces."""

    def test_el_ddl_del_rebuild_y_el_normal_coinciden_en_columnas(self):
        normal = self.db._ddl_accounts("accounts")
        temporal = self.db._ddl_accounts("accounts__new")
        self.assertEqual(normal.replace("accounts", "T", 1),
                         temporal.replace("accounts__new", "T", 1))

    def test_todas_las_columnas_migradas_estan_en_la_lista(self):
        """Si alguien añade un ALTER TABLE nuevo en `init()` sin meter la
        columna en `_ACCOUNTS_COLUMNS`, el rebuild volvería a perderla."""
        import inspect
        fuente = inspect.getsource(self.db.init)
        import re
        anadidas = set(re.findall(r"ADD COLUMN (\w+)", fuente))
        self.assertTrue(anadidas, "no encontré los ALTER TABLE")
        faltan = anadidas - set(self.db.ACCOUNTS_COLUMN_NAMES)
        self.assertEqual(faltan, set(),
                         f"columnas migradas que el rebuild no copiaría: {faltan}")


if __name__ == "__main__":
    import unittest
    unittest.main()
