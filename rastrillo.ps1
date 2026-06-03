# Wrapper para PowerShell: invoca el ejecutable del venv sin tener que
# activarlo. Pasa todos los argumentos tal cual.
#
# Uso:
#   .\rastrillo.ps1               # arranca el dashboard
#   .\rastrillo.ps1 list          # subcomando de debug
#   .\rastrillo.ps1 list --status found

$ErrorActionPreference = 'Stop'
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$exe  = Join-Path $here '.venv\Scripts\rastrillo.exe'

if (-not (Test-Path $exe)) {
    Write-Host "El venv de Rastrillo no está creado todavía." -ForegroundColor Yellow
    Write-Host "Para crearlo, desde esta misma carpeta ejecuta:" -ForegroundColor Yellow
    Write-Host ""
    Write-Host "  python -m venv .venv"
    Write-Host "  .\.venv\Scripts\python.exe -m pip install -r requirements.txt"
    Write-Host "  .\.venv\Scripts\python.exe -m pip install -e ."
    Write-Host "  .\.venv\Scripts\python.exe -m playwright install chromium"
    Write-Host ""
    Write-Host "Después vuelve a lanzar: .\rastrillo.ps1" -ForegroundColor Yellow
    exit 1
}

& $exe @args
exit $LASTEXITCODE
