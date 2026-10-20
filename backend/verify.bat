@echo off
REM ============================================================
REM NAW backend verification — Python 3.11 only
REM Usage:  verify.bat
REM
REM Steps:
REM   [1] Assert venv is Python 3.11
REM   [2] pip install -r requirements.txt
REM   [3] Create .env (SQLite + Fernet) if missing
REM   [4] Drop old SQLite DB, run alembic upgrade head
REM   [5] Smoke import: from app.main import app
REM   [6] Start uvicorn (tracked by PID), curl health/login
REM   [7] Stop uvicorn
REM ============================================================

setlocal EnableDelayedExpansion

REM --- absolute paths so script works from any cwd ---
set "BD=%~dp0"
if "%BD:~-1%"=="\" set "BD=%BD:~0,-1%"
set "PY=%BD%\.venv\Scripts\python.exe"
set "UVICORN=%BD%\.venv\Scripts\uvicorn.exe"
set "INI=%BD%\alembic.ini"
set "REQS=%BD%\requirements.txt"
set "LOG=%BD%\install.log"

set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
set "PYTHONPATH=%BD%"

echo === [1/7] Assert venv is Python 3.11 ===================
if not exist "%PY%" (
    echo ERROR: .venv not found. Create with:
    echo   py -3.11 -m venv .venv
    exit /b 1
)
"%PY%" --version
"%PY%" -c "import sys; assert (3,11) <= sys.version_info < (3,12), 'Requires Python 3.11'" || (
    echo ERROR: Wrong Python version. Recreate:
    echo   rmdir /s /q .venv ^&^& py -3.11 -m venv .venv
    exit /b 1
)

echo === [2/7] Install deps ===================================
"%PY%" -m pip install -r "%REQS%" >> "%LOG%" 2>&1
if errorlevel 1 (
    echo ERROR: pip install failed - see %LOG%
    exit /b 1
)
echo OK

echo === [3/7] Create .env if missing ==========================
if not exist "%BD%\.env" (
    echo Creating .env with SQLite + FERNET_KEY...
    for /f "delims=" %%K in ('"%PY%" -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"') do set "FK=%%K"
    (
        echo SECRET_KEY=local-dev-secret-key-change-me-please-32chars-min
        echo JWT_ALGORITHM=HS256
        echo JWT_EXPIRE_MINUTES=720
        echo FERNET_KEY=!FK!
        echo DATABASE_URL=sqlite:///./naw.db
        echo DATABASE_ECHO=false
        echo ENV=local
        echo DEBUG=true
        echo APP_NAME=Network Automation Workbench
        echo REDIS_URL=redis://localhost:6379/0
        echo CELERY_BROKER_URL=redis://localhost:6379/1
        echo CELERY_RESULT_BACKEND=redis://localhost:6379/2
        echo SSH_TIMEOUT_SECONDS=10
        echo WEBSSH_IDLE_TIMEOUT_SECONDS=600
        echo BOOTSTRAP_ADMIN_USERNAME=admin
        echo BOOTSTRAP_ADMIN_PASSWORD=admin1234
    ) > "%BD%\.env"
) else (
    echo .env exists
)

echo === [4/7] Migrations (drop DB + alembic) ==================
if exist "%BD%\naw.db" del /q "%BD%\naw.db"
"%PY%" -m alembic -c "%INI%" upgrade head
if errorlevel 1 (
    echo ERROR: alembic upgrade failed
    exit /b 1
)

echo === [5/7] Smoke import ====================================
"%PY%" -c "from app.main import app; print('OK app loaded, routes=', len(app.routes))"
if errorlevel 1 (
    echo ERROR: app import failed
    exit /b 1
)

echo === [6/7] Start uvicorn + smoke curl =======================
powershell -ExecutionPolicy Bypass -File "%BD%\verify_core.ps1"
if errorlevel 1 (
    echo ERROR: backend smoke test failed
    echo --- uvicorn.log tail ---
    type "%BD%\uvicorn.log"
    exit /b 1
)

echo === [7/7] Stop uvicorn ====================================
if defined UV_PID taskkill /F /PID !UV_PID! >nul 2>&1
taskkill /F /IM uvicorn.exe >nul 2>&1

echo.
echo === ALL CHECKS PASSED (Python 3.11) ===
endlocal
exit /b 0
