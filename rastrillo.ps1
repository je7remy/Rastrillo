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
    Write-Host "Opción rápida (recomendada):" -ForegroundColor Yellow
    Write-Host "    .\install.ps1" -ForegroundColor Cyan
    Write-Host ""
    Write-Host "Si PowerShell bloquea el script:" -ForegroundColor Yellow
    Write-Host "    powershell -ExecutionPolicy Bypass -File .\install.ps1" -ForegroundColor Cyan
    Write-Host ""
    Write-Host "Después vuelve a lanzar: .\rastrillo.ps1" -ForegroundColor Yellow
    exit 1
}

& $exe @args
exit $LASTEXITCODE
