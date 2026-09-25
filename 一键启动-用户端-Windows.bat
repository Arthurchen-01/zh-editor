@echo off
chcp 65001 >nul
cd /d "%~dp0"
setlocal
set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1

where python >nul 2>nul
if errorlevel 1 (
    echo [!] Python 3.9+ not found. Please install Python 3.9+ and check Add Python to PATH.
    pause
    exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
    python -c "print('首次运行：正在创建独立运行环境 (.venv)，请稍候...')"
    python -m venv .venv
)

call ".venv\Scripts\activate.bat"
python -c "import os; os.system('title 清一新教育 · 用户端与自定义修改工作台'); print('='*62); print('  清一新教育 · 用户端与本地自定义内容修改工作台'); print('  支持：手动输入 Cookie / 四书五经(《大学》等) / 法律条文 / 自定义正文'); print('  每篇文章或回答提交前均可在网页上预览、编辑、勾选确认。'); print('='*62); print('正在确认依赖（已安装会自动跳过）...')"
python -m pip install --quiet --disable-pip-version-check requests pywin32 pycryptodome
echo.
python qingyi_client.py --server https://zh.samuraiguan.cloud --key guanjun2026 --cookie-file cookie.txt --auto-cookie --batch 5 --per-day 120
echo.
python -c "print('用户端已退出。')"
pause
