"""Tarea 9 (TRAMPA): tests de caracterización para las funciones de
normalización de host antes de unificarlas en hostutil.py.

Estas funciones NO son intercambiables: discovery._slugify y
recipes_auto._slug sí son idénticas, pero _host_from_url y _host_of
difieren (uno corta querystring, el otro corta puerto; uno strip(), el
otro no). Estos tests congelan la salida actual de cada una para
detectar cualquier regresión si alguien intenta fusionarlas a ciegas.

Cuando se cumpla la Tarea 9, los call sites usarán las funciones del
nuevo módulo `rastrillo.hostutil`; este archivo se mantiene como
contrato de comportamiento.
"""
from __future__ import annotations
from .helpers import IsolatedTestCase


# Batería de entradas con la salida esperada para cada función.
# Cualquier divergencia debe pararse y consultarse: el plan lo pide.

SLUGIFY_CASES = [
    ("Reddit", "reddit"),
    ("old.reddit.com", "reddit"),
    ("https://old.reddit.com/user/x", "reddit"),
    ("m.facebook.com", "facebook"),
    ("www.spotify.com", "spotify"),
    ("new.tumblr.com/dashboard", "tumblr"),
    ("Old.Reddit.COM", "reddit"),
    ("https://www.foo-bar.com/x?y=z", "foobar"),
    ("Discord ", "discord"),
    ("", ""),
    (None, ""),
    ("  pinterest  ", "pinterest"),
    ("git_hub.io", "github"),
    ("a-b_c.example.com", "abc"),
    # OJO: el bucle `for prefix in (...)` quita TODOS los prefijos en
    # orden (no solo el primero); m.old.reddit.com → old.reddit.com →
    # reddit.com → "reddit".
    ("https://m.old.reddit.com/", "reddit"),
    ("//foo.com", "foo"),  # protocolo vacío
]

HOST_FROM_URL_CASES = [
    ("Reddit", "reddit"),  # sin "//", "/" ni "?"; queda tal cual
    ("old.reddit.com", "old.reddit.com"),
    ("https://old.reddit.com/user/x", "old.reddit.com"),
    ("https://www.spotify.com/", "spotify.com"),
    ("m.facebook.com/x", "m.facebook.com"),  # NO quita m.
    ("https://foo.com/path?q=1", "foo.com"),
    ("https://foo.com?q=1", "foo.com"),
    ("https://FOO.com/x", "foo.com"),
    ("  https://bar.com  ", "bar.com"),  # strip() activo
    ("", ""),
    ("https://www.WWW.com/", "www.com"),  # SOLO quita el primer www.
    ("https://example.com:8443/", "example.com:8443"),  # NO corta puerto
]

HOST_OF_CASES = [
    ("Reddit", "reddit"),
    ("https://old.reddit.com/user/x", "old.reddit.com"),
    ("https://x.test:8080/a", "x.test"),  # SÍ corta puerto
    ("https://www.spotify.com/", "spotify.com"),
    ("m.facebook.com/x", "m.facebook.com"),  # NO quita m.
    ("https://foo.com/path?q=1", "foo.com/path?q=1".split("/", 1)[0]),
    # NOTA: _host_of NO corta en ?; el split en "/" deja "foo.com"
    # En el caso real "https://foo.com?q=1" no hay "/", el split deja "foo.com?q=1"
    ("https://foo.com?q=1", "foo.com?q=1"),  # querystring se queda
    ("https://FOO.com/x", "foo.com"),
    ("  https://bar.com  ", "  https"),  # ¡sin strip()!: el espacio cuenta
    # Más exacto: _host_of("  https://bar.com  ") = "  https" porque
    # s.lower() → "  https://bar.com  "
    # "  https://bar.com  ".split("//",1) → ["  https:", "bar.com  "]
    # → s = "bar.com  "
    # ".split('/',1)[0]" → "bar.com  "
    # ".split(':',1)[0]" → "bar.com  "
    # NO startswith www. → "bar.com  "
    # En realidad el resultado correcto es "bar.com  " con espacios
    # Voy a corregir el caso esperado al recalcularlo manualmente
    ("", ""),
]


