#!/usr/bin/env python3
"""CLI de Rastrillo.

Rastrillo: herramienta local y privada que rastrea tu huella digital, encuentra
tus cuentas regadas por la web y las borra o anonimiza de un solo pase, dejando
solo las que quieres conservar.

Uso:
  rastrillo               # arranca el dashboard y abre el navegador (modo normal)
  rastrillo list          # auxiliar: lista el estado en terminal (sin abrir nada)
  rastrillo run           # auxiliar: procesa la cola desde terminal (debug)

Todo el control normal (escanear, eliminar, anonimizar, continuar tras CAPTCHA)
ocurre desde el dashboard web. La terminal solo arranca y queda corriendo.
"""
import argparse
import sys
import threading
import time
import webbrowser

from rastrillo import db, config

HOST = "127.0.0.1"
PORT = 8765
URL = f"http://{HOST}:{PORT}"


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

    threading.Thread(target=_open_when_ready, daemon=True, name="rastrillo-openbrowser").start()

    print(f"Rastrillo corriendo en {URL}")
    print(f"   abre en el navegador: {url_with_token}")
    if config.DRY_RUN:
        print("   ⚠  DRY-RUN activado: ninguna acción destructiva se ejecutará")
    uvicorn.run(app, host=HOST, port=PORT, log_level="warning")


def cmd_list(args):
    rows = db.list_accounts(status=args.status)
    if not rows:
        print("Nada todavía. Arranca `rastrillo` y escanea desde la web.")
        return
    for r in rows:
        print(f"  [{r['status']:<13}] {r['platform']:<16} {r['identifier'] or '':<28} "
              f"{r['last_message'] or ''}")
    print("\nResumen:", dict(db.stats()))


def cmd_run(_args):
    """Procesa la cola desde terminal (sin abrir UI). Útil para debug."""
    from rastrillo.engine import process_queue
    process_queue(headless=False)


def main():
    config.ensure_dirs()
    db.init()

    p = argparse.ArgumentParser(
        prog="rastrillo",
        description=(
            "Rastrillo: herramienta local y privada que rastrea tu huella digital, "
            "encuentra tus cuentas regadas por la web y las borra o anonimiza de un "
            "solo pase. Sin argumentos: arranca el dashboard y abre el navegador."
        ),
    )
    # Subcomandos opcionales (auxiliares). Si no hay subcomando, modo default.
    sub = p.add_subparsers(dest="cmd")

    sl = sub.add_parser("list", help="(debug) lista las cuentas y su estado en terminal")
    sl.add_argument("--status")
    sl.set_defaults(func=cmd_list)

    sr = sub.add_parser("run", help="(debug) procesa la cola desde terminal sin abrir UI")
    sr.set_defaults(func=cmd_run)

    args = p.parse_args()
    func = getattr(args, "func", cmd_default)
    func(args)


if __name__ == "__main__":
    sys.exit(main())
