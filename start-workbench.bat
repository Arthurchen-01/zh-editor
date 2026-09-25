@echo off
REM QingyiEdu Article Workbench - Windows launcher
REM NOTE: this launcher has NOT been tested on a real Windows machine yet.
REM The Python code itself is stdlib-only and platform-neutral.
REM Keep this file ASCII-only: the Windows console codepage will garble
REM non-ASCII characters in .bat files.

cd /d "%~dp0"

echo ============================================================
echo   QingyiEdu Article Workbench
echo ============================================================
echo.

where python >nul 2>&1
if errorlevel 1 (
  echo [X] python not found in PATH.
  echo     Install Python 3.9+ from https://www.python.org/downloads/
  echo     and tick "Add python.exe to PATH" during setup.
  echo.
  pause
  exit /b 1
)

python -c "import sys; assert sys.version_info >= (3,9)" >nul 2>&1
if errorlevel 1 (
  echo [X] Python 3.9 or newer is required.
  echo.
  pause
  exit /b 1
)

if not exist cookie.txt (
  echo [i] Not logged in yet. After the page opens, click
  echo     "Fill in Cookie" at the bottom-left.
  echo.
)

echo Starting... the browser will open automatically.
echo Close this window to stop.
echo ============================================================
echo.

python -u local\qyapp.py %*
pause
