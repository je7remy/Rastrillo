#!/usr/bin/env bash
# Instalador de un toque para Rastrillo (Linux y macOS).
#
# Idempotente: si el venv ya existe, lo reusa.
#
# Uso:
#     bash install.sh
# o, tras chmod +x:
#     ./install.sh
#
# (ASCII puro a proposito para maxima compatibilidad de terminales.)

set -u
# Nota: NO usamos 'set -e'. Queremos detectar fallos a mano y dar
# mensajes accionables en lugar de un trazado de comando.

REPO_DIR="$(cd "$(dirname "$0")" && pwd)"
VENV="$REPO_DIR/.venv"
PYEXE="$VENV/bin/python"
RASTRILLO_BIN="$VENV/bin/rastrillo"
REQ="$REPO_DIR/requirements.txt"
MIN_PY_MAJOR=3
MIN_PY_MINOR=10

# ---- Colores opcionales (solo si la terminal los soporta) ---------------
if [ -t 1 ] && command -v tput >/dev/null 2>&1 && [ "$(tput colors 2>/dev/null || echo 0)" -ge 8 ]; then
    C_CYAN=$(tput setaf 6); C_GREEN=$(tput setaf 2)
    C_YELLOW=$(tput setaf 3); C_RED=$(tput setaf 1); C_RESET=$(tput sgr0)
else
    C_CYAN=""; C_GREEN=""; C_YELLOW=""; C_RED=""; C_RESET=""
fi

info() { printf "%s[..]  %s%s\n"   "$C_CYAN"   "$1" "$C_RESET"; }
ok()   { printf "%s[OK]  %s%s\n"   "$C_GREEN"  "$1" "$C_RESET"; }
warn() { printf "%s[!!]  %s%s\n"   "$C_YELLOW" "$1" "$C_RESET"; }
fail() { printf "\n%s[FAIL] %s%s\n\n" "$C_RED" "$1" "$C_RESET"; exit 1; }

# ---- Sugerencia de instalacion de Python segun distro/SO ----------------
suggest_python_install() {
    if [ "$(uname -s 2>/dev/null)" = "Darwin" ]; then
        echo "brew install python3   (instala Homebrew desde https://brew.sh si no lo tienes)"
        return
    fi
    if [ -r /etc/os-release ]; then
        # shellcheck disable=SC1091
        . /etc/os-release
        case "${ID:-}${ID_LIKE:-}" in
            *debian*|*ubuntu*|*mint*)
                echo "sudo apt update && sudo apt install -y python3 python3-venv python3-pip"
                return
                ;;
            *fedora*|*rhel*|*centos*|*rocky*|*alma*)
                echo "sudo dnf install -y python3 python3-pip"
                return
                ;;
            *arch*|*manjaro*)
                echo "sudo pacman -S --noconfirm python python-pip"
                return
                ;;
            *suse*)
                echo "sudo zypper install -y python3 python3-pip"
                return
                ;;
            *alpine*)
                echo "sudo apk add python3 py3-pip"
                return
                ;;
        esac
    fi
    echo "Instala Python 3.10+ con el gestor de paquetes de tu sistema."
}

echo
echo "Rastrillo - instalador (Linux/Mac)"
echo "----------------------------------"
echo

# ---- 1. python3 en PATH -------------------------------------------------
info "Buscando python3 en PATH..."
if ! command -v python3 >/dev/null 2>&1; then
    SUG=$(suggest_python_install)
    fail "No encuentro 'python3' en tu PATH.
Instalalo con:
    $SUG
Luego cierra esta terminal, abre otra y vuelve a lanzar el script."
fi

# Version >= 3.10
PYVER_RAW=$(python3 -c "import sys; print('.'.join(str(x) for x in sys.version_info[:3]))" 2>/dev/null) || \
    fail "python3 esta en PATH pero no responde. Algun alias raro?"

PYVER_MAJOR=$(echo "$PYVER_RAW" | cut -d. -f1)
PYVER_MINOR=$(echo "$PYVER_RAW" | cut -d. -f2)
if [ -z "$PYVER_MAJOR" ] || [ -z "$PYVER_MINOR" ]; then
    fail "python3 -c devolvio una version inesperada: '$PYVER_RAW'"
fi
if [ "$PYVER_MAJOR" -lt "$MIN_PY_MAJOR" ] || \
   { [ "$PYVER_MAJOR" -eq "$MIN_PY_MAJOR" ] && [ "$PYVER_MINOR" -lt "$MIN_PY_MINOR" ]; }; then
    fail "Tu python3 es $PYVER_RAW. Rastrillo necesita ${MIN_PY_MAJOR}.${MIN_PY_MINOR} o superior."
fi
ok "Python $PYVER_RAW detectado en $(command -v python3)"

# ---- 2. Venv ------------------------------------------------------------
if [ -x "$PYEXE" ]; then
    ok "Venv ya existe en .venv/ - reusando"
else
    info "Creando venv en .venv/..."
    if ! python3 -m venv "$VENV"; then
        # En Debian/Ubuntu falla si falta python3-venv
        fail "Fallo 'python3 -m venv .venv'.
