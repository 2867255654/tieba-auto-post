@echo off
setlocal
cd /d "%~dp0"
set "PY=C:\Users\yuanliang\.workbuddy\binaries\python\envs\default\Scripts\python.exe"

if not exist "%PY%" (
    echo [ERROR] Python not found:
    echo   %PY%
    pause
    exit /b 1
)

echo ================================================
echo   Tieba Login Helper
echo   A browser will open. Log in to Tieba there,
echo   then come back here and press Enter.
echo ================================================
echo.

"%PY%" login_helper.py

echo.
pause
endlocal
