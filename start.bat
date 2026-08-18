@echo off
setlocal
cd /d "%~dp0"
set "PLAYWRIGHT_BROWSERS_PATH=%CD%\.ms-playwright"
set "PYTHONIOENCODING=utf-8"
chcp 65001 > nul

if not exist ".venv\Scripts\python.exe" (
  echo [INFO] Creating project virtual environment...
  py -3 -m venv .venv
  if errorlevel 1 (
    echo [ERROR] Failed to create virtual environment.
    pause
    exit /b 1
  )
)

echo [INFO] Installing or updating dependencies...
".venv\Scripts\python.exe" -m pip install --upgrade pip
".venv\Scripts\python.exe" -m pip install -r requirements.txt
".venv\Scripts\python.exe" -m pip install -e .
if errorlevel 1 (
  echo [ERROR] Dependency installation failed.
  pause
  exit /b 1
)

echo [INFO] Ensuring project-local Playwright Chromium exists...
".venv\Scripts\python.exe" -m playwright install chromium
if errorlevel 1 (
  echo [ERROR] Playwright browser installation failed.
  pause
  exit /b 1
)

echo [INFO] Starting XHS Profile Exporter...
".venv\Scripts\python.exe" -m xhs_profile_exporter
set "EXIT_CODE=%ERRORLEVEL%"
echo [INFO] Finished with exit code %EXIT_CODE%.
pause
exit /b %EXIT_CODE%