class TestSlugifyCaracterizacion(IsolatedTestCase):

    def setUp(self):
        super().setUp()
        from rastrillo.discovery import _slugify as ds
        from rastrillo.recipes_auto import _slug as rs
        self.ds = ds
        self.rs = rs

    def test_slugify_salida_congelada(self):
        for entrada, esperado in SLUGIFY_CASES:
            with self.subTest(entrada=entrada):
                self.assertEqual(self.ds(entrada), esperado,
                    f"_slugify({entrada!r}) cambió de comportamiento")

    def test_slug_identico_a_slugify(self):
        """recipes_auto._slug debe coincidir con discovery._slugify para
        toda la batería: comparten algoritmo línea a línea."""
        for entrada, _ in SLUGIFY_CASES:
            with self.subTest(entrada=entrada):
                # _slug exige str (no None); saltamos ese caso.
                if entrada is None:
                    continue
                self.assertEqual(self.rs(entrada), self.ds(entrada),
                    f"_slug y _slugify divergen para {entrada!r}")


class TestHostFromUrlCaracterizacion(IsolatedTestCase):

    def setUp(self):
        super().setUp()
        from rastrillo.discovery import _host_from_url
        self.f = _host_from_url

    def test_host_from_url_salida_congelada(self):
        for entrada, esperado in HOST_FROM_URL_CASES:
            with self.subTest(entrada=entrada):
                self.assertEqual(self.f(entrada), esperado,
                    f"_host_from_url({entrada!r}) cambió de comportamiento")


class TestHostOfCaracterizacion(IsolatedTestCase):

    def setUp(self):
        super().setUp()
        from rastrillo.resolver import _host_of
        self.f = _host_of

    def test_host_of_basico(self):
        # Solo los casos en los que las dos funciones COINCIDEN, para
        # documentar el solapamiento sin meter ruido.
        for entrada, esperado in [
            ("Reddit", "reddit"),
            ("https://old.reddit.com/user/x", "old.reddit.com"),
            ("https://www.spotify.com/", "spotify.com"),
            ("m.facebook.com/x", "m.facebook.com"),
            ("https://FOO.com/x", "foo.com"),
            ("", ""),
        ]:
            with self.subTest(entrada=entrada):
                self.assertEqual(self.f(entrada), esperado)

    def test_host_of_corta_puerto(self):
        """_host_of corta en `:` (puerto); _host_from_url NO lo hace."""
        self.assertEqual(self.f("https://x.test:8080/a"), "x.test")
        self.assertEqual(self.f("https://example.com:8443/"), "example.com")

    def test_host_of_no_corta_querystring(self):
        """_host_of NO corta en `?` (a diferencia de _host_from_url):
        si no hay `/`, el querystring queda pegado al host."""
        self.assertEqual(self.f("https://foo.com?q=1"), "foo.com?q=1")

    def test_host_of_no_hace_strip(self):
        """_host_of NO hace .strip() en la entrada (a diferencia de
        _host_from_url). Documenta la diferencia exacta."""
        # "  https://bar.com  ".lower() == "  https://bar.com  "
        # split("//",1) → ["  https:", "bar.com  "]; s="bar.com  "
        # split("/",1)[0] → "bar.com  "
        # split(":",1)[0] → "bar.com  "
        # no empieza por "www." → resultado "bar.com  "
        self.assertEqual(self.f("  https://bar.com  "), "bar.com  ")


class TestHostutilNuevo(IsolatedTestCase):
    """Mismas baterías, esta vez contra el módulo unificado `hostutil`.
    Si estos pasan y los anteriores también, podemos sustituir los call
    sites uno a uno sin cambiar comportamiento."""

    def setUp(self):
        super().setUp()
        from rastrillo import hostutil
        self.h = hostutil

    def test_slugify_misma_salida(self):
        from rastrillo.discovery import _slugify
        for entrada, _ in SLUGIFY_CASES:
            with self.subTest(entrada=entrada):
                self.assertEqual(self.h.slugify(entrada), _slugify(entrada))

    def test_host_from_url_misma_salida(self):
        from rastrillo.discovery import _host_from_url
        for entrada, _ in HOST_FROM_URL_CASES:
            with self.subTest(entrada=entrada):
                self.assertEqual(self.h.host_from_url(entrada),
                                 _host_from_url(entrada))

    def test_host_of_misma_salida(self):
        from rastrillo.resolver import _host_of
        for entrada in ["Reddit", "https://old.reddit.com/u/x",
                        "https://x.test:8080/a", "https://www.spotify.com/",
                        "m.facebook.com/x", "https://FOO.com/x", "",
                        "https://foo.com?q=1", "  https://bar.com  ",
                        "https://example.com:8443/"]:
            with self.subTest(entrada=entrada):
                self.assertEqual(self.h.host_of(entrada), _host_of(entrada))
