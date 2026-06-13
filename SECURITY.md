# Política de seguridad

## Reportar una vulnerabilidad

Si encuentras una vulnerabilidad, **no abras un issue público**.

Mándame un correo a [je7remy@gmail.com](mailto:je7remy@gmail.com)
describiendo:

- Componente afectado (ej. `rastrillo/server.py`, motor, resolver).
- Impacto que has visto o crees probable.
- Pasos para reproducirlo, si los tienes.

Respondo en máximo 72 horas con un acuse de recibo y una primera
valoración.

## Versiones soportadas

| Versión | Soporte |
|---|---|
| 0.1.0 | ✅ activa — fixes aplicados a `main` |

Rastrillo está en `0.1.0`. Los fixes de seguridad se aplican sobre `main`
y se incluyen en la siguiente release.

## Invariantes que respaldan el threat model

Estas propiedades son obligatorias en el código y los tests las verifican:

- **Cero contraseñas guardadas en disco.** La autenticación a sitios va
  por el perfil persistente de Chromium en `~/.rastrillo/browser-profile`;
  el humano se loguea una vez por sitio.
- **Token de auth obligatorio** en TODOS los POST y en todos los GET de
  `/api/*`. El frontend lo recibe por la URL inicial y lo guarda en
  `sessionStorage`. En producción solo viaja por header `X-Rastrillo-Token`.
- **Allowlist de hosts contra DNS rebinding.** El servidor rechaza con
  403 cualquier petición cuyo header `Host` no esté en
  `RASTRILLO_ALLOWED_HOSTS`.
- **SSRF guard en el resolver.** Solo `https://`, host debe resolver a
  IPs públicas; rechaza loopback, privadas, link-local, reservadas y
  multicast.
- **Backup + audit antes de acciones destructivas.** La DB se snapshot-ea
  a `~/.rastrillo/backups/` antes de `clear_accounts`. Cada delete,
  anonymize, mark-sent, own, discard, confirm-account queda en
  `~/.rastrillo/audit.json` (rota a `audit_<ts>.json` cuando supera 5 MB).
- **Solo cuentas del propio usuario.** Antes de cualquier acción
  destructiva el endpoint exige `confirm_owned=true` o que la cuenta esté
  marcada como propia.

## Lo que NO promete Rastrillo

- No resuelve CAPTCHA ni evade detección de bots.
- No verifica automáticamente la propiedad de una cuenta — la confirma el
  usuario.
- El agente IA puede equivocarse; antes de sellar `deleted` re-visita la
  URL del perfil y deja en `manual` si no puede confirmar.

Para el resto, ver [README](README.md) y `CLAUDE.md`.