En Debian/Ubuntu suele faltar el paquete python3-venv:
    sudo apt install -y python3-venv
Despues vuelve a lanzar este script."
    fi
    if [ ! -x "$PYEXE" ]; then
        fail "Venv creado pero no encuentro '$PYEXE'. Algo raro."
    fi
    ok "Venv creado"
fi

# ---- 3. pip upgrade + requirements --------------------------------------
info "Actualizando pip..."
if ! "$PYEXE" -m pip install --upgrade pip >/dev/null 2>&1; then
    fail "Fallo 'pip install --upgrade pip'.
Probablemente no hay conexion a internet o un proxy bloquea pypi.org.
Prueba en otra red o configura HTTP_PROXY/HTTPS_PROXY."
fi
ok "pip actualizado"

info "Instalando dependencias (puede tardar un par de minutos)..."
if ! "$PYEXE" -m pip install --no-cache-dir -r "$REQ"; then
    fail "Fallo 'pip install -r requirements.txt'. Revisa el log de arriba.
Causas habituales: sin internet, proxy, dependencia que requiere
compilador (build-essential / Xcode CLI tools)."
fi
ok "Dependencias instaladas"

# ---- 4. Editable install -----------------------------------------------
info "Registrando los comandos 'rastrillo' y 'rs'..."
if ! "$PYEXE" -m pip install -e "$REPO_DIR" >/dev/null; then
    fail "Fallo 'pip install -e .'"
fi
[ -x "$RASTRILLO_BIN" ] || fail "No encuentro rastrillo en '$VENV/bin' tras instalar."
ok "Comandos registrados (rastrillo en .venv/bin)"

# ---- 5. Chromium para Playwright ---------------------------------------
info "Descargando Chromium para Playwright (~180 MB; puede tardar)..."
SYS_NAME=$(uname -s 2>/dev/null || echo unknown)
PW_OK=0
if [ "$SYS_NAME" = "Linux" ]; then
    # --with-deps instala libs de sistema que Chromium necesita en Linux
    # (libxcomposite1, libnss3, etc.). Requiere sudo y soporte de la distro;
    # si falla, reintentamos sin --with-deps (Chromium baja pero el motor
    # puede no arrancar hasta instalar las libs a mano).
    if "$PYEXE" -m playwright install --with-deps chromium; then
        PW_OK=1
    else
        warn "Instalacion con --with-deps fallo (quiza necesitas sudo o tu distro no es soportada).
Reintento sin las dependencias de sistema."
        if "$PYEXE" -m playwright install chromium; then
            PW_OK=1
            warn "Chromium descargado pero faltan libs de sistema. Si el motor revienta
al arrancar, ejecuta:
    sudo $PYEXE -m playwright install-deps chromium"
        fi
    fi
else
    # macOS: --with-deps no aplica realmente; usamos sin.
    if "$PYEXE" -m playwright install chromium; then
        PW_OK=1
    fi
fi

if [ "$PW_OK" -eq 1 ]; then
    ok "Chromium listo"
else
    warn "Chromium no se descargo. Rastrillo arrancara y mostrara links/borradores
GDPR, pero el motor automatico no funcionara hasta que ejecutes:
    $PYEXE -m playwright install chromium"
fi

# ---- 6. Verificacion final ---------------------------------------------
info "Verificando que rastrillo arranca..."
if ! "$RASTRILLO_BIN" list --help >/dev/null 2>&1; then
    fail "rastrillo no arranca. Revisa el log de arriba."
fi
ok "rastrillo --help OK"

# ---- 7. Crear wrappers --------------------------------------------------
info "Creando wrappers rastrillo.sh y rs.sh..."
cat > "$REPO_DIR/rastrillo.sh" <<'WRAP_EOF'
#!/usr/bin/env bash
# Wrapper para Linux/Mac: invoca el rastrillo del venv sin activarlo.
# Pasa todos los argumentos tal cual.
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
WRAP_EOF

cat > "$REPO_DIR/rs.sh" <<'WRAP_EOF'
#!/usr/bin/env bash
# Alias corto del wrapper.
HERE="$(cd "$(dirname "$0")" && pwd)"
EXE="$HERE/.venv/bin/rs"
if [ ! -x "$EXE" ]; then
    echo "El venv de Rastrillo no esta creado todavia. Ejecuta: bash install.sh"
    exit 1
fi
exec "$EXE" "$@"
WRAP_EOF

chmod +x "$REPO_DIR/rastrillo.sh" "$REPO_DIR/rs.sh" "$REPO_DIR/install.sh" 2>/dev/null || true
ok "Wrappers creados (rastrillo.sh, rs.sh)"

# ---- 8. Mensaje final --------------------------------------------------
echo
printf "%sInstalacion completa.%s\n" "$C_GREEN" "$C_RESET"
echo
echo "Para arrancar Rastrillo:"
echo "    ./rastrillo.sh"
echo
echo "(O usa el alias corto: ./rs.sh)"
echo
