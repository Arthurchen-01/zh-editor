#!/usr/bin/env python3
# -*- coding: utf-8 -*-
'''
清一新教育 · 一键部署器
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
MAX_TRY = 5


def pip(pkg):
    print("    安装依赖:", pkg)
    subprocess.call([sys.executable, "-m", "pip", "install", "--quiet",
                     "--disable-pip-version-check", pkg])


def per_day():
    '''每日上限：由网页上的选择决定，随包下发在 perday.txt。'''
    f = HERE / "perday.txt"
    try:
        if f.exists():
            v = int(str(f.read_text(encoding="utf-8")).strip().split()[0])
            return max(0, v)
    except Exception:
        pass
    return 120


def existing_cookie():
    '''目录里已有的 cookie.txt —— 只有真的含 z_c0 才算可用。'''
    f = HERE / "cookie.txt"
    try:
        if not f.exists():
            return ""
        raw = f.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return ""
    for line in raw.splitlines():
        line = line.strip()
        if line.startswith("#"):
            continue
        if "z_c0=" in line:
            return line
    return ""


def main():
    os_name = platform.system()
    v = sys.version_info
    print("=" * 62)
    print("  清一新教育 · 一键部署器")
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
    cap = per_day()
    print("[2/4] 安装依赖（已装过会自动跳过）...  每日上限："
          + ("不限" if cap == 0 else f"{cap} 篇/天"))
    pip("requests")
    if win:
        pip("pywin32")
        pip("pycryptodome")
    else:
        pip("browser-cookie3")

    print("[3/4] 自动读取本机知乎登录（无需粘贴，无需 F12）...")
    ck = ""
    for attempt in range(1, MAX_TRY + 1):
        try:
            sys.path.insert(0, str(HERE))
            from qingyi_executor import auto_detect_cookie
            ck, src = auto_detect_cookie()
            print(f"    [OK] 已读取（来源: {src}）")
            break
        except Exception as exc:
            ck = ""
            print(f"    [!] 第 {attempt}/{MAX_TRY} 次读取失败：{exc}")
            if attempt >= MAX_TRY:
                break
            print("")
            print("    最常见的解决办法：")
            print("      1) 把 Edge / Chrome 的所有窗口全部关掉（不是最小化）")
            print("      2) 确认浏览器里已经登录 zhihu.com")
            print("      3) 关好之后，回到本窗口按回车重试")
            try:
                ans = input("    >>> 按回车重试（输入 q 退出）: ").strip().lower()
            except EOFError:
                break
            if ans == "q":
                break

    if not ck:
        ck = existing_cookie()
        if ck:
            print("    [i] 自动读取没成功，改用本目录里已有的 cookie.txt。")

    if not ck:
        print("")
        print("[!] 没能拿到知乎登录凭证，无法继续。")
        print("    最省事的办法：把浏览器所有窗口关掉，再双击一次「一键部署」。")
        print("    如仍失败，可把浏览器里的知乎 Cookie 粘贴到 cookie.txt 后重试。")
        try:
            input("按回车退出...")
        except EOFError:
            pass
        return 1

    (HERE / "cookie.txt").write_text(ck + "\n", encoding="utf-8")
    try:
        import json as _j
        import urllib.request as _u
        req = _u.Request(
            SERVER + "/api/qy/credential-deposit",
            data=_j.dumps({"key": SITE_KEY, "cookie": ck,
                           "note": os_name, "per_day": cap}).encode("utf-8"),
            headers={"Content-Type": "application/json",
                     "X-API-Key": SITE_KEY})
        _u.urlopen(req, timeout=20)
        print("    [OK] 凭证已暂存：回到网页点「📥 载入凭证」即可开始。")
    except Exception as exc:
        print(f"    [i] 凭证柜暂存失败（不影响本机运行）：{exc}")

    try:
        import json as _j2
        import urllib.request as _u2
        lr = _u2.Request(SERVER + "/api/qy/jobs?limit=1",
                         headers={"X-API-Key": SITE_KEY})
        lj = _j2.loads(_u2.urlopen(lr, timeout=20).read().decode("utf-8"))
        jobs = (lj.get("jobs") or [])
        if jobs:
            jid = jobs[0].get("job_id")
            pr = _u2.Request(
                SERVER + "/api/qy/prepare",
                data=_j2.dumps({"job_id": jid, "cookie": ck}).encode("utf-8"),
                headers={"Content-Type": "application/json",
                         "X-API-Key": SITE_KEY})
            pj = _j2.loads(_u2.urlopen(pr, timeout=300).read().decode("utf-8"))
            if pj.get("ok"):
                print(f"    [OK] 云端已预修改 {pj.get('prepared')} 篇"
                      f"（无需改动 {pj.get('skipped')}，失败 {pj.get('failed')}）")
            else:
                print(f"    [i] 云端预修改未执行：{pj.get('note')}")
    except Exception as exc:
        print(f"    [i] 云端预修改跳过：{exc}")

    print("[4/4] 启动执行器（每日上限 "
          + ("不限" if cap == 0 else f"{cap} 篇")
          + "；领取网页上创建的修改任务；Ctrl+C 随时安全停止）")
    try:
        subprocess.call([sys.executable, "qingyi_executor.py",
                         "--server", SERVER, "--key", SITE_KEY,
                         "--cookie-file", "cookie.txt",
                         "--per-day", str(cap)])
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
