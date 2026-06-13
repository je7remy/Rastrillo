"""Tarea 6: el resolver no debe poder ser usado como SSRF.

`_http_get` filtra por `_is_safe_url`:
  - solo https,
  - el host debe resolver a IPs públicas (rechaza loopback, privadas,
    link-local, reservadas, multicast).

Los tests no hacen GET reales: solo validan la decisión de filtrado.
Para evitar tocar internet usamos `socket.getaddrinfo` mockeado donde
hace falta.
"""
from __future__ import annotations

from unittest import mock

from .helpers import IsolatedTestCase


def _addrinfo(ip: str):
    """Construye una respuesta sintética estilo socket.getaddrinfo."""
    return [(2, 1, 6, "", (ip, 0))]


class TestSSRFGuard(IsolatedTestCase):

    def setUp(self):
        super().setUp()
        from rastrillo import resolver
        self.resolver = resolver

    # ── _is_safe_url: rechazos ──
    def test_http_rechazado(self):
        # http:// no entra aunque sea un host público.
        with mock.patch("socket.getaddrinfo",
                        return_value=_addrinfo("8.8.8.8")):
            self.assertFalse(self.resolver._is_safe_url("http://example.com/"))

    def test_file_scheme_rechazado(self):
        self.assertFalse(self.resolver._is_safe_url("file:///etc/passwd"))

    def test_ftp_rechazado(self):
        self.assertFalse(self.resolver._is_safe_url("ftp://example.com/"))

    def test_loopback_literal_rechazado(self):
        self.assertFalse(self.resolver._is_safe_url("https://127.0.0.1/"))

    def test_loopback_v6_rechazado(self):
        self.assertFalse(self.resolver._is_safe_url("https://[::1]/"))

    def test_localhost_rechazado(self):
        # `localhost` resuelve a 127.0.0.1 / ::1 → loopback.
        self.assertFalse(self.resolver._is_safe_url("https://localhost/"))

    def test_red_privada_rechazada(self):
        with mock.patch("socket.getaddrinfo",
                        return_value=_addrinfo("192.168.1.10")):
            self.assertFalse(self.resolver._is_safe_url("https://nas.local/"))

    def test_link_local_rechazada(self):
        with mock.patch("socket.getaddrinfo",
                        return_value=_addrinfo("169.254.1.1")):
            self.assertFalse(self.resolver._is_safe_url("https://meta.aws/"))

    def test_host_no_resuelve_rechazado(self):
        import socket
        with mock.patch("socket.getaddrinfo",
                        side_effect=socket.gaierror("no resuelve")):
            self.assertFalse(self.resolver._is_safe_url("https://no.existe/"))

    # ── _is_safe_url: aceptaciones ──
    def test_host_publico_aceptado(self):
        with mock.patch("socket.getaddrinfo",
                        return_value=_addrinfo("93.184.216.34")):
            self.assertTrue(self.resolver._is_safe_url("https://example.com/path"))

    def test_publico_v6_aceptado(self):
        with mock.patch("socket.getaddrinfo",
                        return_value=_addrinfo("2606:2800:220:1:248:1893:25c8:1946")):
            self.assertTrue(self.resolver._is_safe_url("https://example.com/"))

    # ── _http_get: integración del guard ──
    def test_http_get_localhost_devuelve_none(self):
        # No hace falta servidor: el guard corta antes de abrir socket.
        self.assertIsNone(self.resolver._http_get("http://127.0.0.1:1/"))
        self.assertIsNone(self.resolver._http_get("https://127.0.0.1:1/"))

    def test_http_get_red_privada_devuelve_none(self):
        with mock.patch("socket.getaddrinfo",
                        return_value=_addrinfo("10.0.0.5")):
            self.assertIsNone(self.resolver._http_get("https://nas/"))
