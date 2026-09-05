@echo off
setlocal EnableDelayedExpansion

rem ===========================================================================
rem  Vasooli - one-command launcher for Windows
rem
rem    run.bat            preflight, repair, run all three engines, verify,
rem                       then start the API and the dashboard
rem    run.bat demo       preflight, repair, run and verify - no servers
rem    run.bat check      preflight only. Reports problems, repairs nothing
rem    run.bat stop       stop whatever this launcher started
rem
rem  A .bat rather than a .ps1 on purpose: PowerShell's execution policy blocks
rem  unsigned scripts on a locked-down machine, and a reviewer should not have
rem  to argue with their own laptop to run this.
rem
rem  Two conventions this file sticks to, both learned the hard way:
rem
rem  * Every `goto` sits at the top level, never inside a parenthesized block.
rem    cmd mis-resolves the label when it jumps out of a block, and the symptom
rem    is landing in a completely unrelated section. Conditions are inverted and
rem    jump over the block instead.
rem  * Every literal is ASCII. The report prints rupee signs and em-dashes,
rem    which is what chcp 65001 below is for - unified_demo.py reconfigures its
rem    own stdout, but this file's output needs the codepage set.
rem ===========================================================================

cd /d "%~dp0"

rem Remember the console codepage so an interactive shell is left as it was.
set "VASOOLI_OLDCP="
for /f "tokens=2 delims=:" %%c in ('chcp') do set "VASOOLI_OLDCP=%%c"
set "VASOOLI_OLDCP=%VASOOLI_OLDCP: =%"
chcp 65001 >nul 2>&1

set "MODE=%~1"
if "%MODE%"=="" set "MODE=all"

set "VENV_PY=%~dp0apps\api\.venv\Scripts\python.exe"
rem A relative twin for `for /f`, which is fragile about quoted paths. Safe
rem because we cd to the repo root above, so it holds no spaces wherever the
rem repository happens to live.
set "VENV_PY_REL=apps\api\.venv\Scripts\python.exe"
set "EXITCODE=0"

if /i "%MODE%"=="stop" goto :stop

echo.
echo ===========================================================================
echo  Vasooli - revenue recovery engine
echo ===========================================================================
echo.

rem ---------------------------------------------------------------------------
rem  1. Find an interpreter. The one check that cannot be written in Python,
rem     for the obvious reason.
rem ---------------------------------------------------------------------------

set "SYS_PY="
py -3.12 -c "import sys" >nul 2>&1
if not errorlevel 1 set "SYS_PY=py -3.12"
if defined SYS_PY goto :have_python

python -c "import sys" >nul 2>&1
if not errorlevel 1 set "SYS_PY=python"
if defined SYS_PY goto :have_python

echo   [FAIL] No Python interpreter found
echo          Problem: Neither "py -3.12" nor "python" is on PATH.
echo          Fix:     Install Python 3.12 from
echo                   https://www.python.org/downloads/release/python-3120/
echo                   and tick "Add python.exe to PATH" in the installer.
set "EXITCODE=1"
goto :done

:have_python

rem ---------------------------------------------------------------------------
rem  2. Gate on the version before building a venv with it. A venv created on
rem     the wrong interpreter fails later and much less legibly.
rem ---------------------------------------------------------------------------

if exist "%VENV_PY%" goto :venv_exists

%SYS_PY% scripts\preflight.py --stage interpreter
if not errorlevel 1 goto :make_venv
set "EXITCODE=1"
goto :done

:make_venv
echo   [ .. ] Creating virtual environment at apps\api\.venv
%SYS_PY% -m venv "%~dp0apps\api\.venv"
if not errorlevel 1 goto :venv_made
echo   [FAIL] Could not create the virtual environment
echo          Problem: python -m venv exited non-zero.
echo          Fix:     Check you can write to apps\api\, then run:
echo                   %SYS_PY% -m venv apps\api\.venv
set "EXITCODE=1"
goto :done

:venv_made
echo   [FIXED] Virtual environment created

:venv_exists

rem A venv that exists but does not run is worse than one that is missing - it
rem looks fine and fails everywhere. Rebuild it.
"%VENV_PY%" -c "import sys" >nul 2>&1
if not errorlevel 1 goto :venv_ok

