"""Domain Intelligence: recon OSINT defensivo sobre UN dominio.

Dado un dominio, reúne información PÚBLICA y la correlaciona en candidatos:

  - WHOIS (registrador, fechas de creación/expiración/actualización, estados,
    nameservers, registrant si es público) vía el protocolo TCP/43.
  - DNS (registros A, MX, NS, TXT) vía dnspython.
  - Correlación heurística: a partir de los registros ya obtenidos infiere
    proveedores y servicios asociados (MX → proveedor de correo, NS →
    proveedor de DNS/hosting, SPF/verificaciones TXT → SaaS de terceros).

ALCANCE — léelo. Esto AMPLÍA el alcance de Rastrillo más allá de "solo tus
cuentas": es inteligencia sobre un dominio. WHOIS y DNS son datos públicos y
la feature es legítima SOLO para dominios propios o que tienes permiso de
auditar (el "rastrillo corporativo"). La UI y la doc deben mantener ese
encuadre. No es para perfilar a terceros.

──────────────────────────────────────────────────────────────────────────
DECISIÓN DE DEPENDENCIAS (justificación, ver también CHANGELOG/CONTRIBUTING)
──────────────────────────────────────────────────────────────────────────
Estrategia C (híbrida):

  - WHOIS por socket TCP/43 con la stdlib. CERO dependencias: el protocolo
    WHOIS es texto plano sobre el puerto 43; abrir un socket, mandar el
    dominio y leer la respuesta es trivial y portable. Resolvemos el servidor
    autoritativo siguiendo las referencias de `whois.iana.org` (igual que
    haría el binario `whois`), sin depender de que exista un `whois` en PATH
    (no viene por defecto en Windows).

  - DNS con `dnspython` (ÚNICA dependencia nueva). La stdlib NO resuelve
    MX/NS/TXT (`socket.getaddrinfo` solo da A/AAAA) y envolver `dig`/`nslookup`
    como subprocesos sería frágil y roto-por-defecto en Windows. `dnspython`
    es pure-python, ampliamente auditado y portable; encaja en el flujo de
    `pip-compile`/`pip-audit`/lock del repo. Se importa de forma perezosa: si
    no está instalado, `lookup_dns` degrada con un error visible en vez de
    reventar al importar el módulo.

Estilo copiado de `discovery.py`: cada función de red devuelve un dict con
los datos + `error` (nunca lanza salvo input inválido), un fallo puntual no
aborta el resto, y los errores son VISIBLES (nunca una lista vacía silenciosa).

Egress (documentado en TRANSPARENCIA.md): servidores WHOIS (TCP/43, vía
referencia de IANA) y el resolutor DNS del sistema (vía dnspython, UDP/TCP 53).
La correlación NO hace HTTP extra: solo interpreta los registros ya obtenidos,
así que no abre ninguna vía de red nueva ni necesita el guard anti-SSRF.
Las consultas WHOIS sí pasan por un guard (`_host_resolves_public`) calcado
del criterio de `resolver._is_safe_url`: antes de abrir el socket 43
verificamos que el servidor resuelve solo a IPs públicas, para que una
referencia maliciosa no nos haga hablar con la red interna del usuario.

API pública:
  lookup_whois(domain) -> {registrar, created, expires, updated, status,
                           nameservers, registrant, raw, error}
  lookup_dns(domain)   -> {a, mx, ns, txt, error, incomplete}
  correlate(dns, whois)-> [{tipo, proveedor, evidencia, confidence}, ...]
  analyze(domain)      -> {domain, whois, dns, registrar, correlations,
                           errors, generated_at}
"""
from __future__ import annotations

import ipaddress
import logging
import os
import re
import socket
import time
from typing import List, Optional

log = logging.getLogger("rastrillo.domain_intel")


# --- Defaults configurables (patrón _env_int de discovery.py) ---------------
_DEF_WHOIS_TIMEOUT = 10    # segundos por consulta a un servidor WHOIS
_DEF_DNS_TIMEOUT = 5       # segundos por tipo de registro DNS
_MAX_WHOIS_HOPS = 3        # IANA → registro → registrar; corta bucles


