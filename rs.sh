#!/usr/bin/env bash
# Alias corto de rastrillo.sh para Linux/Mac.
HERE="$(cd "$(dirname "$0")" && pwd)"
EXE="$HERE/.venv/bin/rs"
if [ ! -x "$EXE" ]; then
    echo "El venv de Rastrillo no esta creado todavia. Ejecuta: bash install.sh"
    exit 1
fi
exec "$EXE" "$@"
