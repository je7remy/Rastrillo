@echo off
REM Wrapper para cmd.exe / doble-click desde el Explorador. Invoca el
REM ejecutable del venv sin tener que activarlo. Pasa todos los args.
setlocal
set "HERE=%~dp0"
set "EXE=%HERE%.venv\Scripts\rastrillo.exe"

if not exist "%EXE%" (
    echo.
    echo El venv de Rastrillo no esta creado todavia.
    echo Opcion rapida - recomendada:
    echo.
    echo   .\install.ps1     - Windows PowerShell
    echo   bash install.sh   - Linux/Mac
    echo.
    echo Despues vuelve a lanzar rastrillo.cmd o haz doble-click sobre el.
    exit /b 1
)

"%EXE%" %*
exit /b %ERRORLEVEL%
