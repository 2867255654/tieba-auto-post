@echo on
setlocal
REM Auto mode: Task Scheduler passes /auto argument, no pause at end
if "%1"=="/auto" set "AUTO=1"

REM Switch to this bat's directory (project root)
cd /d "%~dp0"

REM Redirect all following output to a log file for debugging
echo [START] %date% %time% > "%~dp0run_log.txt" 2>&1

REM Hard-coded absolute Python path. Do NOT use a custom variable here;
REM Task Scheduler resolves variables differently and may turn "python.exe main.py"
REM into a broken command like "n.py".
if not exist "C:\Users\yuanliang\.workbuddy\binaries\python\envs\default\Scripts\python.exe" (
    echo [ERROR] Python not found: >> "%~dp0run_log.txt" 2>&1
    echo   C:\Users\yuanliang\.workbuddy\binaries\python\envs\default\Scripts\python.exe >> "%~dp0run_log.txt" 2>&1
    pause
    exit /b 1
) >> "%~dp0run_log.txt" 2>&1

REM Optional proxy for overseas RSS sources (PCGamer, GameSpot, etc.)
REM Uncomment and set to your proxy port, e.g. Clash Verge 7897 / V2RayN 10809
REM set "HTTP_PROXY=http://127.0.0.1:7897"
REM set "HTTPS_PROXY=http://127.0.0.1:7897"

echo ================================================ >> "%~dp0run_log.txt" 2>&1
echo   Tieba Game Digest - starting >> "%~dp0run_log.txt" 2>&1
echo   Time: %date% %time% >> "%~dp0run_log.txt" 2>&1
echo ================================================ >> "%~dp0run_log.txt" 2>&1
"C:\Users\yuanliang\.workbuddy\binaries\python\envs\default\Scripts\python.exe" main.py >> "%~dp0run_log.txt" 2>&1
set "RC=%errorlevel%"

echo ================================================ >> "%~dp0run_log.txt" 2>&1
if %RC% equ 0 (
    echo   Finished: success (exit code 0) >> "%~dp0run_log.txt" 2>&1
) else (
    echo   Finished: error (exit code %RC%) >> "%~dp0run_log.txt" 2>&1
)
echo ================================================ >> "%~dp0run_log.txt" 2>&1
echo [END] RC=%RC% %date% %time% >> "%~dp0run_log.txt" 2>&1

REM Pause only in manual mode; auto mode exits immediately
if not defined AUTO pause
endlocal
