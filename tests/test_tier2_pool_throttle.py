"""Tier 2 del Nivel 7:
  - pool acotado en _auto_resolve_pending procesa N hosts correctamente
  - throttle respeta la caché y no re-fetchea hosts ya cacheados
  - UI del scan muestra las fases (Descubriendo / Resolviendo N/total)
"""
import json
import os
import threading
import time
from unittest.mock import patch
from .helpers import IsolatedTestCase, auth_client


class _SlowMockResolver:
    """Mock para `resolver.resolve` que cuenta llamadas concurrentes
    y registra el pico, para verificar que el pool respeta el límite."""
    def __init__(self):
        self.current = 0
        self.peak = 0
        self.calls = []
        self.lock = threading.Lock()

    def __call__(self, host, identifier, force_refresh=False):
        with self.lock:
            self.current += 1
            self.peak = max(self.peak, self.current)
            self.calls.append(host)
        # Simulamos trabajo HTTP
        time.sleep(0.08)
        with self.lock:
            self.current -= 1
        # Fabricamos una Resolution mínima
        from rastrillo.resolver import Resolution
        return Resolution(
            kind="semi_auto", layer="probe",
            title=host, notes="mock",
            url=f"https://{host}/settings",
            language="en",
        )


class TestPoolAcotado(IsolatedTestCase):

    def setUp(self):
        super().setUp()
        from rastrillo import db, jobs
        self.db = db
        self.jobs = jobs
        db.init()

    def _sembrar(self, n):
        for i in range(n):
            self.db.upsert_account(
                f"plat{i}", f"u{i}", source="sherlock",
                source_site=f"host{i}.example", display_name=f"Plat{i}",
                status="found",
            )

    def test_workers_default_5(self):
        os.environ.pop("RASTRILLO_RESOLVER_WORKERS", None)
        self.assertEqual(self.jobs._resolver_workers(), 5)

    def test_workers_env_override(self):
        os.environ["RASTRILLO_RESOLVER_WORKERS"] = "3"
        try:
            self.assertEqual(self.jobs._resolver_workers(), 3)
        finally:
            os.environ.pop("RASTRILLO_RESOLVER_WORKERS")

    def test_workers_clamp(self):
        os.environ["RASTRILLO_RESOLVER_WORKERS"] = "999"
        try:
            self.assertEqual(self.jobs._resolver_workers(), 16)
        finally:
            os.environ.pop("RASTRILLO_RESOLVER_WORKERS")

    def test_pool_procesa_lote_y_respeta_limite(self):
        """Con 12 hosts y 4 workers: las 12 cuentas se procesan y la
        concurrencia simultánea nunca supera 4."""
        os.environ["RASTRILLO_RESOLVER_WORKERS"] = "4"
        try:
            self._sembrar(12)
            mock = _SlowMockResolver()
            with patch("rastrillo.resolver.resolve", side_effect=mock):
                n = self.jobs._auto_resolve_pending()
            self.assertEqual(n, 12, "todas las cuentas deberían resolverse")
            self.assertEqual(len(mock.calls), 12)
            self.assertLessEqual(mock.peak, 4,
                f"el pool excedió 4 workers (peak={mock.peak})")
            self.assertGreaterEqual(mock.peak, 2,
                f"el pool no paralelizó (peak={mock.peak})")
            # Cada cuenta debería tener action_meta tras resolverse
            for row in self.db.list_accounts():
                self.assertIsNotNone(row["action_meta"])
        finally:
            os.environ.pop("RASTRILLO_RESOLVER_WORKERS")

    def test_pool_actualiza_scan_status(self):
        """Mientras procesa, _scan_status.resolved se incrementa hasta total."""
        os.environ["RASTRILLO_RESOLVER_WORKERS"] = "3"
        try:
            self._sembrar(6)
            mock = _SlowMockResolver()
            with patch("rastrillo.resolver.resolve", side_effect=mock):
                self.jobs._auto_resolve_pending()
            st = self.jobs.scan_status()
            self.assertEqual(st["total"], 6)
            self.assertEqual(st["resolved"], 6)
        finally:
            os.environ.pop("RASTRILLO_RESOLVER_WORKERS")

    def test_resolver_excepcion_no_aborta_lote(self):
        """Si resolver explota para un host, los demás siguen."""
        self._sembrar(5)
        seq = ["ok", "boom", "ok", "ok", "ok"]
        i = [0]
        def _maybe(host, identifier, force_refresh=False):
            idx = i[0]; i[0] += 1
            if seq[idx] == "boom":
                raise RuntimeError("simulado")
            from rastrillo.resolver import Resolution
            return Resolution(kind="semi_auto", layer="probe",
                              title=host, url=f"https://{host}/", language="en")
        with patch("rastrillo.resolver.resolve", side_effect=_maybe):
            n = self.jobs._auto_resolve_pending()
        # 4 OK + 1 que reventó = 4 guardadas
        self.assertEqual(n, 4)
        # Todas las cuentas se intentaron
        st = self.jobs.scan_status()
        self.assertEqual(st["resolved"], 5)


