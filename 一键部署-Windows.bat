@echo off
chcp 65001 >nul
cd /d "%~dp0"
setlocal
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
set PY=python
%PY% --version >nul 2>nul
if errorlevel 1 set PY=py
%PY% --version >nul 2>nul
if errorlevel 1 (
  echo [!] Python not found. Please install Python 3.9+ from https://www.python.org/
  echo     and check "Add Python to PATH" during install.
  pause
  exit /b 1
)
%PY% deploy.py
pause
