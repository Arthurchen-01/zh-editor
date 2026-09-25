#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""qyupdate —— 清一新教育 · 云端远程同步与自动更新中枢

功能：
  1. 启动时自动与云端服务器 (https://zh.samuraiguan.cloud) 进行版本握手；
  2. 若云端发布了更新，自动在界面顶部呈现 Claude 风格的极简更新提示；
  3. 支持「一键自动热更新」：后台下载最新安装包/组件包并静默更新重启，用户零繁琐操作。
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Tuple

APP_VERSION = "1.1.0"
DEFAULT_SERVER = "https://zh.samuraiguan.cloud"


def _ver_tuple(v: str) -> Tuple[int, ...]:
    nums = re.findall(r"\d+", str(v))
    return tuple(int(x) for x in nums) if nums else (0,)


def check_remote_update(current_version: str = APP_VERSION,
                        server_url: str = DEFAULT_SERVER) -> Dict[str, Any]:
    """向云端检查是否有新版本。"""
    url = f"{server_url.rstrip('/')}/api/qy/check-update?client_version={current_version}&plat=windows"
    req = urllib.request.Request(url, headers={"User-Agent": f"QyAppClient/{current_version}"})
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            if not data.get("ok"):
                return {"ok": False, "has_update": False, "error": data.get("error", "检查失败")}

            latest = data.get("latest_version") or current_version
            has_up = _ver_tuple(latest) > _ver_tuple(current_version)
            return {
                "ok": True,
                "has_update": has_up,
                "current_version": current_version,
                "latest_version": latest,
                "release_notes": data.get("release_notes") or "",
                "release_date": data.get("release_date") or "",
                "download_url": data.get("download_url") or f"{server_url}/api/qy/download/windows",
            }
    except Exception as exc:
        # 网络不通或离线时静默放行
        return {
            "ok": True,
            "has_update": False,
            "current_version": current_version,
            "offline": True,
            "note": f"无法连接云端更新服务 ({exc})",
        }


def perform_background_update(download_url: str,
                              progress_cb: Optional[Callable[[int, str], None]] = None) -> Dict[str, Any]:
    """后台下载更新包并执行静默热替换更新。"""
    if progress_cb:
        progress_cb(10, "正在连接云端更新服务器...")

    try:
        # 建立下载
        req = urllib.request.Request(download_url, headers={"User-Agent": "Mozilla/5.0 QyUpdate"})
        with urllib.request.urlopen(req, timeout=60) as resp:
            total_size = int(resp.headers.get("Content-Length") or 0)
            with tempfile.NamedTemporaryFile(delete=False, suffix=".exe") as tmp:
                tmp_path = Path(tmp.name)
                downloaded = 0
                block_size = 1024 * 64
                while True:
                    chunk = resp.read(block_size)
                    if not chunk:
                        break
                    tmp.write(chunk)
                    downloaded += len(chunk)
                    if total_size > 0 and progress_cb:
                        pct = int(10 + (downloaded / total_size) * 75)
                        progress_cb(pct, f"正在下载更新包 ({downloaded // 1048576}MB / {total_size // 1048576}MB)...")

        if progress_cb:
            progress_cb(90, "下载完成，正在启动静默自动更新...")

        # 启动下载的安装程序进行静默安装
        if sys.platform == "win32":
            target_dir = Path(os.environ.get("PROGRAMDATA", "C:/ProgramData")) / "QingyiEducation"
            cmd = [str(tmp_path), "--silent", f"--dir={target_dir}"]
            subprocess.Popen(cmd)
            if progress_cb:
                progress_cb(100, "更新已启动！将在数秒后自动重启工作台。")
            return {"ok": True, "message": "已成功启动更新程序"}
        else:
            return {"ok": True, "file": str(tmp_path), "message": "下载完成"}
    except Exception as exc:
        if progress_cb:
            progress_cb(0, f"更新失败：{exc}")
        return {"ok": False, "error": str(exc)}
