@echo off
cd /d "%~dp0"
if exist ".venv\Scripts\python.exe" goto verify
py -3.13 -m venv .venv
if not errorlevel 1 goto verify
py -3.12 -m venv .venv
if errorlevel 1 goto failed
:verify
".venv\Scripts\python.exe" -c "import sys,sysconfig; sys.exit(bool(sysconfig.get_config_var('Py_GIL_DISABLED')) or sys.version_info.major != 3 or sys.version_info.minor in range(10))"
if errorlevel 1 goto incompatible
".venv\Scripts\python.exe" -m pip install --upgrade pip
if errorlevel 1 goto failed
".venv\Scripts\python.exe" -m pip install --only-binary=:all: -r requirements.txt
if errorlevel 1 goto failed
".venv\Scripts\python.exe" -m playwright install chromium
if errorlevel 1 goto failed
echo Setup complete. Run start_windows.bat and paste one Cashify device URL per prompt (blank line to start the batch).
pause
exit /b 0
:incompatible
echo This environment uses an incompatible Python build.
echo Use standard 64-bit Python 3.13, then run: py -3.13 -m venv --clear .venv
pause
exit /b 1
:failed
echo Setup failed. Install standard 64-bit Python 3.13 or 3.12 and check the error above.
pause
exit /b 1