echo   [ .. ] Virtual environment is broken - rebuilding
rmdir /s /q "%~dp0apps\api\.venv" >nul 2>&1
%SYS_PY% -m venv "%~dp0apps\api\.venv"
if not errorlevel 1 goto :venv_rebuilt
echo   [FAIL] Could not rebuild the virtual environment
echo          Problem: python -m venv exited non-zero on a clean directory.
echo          Fix:     rmdir /s /q apps\api\.venv
echo                   %SYS_PY% -m venv apps\api\.venv
set "EXITCODE=1"
goto :done

:venv_rebuilt
echo   [FIXED] Virtual environment rebuilt

:venv_ok

rem ---------------------------------------------------------------------------
rem  3. The doctor. Everything from here is Python, because batch is a bad
rem     language for comparing versions and parsing netstat.
rem ---------------------------------------------------------------------------

set "PREFLIGHT_ARGS="
if /i "%MODE%"=="check" set "PREFLIGHT_ARGS=--no-fix"
if /i "%MODE%"=="demo"  set "PREFLIGHT_ARGS=--skip-ports"

"%VENV_PY%" scripts\preflight.py %PREFLIGHT_ARGS%
set "PREFLIGHT=!errorlevel!"

rem Exit code 2 means ready, but Node is absent so there is no dashboard.
set "SKIP_WEB=0"
if "!PREFLIGHT!"=="2" set "SKIP_WEB=1"

if not "!PREFLIGHT!"=="1" goto :preflight_ok
echo.
echo   Blocked. Fix the items marked [FAIL] above, then run this again.
set "EXITCODE=1"
goto :done

:preflight_ok
if /i not "%MODE%"=="check" goto :run_engines
echo.
echo   Ready. Run "run.bat" to start the product.
goto :done

rem ---------------------------------------------------------------------------
rem  4. The product. One seeded run of all three engines, then the integrity
rem     audit that checks the numbers it just printed.
rem ---------------------------------------------------------------------------

:run_engines
echo.
echo ---------------------------------------------------------------------------
echo  Running all three engines - seed 42
echo ---------------------------------------------------------------------------
echo.

rem The unified run id is derived from the seed, so a second run of seed 42 on
rem the same database is refused by design. Ask what this database needs: the
rem first launch gets the canonical id the docs quote, later ones get a salt.
rem stdout carries the arguments; the explanation arrives on stderr.
set "RUN_ARGS="
for /f "usebackq delims=" %%a in (`%VENV_PY_REL% scripts\preflight.py --run-plan --seed 42`) do set "RUN_ARGS=%%a"

"%VENV_PY%" scripts\unified_demo.py --seed 42 !RUN_ARGS!
if not errorlevel 1 goto :engines_ok
echo.
echo   [FAIL] The batch run did not complete
echo          Problem: unified_demo.py exited non-zero. The error is above.
echo          Fix:     Isolate the reasoning layer by re-running without a
echo                   provider - if this succeeds, the fault is upstream:
echo                   %VENV_PY_REL% scripts\unified_demo.py --seed 42 --deterministic
set "EXITCODE=1"
goto :done

:engines_ok
echo.
echo ---------------------------------------------------------------------------
echo  Verifying those numbers
echo ---------------------------------------------------------------------------
echo.

"%VENV_PY%" scripts\audit_check.py --latest --integrity
if not errorlevel 1 goto :audit_ok
echo.
echo   [FAIL] The integrity audit failed
echo          Problem: A reported number disagrees with the audit trail, or a
echo                   schema validation failed. That is a real defect, not a
echo                   setup problem - the detail is above.
set "EXITCODE=1"
goto :done

:audit_ok
if /i not "%MODE%"=="demo" goto :start_services
echo.
echo   Done. Run "run.bat" with no arguments to also start the dashboard.
goto :done

rem ---------------------------------------------------------------------------
rem  5. The servers. Each gets its own window so its log stays readable and it
rem     can be stopped independently.
rem ---------------------------------------------------------------------------

:start_services
echo.
echo ---------------------------------------------------------------------------
echo  Starting services
echo ---------------------------------------------------------------------------
echo.

