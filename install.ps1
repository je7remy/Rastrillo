# Instalador de un toque para Rastrillo (Windows).
#
# Idempotente: si el venv ya existe, lo reusa.
#
# Si PowerShell te bloquea por politica de ejecucion, usa:
#     powershell -ExecutionPolicy Bypass -File .\install.ps1
# o, una sola vez para esta sesion:
#     Set-ExecutionPolicy -Scope Process -ExecutionPolicy RemoteSigned
#     .\install.ps1
#
# (Sin acentos a proposito: PowerShell 5.1 lee este script como CP1252
#  y los caracteres no-ASCII rompen el parser.)

#Requires -Version 5

# OJO: NO ponemos $ErrorActionPreference='Stop'. pip y playwright vuelcan
# WARNINGs por stderr de forma rutinaria; con 'Stop' PowerShell promueve esos
# WARNINGs a excepciones nativas y se aborta antes de poder leer $LASTEXITCODE.
# Aqui manejamos exitcode a mano tras cada comando externo.
$ErrorActionPreference = 'Continue'
$REPO       = Split-Path -Parent $MyInvocation.MyCommand.Path
$VENV       = Join-Path $REPO ".venv"
$PYEXE      = Join-Path $VENV "Scripts\python.exe"
$RASTRILLO  = Join-Path $VENV "Scripts\rastrillo.exe"
$REQ        = Join-Path $REPO "requirements.txt"
$MIN_PY     = [Version]"3.10"

# Helpers de mensaje (sin unicode para compatibilidad con PS 5.1 / CP1252)
function Info($msg) { Write-Host "[..]  $msg" -ForegroundColor Cyan }
function Ok($msg)   { Write-Host "[OK]  $msg" -ForegroundColor Green }
function Warn($msg) { Write-Host "[!!]  $msg" -ForegroundColor Yellow }
function Fail($msg) {
    Write-Host ""
    Write-Host "[FAIL] $msg" -ForegroundColor Red
    Write-Host ""
    exit 1
}

Write-Host ""
Write-Host "Rastrillo - instalador" -ForegroundColor White
Write-Host "----------------------"
Write-Host ""

# ---- 1. Python en PATH ----------------------------------------------------
Info "Buscando Python en PATH..."
$pyCmd = Get-Command python -ErrorAction SilentlyContinue
if (-not $pyCmd) {
    Fail @"
No encuentro 'python' en tu PATH.
1) Instalalo desde https://www.python.org/downloads/
2) En el instalador, marca 'Add python.exe to PATH'.
3) Cierra esta ventana de PowerShell, abre otra y vuelve a lanzar el script.
"@
}

# Version
$pyverRaw = & python -c "import sys; print('.'.join(str(x) for x in sys.version_info[:3]))" 2>&1
if ($LASTEXITCODE -ne 0 -or -not $pyverRaw) {
    Fail "python -c no devolvio version. Tienes algun alias raro de 'python'?"
}
$parts = $pyverRaw -split '\.'
$pyver = [Version]"$($parts[0]).$($parts[1])"
if ($pyver -lt $MIN_PY) {
    Fail "Tu Python es $pyverRaw. Rastrillo necesita 3.10 o superior."
}
Ok "Python $pyverRaw detectado en $($pyCmd.Source)"

# ---- 2. Venv --------------------------------------------------------------
if (Test-Path $PYEXE) {
    Ok "Venv ya existe en .venv\ - reusando"
} else {
    Info "Creando venv en .venv\..."
    & python -m venv $VENV 2>&1 | Out-Host
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path $PYEXE)) {
        Fail @"
No pude crear el venv.
Comprueba que tu instalacion de Python incluye el modulo 'venv'. Prueba:
    python -m venv .venv
y revisa el error que de.
"@
    }
    Ok "Venv creado"
}

# ---- 3. pip upgrade + requirements ---------------------------------------
Info "Actualizando pip..."
& $PYEXE -m pip install --upgrade pip 2>&1 | Out-Host
if ($LASTEXITCODE -ne 0) {
    Fail @"
Fallo 'pip install --upgrade pip'.
Probablemente no hay conexion a internet o tu proxy bloquea pypi.org.
Prueba en otra red o configura HTTP_PROXY/HTTPS_PROXY.
"@
}
Ok "pip actualizado"

Info "Instalando dependencias (puede tardar un par de minutos)..."
& $PYEXE -m pip install --no-cache-dir -r $REQ 2>&1 | Out-Host
if ($LASTEXITCODE -ne 0) {
    Fail @"
Fallo 'pip install -r requirements.txt'.
Revisa el log de arriba - lo mas comun:
  - sin internet,
  - pypi bloqueado por tu red,
  - una dependencia que requiere compilador en Windows.
Vuelve a lanzar el script cuando lo soluciones; es idempotente.
"@
}
Ok "Dependencias instaladas"

# ---- 4. Paquete editable -------------------------------------------------
Info "Registrando los comandos 'rastrillo' y 'rs'..."
& $PYEXE -m pip install -e $REPO 2>&1 | Out-Host
if ($LASTEXITCODE -ne 0) {
    Fail "Fallo 'pip install -e .'. Revisa el log."
}
if (-not (Test-Path $RASTRILLO)) {
    Fail "No encuentro rastrillo.exe en .venv\Scripts tras instalar. Algo raro paso."
}
Ok "Comandos registrados (rastrillo.exe en .venv\Scripts)"

# ---- 5. Chromium para Playwright -----------------------------------------
Info "Descargando Chromium para Playwright (~180 MB; puede tardar)..."
& $PYEXE -m playwright install chromium 2>&1 | Out-Host
if ($LASTEXITCODE -ne 0) {
    Warn @"
La descarga de Chromium fallo. Rastrillo puede arrancar igual y mostrarte
links / borradores GDPR, pero el motor automatico no funcionara hasta que
completes esto. Cuando puedas, ejecuta:
    .\.venv\Scripts\python.exe -m playwright install chromium
"@
} else {
    Ok "Chromium descargado"
}

# ---- 6. Verificacion final -----------------------------------------------
Info "Verificando que rastrillo arranca..."
& $RASTRILLO list --help | Out-Null
if ($LASTEXITCODE -ne 0) {
    Fail "rastrillo no arranca. Revisa el log de arriba."
}
Ok "rastrillo --help OK"

# ---- 7. Mensaje final ----------------------------------------------------
Write-Host ""
Write-Host "Instalacion completa." -ForegroundColor Green
Write-Host ""
Write-Host "Para arrancar Rastrillo:" -ForegroundColor White
Write-Host "    .\rastrillo.ps1" -ForegroundColor Cyan
Write-Host ""
Write-Host "(O doble-click en rastrillo.cmd desde el Explorador.)" -ForegroundColor Gray
Write-Host ""
