@echo on
setlocal EnableDelayedExpansion
REM Auto mode: Task Scheduler passes /auto argument, no pause at end
if "%1"=="/auto" set "AUTO=1"

REM Switch to this bat's directory (project root)
cd /d "%~dp0"

REM Timestamped archive log so we can compare manual vs Task Scheduler runs
set "PY=C:\Users\yuanliang\.workbuddy\binaries\python\envs\default\Scripts\python.exe"
"%PY%" -c "import datetime; open('run_log.ts.tmp','w',encoding='utf-8').write(datetime.datetime.now().strftime('%Y%m%d_%H%M%S'))" >nul 2>&1
set /p TS=<run_log.ts.tmp 2>nul
del /f run_log.ts.tmp 2>nul
if not defined TS set "TS=unknown"

REM Latest log remains run_log.txt; older runs are archived as run_log_YYYYMMDD_HHMMSS.txt
set "LOG=%~dp0run_log.txt"
set "ARCHIVE=%~dp0run_log_!TS!.txt"

REM Redirect all following output to a log file for debugging
echo [START] %date% %time% > "%LOG%" 2>&1
echo [context] user=%USERNAME% computer=%COMPUTERNAME% auto=%AUTO% >> "%LOG%" 2>&1

REM Hard-coded absolute Python path. Do NOT use a custom variable here;
REM Task Scheduler resolves variables differently and may turn "python.exe main.py"
REM into a broken command like "n.py".
if not exist "%PY%" (
    echo [ERROR] Python not found: >> "%LOG%" 2>&1
    echo   %PY% >> "%LOG%" 2>&1
    pause
    exit /b 1
) >> "%LOG%" 2>&1

REM Optional proxy for overseas RSS sources (PCGamer, GameSpot, etc.)
REM Uncomment and set to your proxy port, e.g. Clash Verge 7897 / V2RayN 10809
REM set "HTTP_PROXY=http://127.0.0.1:7897"
REM set "HTTPS_PROXY=http://127.0.0.1:7897"

echo ================================================ >> "%LOG%" 2>&1
echo   Tieba Game Digest - starting >> "%LOG%" 2>&1
echo   Time: %date% %time% >> "%LOG%" 2>&1
echo ================================================ >> "%LOG%" 2>&1
"%PY%" main.py >> "%LOG%" 2>&1
set "RC=%errorlevel%"

echo ================================================ >> "%LOG%" 2>&1
if %RC% equ 0 (
    echo   Finished: success (exit code 0) >> "%LOG%" 2>&1
) else (
    echo   Finished: error (exit code %RC%) >> "%LOG%" 2>&1
)
echo ================================================ >> "%LOG%" 2>&1
echo [END] RC=%RC% %date% %time% >> "%LOG%" 2>&1

REM Archive this run's log with timestamp for side-by-side comparison
copy /y "%LOG%" "%ARCHIVE%" >nul 2>&1

REM Pause only in manual mode; auto mode exits immediately
if not defined AUTO pause
endlocal
