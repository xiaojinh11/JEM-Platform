@echo off
REM JEM Platform Backend Quick Start Script
cd /d D:\zidongh\backend

echo ========================================
echo JEM Backend Starting...
echo ========================================

REM Check if venv exists
if not exist ".venv\Scripts\activate.bat" (
    echo Creating virtual environment...
    python -m venv .venv
)

REM Activate venv and start backend
call .venv\Scripts\activate.bat

echo Installing/Updating dependencies...
pip install -q -r requirements.txt

echo.
echo Starting Uvicorn server on http://localhost:8000
echo API Docs: http://localhost:8000/docs
echo.
echo [Dev mode] CELERY_TASK_ALWAYS_EAGER=1 (synchronous, no Redis required)
echo            To use the real Celery worker instead, set the env var to 0
echo            and start Redis on localhost:6379.

set CELERY_TASK_ALWAYS_EAGER=1
uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
