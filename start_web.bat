@echo off
setlocal
cd /d "%~dp0"

echo ========================================================
echo   CashAuto Web Control Center
echo ========================================================
echo.

where python >nul 2>nul
if %errorlevel% neq 0 (
    echo [ERROR] Python not found in PATH!
    echo Please install Python 3.10+ from https://www.python.org/
    pause
    exit /b 1
)

echo Starting CashAuto Web UI on http://127.0.0.1:8000 ...
echo Press Ctrl+C in this window to stop the server.
echo.

start "" "http://127.0.0.1:8000"
python -m uvicorn app:app --host 127.0.0.1 --port 8000

pause
