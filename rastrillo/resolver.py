"""Resolvedor en capas: SIEMPRE produce una acción concreta para cada cuenta.

Capas, en orden:
  1. Receta JSON (si existe)               → kind=auto, motor sigue receta
  2. Directorio JustDeleteMe                → kind=auto (con IA) o semi_auto
  3. Búsqueda web localizada (IA)           → kind=auto/semi_auto
  4. Sondeo de paths comunes                → kind=semi_auto
  5. Solicitud por correo (GDPR) — siempre  → kind=email_draft

Caché de hallazgos personales en ~/.rastrillo/discovered.json para no repetir
búsquedas. El usuario nunca debe ver una fila sin acción.

Invariantes:
  - No envía credenciales. Solo dominios y la estructura de la página al modelo.
  - Funciona sin ANTHROPIC_API_KEY (salta capas 2 y 4; cae a 3 o 5).
"""
from __future__ import annotations
import json
import logging
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import List, Optional

from . import ai_assist, config, directory

log = logging.getLogger("rastrillo.resolver")

DISCOVERED_PATH = config.BASE_DIR / "discovered.json"
_lock = threading.Lock()

_USER_AGENT = "rastrillo/0.1 (+local, contact via dashboard)"
_HTTP_TIMEOUT = 12.0


# --- Modelo de resultado ----------------------------------------------------
@dataclass
class Resolution:
    """Acción concreta que el dashboard mostrará al usuario.

    kind:
      auto        → encolar al motor (receta o directorio + IA)
      semi_auto   → link directo; el usuario lo abre y termina con un clic
      email_draft → borrador de correo GDPR listo para enviar
    """
    kind: str
    layer: str
    title: str
    notes: str = ""
    url: Optional[str] = None
    # Solo para email_draft:
    email_to: Optional[str] = None
    email_subject: Optional[str] = None
    email_body: Optional[str] = None
    language: str = "en"
    discovered_at: float = field(default_factory=time.time)

    def to_meta(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_meta(meta: dict) -> "Resolution":
        # Backward-compat: ignoramos claves desconocidas.
        allowed = Resolution.__annotations__.keys()
        return Resolution(**{k: v for k, v in meta.items() if k in allowed})


# --- Caché de hallazgos -----------------------------------------------------
def _read_discovered() -> dict:
    if not DISCOVERED_PATH.exists():
        return {}
    try:
        return json.loads(DISCOVERED_PATH.read_text(encoding="utf-8"))
    except Exception:
        log.exception("discovered.json corrupto; lo ignoro")
        return {}


def _write_discovered(data: dict) -> None:
    config.ensure_dirs()
    DISCOVERED_PATH.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _persist(host: str, res: Resolution) -> None:
    with _lock:
        data = _read_discovered()
        data[host.lower()] = res.to_meta()
        _write_discovered(data)


def _from_cache(host: str) -> Optional[Resolution]:
    with _lock:
        data = _read_discovered()
    raw = data.get(host.lower())
    if not raw:
        return None
    try:
        return Resolution.from_meta(raw)
    except Exception:
        return None


# --- Detección de idioma por TLD --------------------------------------------
_TLD_LANG = {
    "ru": "ru", "ua": "ru", "by": "ru", "kz": "ru",
    "es": "es", "mx": "es", "ar": "es", "cl": "es", "co": "es",
    "pe": "es", "do": "es", "uy": "es", "ve": "es",
    "br": "pt-BR", "pt": "pt",
    "fr": "fr", "be": "fr",
    "de": "de", "at": "de", "ch": "de",
    "it": "it",
    "jp": "ja",
    "cn": "zh-CN", "tw": "zh-TW",
    "kr": "ko",
}


def detect_language(host: str) -> str:
    if not host:
        return "en"
    tld = host.strip().lower().rstrip("/").split(".")[-1]
    return _TLD_LANG.get(tld, "en")


# --- HTTP helper -------------------------------------------------------------
def _http_get(url: str, timeout: float = _HTTP_TIMEOUT) -> Optional[tuple[int, str, str]]:
    """GET sencillo con UA y captura tolerante. Devuelve (status, final_url, body)
    o None si revienta. Limitamos body a 200 KB."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT,
                                                   "Accept-Language": "en,es,ru;q=0.8"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read(200_000).decode("utf-8", errors="ignore")
            return r.status, r.geturl(), body
    except urllib.error.HTTPError as e:
        # Algunas páginas devuelven 404 pero su body contiene info útil; lo aceptamos.
        try:
            body = e.read(200_000).decode("utf-8", errors="ignore")
        except Exception:
            body = ""
        return e.code, url, body
    except Exception as e:
        log.debug("GET %s falló: %s", url, e)
        return None


# --- Palabras clave de borrado (multi-idioma) -------------------------------
_DELETE_KEYWORDS = [
    "delete account", "close account", "deactivate account", "remove account",
    "cancel account", "delete my account", "close my account",
    "удалить аккаунт", "удалить учетную запись", "удалить профиль",
    "удалить учётную запись", "удаление аккаунта",
    "eliminar cuenta", "cerrar cuenta", "dar de baja", "borrar cuenta",
    "darse de baja", "eliminar perfil",
    "supprimer compte", "supprimer mon compte", "fermer compte",
    "konto löschen", "konto schließen",
    "excluir conta", "deletar conta", "encerrar conta",
    "elimina account", "chiudi account",
]


def _has_delete_keyword(text: str) -> Optional[str]:
    t = (text or "").lower()
    for kw in _DELETE_KEYWORDS:
        if kw in t:
            return kw
    return None


# --- Capa 3: sondeo de rutas comunes ----------------------------------------
_COMMON_PATHS = {
    "en": [
        "/settings", "/settings/account", "/account", "/account/settings",
        "/account/delete", "/account/close", "/account/deactivate",
        "/settings/delete", "/settings/close", "/settings/privacy",
        "/deactivate", "/privacy", "/preferences",
        "/help/delete-account", "/help/close-account",
        "/data-deletion", "/help/data-deletion",
        "/profile/delete", "/users/delete",
    ],
    "es": [
        "/cuenta", "/ajustes", "/configuracion", "/configuracion/cuenta",
        "/cuenta/eliminar", "/cuenta/cerrar", "/eliminar-cuenta",
        "/cerrar-cuenta", "/baja", "/dar-de-baja", "/perfil",
        "/perfil/eliminar", "/privacidad",
    ],
    "ru": [
        "/настройки", "/профиль", "/аккаунт", "/удалить",
        "/settings", "/account", "/profile",
    ],
    "pt-BR": [
        "/conta", "/configuracoes", "/preferencias",
        "/excluir-conta", "/encerrar-conta", "/privacidade",
    ],
    "fr": ["/compte", "/parametres", "/profil",
           "/supprimer-compte", "/confidentialite"],
    "de": ["/konto", "/einstellungen", "/profil",
           "/konto-loeschen", "/datenschutz"],
}


def _probe_paths(host: str, lang: str) -> List[dict]:
    """Prueba paths comunes. Devuelve los que respondieron OK y contienen
    keywords de borrado."""
    paths = list(_COMMON_PATHS.get(lang, [])) + _COMMON_PATHS["en"]
    seen = set()
    hits: List[dict] = []
    for p in paths:
        if p in seen:
            continue
        seen.add(p)
        # Encodeamos paths con caracteres no-ASCII para URLs válidas (e.g. ruso).
        try:
            url = f"https://{host}{urllib.parse.quote(p, safe='/')}"
        except Exception:
            continue
        r = _http_get(url, timeout=8)
        if not r:
            continue
        status, final_url, body = r
        if status >= 400:
            continue
        kw = _has_delete_keyword(body)
        if kw:
            hits.append({"url": final_url, "keyword": kw})
            if len(hits) >= 3:    # con 3 buenos paramos
                break
    return hits


def layer_probe(host: str, lang: str) -> Optional[Resolution]:
    hits = _probe_paths(host, lang)
    if not hits:
        return None
    first = hits[0]
    return Resolution(
        kind="semi_auto",
        layer="probe",
        title=f"Página de cuenta detectada en {host}",
        notes=(f"Ruta encontrada con palabra clave de borrado ({first['keyword']!r}). "
               "Ábrela y completa el flujo de cierre."),
        url=first["url"],
        language=lang,
    )


# --- Capa 2: búsqueda web localizada (IA) -----------------------------------
_QUERY_BY_LANG = {
    "ru": "как удалить аккаунт {host}",
    "es": "cómo eliminar cuenta {host}",
    "pt-BR": "como excluir conta {host}",
    "pt": "como apagar conta {host}",
    "fr": "comment supprimer compte {host}",
    "de": "wie konto löschen {host}",
    "it": "come eliminare account {host}",
    "ja": "{host} アカウント削除",
    "zh-CN": "{host} 删除账户",
    "ko": "{host} 계정 삭제",
    "en": "how to delete account {host}",
}


def layer_web_search(host: str, lang: str) -> Optional[Resolution]:
    """Pregunta al modelo con la tool web_search. Devuelve Resolution o None
    si no hay IA / no hay tool / la respuesta no es accionable."""
    if not ai_assist.available():
        return None
    query_tpl = _QUERY_BY_LANG.get(lang, _QUERY_BY_LANG["en"])
    query = query_tpl.format(host=host)
    res = ai_assist.web_search_deletion(host=host, query=query, language=lang)
    if not res or not res.get("url"):
        return None
    url = res["url"].strip()
    notes = (res.get("notes") or "").strip()
    method = (res.get("type") or "").lower()
    if method == "email_only":
        # La búsqueda nos dice que solo se borra por correo → tratamos como
        # email_draft sin contacto aún. Capa 5 completará si hace falta.
        return None
    kind = "auto" if ai_assist.available() else "semi_auto"
    return Resolution(
        kind=kind,
        layer="web_search",
        title=f"Borrado de cuenta en {host}",
        notes=notes or "URL hallada por búsqueda web.",
        url=url,
        language=lang,
    )


# --- Capa 5: solicitud por correo (GDPR) ------------------------------------
_PRIVACY_LINK_HINTS = {
    "en": ["privacy", "data protection", "gdpr", "policy", "terms"],
    "es": ["privacidad", "protección de datos", "política", "términos"],
    "ru": ["конфиденциальность", "приватность", "обработка персональных",
           "политика", "поддержка"],
    "pt-BR": ["privacidade", "proteção de dados", "termos"],
    "pt": ["privacidade", "termos"],
    "fr": ["confidentialité", "protection des données", "mentions légales"],
    "de": ["datenschutz", "impressum", "agb"],
    "it": ["privacy", "informativa", "termini"],
}

# Buzones que SÍ usamos para una solicitud de derecho al olvido. NOREPLY/etc fuera.
_GOOD_LOCALPART = re.compile(
    r"^(privacy|dpo|gdpr|datenschutz|confidentialite|privacidad|legal|"
    r"compliance|support|info|help|contact|hello|abuse)", re.I,
)
_BAD_LOCALPART = re.compile(r"^(noreply|no-reply|mailer|donotreply|jobs|careers)", re.I)
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")


def _find_privacy_emails(host: str, lang: str) -> List[str]:
    """Busca emails de privacidad/soporte en la home y en páginas vinculadas
    cuyo texto coincida con 'privacy/política/etc'."""
    home = _http_get(f"https://{host}/")
    if not home:
        return []
    _, _, body = home
    hints = _PRIVACY_LINK_HINTS.get(lang, _PRIVACY_LINK_HINTS["en"])
    candidate_links: List[str] = []
    for m in re.finditer(r'<a[^>]+href="([^"]+)"[^>]*>([^<]{0,120})</a>',
                         body, re.I):
        href, text = m.group(1), m.group(2).lower()
        if any(h in text for h in hints):
            full = urllib.parse.urljoin(f"https://{host}/", href)
            candidate_links.append(full)
    # Limitamos para no abusar; tomamos hasta 4 candidatos.
    candidate_links = list(dict.fromkeys(candidate_links))[:4]
    bodies = [body]
    for u in candidate_links:
        r = _http_get(u, timeout=8)
        if r:
            bodies.append(r[2])
    emails = []
    for b in bodies:
        for em in _EMAIL_RE.findall(b):
            em_low = em.lower()
            if "@" not in em_low:
                continue
            local = em_low.split("@", 1)[0]
            if _BAD_LOCALPART.match(local):
                continue
            # Si tiene pinta de privacidad/legal/soporte la priorizamos primero.
            emails.append(em_low)
    # Ordena: las que matchean GOOD_LOCALPART primero, sin duplicados.
    seen = set()
    sorted_emails: List[str] = []
    for em in sorted(set(emails), key=lambda x: (0 if _GOOD_LOCALPART.match(x.split("@")[0]) else 1)):
        if em in seen:
            continue
        seen.add(em)
        sorted_emails.append(em)
    return sorted_emails


_EMAIL_TEMPLATES = {
    "en": {
        "subject": "Request for account deletion (GDPR Article 17)",
        "body": """Dear Privacy Team,

I am writing to request the complete deletion of my personal data and account
on {host}. The account is identified by: {identifier}.

Under Article 17 of the EU General Data Protection Regulation (GDPR), I am
exercising my right to erasure. Please delete all personal data associated
with my account and confirm in writing once the deletion is complete.

Thank you for your prompt attention.

Best regards,
{identifier}
""",
    },
    "es": {
        "subject": "Solicitud de eliminación de cuenta (RGPD Art. 17)",
        "body": """Estimado equipo de privacidad:

Por la presente solicito la eliminación completa de mis datos personales y
de la cuenta asociada en {host}. La cuenta se identifica por: {identifier}.

En virtud del Artículo 17 del Reglamento General de Protección de Datos
(RGPD) de la UE, y de la legislación local aplicable, ejerzo mi derecho de
supresión. Procedan a eliminar toda la información personal asociada a mi
cuenta y a confirmarlo por escrito una vez completada la supresión.

Atentamente,
{identifier}
""",
    },
    "ru": {
        "subject": "Запрос на удаление аккаунта (ст. 17 GDPR / 152-ФЗ)",
        "body": """Здравствуйте,

Прошу полностью удалить мою учётную запись и все связанные с ней персональные
данные на сайте {host}. Идентификатор учётной записи: {identifier}.

В соответствии со статьёй 17 Общего регламента ЕС о защите данных (GDPR) и
Федеральным законом 152-ФЗ «О персональных данных» я реализую своё право на
удаление. Прошу удалить все мои персональные данные и подтвердить это в
письменном виде после завершения.

С уважением,
{identifier}
""",
    },
    "pt-BR": {
        "subject": "Solicitação de exclusão de conta (LGPD / RGPD Art. 17)",
        "body": """Prezada equipe de Privacidade,

Solicito a exclusão completa dos meus dados pessoais e da conta associada em
{host}. A conta é identificada por: {identifier}.

Com base no Artigo 17 do Regulamento Geral de Proteção de Dados da UE (RGPD)
e na Lei Geral de Proteção de Dados (LGPD) brasileira, exerço o meu direito
ao apagamento. Solicito que excluam todos os dados pessoais associados à
minha conta e confirmem por escrito após a conclusão.

Atenciosamente,
{identifier}
""",
    },
    "fr": {
        "subject": "Demande de suppression de compte (RGPD Art. 17)",
        "body": """Madame, Monsieur,

Je vous demande la suppression complète de mes données personnelles et du
compte associé sur {host}. Le compte est identifié par : {identifier}.

En vertu de l'article 17 du Règlement Général sur la Protection des Données
(RGPD), j'exerce mon droit à l'effacement. Veuillez supprimer toutes les
données personnelles associées à mon compte et me le confirmer par écrit.

Cordialement,
{identifier}
""",
    },
    "de": {
        "subject": "Antrag auf Löschung des Kontos (DSGVO Art. 17)",
        "body": """Sehr geehrtes Datenschutzteam,

hiermit beantrage ich die vollständige Löschung meiner personenbezogenen
Daten und meines Kontos auf {host}. Das Konto ist gekennzeichnet durch:
{identifier}.

Nach Artikel 17 der Datenschutz-Grundverordnung (DSGVO) übe ich mein Recht
auf Löschung aus. Bitte löschen Sie alle personenbezogenen Daten meines
Kontos und bestätigen Sie dies schriftlich nach Abschluss.

Mit freundlichen Grüßen,
{identifier}
""",
    },
}


def _build_email(host: str, identifier: str, lang: str,
                 to: Optional[str]) -> tuple[str, str]:
    tpl = _EMAIL_TEMPLATES.get(lang) or _EMAIL_TEMPLATES["en"]
    return tpl["subject"], tpl["body"].format(host=host, identifier=identifier)


def layer_gdpr(host: str, identifier: str, lang: str) -> Resolution:
    """Capa 5: SIEMPRE devuelve algo. Busca un buzón de privacidad; si no lo
    encuentra, devuelve un borrador genérico al usuario para que lo edite."""
    emails = _find_privacy_emails(host, lang)
    email_to = emails[0] if emails else f"privacy@{host}"
    subject, body = _build_email(host, identifier, lang, email_to)
    extra = ""
    if not emails:
        extra = ("\n[Nota] No detectamos un contacto de privacidad fiable. "
                 f"Hemos asumido privacy@{host}; revisa la web del sitio para "
                 "confirmar el correo correcto antes de enviar.")
    return Resolution(
        kind="email_draft",
        layer="gdpr",
        title=f"Solicitud de baja por correo a {host}",
        notes=("Borrador de solicitud de derecho al olvido (GDPR Art. 17). "
               "Revísalo y envíalo." + extra),
        url=f"https://{host}/",
        email_to=email_to,
        email_subject=subject,
        email_body=body,
        language=lang,
    )


# --- Orquestación: resolve() -------------------------------------------------
def resolve(host: str, identifier: str, force_refresh: bool = False) -> Resolution:
    """Devuelve SIEMPRE una Resolution para este host/identifier.

    Orden:
      1. Caché de hallazgos personales (a no ser que force_refresh).
      2. Directorio JustDeleteMe (kind=auto si hay IA, semi_auto si no).
      3. Web search con IA (Capa 2).
      4. Sondeo de paths comunes (Capa 3).
      5. Solicitud GDPR (Capa 5) — fallback que SIEMPRE produce algo.

    (La Capa 4 — bucle de IA conduciendo la página — la ejecuta el motor
    cuando recibe una Resolution con kind=auto, no este resolver.)
    """
    host = (host or "").strip().lower()
    if not host:
        return _emergency_fallback(identifier)

    if not force_refresh:
        cached = _from_cache(host)
        if cached:
            log.info("resolve(%s) caché: %s/%s", host, cached.layer, cached.kind)
            return cached

    lang = detect_language(host)
    log.info("resolve(%s) lang=%s", host, lang)

    # 1. Directorio
    entry = directory.lookup(host)
    if entry and entry.get("url") and entry.get("difficulty") != "impossible":
        kind = "auto" if ai_assist.available() else "semi_auto"
        res = Resolution(
            kind=kind, layer="directory",
            title=entry.get("name") or host,
            notes=entry.get("notes") or "",
            url=entry["url"],
            language=lang,
        )
        _persist(host, res)
        return res

    # 2. Web search (necesita IA)
    if ai_assist.available():
        try:
            res = layer_web_search(host, lang)
        except Exception as e:
            log.warning("layer_web_search(%s) excepcionó: %s", host, e)
            res = None
        if res:
            _persist(host, res)
            return res

    # 3. Sondeo de paths
    try:
        res = layer_probe(host, lang)
    except Exception as e:
        log.warning("layer_probe(%s) excepcionó: %s", host, e)
        res = None
    if res:
        _persist(host, res)
        return res

    # 5. GDPR fallback (siempre produce algo)
    try:
        res = layer_gdpr(host, identifier, lang)
    except Exception as e:
        log.warning("layer_gdpr(%s) excepcionó: %s", host, e)
        res = _emergency_fallback(identifier, host=host, lang=lang)
    _persist(host, res)
    return res


def _emergency_fallback(identifier: str, host: str = "", lang: str = "en") -> Resolution:
    """Última red de seguridad si TODO revienta. Borrador estático."""
    subject, body = _build_email(host or "(host desconocido)", identifier, lang, None)
    return Resolution(
        kind="email_draft",
        layer="fallback",
        title="Solicitud genérica de baja",
        notes="Resolución de emergencia: edita el borrador antes de enviar.",
        url=f"https://{host}/" if host else None,
        email_to=f"privacy@{host}" if host else None,
        email_subject=subject,
        email_body=body,
        language=lang,
    )