echo   [ .. ] Starting the API on http://127.0.0.1:8000
start "Vasooli API" /d "%~dp0apps\api" cmd /k "%VENV_PY%" -m uvicorn app.main:app --reload --host 127.0.0.1 --port 8000

rem Poll the health port rather than sleeping a guessed number of seconds.
rem Opening a browser at a socket that is not listening yet shows a reviewer a
rem connection error and teaches them the product is broken.
call :wait_for_port 8000 40
set "API_UP=!PORT_UP!"

if "!API_UP!"=="1" echo   [ OK ] API is up - docs at http://127.0.0.1:8000/docs
if "!API_UP!"=="1" goto :api_ready
echo   [WARN] The API did not answer within 40 seconds
echo          Problem: It may still be starting, or it failed on boot.
echo          Fix:     Check the "Vasooli API" window for the error.

:api_ready
if not "!SKIP_WEB!"=="1" goto :start_web
echo.
echo   Dashboard skipped - Node.js is not installed. See the note above.
echo   The API is running and its docs are browsable.
goto :running

:start_web
echo   [ .. ] Starting the dashboard on http://127.0.0.1:3000
start "Vasooli Dashboard" /d "%~dp0apps\web" cmd /k npm run dev

rem Next.js compiles on first request, so it gets a longer budget than the API.
call :wait_for_port 3000 60

if not "!PORT_UP!"=="1" goto :web_slow
echo   [ OK ] Dashboard is up
start "" "http://127.0.0.1:3000"
goto :running

:web_slow
echo   [WARN] The dashboard did not answer within 60 seconds
echo          Problem: Next.js can be slow to compile on a first run.
echo          Fix:     Watch the "Vasooli Dashboard" window, then open
echo                   http://127.0.0.1:3000 yourself.

:running
echo.
echo ===========================================================================
echo  Running
echo.
echo    Dashboard   http://127.0.0.1:3000
echo    API docs    http://127.0.0.1:8000/docs
echo.
echo  The dashboard's front page shows the run above - same numbers, because
echo  the console report and the API response are the same object.
echo.
echo  To stop: close the two service windows, or run  run.bat stop
echo ===========================================================================
goto :done

rem ---------------------------------------------------------------------------
rem  stop - close what the launcher opened
rem ---------------------------------------------------------------------------

:stop
echo.
echo Stopping Vasooli services
rem Identified by listening port, not window title: a launcher started without
rem a console spawns processes with no title to match, and the port is what
rem blocks the next run anyway. Falls back to titles if the venv is gone.
if not exist "%VENV_PY%" goto :stop_by_title
"%VENV_PY%" scripts\preflight.py --stop
goto :done

:stop_by_title
call :kill_window "Vasooli API" API
call :kill_window "Vasooli Dashboard" Dashboard
goto :done

rem ---------------------------------------------------------------------------
rem  Subroutines
rem ---------------------------------------------------------------------------

:wait_for_port
rem %1 port, %2 attempts. Sets PORT_UP to 1 or 0.
set "PORT_UP=0"
for /l %%i in (1,1,%2) do call :probe_port %1
exit /b 0

:probe_port
if "!PORT_UP!"=="1" exit /b 0
"%VENV_PY%" -c "import socket,sys; s=socket.socket(); s.settimeout(0.5); sys.exit(s.connect_ex(('127.0.0.1',%1)))" >nul 2>&1
if not errorlevel 1 set "PORT_UP=1"
if "!PORT_UP!"=="1" exit /b 0
timeout /t 1 /nobreak >nul
exit /b 0

:kill_window
rem %1 quoted window title, %2 label. taskkill exits 0 even when its filter
rem matches nothing, so count the SUCCESS lines instead of trusting errorlevel.
set "KILLED=0"
for /f %%n in ('taskkill /fi "WINDOWTITLE eq %~1*" /t /f 2^>nul ^| find /c "SUCCESS"') do set "KILLED=%%n"
if "!KILLED!"=="0" echo   [ -- ] %2 was not running
if not "!KILLED!"=="0" echo   [ OK ] %2 stopped
exit /b 0

:done
if defined VASOOLI_OLDCP chcp %VASOOLI_OLDCP% >nul 2>&1
endlocal & exit /b %EXITCODE%