def _env_int(name: str, default: int) -> int:
    try:
        return max(1, int(os.environ.get(name, "") or default))
    except ValueError:
        return default


# --- Validación de input (igual de blindada que _USERNAME_RE en discovery) --
# Un dominio: etiquetas alfanuméricas separadas por puntos, TLD de letras.
# Rechazamos vacíos, espacios y metacaracteres (anti-inyección al socket WHOIS).
_DOMAIN_RE = re.compile(
    r"^(?=.{1,253}$)(?!-)[A-Za-z0-9-]{1,63}(?<!-)"
    r"(?:\.(?!-)[A-Za-z0-9-]{1,63}(?<!-))*\.[A-Za-z]{2,63}$"
)


def valid_domain(domain: str) -> bool:
    return bool(domain) and bool(_DOMAIN_RE.match(domain.strip().lower()))


def _normalize(domain: str) -> str:
    """Normaliza para consulta: minúsculas, sin esquema, sin path ni puerto."""
    d = (domain or "").strip().lower()
    d = re.sub(r"^[a-z]+://", "", d)      # quita https:// si lo pegaron
    d = d.split("/", 1)[0]                # quita path
    d = d.split(":", 1)[0]                # quita puerto
    d = d.rstrip(".")                     # quita punto final del FQDN
    return d


# --- Guard anti-SSRF para el socket WHOIS -----------------------------------
def _host_resolves_public(host: str) -> bool:
    """¿`host` resuelve EXCLUSIVAMENTE a IPs públicas?

    Mismo criterio que `resolver._is_safe_url` pero para TCP/43: antes de
    abrir el socket a un servidor WHOIS (que descubrimos siguiendo una
    referencia y por tanto no controlamos del todo) verificamos que no
    apunte a loopback / privadas / link-local / reservadas / multicast.
    Así una referencia envenenada no nos hace hablar con la red interna.
    """
    if not host:
        return False
    try:
        infos = socket.getaddrinfo(host, 43)
    except OSError as e:
        log.debug("WHOIS guard: %s no resuelve: %s", host, e)
        return False
    for info in infos:
        ip_str = info[4][0]
        try:
            ip = ipaddress.ip_address(ip_str)
        except ValueError:
            return False
        if (ip.is_private or ip.is_loopback or ip.is_link_local
                or ip.is_reserved or ip.is_multicast or ip.is_unspecified):
            log.debug("WHOIS guard: rechazo %s -> %s (IP no pública)", host, ip)
            return False
    return True


# --- Primitiva de red WHOIS (mockeable en tests) ----------------------------
def _whois_query(server: str, query: str, timeout: float) -> str:
    """Abre TCP/43 contra `server`, manda `query\\r\\n` y devuelve la respuesta.

    Es la ÚNICA función que toca la red para WHOIS; los tests la parchean
    para correr offline. Aplica el guard anti-SSRF antes de conectar.
    """
    if not _host_resolves_public(server):
        raise OSError(f"servidor WHOIS no permitido (no resuelve a IP pública): {server}")
    with socket.create_connection((server, 43), timeout=timeout) as sock:
        sock.settimeout(timeout)
        sock.sendall((query + "\r\n").encode("ascii", errors="ignore"))
        chunks: List[bytes] = []
        total = 0
        while True:
            data = sock.recv(4096)
            if not data:
                break
            chunks.append(data)
            total += len(data)
            if total > 256_000:    # cota dura: respuestas WHOIS son pequeñas
                break
    return b"".join(chunks).decode("utf-8", errors="ignore")


# Servidor raíz: IANA nos dice el WHOIS autoritativo del TLD.
_IANA_WHOIS = "whois.iana.org"

# Referencias a un siguiente servidor WHOIS (IANA usa `refer:`/`whois:`,
# los registros usan `Registrar WHOIS Server:`/`ReferralServer:`).
_REFERRAL_RE = re.compile(
    r"^(?:refer|whois|registrar whois server|referralserver)\s*:\s*"
    r"(?:r?whois://)?([A-Za-z0-9.\-]+)",
    re.I | re.M,
)


def _find_referral(text: str) -> Optional[str]:
    m = _REFERRAL_RE.search(text or "")
    if not m:
        return None
    host = m.group(1).strip().lower().rstrip(".")
    # ReferralServer puede traer puerto (whois://host:43): lo cortamos.
    host = host.split(":", 1)[0]
    return host or None


