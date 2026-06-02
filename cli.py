#!/usr/bin/env python3
"""CLI de Rastrillo.

Rastrillo: herramienta local y privada que rastrea tu huella digital, encuentra
tus cuentas regadas por la web y las borra o anonimiza de un solo pase, dejando
solo las que quieres conservar.

Uso típico:
  rastrillo scan -u je7remy -u otro_user -e correo@gmail.com
  rastrillo list
  rastrillo run                 # procesa la cola (abre navegador, pausa cuando toca)
  rastrillo run --only reddit   # solo una plataforma
  rastrillo dashboard           # levanta el panel web

(Si no instalaste el paquete: `python cli.py ...` funciona igual.)
"""
import argparse
import sys

from rastrillo import db, config
from rastrillo.discovery import discover
from rastrillo.engine import process_queue, Engine


def cmd_scan(args):
    print("Escaneando (esto puede tardar)...")
    summary = discover(args.username or [], args.email or [])
    print(f"\n✔ Detectadas: {summary['found']} | Conservadas: {summary['kept']}")
    if summary.get("no_recipe"):
        print("\n⚠ Sin receta (requieren que añadas una o se harán manuales):")
        for p in summary["no_recipe"]:
            print(f"   - {p}")
        print("   Crea recetas en ~/.rastrillo/recipes/  (ver rastrillo/recipes/*.json de ejemplo)")
    if summary.get("errors"):
        print("\n⚠ Errores durante el escaneo (no fatales):")
        for e in summary["errors"]:
            print(f"   - [{e['source']}] {e['id']}: {e['error']}")


def cmd_list(args):
    rows = db.list_accounts(status=args.status)
    if not rows:
        print("Nada todavía. Corre `scan` primero.")
        return
    for r in rows:
        print(f"  [{r['status']:<13}] {r['platform']:<16} {r['identifier'] or '':<28} {r['last_message'] or ''}")
    print("\nResumen:", dict(db.stats()))


def cmd_run(args):
    if args.only:
        db.init()
        eng = Engine(headless=False)
        targets = [r for r in db.list_accounts() if r["platform"] == args.only]
        if not targets:
            print(f"No hay cuentas para '{args.only}'.")
            return
        for acc in targets:
            print(f"\n▶ {acc['platform']} ({acc['identifier']})")
            eng.run_account(acc["id"])
    else:
        process_queue(headless=False)
    print("\nListo. Abre el dashboard para ver el estado: rastrillo dashboard")


def cmd_dashboard(args):
    import uvicorn
    from rastrillo.server import app
    db.init()
    print("Dashboard en http://127.0.0.1:8765  (Ctrl+C para salir)")
    uvicorn.run(app, host="127.0.0.1", port=8765, log_level="warning")


def cmd_skip(args):
    db.init()
    for r in db.list_accounts():
        if r["platform"] == args.platform:
            db.set_status(r["id"], "skipped", "saltada manualmente")
    print(f"'{args.platform}' marcada como conservada.")


def main():
    config.ensure_dirs()
    db.init()
    p = argparse.ArgumentParser(
        prog="rastrillo",
        description=(
            "Rastrillo: herramienta local y privada que rastrea tu huella digital, "
            "encuentra tus cuentas regadas por la web y las borra o anonimiza de un "
            "solo pase, dejando solo las que quieres conservar."
        ),
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("scan", help="descubrir cuentas con Sherlock/Holehe")
    s.add_argument("-u", "--username", action="append")
    s.add_argument("-e", "--email", action="append")
    s.set_defaults(func=cmd_scan)

    s = sub.add_parser("list", help="listar cuentas y estado")
    s.add_argument("--status")
    s.set_defaults(func=cmd_list)

    s = sub.add_parser("run", help="procesar la cola de borrado/anonimizado")
    s.add_argument("--only", help="solo esta plataforma (slug)")
    s.set_defaults(func=cmd_run)

    s = sub.add_parser("skip", help="conservar una plataforma")
    s.add_argument("platform")
    s.set_defaults(func=cmd_skip)

    s = sub.add_parser("dashboard", help="panel web de estado en vivo")
    s.set_defaults(func=cmd_dashboard)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    sys.exit(main())
