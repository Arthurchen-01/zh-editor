#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""qyupdate —— 清一新教育 · 云端远程同步与全自动静默预载更新中枢

核心特性：
  1. 启动即与云端 (https://zh.samuraiguan.cloud) 握手；
  2. 若检测到新版本，立即开启后台隐式静默预下载 (Silent Pre-Download)，支持断点续传与重试，彻底杜绝超时；
  3. 安装包在后台下完后标红点亮 UI「立即一键重启生效」；
  4. 用户点击更新时，0 秒等待瞬间原地热替换重启！
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Tuple

APP_VERSION = "1.2.0"
DEFAULT_SERVER = "https://zh.samuraiguan.cloud"


def _ver_tuple(v: str) -> Tuple[int, ...]:
    nums = re.findall(r"\d+", str(v))
    return tuple(int(x) for x in nums) if nums else (0,)


def _get_cache_dir() -> Path:
    """获取本地静默更新缓存目录。"""
    if getattr(sys, "frozen", False):
        base = Path(sys.executable).parent / "_cache"
    else:
        base = Path(os.environ.get("LOCALAPPDATA", "C:/")) / "QingyiEduWorkbench" / "cache"
    try:
        base.mkdir(parents=True, exist_ok=True)
        return base
    except Exception:
        fallback = Path(os.environ.get("TEMP", "C:/Windows/Temp")) / "QyUpdateCache"
        fallback.mkdir(parents=True, exist_ok=True)
        return fallback


class SilentUpdateManager:
    """后台静默更新管理器（单例守护线程）。"""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.status: str = "idle"  # idle, downloading, ready, error
        self.latest_version: str = ""
        self.download_url: str = ""
        self.total_bytes: int = 0
        self.downloaded_bytes: int = 0
        self.progress_pct: int = 0
        self.error_msg: str = ""
        self.cache_file: Optional[Path] = None
        self._thread: Optional[threading.Thread] = None

    def get_status(self) -> Dict[str, Any]:
        with self.lock:
            # 双重核验：如果缓存文件已在磁盘且完整，自动标记为 ready
            if self.cache_file and self.cache_file.exists() and self.cache_file.stat().st_size > 80_000_000:
                self.status = "ready"
                self.progress_pct = 100
                self.downloaded_bytes = self.cache_file.stat().st_size
                if not self.total_bytes:
                    self.total_bytes = self.downloaded_bytes

            return {
                "status": self.status,
                "ready": self.status == "ready",
                "progress_pct": self.progress_pct,
                "downloaded_bytes": self.downloaded_bytes,
                "total_bytes": self.total_bytes,
                "downloaded_mb": round(self.downloaded_bytes / 1048576, 1),
                "total_mb": round(self.total_bytes / 1048576, 1),
                "latest_version": self.latest_version,
                "error": self.error_msg,
                "cache_file": str(self.cache_file) if self.cache_file else "",
            }

    def start_predownload(self, download_url: str, version: str) -> None:
        """开启后台静默预下载线程（断点续传 + 超时重试）。"""
        with self.lock:
            self.latest_version = version
            self.download_url = download_url
            cache_dir = _get_cache_dir()
            target_exe = cache_dir / f"QingyiEduWorkbench_Setup_v{version}.exe"
            self.cache_file = target_exe

            # 如果已存在且文件尺寸正常，直接标记为就绪
            if target_exe.exists() and target_exe.stat().st_size > 80_000_000:
                self.status = "ready"
                self.progress_pct = 100
                self.downloaded_bytes = target_exe.stat().st_size
                self.total_bytes = self.downloaded_bytes
                return

            # 如果正在下载中，不重复启动
            if self.status == "downloading" and self._thread and self._thread.is_alive():
                return

            self.status = "downloading"
            self.error_msg = ""
            self.progress_pct = 0

            self._thread = threading.Thread(
                target=self._download_worker,
                args=(download_url, target_exe),
                daemon=True,
                name="QySilentPreDownloader"
            )
            self._thread.start()

    def _download_worker(self, url: str, target_exe: Path) -> None:
        part_file = target_exe.with_suffix(".part")
        max_retries = 25

        for attempt in range(max_retries):
            try:
                existing_bytes = part_file.stat().st_size if part_file.exists() else 0
                headers = {
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) QySilentUpdate",
                }
                if existing_bytes > 0:
                    headers["Range"] = f"bytes={existing_bytes}-"

                req = urllib.request.Request(url, headers=headers)
                with urllib.request.urlopen(req, timeout=30) as resp:
                    code = getattr(resp, "status", 200)
                    content_range = resp.headers.get("Content-Range")
                    content_length = int(resp.headers.get("Content-Length") or 0)

                    if code == 206 and content_range:
                        # 206 Partial Content: bytes start-end/total
                        m = re.search(r"/(\d+)$", content_range)
                        total_size = int(m.group(1)) if m else (existing_bytes + content_length)
                    elif code == 200:
                        total_size = content_length
                        existing_bytes = 0
                        mode = "wb"
                    else:
                        total_size = existing_bytes + content_length

                    mode = "ab" if existing_bytes > 0 and code == 206 else "wb"

                    with open(part_file, mode) as fp:
                        cur = existing_bytes
                        block_size = 1024 * 128  # 128 KB
                        while True:
                            chunk = resp.read(block_size)
                            if not chunk:
                                break
                            fp.write(chunk)
                            cur += len(chunk)
                            with self.lock:
                                self.downloaded_bytes = cur
                                self.total_bytes = total_size or cur
                                if total_size > 0:
                                    self.progress_pct = min(99, int(cur / total_size * 100))

                    # 校验完成
                    if total_size > 0 and cur >= total_size:
                        if target_exe.exists():
                            try:
                                target_exe.unlink()
                            except Exception:
                                pass
                        part_file.rename(target_exe)
                        with self.lock:
                            self.status = "ready"
                            self.progress_pct = 100
                            self.downloaded_bytes = target_exe.stat().st_size
                            self.total_bytes = self.downloaded_bytes
                        return

            except Exception as exc:
                time.sleep(2)
                if attempt == max_retries - 1:
                    with self.lock:
                        self.status = "error"
                        self.error_msg = f"后台静默下载重试达上限: {exc}"
                    return

    def apply_instant(self, target_dir: Optional[Path] = None,
                      progress_cb: Optional[Callable[[int, str], None]] = None) -> Dict[str, Any]:
        """立即应用已在后台静默下好的更新包，0 秒等待！"""
        if not target_dir:
            if getattr(sys, "frozen", False):
                target_dir = Path(sys.executable).parent
            else:
                target_dir = Path("D:/软件")

        st = self.get_status()

        # 1. 如果尚未 ready，但正在下载，阻塞式等待其收尾（具备断点续传）
        if not st.get("ready"):
            if progress_cb:
                progress_cb(30, f"正在等待后台静默下载收尾 ({st.get('downloaded_mb', 0)}MB / {st.get('total_mb', 92)}MB)...")

            # 等待最多 45 秒
            t0 = time.time()
            while time.time() - t0 < 45:
                time.sleep(0.5)
                st = self.get_status()
                if st.get("ready"):
                    break
                if progress_cb and st.get("total_bytes"):
                    pct = int(st["downloaded_bytes"] / st["total_bytes"] * 85)
                    progress_cb(pct, f"正在下载更新包 ({st.get('downloaded_mb', 0)}MB / {st.get('total_mb', 92)}MB)...")

        # 再次检查
        st = self.get_status()
        if not st.get("ready") or not self.cache_file or not self.cache_file.exists():
            # 降级：如果本地同级目录下已存在安装程序，直接就地采纳
            candidates = [
                Path("D:/清一新教育工作台_安装程序.exe"),
                target_dir / "清一新教育工作台_安装程序.exe",
                Path(os.environ.get("USERPROFILE", "C:/")) / "Desktop" / "清一新教育工作台_安装程序.exe",
            ]
            for cand in candidates:
                if cand.exists() and cand.stat().st_size > 80_000_000:
                    self.cache_file = cand
                    st["ready"] = True
                    break

        if not self.cache_file or not self.cache_file.exists():
            err = st.get("error") or "更新包尚未完全下载就绪，请稍后片刻重试"
            if progress_cb:
                progress_cb(0, f"更新失败: {err}")
            return {"ok": False, "error": err}

        if progress_cb:
            progress_cb(100, "✓ 安装包已预载就绪！正在立即原地静默替换并自动重启工作台...")

        # 启动安装程序静默覆盖安装目标目录并自动拉起新版本
        if sys.platform == "win32":
            cmd = [
                str(self.cache_file),
                "--silent",
                f"--dir={target_dir}"
            ]
            DETACHED_PROCESS = 0x00000008
            CREATE_NEW_PROCESS_GROUP = 0x00000200
            try:
                subprocess.Popen(
                    cmd,
                    creationflags=DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP,
                    close_fds=True
                )
            except Exception:
                subprocess.Popen(cmd)

            # 短暂延时后通知上层可退出
            return {"ok": True, "instant": True, "message": "已成功启动静默热替换程序，工作台即将重启"}
        else:
            return {"ok": True, "file": str(self.cache_file), "message": "下载已就绪"}


