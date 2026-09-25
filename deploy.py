#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
清一新教育 · 一键部署与本地修改启动器 (v2.0)
支持：
1. 手动提供 Cookie（优先读取 cookie.txt，或直接在终端/本地网页粘贴 Cookie，无需关闭 Edge/Chrome 浏览器）
2. 云端凭证柜自动同步与拉取
3. 本地可视化软件控制台（http://127.0.0.1:8765）：支持在软件内直接选择「四书五经（如《大学》《中庸》《论语》《孟子》）」「国家法律条文（如《宪法》《民法典》）」或「直接填写自定义标题与正文」修改知乎文章与回答
"""

import ctypes
import json
import platform
import subprocess
import sys
import urllib.request
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

if sys.platform == "win32":
    try:
        ctypes.windll.kernel32.SetConsoleTitleW("清一新教育 · 本地一键部署与修改工具 v2.0")
    except Exception:
        pass

SERVER = "https://zh.samuraiguan.cloud"
SITE_KEY = "guanjun2026"
HERE = Path(__file__).resolve().parent


def pip(pkg: str) -> None:
    print("    正在检查/安装依赖:", pkg)
    subprocess.call([
        sys.executable, "-m", "pip", "install", "--quiet",
        "--disable-pip-version-check", pkg
    ])


def per_day() -> int:
    """每日上限：由网页上的选择决定，随包下发在 perday.txt。"""
    f = HERE / "perday.txt"
    try:
        if f.exists():
            v = int(str(f.read_text(encoding="utf-8")).strip().split()[0])
            return max(0, v)
    except Exception:
        pass
    return 120


def clean_cookie(raw: str) -> str:
    if not raw:
        return ""
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.lower().startswith("cookie:"):
            line = line[7:].strip()
        line = line.strip("\"' ")
        if "z_c0=" in line or "d_c0=" in line or len(line) > 60:
            return line
    return ""


def existing_cookie() -> str:
    """优先读取本目录已有的 cookie.txt。"""
    f = HERE / "cookie.txt"
    try:
        if not f.exists():
            return ""
        return clean_cookie(f.read_text(encoding="utf-8", errors="replace"))
    except Exception:
        return ""


def cloud_cookie() -> str:
    """从云端凭证柜拉取最近暂存的凭证。"""
    try:
        req = urllib.request.Request(
            f"{SERVER}/api/qy/credential-latest?key={SITE_KEY}",
            headers={"X-API-Key": SITE_KEY},
        )
        raw = urllib.request.urlopen(req, timeout=12).read().decode("utf-8")
        j = json.loads(raw)
        if j.get("ok") and j.get("cookie"):
            return clean_cookie(j["cookie"])
    except Exception:
        pass
    return ""


def deposit_to_cloud(ck: str, os_name: str, cap: int) -> None:
    if "z_c0=" not in (ck or ""):
        return
    try:
        req = urllib.request.Request(
            SERVER + "/api/qy/credential-deposit",
            data=json.dumps({
                "key": SITE_KEY,
                "cookie": ck,
                "note": os_name,
                "per_day": cap,
            }).encode("utf-8"),
            headers={"Content-Type": "application/json", "X-API-Key": SITE_KEY},
        )
        urllib.request.urlopen(req, timeout=15)
        print("    [OK] 凭证已同步至云端凭证柜（网页端点击「📥 载入凭证」即可使用）。")
    except Exception as exc:
        print(f"    [i] 云端凭证柜同步跳过（不影响本机使用）：{exc}")


def main() -> int:
    os_name = platform.system()
    v = sys.version_info
    print("=" * 66)
    print("  清一新教育 · 一键部署与本地自定义修改工具 v2.0")
    print("  支持：手动输入 Cookie · 四书五经（大学等）· 法律条文 · 自定义修改内容")
    print("=" * 66)
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
    cap = per_day()
    print("[2/4] 检查依赖（已安装会自动跳过）...  每日上限："
          + ("不限" if cap == 0 else f"{cap} 篇/天"))
    pip("requests")
    if win:
        pip("pywin32")
        pip("pycryptodome")
    else:
        pip("browser-cookie3")

    print("[3/4] 获取知乎登录凭证（优先读取 cookie.txt -> 自动检测 -> 云端凭证柜 -> 手动粘贴）...")
    ck = existing_cookie()
    if ck:
        print("    [OK] 已直接从本目录 cookie.txt 读取到知乎 Cookie！")
    else:
        try:
            sys.path.insert(0, str(HERE))
            from qingyi_executor import auto_detect_cookie
            ck_auto, src = auto_detect_cookie()
            ck = clean_cookie(ck_auto)
            if ck:
                print(f"    [OK] 已自动从本机浏览器读取登录（来源: {src}）")
        except Exception as exc:
            print(f"    [i] 浏览器自动读取跳过（{exc}）—— 无需关闭浏览器，支持手动提供！")

    if not ck:
        ck = cloud_cookie()
        if ck:
            print("    [OK] 已从云端凭证柜自动取回知乎 Cookie！")

    if not ck:
        print("")
        print("  ----------------------------------------------------------------")
        print("  您可以直接手动提供知乎 Cookie（无需关闭 Edge / Chrome 浏览器）：")
        print("  · 方法 1：直接将复制的知乎 Cookie 粘贴到下方并按回车；")
        print("  · 方法 2：把 Cookie 粘贴保存到本目录的 cookie.txt 文件后按回车；")
        print("  · 方法 3：直接按回车打开本地可视化网页控制台，在网页里粘贴 Cookie！")
        print("  ----------------------------------------------------------------")
        try:
            ans = input("  >>> 请在此粘贴 Cookie（或直接按回车打开可视化软件界面）: ").strip()
        except EOFError:
            ans = ""
        if ans and ans.lower() != "q":
            ck = clean_cookie(ans)
        if not ck:
            ck = existing_cookie()

    if ck:
        (HERE / "cookie.txt").write_text(ck + "\n", encoding="utf-8")
        deposit_to_cloud(ck, os_name, cap)

    # 如果目录下有 qingyi_client.py，优先启动本地可视化控制台（支持在软件内选四书五经/法律条文/自定义内容/手动填Cookie）
    client_py = HERE / "qingyi_client.py"
    if client_py.exists():
        print("[4/4] 正在启动本地可视化修改工作台（http://127.0.0.1:8765）...")
        print("      在打开的软件页面中，您可以：")
        print("      1) 随时手动粘贴/更换 Cookie；")
        print("      2) 直接选择《大学》《中庸》《论语》《孟子》等四书五经、《宪法》《民法典》等法律条文、或直接填写自定义内容；")
        print("      3) 本地直接拉取文章/回答预览并勾选修改（自动备份原文，可一键还原）。")
        cmd = [
            sys.executable, str(client_py),
            "--server", SERVER,
            "--key", SITE_KEY,
            "--cookie-file", "cookie.txt",
            "--batch", "5",
            "--per-day", str(cap),
        ]
        try:
            return subprocess.call(cmd)
        except KeyboardInterrupt:
            return 0

    print("[4/4] 启动本地执行器（Ctrl+C 随时安全停止）")
    try:
        subprocess.call([
            sys.executable, "qingyi_executor.py",
            "--server", SERVER, "--key", SITE_KEY,
            "--cookie-file", "cookie.txt",
            "--per-day", str(cap),
        ])
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
