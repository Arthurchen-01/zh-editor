#!/bin/bash
# 清一新教育 · 文章工作台 —— macOS 启动器
# 双击即可。不需要安装任何东西。

cd "$(dirname "$0")" || exit 1

echo "============================================================"
echo "  清一新教育 · 文章工作台"
echo "============================================================"
echo

# ---- 找 Python ----
PY=""
for c in python3 /usr/bin/python3 /opt/homebrew/bin/python3 /usr/local/bin/python3; do
  if command -v "$c" >/dev/null 2>&1; then PY="$c"; break; fi
done
if [ -z "$PY" ]; then
  echo "✗ 没找到 python3。"
  echo
  echo "  macOS 自带的 python3 需要装一次命令行工具："
  echo "    打开「终端」，输入：xcode-select --install"
  echo "    弹出窗口点「安装」，等它跑完，再双击本文件。"
  echo
  read -r -p "按回车关闭…" _
  exit 1
fi

echo "  使用 Python：$PY"
"$PY" -c 'import sys; assert sys.version_info >= (3, 9), "需要 Python 3.9 以上"' || {
  echo "✗ Python 版本太低，需要 3.9 或以上。"
  read -r -p "按回车关闭…" _
  exit 1
}
echo "  版本：$("$PY" -V 2>&1)"
echo

if [ ! -f cookie.txt ]; then
  echo "  提示：还没登录。启动后在页面左下角点「填入 Cookie」。"
  echo "  （Cookie 只存在本机的 cookie.txt，不会上传到任何服务器）"
  echo
fi

echo "  正在启动…浏览器会自动打开。关掉这个窗口即停止。"
echo "============================================================"
echo

exec "$PY" -u local/qyapp.py "$@"
