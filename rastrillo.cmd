@echo off
REM Wrapper para cmd.exe / doble-click desde el Explorador. Invoca el
REM ejecutable del venv sin tener que activarlo. Pasa todos los args.
setlocal
set "HERE=%~dp0"
set "EXE=%HERE%.venv\Scripts\rastrillo.exe"

if not exist "%EXE%" (
    echo.
    echo El venv de Rastrillo no esta creado todavia.
    echo Para crearlo, desde esta misma carpeta ejecuta:
    echo.
    echo   python -m venv .venv
    echo   .\.venv\Scripts\python.exe -m pip install -r requirements.txt
    echo   .\.venv\Scripts\python.exe -m pip install -e .
    echo   .\.venv\Scripts\python.exe -m playwright install chromium
    echo.
    echo Despues vuelve a lanzar: rastrillo.cmd
    exit /b 1
)

"%EXE%" %*
exit /b %ERRORLEVEL%
