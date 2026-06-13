#!/usr/bin/env python3
"""CLI de Rastrillo.

Rastrillo: herramienta local y privada que rastrea tu huella digital, encuentra
tus cuentas regadas por la web y las borra o anonimiza de un solo pase, dejando
solo las que quieres conservar.

Modos:
  rastrillo                                arranca el dashboard y abre el navegador (default)
  rastrillo scan --user X [--email Y]      descubre cuentas en headless (sin UI)
  rastrillo list [--status STATUS]         lista las cuentas conocidas
  rastrillo report --format json|csv|pdf --out FILE
                                           genera el informe completo a fichero
  rastrillo run                            (debug) procesa la cola desde terminal

INVARIANTE: el modo CLI **NO** ejecuta acciones destructivas headless.
El flujo de borrado sigue exigiendo dashboard web + Chromium (human-in-the-loop).
`scan` solo descubre, `list` solo lee, `report` solo exporta. Las decisiones
sobre cada cuenta (Eliminar/Anonimizar/Conservar) las sigue tomando el usuario
desde la UI, como impone el invariante del proyecto.
"""
import argparse
import sys
import threading
import time
import webbrowser
from pathlib import Path

from rastrillo import db, config

HOST = "127.0.0.1"
PORT = 8765
URL = f"http://{HOST}:{PORT}"


# ── default: dashboard + Chromium (comportamiento de siempre) ───────────────
def cmd_default(_args):
    """Modo por defecto: levanta uvicorn, abre el navegador, queda corriendo."""
    import uvicorn
    from rastrillo.server import app
    from rastrillo import jobs

    db.init()
    jobs.start_workers()
    # Tier 3.1: si el directorio cacheado tiene > RASTRILLO_DIR_MAX_AGE_DAYS,
    # lo refrescamos en background al arrancar. No bloquea el servidor.
    jobs.start_dir_refresh_if_stale()

    # Auth local: el token se generó en config.AUTH_TOKEN al importarse el
    # módulo. Va en la URL inicial (sessionStorage lo recoge) y la consola
    # también lo imprime, por si quieres pegar la URL en otro navegador.
    token = config.AUTH_TOKEN
    url_with_token = f"{URL}/?token={token}"

    # Abrir el navegador en cuanto el servidor responda. Lo hacemos en un thread
    # para no bloquear el arranque de uvicorn.
    def _open_when_ready():
        import socket
        deadline = time.time() + 8.0
        while time.time() < deadline:
            try:
                with socket.create_connection((HOST, PORT), timeout=0.4):
                    break
            except OSError:
                time.sleep(0.15)
        try:
            webbrowser.open(url_with_token)
        except Exception:
            pass

    threading.Thread(target=_open_when_ready, daemon=True,
                     name="rastrillo-openbrowser").start()

    print(f"Rastrillo corriendo en {URL}")
    print(f"   abre en el navegador: {url_with_token}")
    if config.DRY_RUN:
        print("   ⚠  DRY-RUN activado: ninguna acción destructiva se ejecutará")
    uvicorn.run(app, host=HOST, port=PORT, log_level="warning")


# ── scan: discovery headless (NO destructivo) ───────────────────────────────
def cmd_scan(args):
    """Corre Sherlock/Holehe (+Maigret/HIBP si disponibles) sin abrir UI.

    Imprime un resumen y deja las cuentas en la DB para futuros runs del
    dashboard. NO toca ninguna cuenta — sigue el invariante de
    human-in-the-loop para acciones destructivas.
    """
    from rastrillo.discovery import discover

    usernames = [u.strip() for u in (args.user or []) if u and u.strip()]
    emails = [e.strip() for e in (args.email or []) if e and e.strip()]
    if not usernames and not emails:
        print("error: da al menos un --user o un --email", file=sys.stderr)
        return 2

    print(f"Escaneando: usernames={usernames or '-'} emails={emails or '-'}")
    summary = discover(usernames, emails)
    print()
    print(f"  encontradas: {summary['found']}")
    print(f"  conservadas (KEEP_PLATFORMS): {summary['kept']}")
    raw = summary.get("raw_counts", {})
    print("  por fuente (crudo → guardado):")
    for k in ("sherlock", "holehe", "hibp", "maigret"):
        c = raw.get(k, 0)
        s = raw.get(f"{k}_saved", 0)
        print(f"    {k:<10}: {c} → {s}")
    if summary.get("no_recipe"):
        print(f"  plataformas sin receta: {len(summary['no_recipe'])}")
    if summary.get("errors"):
        print(f"  errores: {len(summary['errors'])}")
        for e in summary["errors"][:6]:
            print(f"    [{e.get('source')}] {e.get('id')}: {e.get('error')}")
    return 0