class TestThrottleYCache(IsolatedTestCase):

    def setUp(self):
        super().setUp()
        from rastrillo import resolver
        self.resolver = resolver
        # Cache discovered.json vacía
        if resolver.DISCOVERED_PATH.exists():
            resolver.DISCOVERED_PATH.unlink()

    def test_cache_evita_segundo_fetch(self):
        """resolve(host) en caché → la 2ª llamada NO toca _http_get ni la
        red ni el directorio (cortocircuito por caché)."""
        # Primer resolve usando un mock de directory.lookup
        from rastrillo import directory
        directory._loaded = directory.Directory(
            [{"name":"Spotify","url":"https://example/close","difficulty":"easy",
              "domains":["spotify.com"]}],
            source="fallback", fetched_at=None,
        )
        # 1ª llamada — pasa por directory (rama no-HTTP)
        r1 = self.resolver.resolve("spotify.com", "alice")
        self.assertIsNotNone(r1)
        # 2ª llamada — debería venir del caché en discovered.json.
        # Mockeamos directory.lookup para asegurar que NO se llama.
        with patch.object(directory, "lookup") as spy_dir, \
             patch.object(self.resolver, "_http_get") as spy_http:
            r2 = self.resolver.resolve("spotify.com", "alice")
            self.assertFalse(spy_dir.called, "no debería tocar directorio (caché)")
            self.assertFalse(spy_http.called, "no debería tocar la red (caché)")
        self.assertEqual(r1.url, r2.url)

    def test_throttle_serializa_mismo_host(self):
        """Dos GETs al mismo host se serializan por RASTRILLO_PROBE_DELAY.
        Con delay=0.4s, 2 fetches al mismo host tardan al menos 0.4s en total."""
        os.environ["RASTRILLO_PROBE_DELAY"] = "0.4"
        try:
            # Limpiamos el estado del throttle (compartido entre tests)
            with self.resolver._throttle_lock:
                self.resolver._last_fetch_by_host.clear()

            # Mockeamos urlopen para que retorne rápido, así el delay observado
            # es puramente el throttle. Tarea 6: además parcheamos
            # socket.getaddrinfo para que la allowlist SSRF acepte el host
            # ficticio (de lo contrario `x.test` no resuelve y _http_get
            # cortaría antes del throttle).
            class _R:
                status = 200
                def read(self, _=None): return b"<html></html>"
                def geturl(self): return "https://x.test/"
                def __enter__(self): return self
                def __exit__(self, *a): pass

            with patch("rastrillo.resolver.urllib.request.urlopen",
                       return_value=_R()), \
                 patch("socket.getaddrinfo",
                       return_value=[(2, 1, 6, "", ("8.8.8.8", 0))]):
                t0 = time.time()
                self.resolver._http_get("https://x.test/a")
                self.resolver._http_get("https://x.test/b")
                elapsed = time.time() - t0
            # 2 GETs al mismo host con delay 0.4 → al menos 0.4s entre ellos.
            self.assertGreaterEqual(elapsed, 0.35,
                f"el throttle no esperó (elapsed={elapsed:.2f}s)")
        finally:
            os.environ.pop("RASTRILLO_PROBE_DELAY")

    def test_throttle_no_bloquea_distintos_hosts(self):
        """GETs a hosts distintos NO se esperan entre sí (concurrencia útil)."""
        os.environ["RASTRILLO_PROBE_DELAY"] = "0.4"
        try:
            with self.resolver._throttle_lock:
                self.resolver._last_fetch_by_host.clear()
            class _R:
                status = 200
                def read(self, _=None): return b""
                def geturl(self): return "https://x/"
                def __enter__(self): return self
                def __exit__(self, *a): pass
            # Tarea 6: ver nota en el test anterior.
            with patch("rastrillo.resolver.urllib.request.urlopen",
                       return_value=_R()), \
                 patch("socket.getaddrinfo",
                       return_value=[(2, 1, 6, "", ("8.8.8.8", 0))]):
                t0 = time.time()
                self.resolver._http_get("https://a.test/x")
                self.resolver._http_get("https://b.test/x")
                elapsed = time.time() - t0
            # 2 GETs a hosts distintos NO deben esperar el delay completo.
            self.assertLess(elapsed, 0.35,
                f"throttle bloqueó hosts distintos (elapsed={elapsed:.2f}s)")
        finally:
            os.environ.pop("RASTRILLO_PROBE_DELAY")


class TestUIFases(IsolatedTestCase):

    def setUp(self):
        super().setUp()
        self.client = auth_client()

    def test_html_lee_fase_y_total(self):
        # Desde Tarea 10 el JS vive en /static/app.js; los marcadores que
        # antes buscábamos en `GET /` ahora están ahí.
        app_js = self.client.get("/static/app.js").text
        self.assertIn('scanRes.phase', app_js)
        self.assertIn('scanRes.total', app_js)
        self.assertIn('scanRes.resolved', app_js)
        self.assertIn('Resolviendo', app_js)
        self.assertIn('Descubriendo', app_js)
        # Y en el cierre, "M resueltas"
        self.assertIn('resueltas', app_js)

    def test_scan_status_endpoint_devuelve_phase_y_resolved(self):
        """Aunque no haya scan corriendo, el dict tiene las claves esperadas."""
        from rastrillo import jobs
        # Simulamos directamente lo que un scan dejaría
        with jobs._lock:
            jobs._scan_status["phase"] = "resolving"
            jobs._scan_status["total"] = 10
            jobs._scan_status["resolved"] = 3
        # Desde Tarea 3 los GET de /api/* exigen token.
        r = self.client.get("/api/scan/status",
                            headers={"X-Rastrillo-Token": self.TOKEN}).json()
        self.assertEqual(r["phase"], "resolving")
        self.assertEqual(r["total"], 10)
        self.assertEqual(r["resolved"], 3)
