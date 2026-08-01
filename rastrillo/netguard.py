"""El criterio anti-SSRF del proyecto, en UN solo sitio.

Hasta el Paso 5 este criterio estaba escrito dos veces, con el bucle de
comprobación de IPs **idéntico byte a byte**:

  - `resolver._is_safe_url(url)`   — valida una URL https antes de un GET.
  - `domain_intel._host_resolves_public(host)` — valida un host antes de
    abrir el socket TCP/43 de WHOIS.

Dos copias del mismo criterio de seguridad es una invitación a que un día se
arregle una y no la otra. Aquí vive la definición; allí quedan envoltorios
finos que conservan sus firmas y su semántica (una exige `https://`, la otra
valida un host a secas, y por eso siguen siendo dos funciones distintas).

Aviso (TOCTOU): hay una ventana entre resolver el host y abrir la
conexión; un DNS rebinding podría devolver IP pública aquí y privada
a la hora de conectar. Para este modelo (local, single-user, la
respuesta solo alimenta una regex de emails) es aceptable; cerrarlo
requeriría un urlopen custom que pase la IP ya validada.

Nota de implementación: `socket.getaddrinfo` se llama SIEMPRE como atributo
del módulo `socket`, nunca importado suelto. Los tests parchean
`socket.getaddrinfo` globalmente (`tests/test_ssrf_guard.py`), y un
`from socket import getaddrinfo` capturaría la referencia original y dejaría
los mocks sin efecto — con el resultado de que la suite haría DNS de verdad.
"""
from __future__ import annotations

import ipaddress
import logging
import socket
import urllib.parse

log = logging.getLogger("rastrillo.netguard")


def ip_publica(ip) -> bool:
    """¿`ip` es una dirección pública enrutable en internet?

    Rechaza loopback, privadas, link-local, reservadas, multicast y la
    dirección sin especificar. Acepta `str` o un objeto de `ipaddress`;
    una cadena que no parsea como IP devuelve False (no es una IP pública).
    """
    if not isinstance(ip, (ipaddress.IPv4Address, ipaddress.IPv6Address)):
        try:
            ip = ipaddress.ip_address(ip)
        except ValueError:
            return False
    return not (ip.is_private or ip.is_loopback or ip.is_link_local
                or ip.is_reserved or ip.is_multicast or ip.is_unspecified)


def host_es_publico(host: str, puerto=None, etiqueta: str = "guard") -> bool:
    """¿`host` resuelve EXCLUSIVAMENTE a IPs públicas?

    Basta UNA dirección no pública para rechazar: un host que resuelve a la
    vez a una IP pública y a 127.0.0.1 sirve igual para el ataque.

    `puerto` se pasa tal cual a `getaddrinfo`; los dos callers históricos
    usaban valores distintos (`None` en el resolver, `43` en WHOIS) y esa
    diferencia se conserva en vez de unificarla a ciegas.

    `etiqueta` solo sale en los logs de debug, para saber quién rechazó.
    """
    if not host:
        return False
    try:
        infos = socket.getaddrinfo(host, puerto)
    except OSError as e:
        log.debug("%s: %s no resuelve: %s", etiqueta, host, e)
        return False
    for info in infos:
        ip_str = info[4][0]
        if not ip_publica(ip_str):
            log.debug("%s: rechazo %s -> %s (IP no pública)",
                      etiqueta, host, ip_str)
            return False
    return True


def url_es_segura(url: str) -> bool:
    """Allowlist anti-SSRF para peticiones HTTP salientes.

    Reglas:
      - Solo `https://`. Bloqueamos http/file/ftp/gopher/etc.
      - El host debe resolver a IPs PÚBLICAS exclusivamente. Cualquier IP
        privada / loopback / link-local / reserved / multicast → no pasa.
        Esto cubre 127.0.0.1, 10.x, 192.168.x, 169.254.x, IPv6 link-local,
        etc.; suficiente para nuestro modelo de amenaza (el resolver corre en
        local del usuario y solo debe tocar internet, nunca hablar con el
        propio dashboard ni con la red interna del usuario).
    """
    try:
        parsed = urllib.parse.urlsplit(url)
    except Exception:
        return False
    if parsed.scheme != "https":
        log.debug("SSRF guard: esquema no permitido %s en %s",
                  parsed.scheme, url)
        return False
    try:
        host = parsed.hostname
    except ValueError:
        # urlsplit acepta cosas que hostname rechaza (p. ej. IPv6 mal cerrado).
        return False
    if not host:
        return False
    return host_es_publico(host, None, etiqueta="SSRF guard")
