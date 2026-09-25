@echo off
chcp 65001 >nul
cd /d "%~dp0"
title QingYi Local Workbench v3.0
setlocal

echo ====================================================================
echo   QingYi User Client v3.0 - All-in-One Local Workbench
echo   (Auto Browser Login / QR Scan / Word Export / Classics ^& Law Edit)
echo ====================================================================
echo.

where python >nul 2>nul || (
  echo [!] Python 3.9+ not found. Please install Python 3.9+ and check "Add Python to PATH".
  pause
  exit /b 1
)

if not exist .venv (
  echo [1/3] Creating local virtual environment .venv ...
  python -m venv .venv
)
call .venv\Scripts\activate.bat

echo [2/3] Checking dependencies (requests / python-docx / beautifulsoup4 / qrcode / pywin32) ...
python -m pip install -q --disable-pip-version-check requests pywin32 pycryptodome python-docx beautifulsoup4 qrcode pillow

echo [3/3] Starting local visual console (http://127.0.0.1:8765) ...
echo.
python qingyi_client.py --server https://zh.samuraiguan.cloud --key guanjun2026 --auto-cookie --cookie-file cookie.txt --batch 5 --per-day 120
pause
