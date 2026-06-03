"""Tier 3 del Nivel 7: operativo.
  - refresh automático del directorio si > N días
  - backup de DB antes de clear_accounts
  - rotación de audit.json al superar el límite
"""
import json
import os
import time
from unittest.mock import patch
from .helpers import IsolatedTestCase


class TestAutoRefreshDirectorio(IsolatedTestCase):

    def setUp(self):
        super().setUp()
        from rastrillo import config, directory, jobs
        self.config = config
        self.directory = directory
        self.jobs = jobs
        config.ensure_dirs()

    def _write_cached_dir(self, fetched_at):
        """Escribe directory.json con `fetched_at` controlado para simular
        cachés de distintas edades."""
        payload = {
            "fetched_at": fetched_at,
            "entries": [{"name":"X","url":"https://x/close",
                         "difficulty":"easy","domains":["x.test"]}],
        }
        self.directory.CACHE_PATH.write_text(
            json.dumps(payload), encoding="utf-8")
        # Reseteamos el cache en memoria para que el siguiente load_directory
        # vuelva a leer del disco.
        self.directory._loaded = None

    def test_dir_max_age_default_30(self):
        os.environ.pop("RASTRILLO_DIR_MAX_AGE_DAYS", None)
        self.assertEqual(self.jobs._dir_max_age_seconds(), 30 * 86400)

    def test_dir_max_age_env(self):
        os.environ["RASTRILLO_DIR_MAX_AGE_DAYS"] = "7"
        try:
            self.assertEqual(self.jobs._dir_max_age_seconds(), 7 * 86400)
        finally:
            os.environ.pop("RASTRILLO_DIR_MAX_AGE_DAYS")

    def test_caché_fresco_NO_dispara_refresh(self):
        """Si la caché tiene 5 días y el umbral es 30, no se re-descarga."""
        self._write_cached_dir(time.time() - 5 * 86400)
        with patch.object(self.directory, "fetch_directory") as spy:
            t = self.jobs.start_dir_refresh_if_stale()
            t.join(timeout=5.0)
            self.assertFalse(spy.called,
                "no debería re-descargar (caché de 5 días, umbral 30)")

    def test_caché_viejo_SI_dispara_refresh(self):
        """Caché de 45 días con umbral 30 → refresca en background."""
        self._write_cached_dir(time.time() - 45 * 86400)
        called = []
        def _fake_fetch(timeout=30):
            called.append(True)
            return self.directory.Directory(
                [{"name":"Y","url":"https://y/","difficulty":"easy","domains":["y.test"]}],
                source="upstream", fetched_at=time.time(),
            )
        with patch.object(self.directory, "fetch_directory",
                          side_effect=_fake_fetch):
            t = self.jobs.start_dir_refresh_if_stale()
            t.join(timeout=5.0)
        self.assertTrue(called, "el directorio viejo debería re-descargarse")

    def test_fallo_de_red_no_aborta(self):
        """Si fetch falla, NO se propaga; nos quedamos con el caché en silencio."""
        self._write_cached_dir(time.time() - 60 * 86400)
        def _boom(timeout=30):
            raise RuntimeError("simulado: sin red")
        with patch.object(self.directory, "fetch_directory", side_effect=_boom):
            t = self.jobs.start_dir_refresh_if_stale()
            t.join(timeout=5.0)
        # El caché sigue ahí
        self.assertTrue(self.directory.CACHE_PATH.exists())


class TestBackupAntesDeClear(IsolatedTestCase):

    def setUp(self):
        super().setUp()
        from rastrillo import db, config
        self.db = db
        self.config = config
        db.init()
        # Sembramos algo para que el snapshot tenga contenido distinguible
        db.upsert_account("reddit", "alice", source="sherlock",
            source_site="reddit.com", display_name="Reddit",
            status="found", confidence="high")

    def test_clear_crea_snapshot(self):
        backup_dir = self.config.BASE_DIR / "backups"
        self.assertFalse(backup_dir.exists())
        self.db.clear_accounts()
        self.assertTrue(backup_dir.exists())
        snapshots = list(backup_dir.glob("rastrillo_*.db"))
        self.assertEqual(len(snapshots), 1,
            f"esperaba 1 snapshot, hay {len(snapshots)}")
        # La cuenta original DEBE estar en el snapshot, y la DB activa, vacía
        import sqlite3
        con = sqlite3.connect(snapshots[0])
        n_snap = con.execute("SELECT COUNT(*) FROM accounts").fetchone()[0]
        con.close()
        self.assertEqual(n_snap, 1)
        self.assertEqual(len(list(self.db.list_accounts())), 0)

    def test_snapshot_sin_db_no_revienta(self):
        """Si rastrillo.db no existe, snapshot_db devuelve None sin error."""
        # Borramos la DB activa
        if self.config.DB_PATH.exists():
            self.config.DB_PATH.unlink()
        # Y reseteamos eventos del init previo
        self.assertIsNone(self.db.snapshot_db())

    def test_dos_clears_dos_snapshots(self):
        """Cada clear deja su propio snapshot (no se sobreescriben)."""
        self.db.clear_accounts()
        # Sembramos otra vez para que el segundo snapshot tenga contenido distinto
        self.db.upsert_account("tumblr", "bob", source="sherlock",
            source_site="tumblr.com", display_name="Tumblr",
            status="found", confidence="high")
        time.sleep(1.1)   # tienen timestamps en segundos
        self.db.clear_accounts()
        snaps = list((self.config.BASE_DIR / "backups").glob("rastrillo_*.db"))
        self.assertEqual(len(snaps), 2)


