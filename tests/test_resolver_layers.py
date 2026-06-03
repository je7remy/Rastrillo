"""Invariante: el resolver prueba las capas en orden y SIEMPRE devuelve
una Resolution. El caché personal cortocircuita después de la primera vez.

Capas:
  1. directorio (kind=auto si IA, sino semi_auto)
  2. web_search IA (kind=auto/semi_auto)
  3. probe paths (kind=semi_auto)
  5. GDPR fallback (kind=email_draft, siempre)
"""
from unittest.mock import patch
from .helpers import IsolatedTestCase


class TestResolverLayers(IsolatedTestCase):

    def setUp(self):
        super().setUp()
        from rastrillo import resolver, directory, ai_assist
        # Forzamos directorio mínimo con un solo sitio conocido.
        directory._loaded = directory.Directory(
            [{"name": "Spotify", "url": "https://support.spotify.com/close",
              "difficulty": "medium", "notes": "easy",
              "domains": ["spotify.com"]}],
            source="fallback", fetched_at=None,
        )
        # Limpiamos caché de hallazgos personales antes de cada test.
        if resolver.DISCOVERED_PATH.exists():
            resolver.DISCOVERED_PATH.unlink()
        self.resolver = resolver
        self.directory = directory
        self.ai_assist = ai_assist

    def test_capa1_directorio_sin_ia_es_semi_auto(self):
        """Host en directorio + sin IA → kind=semi_auto con la URL."""
        with patch.object(self.ai_assist, "available", return_value=False):
            res = self.resolver.resolve("spotify.com", "alice")
        self.assertEqual(res.layer, "directory")
        self.assertEqual(res.kind, "semi_auto")
        self.assertEqual(res.url, "https://support.spotify.com/close")

    def test_capa1_directorio_con_ia_es_auto(self):
        """Host en directorio + IA disponible → kind=auto."""
        with patch.object(self.ai_assist, "available", return_value=True):
            res = self.resolver.resolve("spotify.com", "alice")
        self.assertEqual(res.layer, "directory")
        self.assertEqual(res.kind, "auto")

    def test_capa3_probe_si_no_directorio_y_no_ia(self):
        """Sin directorio ni IA, si el probe encuentra paths con keywords de
        borrado → semi_auto layer=probe."""
        with patch.object(self.ai_assist, "available", return_value=False), \
             patch.object(self.resolver, "_probe_paths",
                          return_value=[{"url": "https://x.com/settings/account",
                                         "keyword": "delete account"}]):
            res = self.resolver.resolve("desconocido.x", "alice")
        self.assertEqual(res.layer, "probe")
        self.assertEqual(res.kind, "semi_auto")
        self.assertEqual(res.url, "https://x.com/settings/account")

    def test_capa5_gdpr_siempre_produce_algo(self):
        """Sin directorio, sin IA, probe vacío → fallback GDPR (email_draft)."""
        with patch.object(self.ai_assist, "available", return_value=False), \
             patch.object(self.resolver, "_probe_paths", return_value=[]), \
             patch.object(self.resolver, "_find_privacy_emails", return_value=[]):
            res = self.resolver.resolve("desconocido.x", "alice")
        self.assertEqual(res.layer, "gdpr")
        self.assertEqual(res.kind, "email_draft")
        self.assertIsNotNone(res.email_to)
        self.assertIsNotNone(res.email_subject)
        self.assertIsNotNone(res.email_body)

    def test_cache_cortocircuita(self):
        """La segunda llamada al mismo host viene del caché y NO ejecuta las
        capas otra vez (sin tocar el filesystem/red)."""
        with patch.object(self.ai_assist, "available", return_value=False):
            r1 = self.resolver.resolve("spotify.com", "alice")
        with patch.object(self.directory, "lookup", return_value=None) as spy_dir, \
             patch.object(self.resolver, "_probe_paths") as spy_probe, \
             patch.object(self.resolver, "layer_gdpr") as spy_gdpr:
            r2 = self.resolver.resolve("spotify.com", "alice")
            self.assertFalse(spy_dir.called,   "directory.lookup no debería invocarse: viene de caché")
            self.assertFalse(spy_probe.called, "probe no debería invocarse")
            self.assertFalse(spy_gdpr.called,  "gdpr no debería invocarse")
        self.assertEqual(r1.layer, r2.layer)
        self.assertEqual(r1.url, r2.url)

    def test_force_refresh_omite_cache(self):
        """force_refresh=True ignora el caché y vuelve a ejecutar las capas."""
        with patch.object(self.ai_assist, "available", return_value=False):
            self.resolver.resolve("spotify.com", "alice")
        with patch.object(self.directory, "lookup", return_value=None) as spy_dir, \
             patch.object(self.resolver, "_probe_paths", return_value=[]), \
             patch.object(self.resolver, "_find_privacy_emails", return_value=[]):
            self.resolver.resolve("spotify.com", "alice", force_refresh=True)
            self.assertTrue(spy_dir.called)

    def test_difficulty_impossible_no_es_auto(self):
        """Entry de directorio marcado como 'impossible' NO va a kind=auto."""
        self.directory._loaded = self.directory.Directory(
            [{"name":"X","url":"https://example.com/help","difficulty":"impossible",
              "domains":["impossible.x"]}],
            source="fallback", fetched_at=None,
        )
        if self.resolver.DISCOVERED_PATH.exists():
            self.resolver.DISCOVERED_PATH.unlink()
        with patch.object(self.ai_assist, "available", return_value=True), \
             patch.object(self.resolver, "_probe_paths", return_value=[]), \
             patch.object(self.resolver, "_find_privacy_emails", return_value=[]):
            res = self.resolver.resolve("impossible.x", "alice")
        # Cae a probe o GDPR — pero NO debe ser layer=directory kind=auto
        self.assertNotEqual(res.layer, "directory")
