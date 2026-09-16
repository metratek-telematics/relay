@echo off
cd /d "%~dp0"
title Relay

echo.
echo   Relay  -  your coding agents, working as a team
echo   Open: http://127.0.0.1:8767
echo   Keep this window open while using the UI.
echo.

where python >nul 2>&1
if errorlevel 1 (
  echo Python 3.10+ was not found in PATH.
  pause
  exit /b 1
)

python -c "import flask, waitress" >nul 2>&1
if errorlevel 1 (
  echo Installing local UI dependencies...
  python -m pip install -r requirements.txt
  if errorlevel 1 (
    echo Dependency installation failed.
    pause
    exit /b 1
  )
)

python web_app.py %*
if errorlevel 1 pause