# --- Parsing WHOIS (puro, sin red — fácilmente testeable) -------------------
# Cada campo lista los nombres de etiqueta vistos entre TLDs/registradores.
# Case-insensitive y multiline. Para campos de un solo valor tomamos la
# respuesta MÁS específica (la del registrar gana a la del registro).
_FIELD_PATTERNS = {
    "registrar": [
        r"^\s*Registrar\s*:\s*(.+?)\s*$",
        r"^\s*Sponsoring Registrar\s*:\s*(.+?)\s*$",
        r"^\s*registrar\s*:\s*(.+?)\s*$",
    ],
    "created": [
        r"^\s*Creation Date\s*:\s*(.+?)\s*$",
        r"^\s*Created On\s*:\s*(.+?)\s*$",
        r"^\s*created\s*:\s*(.+?)\s*$",
        r"^\s*Registered on\s*:\s*(.+?)\s*$",
        r"^\s*Domain Registration Date\s*:\s*(.+?)\s*$",
    ],
    "expires": [
        r"^\s*Registry Expiry Date\s*:\s*(.+?)\s*$",
        r"^\s*Registrar Registration Expiration Date\s*:\s*(.+?)\s*$",
        r"^\s*Expiry Date\s*:\s*(.+?)\s*$",
        r"^\s*Expiration Date\s*:\s*(.+?)\s*$",
        r"^\s*Expires On\s*:\s*(.+?)\s*$",
        r"^\s*paid-till\s*:\s*(.+?)\s*$",
        r"^\s*expire\s*:\s*(.+?)\s*$",
    ],
    "updated": [
        r"^\s*Updated Date\s*:\s*(.+?)\s*$",
        r"^\s*Last Updated On\s*:\s*(.+?)\s*$",
        r"^\s*last-update\s*:\s*(.+?)\s*$",
        r"^\s*changed\s*:\s*(.+?)\s*$",
    ],
    "registrant": [
        r"^\s*Registrant Organization\s*:\s*(.+?)\s*$",
        r"^\s*Registrant Name\s*:\s*(.+?)\s*$",
        r"^\s*org\s*:\s*(.+?)\s*$",
    ],
}

# Valores múltiples: estados y nameservers.
_STATUS_RE = re.compile(r"^\s*(?:Domain Status|status)\s*:\s*(.+?)\s*$", re.I | re.M)
_NS_RE = re.compile(r"^\s*(?:Name Server|nserver|Nameservers?)\s*:\s*(.+?)\s*$", re.I | re.M)

# Marcadores de "no encontrado" que algunos registros devuelven con 200.
_NOT_FOUND_RE = re.compile(
    r"(no match|not found|no data found|no entries found|status:\s*free|"
    r"domain not found|no object found)",
    re.I,
)


def _first(responses: List[str], patterns: List[str]) -> Optional[str]:
    """Primer match de cualquiera de los patrones, recorriendo las respuestas
    de la MÁS específica (última) a la más genérica (primera)."""
    for text in reversed(responses):
        for pat in patterns:
            m = re.search(pat, text, re.I | re.M)
            if m:
                val = m.group(1).strip()
                if val:
                    return val
    return None


def _all(responses: List[str], regex: re.Pattern) -> List[str]:
    """Todos los valores de un campo multi-valor, dedup preservando orden."""
    seen = set()
    out: List[str] = []
    for text in responses:
        for m in regex.finditer(text):
            val = m.group(1).strip().lower().rstrip(".")
            # Domain Status suele venir como "clientTransferProhibited https://..."
            if val and val not in seen:
                seen.add(val)
                out.append(val)
    return out


def _parse_whois(responses: List[str]) -> dict:
    """Convierte una o varias respuestas WHOIS crudas en campos estructurados."""
    out = {
        "registrar": _first(responses, _FIELD_PATTERNS["registrar"]),
        "created": _first(responses, _FIELD_PATTERNS["created"]),
        "expires": _first(responses, _FIELD_PATTERNS["expires"]),
        "updated": _first(responses, _FIELD_PATTERNS["updated"]),
        "registrant": _first(responses, _FIELD_PATTERNS["registrant"]),
        "status": _all(responses, _STATUS_RE),
        "nameservers": _all(responses, _NS_RE),
    }
    return out


