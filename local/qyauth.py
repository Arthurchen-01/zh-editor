#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""qyauth —— 清一新教育 · 智能免 F12 登录中枢

支持 4 种登录方式：
  1. 自动读取本机 Edge / Chrome 浏览器登录态（DPAPI + AES-GCM 解密）
  2. 若浏览器占用锁住文件，提供「关闭浏览器并直读」一键秒解
  3. 手机知乎 App 扫码登录（内嵌高清 Base64 二维码 + 轮询）
  4. 从云端凭证柜同步（如果使用过浏览器扩展或云端扫码）
"""
from __future__ import annotations

import base64
import io
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
import urllib.request

try:
    import requests
except ImportError:
    requests = None  # type: ignore

try:
    import qrcode
    from PIL import Image
except ImportError:
    qrcode = None
    Image = None

ROOT = Path(__file__).resolve().parent.parent
COOKIE_FILE = ROOT / "cookie.txt"

# 扫码会话缓存 (token -> dict)
_QR_SESSIONS: Dict[str, Dict[str, Any]] = {}
_QR_LOCK = threading.RLock()


def clean_cookie_str(raw: str) -> str:
    """清理并格式化知乎 Cookie 字符串。"""
    if not raw:
        return ""
    pairs = []
    for chunk in raw.replace("\r", "").replace("\n", ";").split(";"):
        c = chunk.strip()
        if not c or "=" not in c:
            continue
        k, v = c.split("=", 1)
        k, v = k.strip(), v.strip()
        if k:
            pairs.append(f"{k}={v}")
    return "; ".join(pairs)


def save_cookie_to_disk(cookie: str) -> None:
    COOKIE_FILE.parent.mkdir(parents=True, exist_ok=True)
    COOKIE_FILE.write_text(cookie.strip(), encoding="utf-8")


def read_cookie_from_disk() -> str:
    if COOKIE_FILE.exists():
        try:
            return clean_cookie_str(COOKIE_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return ""


# --------------------------------------------------------------------------- #
# 1. 自动读取本机 Chromium (Edge / Chrome) 浏览器 Cookie
# --------------------------------------------------------------------------- #

def close_browsers_for_read() -> List[str]:
    """关闭可能锁住 Cookie 数据库的 Edge / Chrome 进程。"""
    closed = []
    if sys.platform == "win32":
        for exe, label in (("msedge.exe", "Edge"), ("chrome.exe", "Chrome")):
            try:
                r = subprocess.run(["taskkill", "/F", "/IM", exe],
                                   capture_output=True, timeout=5)
                if r.returncode == 0:
                    closed.append(label)
            except Exception:
                pass
    elif sys.platform == "darwin":
        for app in ("Microsoft Edge", "Google Chrome"):
            try:
                r = subprocess.run(["killall", app], capture_output=True, timeout=5)
                if r.returncode == 0:
                    closed.append(app)
            except Exception:
                pass
    if closed:
        time.sleep(0.8)
    return closed


def auto_detect_browser_cookie(close_browser: bool = False) -> Dict[str, Any]:
    """尝试自动从 Edge / Chrome 提取知乎登录凭证。"""
    if close_browser:
        closed = close_browsers_for_read()

    if sys.platform == "win32":
        try:
            import win32crypt
            from Crypto.Cipher import AES
        except ImportError:
            return {
                "ok": False,
                "error": "缺少本地解密组件（pywin32 / pycryptodome）。",
            }

        home = Path.home()
        candidates = [
            ("Edge", home / "AppData" / "Local" / "Microsoft" / "Edge" / "User Data"),
            ("Chrome", home / "AppData" / "Local" / "Google" / "Chrome" / "User Data"),
            ("Brave", home / "AppData" / "Local" / "BraveSoftware" / "Brave-Browser" / "User Data"),
        ]

        locked_browsers = []
        found_cookie = ""
        found_source = ""

        for b_name, user_data in candidates:
            if not user_data.exists():
                continue

            # 1. 优先扫描 Chromium 的 Local Storage (LevelDB)
            # 无需关闭浏览器、无视 Chromium 127+ v20 App-Bound 限制，秒级瞬时直读！
            profiles = ["Default"] + [f"Profile {i}" for i in range(1, 6)]
            for prof in profiles:
                leveldb_dir = user_data / prof / "Local Storage" / "leveldb"
                if not leveldb_dir.is_dir():
                    continue
                try:
                    for f in list(leveldb_dir.glob("*.ldb")) + list(leveldb_dir.glob("*.log")):
                        try:
                            with open(f, "rb") as fp:
                                data = fp.read()
                            idx = data.find(b"z_c0=2|1:0|10:")
                            if idx != -1:
                                start = max(0, idx - 400)
                                end = min(len(data), idx + 600)
                                sub = data[start:end]
                                m = re.findall(rb'([a-zA-Z0-9_\-\.]+)=([^;,\r\n\" \x00-\x1f]+)', sub)
                                ck_dict = {}
                                for k, v in m:
                                    ks = k.decode('ascii', errors='ignore')
                                    vs = v.decode('ascii', errors='ignore')
                                    if ks in ('z_c0', 'q_c1', '_zap', 'd_c0', '__zse_ck', 'capsion_ticket'):
                                        ck_dict[ks] = vs
                                if 'z_c0' in ck_dict:
                                    found_cookie = "; ".join(f"{k}={v}" for k, v in ck_dict.items())
                                    found_source = f"{b_name} ({prof} 极速直读)"
                                    break
                        except Exception:
                            pass
                    if found_cookie:
                        break
                except Exception:
                    pass
            if found_cookie:
                break

            # 2. 传统 SQLite 扫描（适用于旧版 Chromium v10/v11）
            ls_path = user_data / "Local State"
            if not ls_path.exists():
                continue
            try:
                ls_data = json.loads(ls_path.read_text(encoding="utf-8"))
                enc_key = base64.b64decode(ls_data["os_crypt"]["encrypted_key"])[5:]
                master_key = win32crypt.CryptUnprotectData(enc_key, None, None, None, 0)[1]
            except Exception:
                continue

            profiles = ["Default"] + [f"Profile {i}" for i in range(1, 6)]
            for prof in profiles:
                db_path = user_data / prof / "Network" / "Cookies"
                if not db_path.exists():
                    db_path = user_data / prof / "Cookies"
                if not db_path.exists():
                    continue

                tmp_db = None
                try:
                    with tempfile.NamedTemporaryFile(delete=False, suffix=".sqlite") as t:
                        tmp_db = Path(t.name)
                    try:
                        shutil.copy2(db_path, tmp_db)
                    except PermissionError:
                        # 正在被浏览器运行锁住
                        locked_browsers.append(b_name)
                        continue

                    conn = sqlite3.connect(tmp_db)
                    cursor = conn.cursor()
                    rows = cursor.execute(
                        "SELECT name, encrypted_value FROM cookies WHERE host_key LIKE '%zhihu.com%'"
                    ).fetchall()
                    conn.close()

                    cd = {}
                    for name, enc in rows:
                        try:
                            if enc[:3] in (b"v10", b"v11"):
                                nonce = enc[3:15]
                                ciphertext = enc[15:-16]
                                tag = enc[-16:]
                                cipher = AES.new(master_key, AES.MODE_GCM, nonce=nonce)
                                val = cipher.decrypt_and_verify(ciphertext, tag).decode("utf-8", "ignore")
                            else:
                                val = win32crypt.CryptUnprotectData(enc, None, None, None, 0)[1].decode("utf-8", "ignore")
                            if val:
                                cd[name] = val
                        except Exception:
                            pass

                    if "z_c0" in cd:
                        found_cookie = clean_cookie_str("; ".join(f"{k}={v}" for k, v in cd.items()))
                        found_source = f"{b_name} ({prof})"
                        break
                except Exception:
                    pass
                finally:
                    if tmp_db and tmp_db.exists():
                        try:
                            tmp_db.unlink()
                        except Exception:
                            pass
            if found_cookie:
                break

        if found_cookie:
            save_cookie_to_disk(found_cookie)
            return {"ok": True, "cookie": found_cookie, "source": found_source}

        if locked_browsers:
            uniq_locked = "/".join(sorted(set(locked_browsers)))
            return {
                "ok": False,
                "locked": True,
                "browser": uniq_locked,
                "error": f"{uniq_locked} 正在运行并锁定了数据文件。请点击「关闭浏览器并直读」即可一键提取。",
            }

        return {
            "ok": False,
            "locked": False,
            "error": "未在 Edge/Chrome 中找到知乎登录记录。请先在浏览器中登录知乎，或点击「手机扫码登录」。",
        }

    elif sys.platform == "darwin":
        try:
            import browser_cookie3
            for fn in ("chrome", "edge", "safari"):
                try:
                    jar = getattr(browser_cookie3, fn)(domain_name=".zhihu.com")
                    cd = {c.name: c.value for c in jar if "zhihu" in getattr(c, "domain", "")}
                    if "z_c0" in cd:
                        ck = clean_cookie_str("; ".join(f"{k}={v}" for k, v in cd.items()))
                        save_cookie_to_disk(ck)
                        return {"ok": True, "cookie": ck, "source": f"macOS {fn}"}
                except Exception:
                    pass
            return {"ok": False, "error": "未能从 Mac 浏览器读取到知乎登录态，请扫码或手动粘贴。"}
        except ImportError:
            return {"ok": False, "error": "缺少 browser-cookie3 组件。"}

    return {"ok": False, "error": "当前系统环境暂不支持自动解密，请使用扫码登录。"}


# --------------------------------------------------------------------------- #
# 2. 手机知乎扫码登录 (官方接口 + 轮询，完全基于标准库 urllib)
# --------------------------------------------------------------------------- #

def start_qr_login() -> Dict[str, Any]:
    """生成知乎官方扫码登录二维码及轮询会话。"""
    import http.cookiejar

    cj = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
    opener.addheaders = [
        ("User-Agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"),
        ("Referer", "https://www.zhihu.com/signin"),
        ("Origin", "https://www.zhihu.com"),
    ]

    try:
        req_udid = urllib.request.Request("https://www.zhihu.com/udid", data=b"", method="POST")
        try:
            with opener.open(req_udid, timeout=5) as _:
                pass
        except Exception:
            pass

        req_qr = urllib.request.Request("https://www.zhihu.com/api/v3/account/api/login/qrcode", data=b"", method="POST")
        with opener.open(req_qr, timeout=6) as resp:
            data = json.loads(resp.read().decode("utf-8"))

        token = data.get("token") or ""
        link = data.get("link") or f"https://www.zhihu.com/account/scan/login/{token}?/api/login/qrcode"
    except Exception as exc:
        return {"ok": False, "error": f"获取知乎扫码凭据失败：{exc}"}

    qr_b64 = ""
    if qrcode and Image:
        try:
            qr = qrcode.QRCode(version=1, box_size=8, border=2)
            qr.add_data(link)
            qr.make(fit=True)
            img = qr.make_image(fill_color="#18181b", back_color="white")
            buf = io.BytesIO()
            img.save(buf, format="PNG")
            qr_b64 = "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode("ascii")
        except Exception:
            pass

    with _QR_LOCK:
        _QR_SESSIONS[token] = {
            "opener": opener,
            "cookie_jar": cj,
            "created_at": time.time(),
            "expires_at": time.time() + 180,
            "link": link,
            "status": "waiting",
        }

    return {
        "ok": True,
        "token": token,
        "link": link,
        "qr_base64": qr_b64,
        "expires_in": 180,
    }


def poll_qr_login(token: str) -> Dict[str, Any]:
    """轮询知乎扫码状态。"""
    with _QR_LOCK:
        sess_info = _QR_SESSIONS.get(token)
    if not sess_info:
        return {"ok": False, "status": "expired", "error": "扫码会话已过期，请重新生成。"}

    if time.time() > sess_info["expires_at"]:
        with _QR_LOCK:
            _QR_SESSIONS.pop(token, None)
        return {"ok": False, "status": "expired", "error": "二维码已过期，请点击刷新。"}

    opener: urllib.request.OpenerDirector = sess_info["opener"]
    cj = sess_info["cookie_jar"]

    try:
        url = f"https://www.zhihu.com/api/v3/account/api/login/qrcode/{token}/scan_info"
        req = urllib.request.Request(url)
        with opener.open(req, timeout=5) as resp:
            jd = json.loads(resp.read().decode("utf-8"))

        cookie_dict = {c.name: c.value for c in cj}
        if "z_c0" in cookie_dict:
            ck_str = clean_cookie_str("; ".join(f"{k}={v}" for k, v in cookie_dict.items()))
            save_cookie_to_disk(ck_str)
            with _QR_LOCK:
                _QR_SESSIONS.pop(token, None)
            return {
                "ok": True,
                "status": "success",
                "cookie": ck_str,
                "message": "扫码登录成功！",
            }

        st = jd.get("status")
        if st == 1:
            return {
                "ok": True,
                "status": "scanned",
                "message": "📱 手机已扫码！请在手机知乎上点击「确认登录」…",
            }
        elif st == 2:
            with _QR_LOCK:
                _QR_SESSIONS.pop(token, None)
            return {"ok": False, "status": "expired", "error": "扫码已取消或已过期"}
    except Exception as exc:
        return {"ok": False, "status": "waiting", "error": str(exc)}

    return {"ok": True, "status": "waiting", "message": "请使用手机知乎 App 扫描二维码"}


# --------------------------------------------------------------------------- #
# 3. 从云端凭证柜同步
# --------------------------------------------------------------------------- #

def sync_cloud_credential(server_url: str = "https://zh.samuraiguan.cloud", key: str = "guanjun2026") -> Dict[str, Any]:
    url = f"{server_url.rstrip('/')}/api/qy/credential-latest?key={key}"
    req = urllib.request.Request(url, headers={"User-Agent": "QyAppClient/1.1"})
    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            if data.get("ok") and data.get("cookie"):
                ck = clean_cookie_str(data["cookie"])
                save_cookie_to_disk(ck)
                return {"ok": True, "cookie": ck, "note": data.get("note", "云端凭证同步成功")}
            return {"ok": False, "error": data.get("note") or "云端凭证柜暂无有效凭证"}
    except Exception as exc:
        return {"ok": False, "error": f"连接云端凭证柜失败：{exc}"}
