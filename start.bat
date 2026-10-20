@echo off
title NAW Launcher

echo === NAW Startup ===

echo [0/3] Clearing residual processes ...
for /f "tokens=5" %%P in ('netstat -ano ^| findstr ":18000" ^| findstr "LISTENING"') do (
    taskkill /PID %%P /F >nul 2>&1
)
for /f "tokens=5" %%P in ('netstat -ano ^| findstr ":5173" ^| findstr "LISTENING"') do (
    taskkill /PID %%P /F >nul 2>&1
)
timeout /t 2 /nobreak >nul

echo [1/3] Starting backend (FastAPI :18000) ...
REM NOTE: --reload removed.  During a multipart upload it was firing on
REM in-flight requests, killing them with ECONNRESET/10054 mid-stream.
REM Restart the backend manually after code changes:
REM   taskkill /IM python.exe /FI "WINDOWTITLE eq NAW-Backend*" /F
REM   start "NAW-Backend" cmd /k "cd /d %~dp0backend && .venv\Scripts\activate.bat && uvicorn app.main:app --host 127.0.0.1 --port 18000"
start "NAW-Backend" cmd /k "cd /d %~dp0backend && .venv\Scripts\activate.bat && uvicorn app.main:app --host 127.0.0.1 --port 18000 --timeout-keep-alive 300"

echo [2/3] Waiting for backend ...
:wait_backend
timeout /t 2 /nobreak >nul
powershell -NoProfile -Command "try { (Invoke-WebRequest -UseBasicParsing -TimeoutSec 3 -Uri 'http://127.0.0.1:18000/api/v1/auth/bootstrap-status').StatusCode } catch { 0 }" > "%TEMP%\naw_probe.txt" 2>nul
set /p STATUS=<"%TEMP%\naw_probe.txt"
if not "%STATUS%"=="200" goto wait_backend
del "%TEMP%\naw_probe.txt" >nul 2>&1
echo      Backend ready.

echo [3/3] Starting frontend (Vite :5173) ...
start "NAW-Frontend" cmd /k "cd /d %~dp0frontend && npm run dev"

echo === All services started ===
echo Frontend: http://localhost:5173
echo Backend:  http://127.0.0.1:18000
echo.
echo Close this window to exit launcher.
echo Backend/frontend will keep running in their own windows.
echo Press any key to open browser.
pause >nul
start "" http://localhost:5173