def lookup_whois(domain: str) -> dict:
    """Consulta WHOIS recursiva (IANA → registro → registrar).

    Devuelve {registrar, created, expires, updated, status, nameservers,
    registrant, raw, error}. Nunca lanza: un fallo de red sale en `error`.
    `raw` es la respuesta más específica recortada (sin credenciales: WHOIS
    es público).
    """
    base = {
        "registrar": None, "created": None, "expires": None, "updated": None,
        "status": [], "nameservers": [], "registrant": None,
        "raw": None, "error": None,
    }
    domain = _normalize(domain)
    if not valid_domain(domain):
        base["error"] = f"dominio inválido: {domain!r}"
        return base

    timeout = float(_env_int("RASTRILLO_WHOIS_TIMEOUT", _DEF_WHOIS_TIMEOUT))
    responses: List[str] = []
    server = _IANA_WHOIS
    seen: set = set()
    last_error: Optional[str] = None

    for _ in range(_MAX_WHOIS_HOPS):
        if not server or server in seen:
            break
        seen.add(server)
        try:
            resp = _whois_query(server, domain, timeout)
        except (OSError, socket.timeout) as e:
            last_error = f"whois {server}: {e}"
            log.info("lookup_whois(%s): %s", domain, last_error)
            break
        except Exception as e:   # defensivo: no abortamos analyze() por esto
            last_error = f"whois {server}: {type(e).__name__}: {e}"
            log.warning("lookup_whois(%s) inesperado: %s", domain, e)
            break
        if resp:
            responses.append(resp)
        nxt = _find_referral(resp)
        if not nxt or nxt == server:
            break
        server = nxt

    if not responses:
        base["error"] = last_error or "WHOIS no devolvió datos"
        return base

    parsed = _parse_whois(responses)
    base.update(parsed)
    base["raw"] = responses[-1][:8000]

    # Diagnóstico visible: respondió pero parece "no registrado" o sin campos.
    if _NOT_FOUND_RE.search(responses[-1]) and not base["registrar"]:
        base["error"] = "el servidor WHOIS no tiene registro para este dominio"
    elif not any((base["registrar"], base["created"], base["expires"],
                  base["nameservers"])):
        base["error"] = (last_error or
                         "WHOIS respondió pero no pude extraer campos conocidos")
    return base


# --- Primitiva DNS (mockeable; importa dnspython de forma perezosa) ---------
def _dns_query(name: str, rtype: str, timeout: float) -> List[str]:
    """Resuelve `rtype` para `name` y devuelve los valores como texto.

    Única función que toca el resolutor DNS; los tests la parchean. Importa
    dnspython aquí dentro para que el módulo cargue aunque la dependencia no
    esté instalada (en ese caso `lookup_dns` lo reporta como error).
    """
    import dns.resolver   # import perezoso: ver decisión de dependencias arriba

    resolver = dns.resolver.Resolver()
    resolver.timeout = timeout
    resolver.lifetime = timeout
    answers = resolver.resolve(name, rtype)
    out: List[str] = []
    for rdata in answers:
        out.append(rdata.to_text())
    return out


