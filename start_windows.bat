@echo off
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Run setup_windows.bat first.
  pause
  exit /b 1
)
".venv\Scripts\python.exe" -c "import playwright" >nul 2>nul
if errorlevel 1 (
  echo Playwright is missing. Run setup_windows.bat and check that installation succeeds.
  pause
  exit /b 1
)
".venv\Scripts\python.exe" cashify_runner.py %*
pause