class TestRotacionAudit(IsolatedTestCase):

    def setUp(self):
        super().setUp()
        from rastrillo import audit, config
        self.audit = audit
        self.config = config
        config.ensure_dirs()

    def _fake_acc(self):
        # Pasamos un dict simulando una fila SQLite
        return {"id": 1, "platform": "reddit", "source": "sherlock",
                "source_site": "reddit.com", "identifier": "alice",
                "profile_url": "https://reddit.com/u/alice", "status": "found",
                "confidence": "medium", "owned": 0, "display_name": "Reddit"}

    def test_no_rota_bajo_umbral(self):
        """Con umbral alto, audit.json no se rota aunque tenga entradas."""
        os.environ.pop("RASTRILLO_AUDIT_MAX_BYTES", None)
        for _ in range(3):
            self.audit.record("delete", self._fake_acc())
        self.assertTrue(self.audit.AUDIT_PATH.exists())
        # No deberían existir archivos audit_*.json
        rotated = list(self.audit.AUDIT_PATH.parent.glob("audit_*.json"))
        self.assertEqual(rotated, [])

    def test_rota_al_superar_umbral(self):
        """Con umbral muy bajo, la siguiente escritura rota antes de añadir."""
        os.environ["RASTRILLO_AUDIT_MAX_BYTES"] = "300"
        try:
            # 1ª escritura: archivo nuevo
            self.audit.record("delete", self._fake_acc())
            tam = self.audit.AUDIT_PATH.stat().st_size
            self.assertGreater(tam, 0)
            # 2ª-3ª: forzamos a que crezca y supere el umbral
            for _ in range(5):
                self.audit.record("delete", self._fake_acc())
            # Tras superar, debería haberse rotado al menos una vez
            rotated = list(self.audit.AUDIT_PATH.parent.glob("audit_*.json"))
            self.assertGreaterEqual(len(rotated), 1,
                "audit no se rotó pese a superar el umbral")
            # El audit.json activo existe y es válido (lista no vacía)
            self.assertTrue(self.audit.AUDIT_PATH.exists())
            data = json.loads(self.audit.AUDIT_PATH.read_text(encoding="utf-8"))
            self.assertIsInstance(data, list)
        finally:
            os.environ.pop("RASTRILLO_AUDIT_MAX_BYTES")

    def test_rotacion_no_pierde_entradas(self):
        """Con un umbral muy bajo (frecuentes rotaciones), la SUMA de entradas
        en audit.json activo + todos los rotados = total de records hechos."""
        os.environ["RASTRILLO_AUDIT_MAX_BYTES"] = "350"
        try:
            for _ in range(3):
                self.audit.record("delete", self._fake_acc())
            for _ in range(5):
                self.audit.record("own", self._fake_acc())
            # Suma todas las entradas: activo + rotados
            total = 0
            actions_seen = set()
            for path in [self.audit.AUDIT_PATH] + list(
                    self.audit.AUDIT_PATH.parent.glob("audit_*.json")):
                if not path.exists():
                    continue
                data = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(data, list):
                    total += len(data)
                    for e in data:
                        actions_seen.add(e.get("action"))
            self.assertEqual(total, 8, "se perdieron entradas en la rotación")
            self.assertEqual(actions_seen, {"delete", "own"})
            # Debe haber HABIDO al menos una rotación
            rotated = list(self.audit.AUDIT_PATH.parent.glob("audit_*.json"))
            self.assertGreaterEqual(len(rotated), 1)
        finally:
            os.environ.pop("RASTRILLO_AUDIT_MAX_BYTES")
