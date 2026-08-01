"""Paso 5, Entrega 2: el criterio anti-SSRF, ahora en un solo sitio.

`netguard` nació de un refactor puro: `resolver._is_safe_url` y
`domain_intel._host_resolves_public` tenían el bucle de comprobación de IPs
**idéntico byte a byte**. Aquí se prueba la definición única y, sobre todo, que
los dos envoltorios siguen decidiendo exactamente lo mismo que antes.

El criterio de éxito de la entrega no está en este fichero sino en los otros
cinco que ya ejercitaban la guarda (`test_ssrf_guard`, `test_domain_intel`,
`test_canario`, `test_bloqueo_vs_red`, `test_anti_false_deleted`): pasan **sin
editar una línea**. Este añade la cobertura directa que antes no existía.
"""
from __future__ import annotations

import socket
from unittest import mock

from .helpers import IsolatedTestCase


def _addrinfo(*ips: str):
    """Respuesta sintética estilo `socket.getaddrinfo`, con N direcciones."""
    return [(2, 1, 6, "", (ip, 0)) for ip in ips]


class _Base(IsolatedTestCase):
    def setUp(self):
        super().setUp()
        from rastrillo import netguard
        self.ng = netguard


class TestIpPublica(_Base):
    """El predicado atómico. Sin red: solo clasifica direcciones."""

    def test_rechaza_no_publicas(self):
        casos = [
            ("127.0.0.1",   "loopback"),
            ("::1",         "loopback v6"),
            ("10.0.0.5",    "privada 10/8"),
            ("192.168.1.1", "privada 192.168/16"),
            ("172.16.0.1",  "privada 172.16/12"),
            ("169.254.1.1", "link-local"),
            ("fe80::1",     "link-local v6"),
            ("224.0.0.1",   "multicast"),
            ("ff02::1",     "multicast v6"),
            ("240.0.0.1",   "reservada"),
            ("0.0.0.0",     "sin especificar"),
        ]
        for ip, motivo in casos:
            with self.subTest(ip=ip, motivo=motivo):
                self.assertFalse(self.ng.ip_publica(ip),
                                 f"{ip} ({motivo}) no debería pasar")

    def test_acepta_publicas(self):
        for ip in ("8.8.8.8", "93.184.216.34", "1.1.1.1",
                   "2606:2800:220:1:248:1893:25c8:1946"):
            with self.subTest(ip=ip):
                self.assertTrue(self.ng.ip_publica(ip))

    def test_basura_no_es_ip_publica(self):
        """Lo que no parsea como IP no puede colarse como pública."""
        for basura in ("", "no-soy-una-ip", "999.999.999.999", None, 12345):
            with self.subTest(basura=basura):
                self.assertFalse(self.ng.ip_publica(basura))

    def test_acepta_objetos_ipaddress(self):
        import ipaddress
        self.assertTrue(self.ng.ip_publica(ipaddress.ip_address("8.8.8.8")))
        self.assertFalse(self.ng.ip_publica(ipaddress.ip_address("127.0.0.1")))


class TestHostEsPublico(_Base):
    """Valida un host a secas: no exige esquema. Es lo que usa WHOIS."""

    def test_host_vacio_rechazado(self):
        self.assertFalse(self.ng.host_es_publico(""))
        self.assertFalse(self.ng.host_es_publico(None))

    def test_literales_sin_red(self):
        """IPs literales: `getaddrinfo` resuelve sin salir a la red."""
        self.assertFalse(self.ng.host_es_publico("127.0.0.1"))
        self.assertFalse(self.ng.host_es_publico("192.168.1.1"))

    def test_host_que_no_resuelve(self):
        with mock.patch("socket.getaddrinfo",
                        side_effect=socket.gaierror("no resuelve")):
            self.assertFalse(self.ng.host_es_publico("no.existe.invalido"))

    def test_host_publico(self):
        with mock.patch("socket.getaddrinfo", return_value=_addrinfo("8.8.8.8")):
            self.assertTrue(self.ng.host_es_publico("dns.google"))

    def test_basta_una_ip_mala(self):
        """Un host que resuelve a pública Y a loopback sirve para el ataque.

        Es el caso interesante: si solo mirásemos la primera dirección, un
        atacante pondría una IP pública delante y colaría la privada detrás.
        """
        with mock.patch("socket.getaddrinfo",
                        return_value=_addrinfo("93.184.216.34", "127.0.0.1")):
            self.assertFalse(self.ng.host_es_publico("mixto.example"))

    def test_el_puerto_se_pasa_a_getaddrinfo(self):
        """WHOIS resuelve contra el 43; el resolver, contra None. Se conserva."""
        with mock.patch("socket.getaddrinfo",
                        return_value=_addrinfo("8.8.8.8")) as m:
            self.ng.host_es_publico("ejemplo.com", 43)
            self.assertEqual(m.call_args[0][1], 43)
            self.ng.host_es_publico("ejemplo.com")
            self.assertIsNone(m.call_args[0][1])

    def test_no_exige_esquema(self):
        """A diferencia de `url_es_segura`, aquí un host pelado es válido."""
        with mock.patch("socket.getaddrinfo", return_value=_addrinfo("8.8.8.8")):
            self.assertTrue(self.ng.host_es_publico("whois.verisign-grs.com"))


