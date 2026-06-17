# Changelog

Todos los cambios notables en este proyecto se documentan en este
fichero.

El formato sigue [Keep a Changelog](https://keepachangelog.com/es-ES/1.1.0/)
y el proyecto se adhiere a [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- **Domain Intelligence** (`rastrillo/domain_intel.py`): recon OSINT
  defensivo sobre un dominio. WHOIS (registrador, fechas de
  creación/expiración/actualización, estados, nameservers, registrant),
  DNS (A/MX/NS/TXT) y correlación heurística de proveedores/servicios
  (MX→correo, NS→DNS/hosting, SPF/verificaciones TXT→SaaS), cada
  inferencia etiquetada con confianza y evidencia. Endpoints
  `POST /api/domain/analyze` y `GET /api/domain/report`, tabla
  `domain_reports` en SQLite y una vista resumida en el dashboard.
  Pensado **solo para infra propia o que tienes permiso de auditar**.
- Guard anti-SSRF para el socket WHOIS:43 (`_host_resolves_public`),
  con el mismo criterio que el del resolver: solo IPs públicas.

### Dependencies

- Añadida **`dnspython>=2.6`** (única dep nueva), necesaria porque la
  stdlib no resuelve MX/NS/TXT. WHOIS se hace por socket TCP/43 con la
  stdlib (cero deps). Decisión y alternativas descartadas documentadas en
  la cabecera de `rastrillo/domain_intel.py` y en `CONTRIBUTING.md`.
  Reflejada en `pyproject.toml`, `requirements.txt` y `requirements.lock`
  (regenerado con `pip-compile`).

### Variables de entorno

- `RASTRILLO_WHOIS_TIMEOUT` (default 10 s) y `RASTRILLO_DNS_TIMEOUT`
  (default 5 s): timeouts por consulta del módulo Domain Intelligence.

## [0.1.0]

Release inicial. Esqueleto completo y verificado.

### Added

- Descubrimiento multi-fuente: Sherlock y Holehe siempre; Maigret y
  HaveIBeenPwned opcionales.
- Resolver por capas: recetas JSON → directorio JustDeleteMe → web search
  Anthropic → sondeo de paths → agente IA → borrador GDPR.
- Dashboard local FastAPI en `http://127.0.0.1:8765` con token de auth.
- Allowlist de hosts contra DNS rebinding.
- SSRF guard en el resolver (solo HTTPS, IPs públicas).
- Plantillas GDPR iniciales y de follow-up en 6 idiomas (EN, ES, RU,
  PT-BR, FR, DE), con detección por TLD.
- Engine Playwright sobre perfil persistente de Chromium, resumible
  (persistencia de `current_step` + hash de receta).
- Aprendizaje sobre la marcha: secuencias exitosas serializadas como
  recetas en `~/.rastrillo/recipes/` (omitiendo pasos `fill`).
- Modo simulación (`RASTRILLO_DRY_RUN`).
- Informes exportables en JSON, CSV y PDF (endpoint `/api/report` y CLI
  `rastrillo report`).
- Audit log append-only en `~/.rastrillo/audit.json` con rotación por
  tamaño.
- Backup automático de la DB antes de borrados masivos.
- 5 recetas de ejemplo: Reddit, Tumblr, Pinterest, Discord, Spotify, más
  plantilla de anonimizado.
- Endurecimiento contra falsos `deleted`: revisita HTTP a la
  `profile_url`, detección de redirect a login, keywords multi-idioma de
  cierre.
- Onboarding en una sola pasada (marker en `~/.rastrillo/onboarded.json`).
- Pool acotado del auto-resolver (`RASTRILLO_RESOLVER_WORKERS`,
  default 5, rango [1, 16]).
- Throttle por dominio en el resolver (`RASTRILLO_PROBE_DELAY`,
  default 1.5 s).
- Refresh automático del directorio JustDeleteMe si la caché supera
  `RASTRILLO_DIR_MAX_AGE_DAYS` (default 30).
- Empaquetado para AUR (`packaging/aur/PKGBUILD`).
- Instaladores idempotentes (`install.sh` para Linux/Mac,
  `install.ps1` para Windows) y wrappers sin activar venv
  (`rastrillo.sh`, `rastrillo.ps1`, `rastrillo.cmd`, `rs.sh`).
- Suite de **165 tests** con `unittest` de stdlib (sin deps nuevas).
- CI en GitHub Actions: tests sobre Python 3.10/3.11/3.12 y
  `pip-audit` sobre `requirements.lock`.

### Security

- Cero contraseñas guardadas en disco.
- Token de auth obligatorio en TODOS los POST y en todos los GET de
  `/api/*`.
- Human-in-the-loop obligatorio para CAPTCHA, 2FA y confirmaciones.

[Unreleased]: https://github.com/je7remy/Rastrillo/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/je7remy/Rastrillo/releases/tag/v0.1.0
