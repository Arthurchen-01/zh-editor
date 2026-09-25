#!/usr/bin/env python3
"""清一新教育 · 知乎内容工作台 (v3.0 桌面专业版)
集成：
1. 本地轻量级 HTTP 引擎 (qingyi_client)
2. 现代原生桌面窗口 (Edge WebView2)
3. Windows 右下角系统托盘常驻 (点击叉叉最小化至托盘后台，双击或菜单随时呼出)
4. 应用图标 (.ico / .png)
"""
import argparse
import os
import sys
import threading
import time
import webbrowser
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Optional

from PIL import Image
import pystray
import webview

# 确保能找到本目录下的 qingyi_client 及其依赖
if getattr(sys, "frozen", False):
    APP_DIR = Path(sys._MEIPASS)  # type: ignore[attr-defined]
    EXE_DIR = Path(sys.executable).parent
else:
    APP_DIR = Path(__file__).resolve().parent
    EXE_DIR = APP_DIR

sys.path.insert(0, str(APP_DIR))

import qingyi_client as qc  # noqa: E402

_ICON_PNG = APP_DIR / "app_icon.png"
_ICON_ICO = APP_DIR / "app_icon.ico"
if not _ICON_PNG.exists() and (EXE_DIR / "app_icon.png").exists():
    _ICON_PNG = EXE_DIR / "app_icon.png"
if not _ICON_ICO.exists() and (EXE_DIR / "app_icon.ico").exists():
    _ICON_ICO = EXE_DIR / "app_icon.ico"


class DesktopApp:
    def __init__(self, port: int = 8765, server: str = "https://zh.samuraiguan.cloud"):
        self.port = qc._pick_port(port)
        self.server_url = server
        self.window = None
        self.tray = None
        self.httpd = None
        self.server_thread = None
        self.is_exiting = False
        self.exports_dir = EXE_DIR / "exports"
        self.exports_dir.mkdir(parents=True, exist_ok=True)

        # 加载托盘图标图像
        if _ICON_PNG.exists():
            self.icon_image = Image.open(_ICON_PNG)
        else:
            self.icon_image = Image.new("RGBA", (64, 64), (37, 99, 235, 255))

    def start_http_server(self):
        key = os.environ.get("QY_API_KEY") or "guanjun2026"
        cookie = ""
        cfile = EXE_DIR / "cookie.txt"
        if cfile.exists():
            cookie = qc._clean_cookie_str(cfile.read_text(encoding="utf-8", errors="replace"))

        client = qc.Client(
            server=self.server_url,
            key=key,
            cookie=cookie,
            cookie_file=str(cfile),
            per_day=120,
            batch=qc.DEFAULT_BATCH,
            backup_dir=str(EXE_DIR / "data" / "qyedu_backup"),
            daily_path=str(EXE_DIR / "data" / "daily.json"),
            stagger=1.5,
            port=self.port,
        )
        if client.cookie:
            try:
                client.preflight()
                client.deposit_credential()
                client.load_local_articles(kind="all", reset=True)
            except Exception as e:
                client.mark_startup_error(str(e))
        else:
            client.mark_startup_error("尚未登录知乎 —— 请点击上方「🔒 自动关闭浏览器并直接读取登录」或「📱 扫码登录知乎」")

        qc._Handler.client = client
        self.httpd = ThreadingHTTPServer(("127.0.0.1", self.port), qc._Handler)
        self.server_thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.server_thread.start()

    def show_window(self, icon=None, item=None):
        if self.window:
            self.window.show()
            self.window.restore()

    def open_manual(self, icon=None, item=None):
        webbrowser.open(f"{self.server_url}/api/qy/console")

    def open_exports(self, icon=None, item=None):
        try:
            os.startfile(str(self.exports_dir))
        except Exception:
            webbrowser.open(str(self.exports_dir))

    def exit_app(self, icon=None, item=None):
        self.is_exiting = True
        if self.tray:
            self.tray.stop()
        if self.window:
            self.window.destroy()
        if self.httpd:
            try:
                self.httpd.shutdown()
            except Exception:
                pass
        sys.exit(0)

    def on_window_closing(self):
        """用户点击窗口右上角叉叉时，拦截关闭事件并最小化到右下角托盘后台。"""
        if self.is_exiting:
            return True
        # 隐藏窗口
        if self.window:
            self.window.hide()
        # 发送系统托盘气泡通知
        if self.tray:
            try:
                self.tray.notify(
                    "清一新教育工作台已最小化至右下角系统托盘。\n双击托盘图标或右键即可重新打开。",
                    "已最小化至后台",
                )
            except Exception:
                pass
        return False  # 阻止窗口真正被销毁

    def setup_tray(self):
        menu = pystray.Menu(
            pystray.MenuItem("🖥️ 显示工作台主界面", self.show_window, default=True),
            pystray.MenuItem("📖 查看云端使用手册", self.open_manual),
            pystray.MenuItem("📂 打开存证导出目录", self.open_exports),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("🚪 退出程序", self.exit_app),
        )
        self.tray = pystray.Icon(
            "qingyi_edu_tray",
            self.icon_image,
            "清一新教育 · 知乎内容工作台 (运行中)",
            menu,
        )
        tray_thread = threading.Thread(target=self.tray.run, daemon=True)
        tray_thread.start()

    def run(self):
        self.start_http_server()
        self.setup_tray()

        url = f"http://127.0.0.1:{self.port}"
        self.window = webview.create_window(
            title="清一新教育 · 知乎内容工作台",
            url=url,
            width=1280,
            height=860,
            min_size=(1024, 700),
            confirm_close=False,
            background_color="#f4f6f9",
        )
        self.window.events.closing += self.on_window_closing

        # 启动 WebView 事件主循环（在 Windows 上阻塞当前主线程）
        webview.start(debug=False)


def main():
    ap = argparse.ArgumentParser(description="清一新教育 · 知乎内容工作台 (桌面版)")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--server", default="https://zh.samuraiguan.cloud")
    args = ap.parse_args()

    app = DesktopApp(port=args.port, server=args.server)
    app.run()


if __name__ == "__main__":
    main()