def lookup_dns(domain: str) -> dict:
    """Resuelve A, MX, NS y TXT. Devuelve {a, mx, ns, txt, error, incomplete}.

    Un fallo en un tipo concreto (NoAnswer, timeout…) NO aborta el resto: se
    deja ese tipo vacío y se marca `incomplete=True` con detalle en `error`.
    NXDOMAIN se reporta como error global (el dominio no existe).
    """
    out = {"a": [], "mx": [], "ns": [], "txt": [], "error": None, "incomplete": False}
    domain = _normalize(domain)
    if not valid_domain(domain):
        out["error"] = f"dominio inválido: {domain!r}"
        return out

    timeout = float(_env_int("RASTRILLO_DNS_TIMEOUT", _DEF_DNS_TIMEOUT))

    # Excepciones de dnspython que tratamos por tipo. Import perezoso: si
    # dnspython no está, el primer _dns_query lanza ImportError y lo
    # reportamos como error global degradando con elegancia.
    try:
        import dns.resolver  # noqa: F401
        import dns.exception
        nxdomain = dns.resolver.NXDOMAIN
        no_answer = dns.resolver.NoAnswer
        no_ns = dns.resolver.NoNameservers
        dns_timeout = dns.exception.Timeout
    except ImportError as e:
        out["error"] = f"dnspython no instalado ({e}); DNS no disponible"
        out["incomplete"] = True
        return out

    errors: List[str] = []
    for rtype in ("A", "MX", "NS", "TXT"):
        try:
            vals = _dns_query(domain, rtype, timeout)
        except nxdomain:
            out["error"] = "NXDOMAIN: el dominio no existe en el DNS"
            out["incomplete"] = True
            return out
        except no_answer:
            continue   # sin registros de ese tipo: normal, no es error
        except (no_ns, dns_timeout) as e:
            errors.append(f"{rtype}: {type(e).__name__}")
            out["incomplete"] = True
            continue
        except Exception as e:   # defensivo
            errors.append(f"{rtype}: {type(e).__name__}: {e}")
            out["incomplete"] = True
            continue
        key = rtype.lower()
        if rtype == "TXT":
            # dnspython devuelve los TXT entre comillas; las quitamos y unimos
            # los fragmentos de un mismo registro largo.
            out[key] = [_clean_txt(v) for v in vals]
        else:
            out[key] = [v.strip().rstrip(".") if rtype != "MX" else v.strip()
                        for v in vals]
    if errors:
        out["error"] = "; ".join(errors)
    return out


def _clean_txt(value: str) -> str:
    """Normaliza un TXT: quita comillas y une fragmentos `"a" "b"` → `ab`."""
    parts = re.findall(r'"([^"]*)"', value)
    if parts:
        return "".join(parts)
    return value.strip().strip('"')


# --- Correlación (heurística, SIN red) --------------------------------------
# Reglas data-driven (estilo "directorio"): empiezan con un set pequeño y
# honesto y son AMPLIABLES. Cada inferencia lleva confianza + evidencia; NO
# afirmamos un hecho ("usa X") sino una correlación ("MX apunta a X").
#
# confidence:
#   high   → el registro enruta/delegada directamente (MX, NS).
#   medium → autorización para enviar correo en su nombre (SPF include) o
#            registro A (puede ser CDN/proxy, no el origen real).
#   low    → token de verificación: indica que el dominio se dio de alta en
#            ese servicio en algún momento, no que lo use activamente.
_RECORD_RULES = [
    # (tipo_registro, needle_en_minúsculas, tipo, proveedor, confidence)
    ("mx", "aspmx.l.google.com", "correo", "Google Workspace", "high"),
    ("mx", "googlemail.com", "correo", "Google Workspace", "high"),
    ("mx", "google.com", "correo", "Google Workspace", "high"),
    ("mx", "protection.outlook.com", "correo", "Microsoft 365", "high"),
    ("mx", "outlook.com", "correo", "Microsoft 365", "high"),
    ("mx", "zoho.com", "correo", "Zoho Mail", "high"),
    ("mx", "zoho.eu", "correo", "Zoho Mail", "high"),
    ("mx", "messagingengine.com", "correo", "Fastmail", "high"),
    ("mx", "amazonaws.com", "correo", "Amazon WorkMail/SES", "medium"),
    ("ns", "cloudflare.com", "dns", "Cloudflare", "high"),
    ("ns", "awsdns", "dns", "AWS Route 53", "high"),
    ("ns", "azure-dns", "dns", "Azure DNS", "high"),
    ("ns", "googledomains.com", "dns", "Google Domains", "high"),
    ("ns", "domaincontrol.com", "dns", "GoDaddy", "high"),
    ("ns", "nsone.net", "dns", "NS1", "high"),
    ("ns", "dnsimple.com", "dns", "DNSimple", "high"),
]

