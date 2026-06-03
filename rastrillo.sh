#!/usr/bin/env bash
# Wrapper para Linux/Mac: invoca el rastrillo del venv sin activarlo.
# Pasa todos los argumentos tal cual.
#
# Uso:
#     ./rastrillo.sh               # arranca el dashboard
#     ./rastrillo.sh list          # subcomando de debug
#     ./rastrillo.sh list --status found

HERE="$(cd "$(dirname "$0")" && pwd)"
EXE="$HERE/.venv/bin/rastrillo"
if [ ! -x "$EXE" ]; then
    cat <<MSG
El venv de Rastrillo no esta creado todavia.
Desde esta misma carpeta ejecuta:
    bash install.sh
Despues vuelve a lanzar: ./rastrillo.sh
MSG
    exit 1
fi
exec "$EXE" "$@"
