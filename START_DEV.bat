@echo off
echo ========================================
echo NAW Development Environment Startup
echo ========================================
echo.

echo [1/3] Starting Backend (FastAPI + SQLite)...
start "NAW Backend" cmd /k "cd backend && .venv\Scripts\activate && uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload"

timeout /t 3 /nobreak > nul

echo [2/3] Starting Frontend (Vite Dev Server)...
start "NAW Frontend" cmd /k "cd frontend && npm run dev"

echo.
echo ========================================
echo Development servers starting...
echo ========================================
echo.
echo Backend:  http://localhost:8000
echo API Docs: http://localhost:8000/docs
echo Frontend: http://localhost:5173
echo.
echo Default Login:
echo   Username: admin
echo   Password: admin1234
echo.
echo Press Ctrl+C in each terminal to stop servers.
echo ========================================
