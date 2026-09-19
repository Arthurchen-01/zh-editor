@echo off
chcp 65001 >nul
title 清一新教育 · 一键部署
cd /d %~dp0
set PY=python
%PY% --version >nul 2>nul
if errorlevel 1 set PY=py
%PY% --version >nul 2>nul
if errorlevel 1 (
  echo Python not found. Please install Python 3.9+ from python.org
  echo and check "Add Python to PATH" during install.
  pause
  exit /b 1
)
%PY% deploy.py
pause