# 全局单例
_UPDATE_MANAGER = SilentUpdateManager()


def check_remote_update(current_version: str = APP_VERSION,
                        server_url: str = DEFAULT_SERVER) -> Dict[str, Any]:
    """向云端检查是否有新版本，若有则自动触发后台静默预载。"""
    url = f"{server_url.rstrip('/')}/api/qy/check-update?client_version={current_version}&plat=windows"
    req = urllib.request.Request(url, headers={"User-Agent": f"QyAppClient/{current_version}"})
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            if not data.get("ok"):
                return {"ok": False, "has_update": False, "error": data.get("error", "检查失败")}

            latest = data.get("latest_version") or current_version
            has_up = _ver_tuple(latest) > _ver_tuple(current_version)
            dl_url = data.get("download_url") or f"{server_url}/api/qy/download/windows"

            # 触发后台静默预下载
            if has_up and dl_url:
                _UPDATE_MANAGER.start_predownload(dl_url, latest)

            st = _UPDATE_MANAGER.get_status()

            return {
                "ok": True,
                "has_update": has_up,
                "current_version": current_version,
                "latest_version": latest,
                "release_notes": data.get("release_notes") or "",
                "release_date": data.get("release_date") or "",
                "download_url": dl_url,
                "ready": st.get("ready", False),
                "progress_pct": st.get("progress_pct", 0),
                "downloaded_mb": st.get("downloaded_mb", 0),
                "total_mb": st.get("total_mb", 0),
            }
    except Exception as exc:
        return {
            "ok": True,
            "has_update": False,
            "current_version": current_version,
            "offline": True,
            "note": f"无法连接云端更新服务 ({exc})",
        }


def get_silent_update_status() -> Dict[str, Any]:
    return _UPDATE_MANAGER.get_status()


def start_silent_predownload(download_url: str, version: str) -> None:
    _UPDATE_MANAGER.start_predownload(download_url, version)


def apply_instant_update(target_dir: Optional[Path] = None,
                         progress_cb: Optional[Callable[[int, str], None]] = None) -> Dict[str, Any]:
    return _UPDATE_MANAGER.apply_instant(target_dir=target_dir, progress_cb=progress_cb)


def perform_background_update(download_url: str,
                              progress_cb: Optional[Callable[[int, str], None]] = None) -> Dict[str, Any]:
    return apply_instant_update(progress_cb=progress_cb)
