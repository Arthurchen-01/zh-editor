#!/usr/bin/env python3
# -*- coding: utf-8 -*-
'''
清一新教育 · 傻瓜部署器
自动：识别设备 → 装依赖 → 读取本机浏览器知乎登录 → 生成 cookie.txt →
      上传凭证柜（网页点「载入凭证」即可用）→ 启动执行器。
全程不需要粘贴，不需要 F12。
'''

import platform
import subprocess
import sys
from pathlib import Path

SERVER = "https://zh.samuraiguan.cloud"
SITE_KEY = "guanjun2026"
HERE = Path(__file__).resolve().parent


def pip(pkg):
    print("    安装依赖:", pkg)
    subprocess.call([sys.executable, "-m", "pip", "install", "--quiet",
                     "--disable-pip-version-check", pkg])


def main():
    os_name = platform.system()
    v = sys.version_info
    print("=" * 62)
    print("  清一新教育 · 傻瓜部署器")
    print("=" * 62)
    print(f"[1/4] 设备识别: {os_name} · Python {v.major}.{v.minor}.{v.micro}")
    if v < (3, 9):
        print("[!] 需要 Python 3.9 及以上。请到 python.org 安装，")
        print("    Windows 安装时务必勾选 Add Python to PATH。")
        try:
            input("按回车退出...")
        except EOFError:
            pass
        return 1

    win = os_name == "Windows"
    print("[2/4] 安装依赖（已装过会自动跳过）...")
    pip("requests")
    if win:
        pip("pywin32")
        pip("pycryptodome")
    else:
        pip("browser-cookie3")

    print("[3/4] 自动读取本机知乎登录（无需粘贴，无需 F12）...")
    ck = ""
    for attempt in range(1, 4):
        try:
            sys.path.insert(0, str(HERE))
            from qingyi_executor import auto_detect_cookie
            ck, src = auto_detect_cookie()
            print(f"    [OK] 已读取（来源: {src}）")
            break
        except Exception as exc:
            print(f"    [!] 第 {attempt} 次尝试失败：{exc}")
            ck = ""
            if (HERE / "cookie.txt").exists():
                print("    检测到目录里已有 cookie.txt，将直接使用它继续。")
                break
            if attempt >= 3:
                try:
                    input("    按回车退出...")
                except EOFError:
                    pass
                return 1
            try:
                ans = input(
                    "    请先确保浏览器已登录 zhihu.com；Windows 请完全退出 Edge/Chrome"
                    "（关闭所有窗口），然后按回车重试（输入 q 退出）: ").strip().lower()
            except EOFError:
                return 1
            if ans == "q":
                return 1

    if ck:
        (HERE / "cookie.txt").write_text(ck + "\n", encoding="utf-8")
        try:
            import json as _j
            import urllib.request as _u
            req = _u.Request(
                SERVER + "/api/qy/credential-deposit",
                data=_j.dumps({"key": SITE_KEY, "cookie": ck,
                               "note": os_name}).encode("utf-8"),
                headers={"Content-Type": "application/json",
                         "X-API-Key": SITE_KEY})
            _u.urlopen(req, timeout=20)
            print("    [OK] 凭证已暂存（10 分钟有效）：回到网页点「📥 载入凭证」即可。")
        except Exception as exc:
            print(f"    [i] 凭证柜暂存失败（不影响本机运行）：{exc}")

    print("[4/4] 启动执行器（领取网页上创建的修改任务；Ctrl+C 随时安全停止）")
    try:
        subprocess.call([sys.executable, "qingyi_executor.py",
                         "--server", SERVER, "--key", SITE_KEY,
                         "--cookie-file", "cookie.txt"])
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