# SPF includes (TXT que empieza por v=spf1): autorizaciones de envío.
_SPF_INCLUDES = [
    ("_spf.google.com", "correo", "Google Workspace (SPF)", "medium"),
    ("spf.protection.outlook.com", "correo", "Microsoft 365 (SPF)", "medium"),
    ("zoho.com", "correo", "Zoho (SPF)", "medium"),
    ("amazonses.com", "correo transaccional", "Amazon SES", "medium"),
    ("sendgrid.net", "correo transaccional", "SendGrid", "medium"),
    ("mailgun.org", "correo transaccional", "Mailgun", "medium"),
    ("spf.mandrillapp.com", "correo transaccional", "Mailchimp/Mandrill", "medium"),
    ("servers.mcsv.net", "correo transaccional", "Mailchimp", "medium"),
    ("_spf.salesforce.com", "SaaS", "Salesforce", "medium"),
    ("_spf.intacct.com", "SaaS", "Sage Intacct", "medium"),
]

# Tokens de verificación en TXT: dominio dado de alta en un servicio.
_TXT_VERIFICATION = [
    ("google-site-verification", "verificación", "Google", "low"),
    ("facebook-domain-verification", "verificación", "Meta / Facebook", "low"),
    ("ms=", "verificación", "Microsoft 365", "low"),
    ("stripe-verification", "verificación", "Stripe", "low"),
    ("atlassian-domain-verification", "verificación", "Atlassian", "low"),
    ("adobe-idp-site-verification", "verificación", "Adobe", "low"),
    ("docusign=", "verificación", "DocuSign", "low"),
    ("apple-domain-verification", "verificación", "Apple", "low"),
    ("zoom-domain-verification", "verificación", "Zoom", "low"),
]


def correlate(dns: dict, whois: Optional[dict] = None) -> List[dict]:
    """Infiere proveedores/servicios a partir de los registros ya obtenidos.

    NO hace ninguna petición de red: solo interpreta `dns` (y opcionalmente
    `whois`). Devuelve una lista de candidatos
    `{tipo, proveedor, evidencia, confidence}`, deduplicados por proveedor.
    """
    dns = dns or {}
    inferences: List[dict] = []
    seen: set = set()

    def _add(tipo, proveedor, evidencia, confidence):
        key = (tipo, proveedor)
        if key in seen:
            return
        seen.add(key)
        inferences.append({
            "tipo": tipo, "proveedor": proveedor,
            "evidencia": evidencia, "confidence": confidence,
        })

    # MX / NS por substring.
    for rtype, needle, tipo, proveedor, conf in _RECORD_RULES:
        for rec in dns.get(rtype) or []:
            if needle in rec.lower():
                _add(tipo, proveedor, f"{rtype.upper()} → {rec}", conf)
                break

    # TXT: SPF includes y tokens de verificación.
    for txt in dns.get("txt") or []:
        low = txt.lower()
        if low.startswith("v=spf1"):
            for needle, tipo, proveedor, conf in _SPF_INCLUDES:
                if needle in low:
                    _add(tipo, proveedor, f"SPF include:{needle}", conf)
        for needle, tipo, proveedor, conf in _TXT_VERIFICATION:
            if needle in low:
                ev = txt if len(txt) <= 60 else txt[:57] + "…"
                _add(tipo, proveedor, f"TXT {ev}", conf)

    return inferences


# --- Orquestación -----------------------------------------------------------
def analyze(domain: str) -> dict:
    """Orquesta WHOIS + DNS + correlación y devuelve un informe completo.

    Lanza ValueError SOLO si el dominio es inválido (el endpoint lo mapea a
    400). Cualquier otro fallo (timeout WHOIS, DNS incompleto…) sale en
    `errors` sin abortar: el informe siempre se devuelve.
    """
    norm = _normalize(domain)
    if not valid_domain(norm):
        raise ValueError(f"dominio inválido: {domain!r}")

    errors: List[dict] = []

    whois = lookup_whois(norm)
    if whois.get("error"):
        errors.append({"source": "whois", "error": whois["error"]})

    dns = lookup_dns(norm)
    if dns.get("error"):
        errors.append({"source": "dns", "error": dns["error"]})

    correlations = correlate(dns, whois)

    return {
        "domain": norm,
        "whois": whois,
        "dns": dns,
        "registrar": whois.get("registrar"),
        "correlations": correlations,
        "errors": errors,
        "generated_at": time.time(),
    }