# ── list: leer la DB y volcar a stdout ──────────────────────────────────────
def cmd_list(args):
    rows = db.list_accounts(status=args.status)
    if not rows:
        if args.status:
            print(f"Nada con status={args.status!r}.")
        else:
            print("Nada todavía. Arranca `rastrillo` y escanea desde la web "
                  "o usa `rastrillo scan --user X`.")
        return 0
    for r in rows:
        print(f"  [{r['status']:<13}] {r['platform']:<16} "
              f"{r['identifier'] or '':<28} {r['last_message'] or ''}")
    print("\nResumen:", dict(db.stats()))
    return 0


# ── report: usa rastrillo.reports (compartido con /api/report) ──────────────
def cmd_report(args):
    """Escribe el informe completo al fichero indicado en `--out`.

    El contenido es idéntico al que devuelve el endpoint `/api/report` con
    el mismo `--format`. Ningún borrado ni acción destructiva: solo
    exportar.
    """
    from rastrillo import reports
    import json as _json

    fmt = args.format.lower()
    try:
        content, _media, _filename = reports.build_report(fmt)
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, dict):
        # JSON: dict → texto indentado para que sea legible al volcar a
        # fichero (el endpoint devuelve el dict crudo; aquí preferimos
        # legibilidad humana).
        out.write_text(_json.dumps(content, ensure_ascii=False, indent=2),
                       encoding="utf-8")
    else:
        out.write_bytes(content)
    print(f"Informe ({fmt}) escrito en {out}")
    return 0


# ── run: debug helper que ya existía ────────────────────────────────────────
def cmd_run(_args):
    """Procesa la cola desde terminal (sin abrir UI). Útil para debug.

    NO es modo destructivo headless: las recetas siguen pausando para que
    el usuario resuelva CAPTCHA/2FA en el Chromium. Igual que el flujo de
    siempre, pero arrancado desde la terminal.
    """
    from rastrillo.engine import process_queue
    process_queue(headless=False)


def build_parser() -> argparse.ArgumentParser:
    """Construye el parser de argparse. Extraído para poder testearlo sin
    ejecutar nada."""
    p = argparse.ArgumentParser(
        prog="rastrillo",
        description=(
            "Rastrillo: herramienta local y privada que rastrea tu huella "
            "digital, encuentra tus cuentas regadas por la web y las borra "
            "o anonimiza de un solo pase. Sin argumentos: arranca el "
            "dashboard y abre el navegador."
        ),
    )
    sub = p.add_subparsers(dest="cmd")

    sc = sub.add_parser("scan",
        help="discovery headless (sin abrir UI): sherlock + holehe + opcionalmente maigret/hibp")
    sc.add_argument("--user", action="append", default=[],
        help="username a buscar (repite la opción para varios)")
    sc.add_argument("--email", action="append", default=[],
        help="email a buscar (repite la opción para varios)")
    sc.set_defaults(func=cmd_scan)

    sl = sub.add_parser("list",
        help="lista las cuentas conocidas en la DB local")
    sl.add_argument("--status",
        help="filtrar por estado (found, deleted, email_draft, ...)")
    sl.set_defaults(func=cmd_list)

    sr = sub.add_parser("report",
        help="exporta el informe completo a un fichero (json/csv/pdf)")
    sr.add_argument("--format", required=True, choices=("json", "csv", "pdf"),
        help="formato de salida")
    sr.add_argument("--out", required=True,
        help="ruta del fichero de salida (se crean los directorios padres)")
    sr.set_defaults(func=cmd_report)

    srun = sub.add_parser("run",
        help="(debug) procesa la cola desde terminal sin abrir UI")
    srun.set_defaults(func=cmd_run)

    return p


def main():
    config.ensure_dirs()
    db.init()

    parser = build_parser()
    args = parser.parse_args()
    func = getattr(args, "func", cmd_default)
    rc = func(args)
    return rc or 0


if __name__ == "__main__":
    sys.exit(main())