class TestUrlEsSegura(_Base):
    """Valida una URL: además del criterio de IP, exige https."""

    def test_solo_https(self):
        with mock.patch("socket.getaddrinfo", return_value=_addrinfo("8.8.8.8")):
            self.assertTrue(self.ng.url_es_segura("https://ejemplo.com/x"))
            self.assertFalse(self.ng.url_es_segura("http://ejemplo.com/x"))
        for url in ("file:///etc/passwd", "ftp://ejemplo.com/",
                    "gopher://ejemplo.com/", "javascript:alert(1)"):
            with self.subTest(url=url):
                self.assertFalse(self.ng.url_es_segura(url))

    def test_el_esquema_se_mira_antes_que_el_dns(self):
        """Un `http://` no debe ni provocar una resolución."""
        with mock.patch("socket.getaddrinfo") as m:
            self.assertFalse(self.ng.url_es_segura("http://ejemplo.com/"))
            m.assert_not_called()

    def test_sin_host(self):
        self.assertFalse(self.ng.url_es_segura("https://"))
        self.assertFalse(self.ng.url_es_segura(""))

    def test_basura_no_revienta(self):
        for basura in ("", "no es una url", "https://[malformado",
                       "://", "https://:::/"):
            with self.subTest(basura=basura):
                self.assertFalse(self.ng.url_es_segura(basura))

    def test_ip_privada_en_la_url(self):
        self.assertFalse(self.ng.url_es_segura("https://127.0.0.1/"))
        self.assertFalse(self.ng.url_es_segura("https://[::1]/"))
        with mock.patch("socket.getaddrinfo", return_value=_addrinfo("10.0.0.5")):
            self.assertFalse(self.ng.url_es_segura("https://nas.local/"))


class TestEquivalenciaConLosEnvoltorios(IsolatedTestCase):
    """El refactor no puede cambiar NI UNA decisión.

    Se barre un conjunto de direcciones y se comprueba que los dos envoltorios
    coinciden con el helper. Si alguien "mejora" `netguard` sin darse cuenta de
    que hay dos consumidores con semánticas distintas, esto lo caza.
    """

    IPS = ["8.8.8.8", "93.184.216.34", "127.0.0.1", "10.0.0.5",
           "192.168.1.1", "172.16.0.1", "169.254.1.1", "224.0.0.1",
           "240.0.0.1", "0.0.0.0", "::1", "fe80::1",
           "2606:2800:220:1:248:1893:25c8:1946"]

    def setUp(self):
        super().setUp()
        from rastrillo import domain_intel, netguard, resolver
        self.ng = netguard
        self.resolver = resolver
        self.domain_intel = domain_intel

    def test_resolver_is_safe_url_equivale_a_url_es_segura(self):
        for ip in self.IPS:
            with self.subTest(ip=ip):
                with mock.patch("socket.getaddrinfo", return_value=_addrinfo(ip)):
                    esperado = self.ng.url_es_segura("https://ejemplo.com/")
                    obtenido = self.resolver._is_safe_url("https://ejemplo.com/")
                    self.assertEqual(obtenido, esperado)
                    # Y el resultado sigue siendo el de siempre: pública → True.
                    self.assertEqual(obtenido, self.ng.ip_publica(ip))

    def test_domain_intel_host_resolves_public_equivale(self):
        for ip in self.IPS:
            with self.subTest(ip=ip):
                with mock.patch("socket.getaddrinfo", return_value=_addrinfo(ip)):
                    obtenido = self.domain_intel._host_resolves_public("srv.example")
                    self.assertEqual(obtenido, self.ng.ip_publica(ip))

    def test_las_dos_semanticas_siguen_siendo_distintas(self):
        """`http://` lo rechaza el envoltorio de URL; al de host no le incumbe.

        Es la razón por la que son dos funciones y no una: `domain_intel`
        valida un host para TCP/43, donde no hay esquema que mirar.
        """
        with mock.patch("socket.getaddrinfo", return_value=_addrinfo("8.8.8.8")):
            self.assertFalse(self.resolver._is_safe_url("http://ejemplo.com/"))
            self.assertTrue(self.domain_intel._host_resolves_public("ejemplo.com"))


class TestSiguenSiendoParcheables(IsolatedTestCase):
    """Los envoltorios tienen que seguir siendo funciones del módulo.

    Cinco ficheros de tests hacen `patch.object(modulo, "_is_safe_url", ...)` o
    se lo pasan al canario como primitiva inyectada. Si el refactor los hubiera
    convertido en un `from netguard import ...`, esos parches dejarían de
    aplicar y la suite haría DNS de verdad — pasando en verde por accidente.
    """

    def setUp(self):
        super().setUp()
        from rastrillo import domain_intel, resolver
        self.resolver = resolver
        self.domain_intel = domain_intel

    def test_is_safe_url_es_parcheable(self):
        from unittest.mock import patch
        with patch.object(self.resolver, "_is_safe_url", return_value=False):
            resultado, motivo = self.resolver._http_get_detallado("https://ejemplo.com/")
            self.assertIsNone(resultado)
            self.assertEqual(motivo, self.resolver.MOTIVO_SSRF)

    def test_host_resolves_public_es_parcheable(self):
        from unittest.mock import patch
        with patch.object(self.domain_intel, "_host_resolves_public",
                          return_value=False):
            self.assertFalse(self.domain_intel._host_resolves_public("lo-que-sea"))

    def test_ambos_son_funciones_del_modulo(self):
        import types
        self.assertIsInstance(self.resolver._is_safe_url, types.FunctionType)
        self.assertIsInstance(self.domain_intel._host_resolves_public,
                              types.FunctionType)
        self.assertEqual(self.resolver._is_safe_url.__module__,
                         "rastrillo.resolver")
        self.assertEqual(self.domain_intel._host_resolves_public.__module__,
                         "rastrillo.domain_intel")
