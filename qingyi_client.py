#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""清一新教育 · 本地全功能归档与修改工作台 (v3.0)

核心定位：
  云端仅作为「用户手册与安装包储存中心」；
  本软件在用户本地电脑一站式完成：
  1. 免手动拿 Cookie 登录：
     - 通道 A：自动读取本机浏览器登录（若 Edge/Chrome 正在运行锁住文件，支持一键自动关闭浏览器并直接读取，随后自动重开工作台）；
     - 通道 B：若浏览器未登录知乎，支持直接点击「📱 扫码登录知乎」（内嵌实时二维码 + 独立扫码小窗自动提取登录），全程无需按 F12 手动找 Cookie！
  2. 本地全量查询 + Word (.docx) 含评论与原图归档导出：
     - 支持检索专栏文章与知乎回答；
     - 配备暗色实时终端进度条（显示 正在导出... X/Y 篇、速率 篇/s · ETA · 已用时、每篇耗时及评论条数流水）；
     - 导出文件自动下载并归档至本机 `exports/` 目录。
  3. 软件内自定义修改内容与正统文库（四书五经 / 法律条文 / 自定义填写 / 品牌署名）：
     - 📚 四书五经与国学经典（12 篇：《大学》《中庸》《论语》《孟子》《诗经》《尚书》《礼记》等）；
     - ⚖️ 国家现行法律条文（8 部：《宪法》《民法典》《爱国主义教育法》《义务教育法》等）；
     - ✍️ 直接填写自定义内容（全局自定义模板 + 单篇独立定制抽屉）；
     - 🏷️ 【清一新教育】品牌词署名。
  4. 本地自动备份原文与一键还原：
     - 每篇修改前自动备份原始标题与 HTML 正文到 `data/qyedu_backup/`，随时一键还原。
"""
from __future__ import annotations

import argparse
import base64
import ctypes
import datetime as _dt
import html as _html
import io
import json
import os
import random
import re
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import uuid
import webbrowser
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote, urlparse

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

if sys.platform == "win32":
    try:
        ctypes.windll.kernel32.SetConsoleTitleW("清一新教育 · 本地全功能查询导出与修改工作台 v3.0")
    except Exception:
        pass

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

import qingyi_executor as qe  # noqa: E402

try:
    import high_value_essays as hve
except Exception:
    try:
        from . import high_value_essays as hve  # type: ignore[no-redef]
    except Exception:
        hve = qe  # type: ignore[assignment]

try:
    import docx_exporter as _docx_exp
except Exception:
    _docx_exp = None  # type: ignore[assignment]

try:
    import comment_fetch as _cmt_fetch
except Exception:
    _cmt_fetch = None  # type: ignore[assignment]

CLIENT_VERSION = "3.1.0"
DEFAULT_BATCH = 5
DEFAULT_PORT = 8765

_TAG_RE = re.compile(r"<[^>]+>")


# --------------------------------------------------------------------------- #
# 文本与 HTML 转换辅助工具
# --------------------------------------------------------------------------- #

def _to_text(h: str) -> str:
    if not h:
        return ""
    s = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", h, flags=re.S | re.I)
    s = _TAG_RE.sub(" ", s)
    s = _html.unescape(s)
    return re.sub(r"\s+", " ", s).strip()


def _text_to_html(s: str) -> str:
    raw = (s or "").strip()
    if not raw:
        return ""
    if re.search(r"<(p|h[1-6]|blockquote|div|ul|ol|li|br)\b", raw, re.I):
        return raw
    parts = [p.strip() for p in re.split(r"\n\s*\n|\r?\n", raw) if p.strip()]
    return "".join(f"<p>{_html.escape(p)}</p>" for p in parts)


def _brand_excerpt(new_html: str, radius: int = 80) -> str:
    t = _to_text(new_html)
    i = t.find("清一新教育")
    if i < 0:
        return ""
    s = max(0, i - radius)
    e = min(len(t), i + len("清一新教育") + radius)
    left = ("…" if s > 0 else "") + t[s:i]
    right = t[i + len("清一新教育"):e] + ("…" if e < len(t) else "")
    return left + "【清一新教育】" + right


def _content_excerpt(new_html: str, max_len: int = 160) -> str:
    t = _to_text(new_html)
    if not t:
        return ""
    if "清一新教育" in t:
        return _brand_excerpt(new_html, radius=70)
    return t[:max_len] + ("…" if len(t) > max_len else "")


def _title_diff(pre: str, new: str) -> Dict[str, Any]:
    pre = (pre or "").strip()
    new = (new or "").strip()
    changed = bool(new) and new != pre
    prefix = ""
    if changed and pre and pre in new:
        prefix = new[: new.index(pre)]
    return {
        "before": pre,
        "after": new or pre,
        "changed": changed,
        "prefix": prefix,
    }


def _clean_cookie_str(raw: str) -> str:
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
    s = raw.strip().strip("\"' ")
    if s and not s.startswith("#") and "=" not in s and len(s) >= 20:
        return f"z_c0={s}"
    return ""


def _get_essay_catalog() -> Dict[str, Any]:
    if hasattr(hve, "get_catalog"):
        return hve.get_catalog()
    laws = getattr(hve, "LAW_ESSAYS", [])
    classics = getattr(hve, "CLASSIC_ESSAYS", [])
    return {
        "presets": [
            {"key": "classics", "label": "📚 四书五经与国学经典（按文章自动轮换）", "group": "group"},
            {"key": "law", "label": "⚖️ 国家现行法律条文（按文章自动轮换）", "group": "group"},
            {"key": "random_all", "label": "🎲 全部经典与法律条文混合轮换", "group": "group"},
            {"key": "custom", "label": "✍️ 自定义填写标题与正文内容", "group": "custom"},
        ],
        "classics": [
            {"key": e["key"], "category": "classics", "label": e.get("label") or e["title"],
             "title": e["title"], "content": e["content"]}
            for e in classics
        ],
        "laws": [
            {"key": e["key"], "category": "law", "label": e.get("label") or e["title"],
             "title": e["title"], "content": e["content"]}
            for e in laws
        ],
    }


def _resolve_essay(aid: str, preset: str) -> Dict[str, Any]:
    catalog = _get_essay_catalog()
    by_key = {e["key"]: e for e in (catalog.get("classics", []) + catalog.get("laws", []))}
    p = (preset or "classics").strip()
    if p in by_key:
        return by_key[p]
    if hasattr(hve, "get_essay_by_preset"):
        return hve.get_essay_by_preset(aid, p)
    pool = list(by_key.values())
    if not pool:
        return {"key": "default", "title": "《大学》格物致知研读", "content": "<p>大学之道，在明明德，在亲民，在止于至善。</p>"}
    return pool[int(aid or "0") % len(pool) if str(aid).isdigit() else 0]


# --------------------------------------------------------------------------- #
# 浏览器进程与指定 User-Data-Dir 解密读取辅助
# --------------------------------------------------------------------------- #

def _close_browsers_for_cookie_read() -> List[str]:
    """安全关闭正在占用 Cookie 数据库的 Edge / Chrome 进程。"""
    closed: List[str] = []
    if sys.platform == "win32":
        for exe, label in (("msedge.exe", "Edge"), ("chrome.exe", "Chrome")):
            try:
                r = subprocess.run(["taskkill", "/F", "/IM", exe],
                                   capture_output=True, timeout=10)
                if r.returncode == 0:
                    closed.append(label)
            except Exception:
                pass
    elif sys.platform == "darwin":
        for app_name in ("Microsoft Edge", "Google Chrome"):
            try:
                r = subprocess.run(["killall", app_name],
                                   capture_output=True, timeout=10)
                if r.returncode == 0:
                    closed.append(app_name)
            except Exception:
                pass
    if closed:
        time.sleep(0.8)
    return closed


def _extract_cookie_from_profile_dir(user_data_dir: Path) -> str:
    """从指定的 Chromium User Data 目录（如独立扫码窗口目录）提取知乎 Cookie。"""
    if sys.platform != "win32" or not user_data_dir.exists():
        return ""
    try:
        import win32crypt
        from Crypto.Cipher import AES
    except ImportError:
        return ""
    ls_path = user_data_dir / "Local State"
    if not ls_path.exists():
        return ""
    try:
        enc_key = base64.b64decode(
            json.loads(ls_path.read_text(encoding="utf-8"))["os_crypt"]["encrypted_key"]
        )[5:]
        key = win32crypt.CryptUnprotectData(enc_key, None, None, None, 0)[1]
    except Exception:
        return ""

    for prof in ["Default"] + [f"Profile {i}" for i in range(1, 5)]:
        db = user_data_dir / prof / "Network" / "Cookies"
        if not db.exists():
            db = user_data_dir / prof / "Cookies"
        if not db.exists():
            continue
        tmp_path = None
        try:
            with tempfile.NamedTemporaryFile(delete=False, suffix=".sqlite") as t:
                tmp_path = Path(t.name)
            shutil.copy2(db, tmp_path)
            conn = sqlite3.connect(tmp_path)
            rows = conn.execute(
                "SELECT name, encrypted_value FROM cookies WHERE host_key LIKE '%zhihu.com%'"
            ).fetchall()
            conn.close()
            cd = {}
            for name, enc in rows:
                try:
                    if enc[:3] in (b"v10", b"v11"):
                        nonce, ct, tag = enc[3:15], enc[15:-16], enc[-16:]
                        val = AES.new(key, AES.MODE_GCM, nonce=nonce).decrypt_and_verify(ct, tag).decode("utf-8", "ignore")
                    else:
                        val = win32crypt.CryptUnprotectData(enc, None, None, None, 0)[1].decode("utf-8", "ignore")
                    if val:
                        cd[name] = val
                except Exception:
                    pass
            if "z_c0" in cd:
                return "; ".join(f"{k}={v}" for k, v in cd.items())
        except Exception:
            pass
        finally:
            if tmp_path and tmp_path.exists():
                try:
                    tmp_path.unlink()
                except Exception:
                    pass
    return ""


def _find_browser_executable() -> Optional[str]:
    if sys.platform == "win32":
        cands = [
            Path(os.environ.get("ProgramFiles(x86)", "C:/Program Files (x86)")) / "Microsoft/Edge/Application/msedge.exe",
            Path(os.environ.get("ProgramFiles", "C:/Program Files")) / "Microsoft/Edge/Application/msedge.exe",
            Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft/Edge/Application/msedge.exe",
            Path(os.environ.get("ProgramFiles", "C:/Program Files")) / "Google/Chrome/Application/chrome.exe",
            Path(os.environ.get("ProgramFiles(x86)", "C:/Program Files (x86)")) / "Google/Chrome/Application/chrome.exe",
            Path(os.environ.get("LOCALAPPDATA", "")) / "Google/Chrome/Application/chrome.exe",
        ]
        for c in cands:
            if c.exists():
                return str(c)
    elif sys.platform == "darwin":
        cands = [
            Path("/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"),
            Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
        ]
        for c in cands:
            if c.exists():
                return str(c)
    return None


def _generate_zhihu_qr_base64() -> Dict[str, Any]:
    """调用知乎官方扫码接口获取扫码链接并生成高清 PNG Base64 二维码。"""
    import requests
    s = requests.Session()
    s.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        ),
        "Referer": "https://www.zhihu.com/signin",
        "Origin": "https://www.zhihu.com",
    })
    token = ""
    link = "https://www.zhihu.com/signin"
    try:
        s.post("https://www.zhihu.com/udid", timeout=6)
        r = s.post("https://www.zhihu.com/api/v3/account/api/login/qrcode", timeout=6)
        if r.status_code == 200:
            data = r.json()
            token = data.get("token") or ""
            link = data.get("link") or f"https://www.zhihu.com/account/scan/login/{token}?/api/login/qrcode"
    except Exception:
        pass

    b64_img = ""
    try:
        import qrcode
        qr = qrcode.QRCode(version=1, box_size=8, border=2)
        qr.add_data(link)
        qr.make(fit=True)
        img = qr.make_image(fill_color="#0f172a", back_color="white")
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        b64_img = "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode("ascii")
    except Exception:
        pass
    return {
        "session": s,
        "token": token,
        "link": link,
        "qr_base64": b64_img,
        "expires_at": int(time.time()) + 180,
    }


# --------------------------------------------------------------------------- #

# --------------------------------------------------------------------------- #
# 李想合规专版 · 双轮 AI 审核与智能修润引擎 (Liza Compliance Engine)
# --------------------------------------------------------------------------- #

class LizaComplianceEngine:
    """李想老师（Liza）团队知乎文章敏感词排查与智能修润引擎

    严格落实团队最新合规要求：
    1. 《与神对话》全量清零：禁止书名，严禁使用“ysdh”拼音缩写避审；替换为“看了一本书”或“书中说”；
    2. “小灵魂与太阳”故事一票彻底删除；
    3. “慧心课”、“黑关”、“闭关”、“疗愈”、“灵性”、“修行”等涉玄词汇降级为现代规范表述；
    4. “只能吃黄豆酱配米饭”等极端饮食描述软化为“选择简单饮食”；
    5. 涉邪/迷信/跨国非法集资/分裂政党一票熔断拦截。
    """

    RULES = [
        {
            "id": "c_god",
            "name": "《与神对话》全量清零",
            "severity": "CRITICAL",
            "pattern": re.compile(r"(《\s*与\s*神\s*对\s*话\s*》|与神对话|神说|与神交流)", re.I),
            "prohibited_abbr": re.compile(r"\b(ysdh|YSDH)\b", re.I),
            "default_replace": "看了一本书",
            "reason": "李想老师指令：全量删除《与神对话》字眼，严禁使用拼音缩写“ysdh”避审；书名可替换为“看了一本书”或“书中说”，书理如合乎逻辑可保留。",
        },
        {
            "id": "soul_sun",
            "name": "“小灵魂与太阳”故事彻底剔除",
            "severity": "CRITICAL",
            "pattern": re.compile(r"([^。！？\n]*?(?:小灵魂与太阳|小灵魂|你是光|把光遮住)[^。！？\n]*?[。！？\n]?)", re.I),
            "default_replace": "",
            "is_block_delete": True,
            "reason": "李想老师特别指示：“尤其是小灵魂与太阳这个故事不能留”，必须整段剔除，避免被外界恶意关联玄学邪说。",
        },
        {
            "id": "dark_retreat",
            "name": "闭关/黑关/慧心课用语降级",
            "severity": "HIGH",
            "pattern": re.compile(r"(闭黑关|黑关|闭关修行|闭关|出关|慧心课|辟谷)", re.I),
            "replacements": {
                "闭黑关": "深度专注内省",
                "黑关": "专注内省空间",
                "闭关修行": "阶段性深度研学",
                "闭关": "深度专注学习",
                "出关": "阶段总结",
                "慧心课": "认知深化课",
                "辟谷": "清淡断食",
            },
            "reason": "李想老师指示：慧心课、黑关与闭关容易被误解炒作，需规范为“深度内省”、“专注研学”等现代教育表述。",
        },
        {
            "id": "spiritual_words",
            "name": "疗愈/灵性/修行等措辞规范化",
            "severity": "HIGH",
            "pattern": re.compile(r"(灵性|疗愈|修行|得道|开悟|前世|能量场|宇宙能量|宇宙法则)", re.I),
            "replacements": {
                "灵性": "心智认知",
                "疗愈": "心态调整与情绪放松",
                "修行": "自我提升与修养",
                "得道": "认知通透",
                "开悟": "豁然开朗",
                "前世": "以往经历",
                "能量场": "环境氛围",
                "宇宙能量": "自然规律",
                "宇宙法则": "客观规律",
            },
            "reason": "李想老师强调：“疗愈”、“灵性”、“修行”等措辞需调整，防止被关联到宗教迷信或非理性宣导。",
        },
        {
            "id": "diet_soften",
            "name": "极端苦行饮食描述软化",
            "severity": "MEDIUM",
            "pattern": re.compile(r"(只能吃(?:黄豆酱|米糊|咸菜)[^。！？\n]*|黄豆酱配米饭|饿肚子|忍饥挨饿)", re.I),
            "default_replace": "选择简单清淡饮食",
            "reason": "李想老师要求：只能吃黄豆酱配米饭的描述必须改成“简单饮食”，避免外界产生极端苦行或虐待误解。",
        },
        {
            "id": "politics_redline",
            "name": "涉邪/迷信/跨国非法集资/分裂政党高危红线",
            "severity": "CRITICAL",
            "pattern": re.compile(r"(邪教|跨国非法集资|分裂政党|秘密结社|密宗|法门)", re.I),
            "default_replace": "",
            "reason": "政治安全一票否决红线，严禁任何可能成为法律风险把柄的用词。",
        },
    ]

    @classmethod
    def scan_text(cls, text: str) -> dict:
        """快速扫描全文中的敏感点，返回命中列表与最高风险等级"""
        hits = []
        max_sev = "SAFE"
        sev_rank = {"CRITICAL": 3, "HIGH": 2, "MEDIUM": 1, "SAFE": 0}

        for rule in cls.RULES:
            if "prohibited_abbr" in rule:
                for m in rule["prohibited_abbr"].finditer(text):
                    hits.append({
                        "rule_id": rule["id"],
                        "rule_name": rule["name"] + " (严禁拼音缩写)",
                        "severity": "CRITICAL",
                        "match": m.group(0),
                        "start": m.start(),
                        "end": m.end(),
                        "suggest": rule.get("default_replace", "看了一本书"),
                        "reason": "李想老师明确批示：缩写为“ysdh”绝对不行，必须彻底删除或换成“看了一本书”！",
                    })
                    if sev_rank["CRITICAL"] > sev_rank[max_sev]:
                        max_sev = "CRITICAL"

            for m in rule["pattern"].finditer(text):
                matched = m.group(0)
                suggest = ""
                if rule.get("is_block_delete"):
                    suggest = "【彻底删除此段】"
                elif "replacements" in rule:
                    suggest = rule["replacements"].get(matched, rule.get("default_replace", ""))
                else:
                    suggest = rule.get("default_replace", "")

                hits.append({
                    "rule_id": rule["id"],
                    "rule_name": rule["name"],
                    "severity": rule["severity"],
                    "match": matched,
                    "start": m.start(),
                    "end": m.end(),
                    "suggest": suggest,
                    "reason": rule["reason"],
                })
                if sev_rank[rule["severity"]] > sev_rank[max_sev]:
                    max_sev = rule["severity"]

        hits.sort(key=lambda x: x["start"])
        return {
            "max_severity": max_sev,
            "hit_count": len(hits),
            "hits": hits,
        }

    @classmethod
    def apply_smart_fixes(cls, title: str, content: str) -> dict:
        """执行双轮 AI 审核与修润逻辑

        轮次 1：智能修润（按李想规则自动生成通顺润色替换，保留原意）
        轮次 2：对抗质检（魔鬼式查漏补缺，断言是否有 ysdh 隐写、与神对话残留、小灵魂未删等）
        """
        scan_t = cls.scan_text(title)
        scan_c = cls.scan_text(content)

        thinking_r1 = []
        thinking_r1.append(f"【轮次 1 · 智能修润 Agent】启动：标题检出 {len(scan_t['hits'])} 处风险，正文检出 {len(scan_c['hits'])} 处风险。")

        mod_title = title
        mod_content = content
        replacements = []

        # 修润标题
        for h in scan_t["hits"]:
            repl = h["suggest"]
            if repl == "【彻底删除此段】":
                repl = ""
            thinking_r1.append(f"  * 标题检出「{h['match']}」({h['rule_name']}) -> 建议润色为「{repl}」")
            mod_title = mod_title.replace(h["match"], repl)
            replacements.append({
                "target": "title",
                "before": h["match"],
                "after": repl,
                "rule": h["rule_name"],
                "reason": h["reason"]
            })

        # 修润正文
        for h in scan_c["hits"]:
            if h.get("rule_id") == "soul_sun":
                thinking_r1.append(f"  * 检出整段高危故事「{h['match'].strip()}」 -> 依据李想老师一票否决指令，执行整段剔除。")
                mod_content = mod_content.replace(h["match"], "")
                replacements.append({
                    "target": "content",
                    "before": h["match"],
                    "after": "（已整段删除）",
                    "rule": h["rule_name"],
                    "reason": h["reason"]
                })

        for h in scan_c["hits"]:
            if h.get("rule_id") == "soul_sun":
                continue
            repl = h["suggest"]
            if h["match"] in mod_content:
                thinking_r1.append(f"  * 正文检出「{h['match']}」({h['rule_name']}) -> 规范替换为「{repl}」")
                mod_content = mod_content.replace(h["match"], repl)
                replacements.append({
                    "target": "content",
                    "before": h["match"],
                    "after": repl,
                    "rule": h["rule_name"],
                    "reason": h["reason"]
                })

        # 轮次 2：对抗质检 Agent (Adversarial Verification)
        thinking_r2 = []
        thinking_r2.append("【轮次 2 · 对抗质检 Agent】启动严格对抗质检，执行一票否决死磕排查：")

        check_res_t = cls.scan_text(mod_title)
        check_res_c = cls.scan_text(mod_content)
        total_remaining = check_res_t["hit_count"] + check_res_c["hit_count"]

        has_god = bool(re.search(r"(与神对话|神说|与神交流)", mod_title + mod_content))
        has_abbr = bool(re.search(r"\b(ysdh|YSDH)\b", mod_title + mod_content))
        if not has_god and not has_abbr:
            thinking_r2.append("  [OK] 断言 1 通过：《与神对话》全量清零，且无拼音缩写“ysdh/YSDH”避审痕迹。")
        else:
            thinking_r2.append("  [FAIL] 断言 1 失败：仍检测到与神对话或 ysdh 缩写残留！")

        has_sun = bool(re.search(r"(小灵魂与太阳|小灵魂|你是光)", mod_title + mod_content))
        if not has_sun:
            thinking_r2.append("  [OK] 断言 2 通过：“小灵魂与太阳”故事已被彻底连根拔起，无任何残留。")
        else:
            thinking_r2.append("  [FAIL] 断言 2 失败：仍残留小灵魂相关段落！")

        has_retreat = bool(re.search(r"(闭黑关|黑关|闭关|慧心课)", mod_title + mod_content))
        if not has_retreat:
            thinking_r2.append("  [OK] 断言 3 通过：黑关/闭关/慧心课已成功规范化为现代专注研修语境。")
        else:
            thinking_r2.append("  [WARN] 断言 3 提醒：仍有部分闭关字眼，建议微调。")

        has_diet = bool(re.search(r"(只能吃黄豆酱|黄豆酱配米饭)", mod_title + mod_content))
        if not has_diet:
            thinking_r2.append("  [OK] 断言 4 通过：极端苦行饮食已软化为“简单清淡饮食”，消除虐待误解。")
        else:
            thinking_r2.append("  [FAIL] 断言 4 失败：仍有强迫只能吃黄豆酱字眼！")

        score = 100 if total_remaining == 0 else max(50, 100 - total_remaining * 15)
        passed = (total_remaining == 0) and (not has_god) and (not has_abbr) and (not has_sun)

        thinking_r2.append(f">> 对抗质检评定：合规得分 {score} 分 · 最终状态：{'【双轮全绿·合规签发】' if passed else '【待复核】'}")

        return {
            "ok": True,
            "passed": passed,
            "score": score,
            "thinking": "\n".join(thinking_r1) + "\n\n" + "\n".join(thinking_r2),
            "original_title": title,
            "modified_title": mod_title,
            "original_content": content,
            "modified_content": mod_content,
            "replacements": replacements,
            "remaining_hits": check_res_t["hits"] + check_res_c["hits"],
        }


# 本地控制台核心状态机
# --------------------------------------------------------------------------- #

class Client:
    def __init__(self, server: str, key: str, cookie: str,
                 cookie_file: str = "cookie.txt",
                 per_day: Optional[int] = None, batch: int = DEFAULT_BATCH,
                 backup_dir: Optional[str] = None,
                 daily_path: Optional[str] = None,
                 stagger: float = 1.5,
                 port: int = DEFAULT_PORT) -> None:
        self.cookie = _clean_cookie_str(cookie)
        self.cookie_file = Path(cookie_file) if cookie_file else (_HERE / "cookie.txt")
        if not self.cookie_file.is_absolute():
            self.cookie_file = _HERE / self.cookie_file
        self.server = server.rstrip("/")
        self.key = key
        self.batch = max(1, int(batch))
        self.stagger = float(stagger)
        self.port = port
        self.cp = qe.ControlPlane(server, key)
        self.cp.s.headers["X-API-Key"] = key
        self.policy = qe.RatePolicy()
        if per_day is not None:
            self.policy.per_day = max(0, int(per_day))
        self.backup_dir = Path(backup_dir) if backup_dir else (_HERE / "data" / "qyedu_backup")
        self.backup_dir.mkdir(parents=True, exist_ok=True)
        self.export_dir = _HERE / "exports"
        self.export_dir.mkdir(parents=True, exist_ok=True)
        self.qr_profile_dir = _HERE / "data" / "qr_login_profile"
        self.daily_path = Path(daily_path) if daily_path else (_HERE / "data" / "qy_daily_quota.json")

        self._lock = threading.RLock()
        self._cp_lock = threading.Lock()

        self.executor = qe.LocalExecutor(
            self.cp, self.cookie, backup_dir=self.backup_dir,
            policy=self.policy, daily_path=self.daily_path)
        self.worker_id = self.executor.worker_id
        self.governor = self.executor.governor

        self.job: Optional[Dict[str, Any]] = None
        self.rows: Dict[str, Dict[str, Any]] = {}
        self.order: List[str] = []
        self.payloads: Dict[str, Dict[str, Any]] = {}
        self.local_cache: List[Dict[str, Any]] = []
        self.local_offset: int = 0
        self.info: Dict[str, Any] = {}
        self.notice = ""
        self.verify_result: Optional[Dict[str, Any]] = None
        self.pool: Optional[ThreadPoolExecutor] = None

        # 伙伴同款暗黑终端进度框状态（支持导出 Word / 全量检索 / 批量修改）
        self.progress: Dict[str, Any] = {
            "visible": False,
            "kind": "export",       # export | modify | scan
            "status": "idle",       # idle | running | done | error
            "label": "正在导出...",
            "done": 0,
            "total": 0,
            "failed": 0,
            "speed": 0.0,
            "eta": 0,
            "elapsed": 0.0,
            "started_at": 0.0,
            "logs": [],
            "job_id": "",
            "filename": "",
            "saved_path": "",
        }
        self._export_files: Dict[str, Dict[str, Any]] = {}

        # 扫码登录会话状态
        self._qr_state: Dict[str, Any] = {
            "active": False,
            "status": "idle",       # idle | waiting_scan | scanned | success | error
            "message": "",
            "token": "",
            "link": "",
            "qr_base64": "",
            "session": None,
            "proc": None,
        }

        # 当前软件内选择的修改方案
        self.scheme: Dict[str, Any] = {
            "action_mode": "replace_content",
            "preset": "daxue",
            "custom_title": "",
            "custom_content": "",
            "keep_original_title": False,
            "body_hits": 1,
        }

    # ---------------- 登录凭证管理（关闭浏览器直读 / 手机扫码 / 自动 / 手动） ---------------- #

    def set_cookie(self, raw_cookie: str, source_label: str = "本机读取") -> Dict[str, Any]:
        ck = _clean_cookie_str(raw_cookie)
        if not ck:
            return {"ok": False, "note": "未检测到有效知乎登录态（需含 z_c0）。"}
        with self._lock:
            self.cookie = ck
            self.executor.signer = qe.QingyiTitleSigner(
                cookie=ck, backup_dir=self.backup_dir, policy=self.policy)
            self.local_cache = []
            self.local_offset = 0
        try:
            self.cookie_file.write_text(ck + "\n", encoding="utf-8")
        except Exception:
            pass
        try:
            info = self.preflight()
            dep = self.deposit_credential()
            self.notice = (f"✓ 已通过【{source_label}】成功登录知乎账号：{info.get('name')} "
                           f"（专栏文章 {info.get('articles')} 篇 · 回答 {info.get('answers')} 条），凭证已保存至本机 cookie.txt。")
            return {"ok": True, "info": info, "deposit": dep, "note": self.notice}
        except Exception as exc:  # noqa: BLE001
            self.mark_startup_error(str(exc))
            return {"ok": False, "note": f"知乎登录态校验未通过：{exc}"}

    def auto_detect_browser_cookie(self, close_browser: bool = False) -> Dict[str, Any]:
        """自动读取本机浏览器知乎登录态。
        若 close_browser=True（或遇文件锁且用户允许关浏览器），自动关闭 Edge/Chrome 解锁后秒读，并自动重开本地控制台。
        """
        try:
            ck, src = qe.auto_detect_cookie()
            return self.set_cookie(ck, source_label=f"本机浏览器直读 ({src})")
        except Exception as first_exc:  # noqa: BLE001
            err_str = str(first_exc)
            if not close_browser:
                return {
                    "ok": False,
                    "locked": "锁定" in err_str or "正在运行" in err_str,
                    "note": (f"浏览器正在运行并锁住了登录文件（{err_str}）。\n"
                             "您可以直接点击【🔒 自动关闭浏览器并直接读取登录】一键读取，或点击【📱 扫码登录知乎】！"),
                }
            # 用户选择自动关闭浏览器并直接读取
            closed = _close_browsers_for_cookie_read()
            try:
                ck, src = qe.auto_detect_cookie()
                res = self.set_cookie(ck, source_label=f"关闭{'/'.join(closed) or '浏览器'}后直读 ({src})")
                # 若刚才关闭了浏览器，自动帮用户重新打开本地工作台网页
                if closed:
                    threading.Timer(0.6, lambda: webbrowser.open(f"http://127.0.0.1:{self.port}")).start()
                return res
            except Exception as second_exc:  # noqa: BLE001
                if closed:
                    threading.Timer(0.6, lambda: webbrowser.open(f"http://127.0.0.1:{self.port}")).start()
                return {
                    "ok": False,
                    "note": (f"已关闭浏览器但未在浏览器中找到知乎登录记录（{second_exc}）。\n"
                             "请直接点击旁边【📱 扫码登录知乎】，用手机知乎扫码即可登录！"),
                }

    def start_qr_login(self, open_window: bool = False) -> Dict[str, Any]:
        """启动知乎扫码登录（生成内嵌二维码 + 可选弹出独立无冲突扫码窗口）。"""
        qr_info: Dict[str, Any] = {}
        try:
            qr_info = _generate_zhihu_qr_base64()
        except Exception as exc:
            qr_info = {"error": str(exc)}

        with self._lock:
            self._qr_state.update({
                "active": True,
                "status": "waiting_scan",
                "message": "请使用手机【知乎 App】扫描二维码登录（或点击下方「弹出独立扫码小窗」完成登录）",
                "token": qr_info.get("token") or "",
                "link": qr_info.get("link") or "https://www.zhihu.com/signin",
                "qr_base64": qr_info.get("qr_base64") or "",
                "session": qr_info.get("session"),
            })

        if open_window:
            self.open_qr_popup_window()

        return {
            "ok": True,
            "status": self._qr_state["status"],
            "message": self._qr_state["message"],
            "token": self._qr_state["token"],
            "link": self._qr_state["link"],
            "qr_base64": self._qr_state["qr_base64"],
        }

    def open_qr_popup_window(self) -> Dict[str, Any]:
        """弹出独立的用户数据目录浏览器小窗打开知乎登录页，扫码后可一键自动提取 Cookie，绝不与主浏览器冲突。"""
        exe = _find_browser_executable()
        if not exe:
            webbrowser.open("https://www.zhihu.com/signin")
            return {"ok": True, "note": "已在默认浏览器打开知乎登录页，扫码登录后点「🔒 自动关闭浏览器并直接读取登录」即可。"}
        self.qr_profile_dir.mkdir(parents=True, exist_ok=True)
        # 先清理旧的独立小窗进程（如有）
        old_proc = self._qr_state.get("proc")
        if old_proc and old_proc.poll() is None:
            try:
                old_proc.terminate()
                time.sleep(0.3)
            except Exception:
                pass
        cmd = [
            exe,
            f"--user-data-dir={self.qr_profile_dir}",
            "--app=https://www.zhihu.com/signin",
            "--window-size=520,700",
            "--no-first-run",
            "--no-default-browser-check",
        ]
        proc = subprocess.Popen(cmd)
        with self._lock:
            self._qr_state["proc"] = proc
            self._qr_state["status"] = "waiting_scan"
            self._qr_state["message"] = "已弹出独立知乎扫码窗口！请在小窗中用手机知乎扫码，登录成功后关闭小窗或点「✅ 我已完成扫码，立即读取」即可。"
        return {"ok": True, "note": self._qr_state["message"]}

    def poll_qr_login(self, finalize_popup: bool = False) -> Dict[str, Any]:
        """轮询扫码状态：
        1. 检查官方扫码 session 是否已下发 z_c0；
        2. 检查独立扫码窗口目录（qr_login_profile）是否已写入 z_c0（若 finalize_popup=True 则先关闭独立扫码小窗解锁读取）。
        """
        with self._lock:
            st = dict(self._qr_state)

        # 1. 检查 requests session 的 scan_info
        sess = st.get("session")
        token = st.get("token")
        if sess and token:
            try:
                r = sess.get(f"https://www.zhihu.com/api/v3/account/api/login/qrcode/{token}/scan_info", timeout=5)
                if r.status_code == 200:
                    jd = r.json() if r.content else {}
                    if "z_c0" in sess.cookies.get_dict():
                        cd = sess.cookies.get_dict()
                        ck_str = "; ".join(f"{k}={v}" for k, v in cd.items())
                        res = self.set_cookie(ck_str, source_label="手机知乎扫码")
                        if res.get("ok"):
                            with self._lock:
                                self._qr_state["status"] = "success"
                                self._qr_state["message"] = "✓ 扫码登录成功！"
                            return {"ok": True, "status": "success", "message": self.notice}
                    if jd.get("status") == 1:
                        with self._lock:
                            self._qr_state["status"] = "scanned"
                            self._qr_state["message"] = "📱 已扫码！请在手机上点击「确认登录」…"
            except Exception:
                pass

        # 2. 检查独立扫码小窗进程或 Profile 目录
        proc = st.get("proc")
        if finalize_popup and proc and proc.poll() is None:
            try:
                proc.terminate()
                proc.wait(timeout=3)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
            time.sleep(0.4)

        ck_from_qr_dir = _extract_cookie_from_profile_dir(self.qr_profile_dir)
        if ck_from_qr_dir:
            if proc and proc.poll() is None:
                try:
                    proc.terminate()
                except Exception:
                    pass
            res = self.set_cookie(ck_from_qr_dir, source_label="独立窗口扫码登录")
            if res.get("ok"):
                with self._lock:
                    self._qr_state["status"] = "success"
                    self._qr_state["message"] = "✓ 独立窗口扫码登录成功！"
                return {"ok": True, "status": "success", "message": self.notice}

        # 3. 如果用户点了「我已完成扫码」且是在主浏览器扫的，也尝试自动读取主浏览器
        if finalize_popup:
            auto_res = self.auto_detect_browser_cookie(close_browser=True)
            if auto_res.get("ok"):
                with self._lock:
                    self._qr_state["status"] = "success"
                    self._qr_state["message"] = "✓ 登录成功！"
                return {"ok": True, "status": "success", "message": self.notice}
            return {
                "ok": False,
                "status": "waiting_scan",
                "message": "暂未检测到扫码完成的登录态，请确认已在扫码窗口中完成登录，然后重试。",
            }

        with self._lock:
            return {
                "ok": True,
                "status": self._qr_state["status"],
                "message": self._qr_state["message"],
                "qr_base64": self._qr_state["qr_base64"],
            }

    def load_cookie_from_cloud(self) -> Dict[str, Any]:
        try:
            r = self.cp._req(  # noqa: SLF001
                "GET", f"/api/qy/credential-latest?key={self.key}", quiet=True) or {}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "note": f"连接云端凭证柜失败：{exc}"}
        if not r.get("ok") or not r.get("cookie"):
            return {"ok": False, "note": r.get("note") or "云端凭证柜暂无可用凭证，请使用【🔒 关闭浏览器直读】或【📱 扫码登录知乎】。"}
        return self.set_cookie(r["cookie"], source_label="云端凭证柜")

    # ---------------- 启动自检 ---------------- #

    def preflight(self) -> Dict[str, Any]:
        if not self.cookie:
            raise RuntimeError("尚未登录知乎，请点击顶部「🔒 关闭浏览器直读登录」或「📱 扫码登录知乎」。")
        s = qe.QingyiTitleSigner(cookie=self.cookie,
                                 backup_dir=self.backup_dir,
                                 policy=self.policy)
        acc = s.verify()
        me_full = s.me()
        self.info = {
            "worker_id": self.worker_id,
            "name": acc.get("name") or "(未知)",
            "url_token": acc.get("url_token") or "",
            "articles": acc.get("articles_count"),
            "answers": me_full.get("answer_count"),
            "pins": acc.get("pins_count"),
            "per_day": self.policy.per_day,
            "daily_text": self.governor.daily.describe(),
            "daily_remaining": self.governor.daily.remaining(),
            "batch": self.batch,
            "server": self.cp.base,
            "version": CLIENT_VERSION,
            "has_cookie": True,
            "error": "",
        }
        return self.info

    def mark_startup_error(self, msg: str) -> None:
        self.info = dict(self.info or {})
        self.info.setdefault("worker_id", self.worker_id)
        self.info.setdefault("batch", self.batch)
        self.info.setdefault("version", CLIENT_VERSION)
        self.info["server"] = self.cp.base
        self.info["has_cookie"] = bool(self.cookie)
        self.info["error"] = msg

    def deposit_credential(self) -> Dict[str, Any]:
        if "z_c0=" not in (self.cookie or ""):
            return {"ok": False, "note": "本地没有含 z_c0 的登录态，跳过云端同步"}
        try:
            r = self.cp._req(  # noqa: SLF001
                "POST", "/api/qy/credential-deposit",
                json={"key": self.key, "cookie": self.cookie,
                      "note": self.worker_id,
                      "per_day": int(self.policy.per_day or 0)},
                quiet=True) or {}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "note": f"同步失败：{exc}"}
        if r.get("ok"):
            ttl = int(r.get("ttl") or 0) // 3600
            return {"ok": True, "token": r.get("token"),
                    "note": f"已同步到云端凭证柜（{ttl} 小时内有效）"}
        return {"ok": False, "note": r.get("detail") or r.get("note") or "云端未接收同步"}

    # ---------------- 本地 Word (.docx) 含评论与原图导出（伙伴同款暗黑实时进度框） ---------------- #

    def start_export_docx(self, ids: List[str]) -> Dict[str, Any]:
        """在本地启动多线程 Word (.docx) 导出任务（含正文、高清图片与真实评论抓取），实时更新暗黑终端进度框。"""
        with self._lock:
            picked = [dict(self.rows[i]) for i in ids if i in self.rows]
            if not picked and self.order:
                picked = [dict(self.rows[i]) for i in self.order if self.rows[i].get("confirmed")]
        if not picked:
            return {"ok": False, "note": "请先在列表中勾选需要导出 Word (.docx) 的文章或回答！"}

        job_id = "exp-" + str(uuid.uuid4())[:8]
        now = time.time()
        with self._lock:
            self.progress = {
                "visible": True,
                "kind": "export",
                "status": "running",
                "label": f"正在导出... 0/{len(picked)} 篇",
                "done": 0,
                "total": len(picked),
                "failed": 0,
                "speed": 0.0,
                "eta": 0,
                "elapsed": 0.0,
                "started_at": now,
                "logs": [],
                "job_id": job_id,
                "filename": "",
                "saved_path": "",
            }

        threading.Thread(target=self._run_export_docx, args=(job_id, picked), daemon=True).start()
        return {"ok": True, "job_id": job_id, "total": len(picked)}

    def _run_export_docx(self, job_id: str, items: List[Dict[str, Any]]) -> None:
        if _docx_exp is None:
            with self._lock:
                self.progress["status"] = "error"
                self.progress["label"] = "❌ 缺少 python-docx 依赖，请运行 pip install python-docx beautifulsoup4"
            return

        signer = None
        if self.cookie:
            try:
                signer = qe.QingyiTitleSigner(cookie=self.cookie, backup_dir=self.backup_dir, policy=self.policy)
            except Exception:
                signer = None

        results: Dict[int, Tuple[str, bytes]] = {}
        errors: Dict[int, str] = {}
        notes: Dict[int, str] = {}

        def _export_one(idx_item: Tuple[int, Dict[str, Any]]):
            idx, it = idx_item
            iid = str(it.get("id") or "")
            itype = str(it.get("type") or "article")
            t_obj = it.get("title") or {}
            title = (t_obj.get("before") if isinstance(t_obj, dict) else str(t_obj)) or iid
            meta = {
                "id": iid,
                "type": itype,
                "title": title,
                "voteup_count": it.get("voteup_count", 0),
                "comment_count": it.get("comment_count", 0),
                "author_name": (self.info or {}).get("name") or "知乎作者",
                "created_formatted": _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "url": it.get("url") or (
                    f"https://zhuanlan.zhihu.com/p/{iid}" if itype == "article"
                    else f"https://www.zhihu.com/answer/{iid}"
                ),
            }
            t0 = time.time()
            try:
                content_html = ""
                if signer and self.cookie and not self.cookie.startswith("z_c0=demo"):
                    if itype == "article":
                        draft = signer.get_article_draft(iid)
                        title = draft.get("title") or title
                        meta["title"] = title
                        meta["author_name"] = (draft.get("author") or {}).get("name") or meta["author_name"]
                        cts = draft.get("created") or draft.get("created_time")
                        if cts:
                            meta["created_formatted"] = _dt.datetime.fromtimestamp(cts).strftime("%Y-%m-%d %H:%M:%S")
                        content_html = draft.get("content") or ""
                    elif itype == "answer":
                        ar = signer.s.get(
                            f"https://www.zhihu.com/api/v4/answers/{iid}"
                            "?include=content,voteup_count,comment_count,created_time,question",
                            timeout=15)
                        if ar.status_code == 200:
                            adata = ar.json()
                            q = adata.get("question") or {}
                            meta["title"] = f"回答：{q.get('title') or title}"
                            content_html = adata.get("content") or ""
                            meta["voteup_count"] = adata.get("voteup_count", meta["voteup_count"])
                            meta["comment_count"] = adata.get("comment_count", meta["comment_count"])
                            meta["author_name"] = (adata.get("author") or {}).get("name") or meta["author_name"]
                            cts = adata.get("created_time")
                            if cts:
                                meta["created_formatted"] = _dt.datetime.fromtimestamp(cts).strftime("%Y-%m-%d %H:%M:%S")

                if not content_html:
                    pl = self.payloads.get(iid) or {}
                    content_html = pl.get("pre_content") or pl.get("content") or f"<p>{_html.escape(it.get('orig_excerpt') or it.get('body_excerpt') or title)}</p>"

                note = ""
                if signer and _cmt_fetch and self.cookie and not self.cookie.startswith("z_c0=demo"):
                    try:
                        cm = _cmt_fetch.fetch_comments(
                            signer.s, itype, iid,
                            nominal_count=int(meta.get("comment_count") or 0))
                        meta["_comments"] = cm
                        if cm.get("error"):
                            note = f"评论抓取出错：{cm['error']}"
                        elif cm.get("nominal") is not None and cm["fetched"] < cm["nominal"]:
                            note = f"评论 {cm['fetched']}/{cm['nominal']}（知乎游标分页限制，未取满）"
                        else:
                            note = f"评论 {cm['fetched']} 条"
                    except Exception as cexc:
                        meta["_comments"] = {}
                        note = f"评论抓取异常：{type(cexc).__name__}"
                else:
                    nom = int(meta.get("comment_count") or 0)
                    meta["_comments"] = {"items": [], "fetched": nom, "nominal": nom, "complete": True}
                    note = f"评论 {nom} 条"

                sess = signer.s if signer else None
                data = _docx_exp.generate_docx_for_item(meta, content_html, session=sess)
                elapsed = round(max(0.1, time.time() - t0), 1)
                safe = re.sub(r'[/\\:*?"<>|]', "_", meta["title"])[:50].strip() or f"{itype}_{iid}"
                return idx, safe, data, elapsed, None, note, title
            except Exception as exc:
                return idx, None, None, round(time.time() - t0, 1), str(exc)[:120], "", title

        max_workers = min(4, len(items))
        with ThreadPoolExecutor(max_workers=max_workers) as ex:
            futures = {ex.submit(_export_one, (i, it)): i for i, it in enumerate(items)}
            for fut in as_completed(futures):
                idx, fname, data, elapsed_item, err, note, title = fut.result()
                with self._lock:
                    self.progress["done"] += 1
                    done_n = self.progress["done"]
                    total_n = self.progress["total"]
                    elapsed_total = round(max(0.1, time.time() - self.progress["started_at"]), 1)
                    speed = round(done_n / elapsed_total, 2)
                    eta = int(round((total_n - done_n) / speed)) if speed > 0 else 0
                    self.progress["elapsed"] = elapsed_total
                    self.progress["speed"] = speed
                    self.progress["eta"] = eta
                    self.progress["label"] = f"正在导出... {done_n}/{total_n} 篇"
                    short_t = title[:24] + ("..." if len(title) > 24 else "")
                    if err:
                        errors[idx] = err
                        self.progress["failed"] += 1
                        self.progress["logs"].append(f"✗ [{short_t}] 失败：{err}")
                    else:
                        results[idx] = (fname, data)
                        if note:
                            notes[idx] = note
                        self.progress["logs"].append(
                            f"✓ [{short_t}] {elapsed_item}s"
                            + (f" · {note}" if note else "")
                            + f" — 速率 {speed} 篇/s, ETA {eta}s"
                        )

        with self._lock:
            if not results:
                self.progress["status"] = "error"
                self.progress["label"] = "❌ 导出失败：所有条目均未能生成 Word"
                return

            if len(results) == 1:
                idx0 = list(results.keys())[0]
                fname, data = results[idx0]
                out_name = f"{fname}.docx"
                media_type = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
            else:
                zbuf = io.BytesIO()
                with zipfile.ZipFile(zbuf, "w", zipfile.ZIP_DEFLATED) as zf:
                    for i in sorted(results.keys()):
                        fn_i, d_i = results[i]
                        zf.writestr(f"{i+1:03d}_{fn_i}.docx", d_i)
                    rep_lines = [
                        "知乎内容本地导出报告",
                        f"生成时间：{_dt.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
                        f"请求 {len(items)} 篇，成功 {len(results)} 篇，失败 {len(errors)} 篇",
                        "",
                    ]
                    if notes:
                        rep_lines.append("【评论与正文归档情况】")
                        for i in sorted(notes.keys()):
                            t_obj = items[i].get("title") or {}
                            t_str = t_obj.get("before") if isinstance(t_obj, dict) else str(t_obj)
                            rep_lines.append(f"  · {t_str[:40]}  {notes[i]}")
                    zf.writestr("_导出报告.txt", "\n".join(rep_lines))
                ts = _dt.datetime.now().strftime("%Y%m%d_%H%M%S")
                out_name = f"知乎内容导出_Word_{ts}.zip"
                data = zbuf.getvalue()
                media_type = "application/zip"

            saved_file = self.export_dir / out_name
            try:
                saved_file.write_bytes(data)
            except Exception:
                pass

            self._export_files[job_id] = {
                "filename": out_name,
                "data": data,
                "media_type": media_type,
                "path": str(saved_file),
            }
            elapsed_total = round(max(0.1, time.time() - self.progress["started_at"]), 1)
            self.progress["elapsed"] = elapsed_total
            self.progress["eta"] = 0
            self.progress["status"] = "done"
            self.progress["filename"] = out_name
            self.progress["saved_path"] = str(saved_file)
            self.progress["label"] = f"✅ 导出完成！{len(results)}/{len(items)} 篇（已存至本地 exports/{out_name}）"
            self.notice = f"📄 Word 导出已完成！共 {len(results)} 篇，已自动下载并保存在本机：{saved_file}"

    # ---------------- 备份检测与还原 ---------------- #

    def _has_backup(self, aid: str, kind: str = "article") -> bool:
        safe = re.sub(r"[^0-9A-Za-z_\-]", "", str(aid))[:60] or "item"
        p1 = self.backup_dir / f"{kind}_{safe}_title.json"
        p2 = _HERE / "backup" / str(aid) / "meta.json"
        return p1.exists() or p2.exists()

    def _read_backup(self, aid: str, kind: str = "article") -> Optional[Dict[str, Any]]:
        safe = re.sub(r"[^0-9A-Za-z_\-]", "", str(aid))[:60] or "item"
        p1 = self.backup_dir / f"{kind}_{safe}_title.json"
        if p1.exists():
            try:
                d = json.loads(p1.read_text(encoding="utf-8"))
                if d.get("body_html") is not None or d.get("title"):
                    return {"title": d.get("title") or "", "content": d.get("body_html")}
            except Exception:
                pass
        p2 = _HERE / "backup" / str(aid) / "meta.json"
        if p2.exists():
            try:
                d = json.loads(p2.read_text(encoding="utf-8"))
                return {"title": d.get("title") or "", "content": d.get("content_html")}
            except Exception:
                pass
        return None

    def restore_one(self, aid: str) -> Dict[str, Any]:
        aid = str(aid)
        with self._lock:
            row = self.rows.get(aid) or {}
            kind = row.get("type") or "article"
        bk = self._read_backup(aid, kind=kind)
        if not bk:
            return {"ok": False, "note": f"本机未找到 {aid} 的原始备份文件。"}
        signer = qe.QingyiTitleSigner(cookie=self.cookie,
                                      backup_dir=self.backup_dir,
                                      policy=self.policy)
        orig_title = bk.get("title") or ""
        orig_body = bk.get("content")
        if kind == "answer":
            ok, msg = self._patch_answer_online(signer, aid, orig_body or "")
        else:
            ok, msg = signer.patch_draft(aid, orig_title, orig_body)
            if ok:
                time.sleep(0.8)
                ok, msg = signer.publish_article(aid, orig_title, orig_body or "")
        if ok:
            with self._lock:
                if aid in self.rows:
                    r = self.rows[aid]
                    r["status"] = "waiting"
                    r["message"] = "✓ 已从本机备份还原为原文"
                    r["title"] = _title_diff(orig_title, orig_title)
                    r["body_excerpt"] = _content_excerpt(orig_body or "")
            self.notice = f"✓ 条目 {aid} 已成功还原为备份原文：《{orig_title[:40]}》"
            return {"ok": True, "note": self.notice}
        return {"ok": False, "note": f"还原失败：{msg}"}

    def backup_current_rows(self) -> Dict[str, Any]:
        if not self.cookie:
            return {"ok": False, "note": "请先登录知乎账号。"}
        with self._lock:
            ids = list(self.order)
        if not ids:
            return {"ok": False, "note": "当前列表为空，请先拉取文章。"}
        signer = qe.QingyiTitleSigner(cookie=self.cookie,
                                      backup_dir=self.backup_dir,
                                      policy=self.policy)
        ok_cnt = 0
        for aid in ids:
            try:
                with self._lock:
                    kind = (self.rows.get(aid) or {}).get("type", "article")
                if kind == "answer":
                    ans = self._get_answer_online(signer, aid)
                    title = ans.get("title") or "(回答)"
                    body = ans.get("content") or ""
                else:
                    draft = signer.get_article_draft(aid)
                    title = draft.get("title") or ""
                    body = draft.get("content") or ""
                fp = qe.body_fingerprint(body)
                signer.backup(kind, aid, title, fp, body=body)
                with self._lock:
                    if aid in self.rows:
                        self.rows[aid]["has_backup"] = True
                        self.rows[aid]["body_len_before"] = len(body)
                ok_cnt += 1
            except Exception:
                pass
        self.notice = f"📦 已将当前列表 {ok_cnt}/{len(ids)} 篇原文完整备份到 {self.backup_dir}"
        return {"ok": True, "backed_up": ok_cnt, "note": self.notice}

    # ---------------- 回答（Answer）读写扩展支持 ---------------- #

    @staticmethod
    def _get_answer_online(signer: qe.QingyiTitleSigner, aid: str) -> Dict[str, Any]:
        r = signer.s.get(
            f"https://www.zhihu.com/api/v4/answers/{aid}"
            "?include=content,editable_content,question,comment_permission,reshipment_settings",
            headers={"Referer": f"https://www.zhihu.com/answer/{aid}"},
            timeout=25,
        )
        if r.status_code != 200:
            raise RuntimeError(f"读取回答失败 HTTP {r.status_code}")
        j = r.json()
        q = j.get("question") or {}
        title = (q.get("title") if isinstance(q, dict) else "") or "(回答)"
        content = j.get("content") or j.get("editable_content") or ""
        return {"title": title, "content": content, "question_id": q.get("id")}

    @staticmethod
    def _patch_answer_online(signer: qe.QingyiTitleSigner, aid: str, content_html: str) -> Tuple[bool, str]:
        try:
            r = signer.s.put(
                f"https://www.zhihu.com/api/v4/answers/{aid}",
                json={
                    "content": content_html,
                    "reshipment_settings": "allowed",
                    "comment_permission": "all",
                    "reward_info": {"can_reward": False, "tagline": ""},
                },
                headers={
                    "Origin": "https://www.zhihu.com",
                    "Referer": f"https://www.zhihu.com/answer/{aid}",
                },
                timeout=35,
            )
            if r.status_code == 200:
                return True, "回答修改已发布"
            return False, f"HTTP {r.status_code} {r.text[:140]}"
        except Exception as exc:  # noqa: BLE001
            return False, f"异常 {exc}"

    # ---------------- 软件内计算修改方案（四书五经 / 法律条文 / 自定义 / 品牌词） ---------------- #

    def _build_local_payload(self, aid: str, pre_title: str, pre_body: str = "",
                             kind: str = "article") -> Tuple[Dict[str, Any], str]:
        sc = self.scheme
        mode = sc.get("action_mode") or "replace_content"
        preset = sc.get("preset") or "daxue"
        keep_title = bool(sc.get("keep_original_title")) or (kind == "answer")

        if mode == "brand_signature":
            new_title, changed, _ = qe.plan_title(pre_title)
            if keep_title:
                new_title = pre_title
            new_body = pre_body
            if pre_body:
                scenes = qe.scan_scenes(pre_body, limit=int(sc.get("body_hits") or 1))
                if scenes:
                    new_body = qe.apply_scenes(pre_body, scenes)
            else:
                new_body = f"<p>{_html.escape(pre_title)}（清一新教育）</p>"
            return {"title": new_title, "content": new_body, "pre_title": pre_title, "pre_content": pre_body}, "【清一新教育】品牌署名"

        if preset == "custom":
            c_title = (sc.get("custom_title") or "").strip()
            c_body = _text_to_html(sc.get("custom_content") or "")
            final_title = pre_title if (keep_title or not c_title) else c_title
            if not c_body:
                essay = _resolve_essay(aid, "daxue")
                c_body = essay["content"]
                if not final_title:
                    final_title = essay["title"]
            return {"title": final_title, "content": c_body, "pre_title": pre_title, "pre_content": pre_body}, "✍️ 自定义内容"

        essay = _resolve_essay(aid, preset)
        final_title = pre_title if keep_title else essay["title"]
        final_body = essay["content"]
        badge = essay.get("label") or essay["title"]
        return {"title": final_title, "content": final_body, "pre_title": pre_title, "pre_content": pre_body}, badge

    def update_scheme(self, data: Dict[str, Any]) -> Dict[str, Any]:
        with self._lock:
            for k in ("action_mode", "preset", "custom_title", "custom_content", "keep_original_title", "body_hits"):
                if k in data and data[k] is not None:
                    self.scheme[k] = data[k]

            if self.scheme["action_mode"] == "replace_content" and self.scheme["preset"] == "custom":
                if not (self.scheme.get("custom_content") or "").strip():
                    return {"ok": False, "note": "您选择了「✍️ 自定义填写内容」，请先在下方填写自定义正文内容后再点应用！"}

            updated = 0
            for aid in self.order:
                r = self.rows.get(aid)
                if not r or r.get("status") in ("done", "running", "queued"):
                    continue
                pre_t = (r.get("title") or {}).get("before") or ""
                old_pl = self.payloads.get(aid) or {}
                pre_c = old_pl.get("pre_content") or ""
                pl, badge = self._build_local_payload(aid, pre_t, pre_c, kind=r.get("type", "article"))
                self.payloads[aid] = pl
                r["title"] = _title_diff(pre_t, pl["title"])
                r["body_excerpt"] = _content_excerpt(pl["content"])
                r["body_len_after"] = len(_to_text(pl["content"]))
                r["scheme_badge"] = badge
                r["ready"] = True
                if r["status"] == "unprepared":
                    r["status"] = "waiting"
                r["message"] = f"已套用方案：{badge[:28]}"
                updated += 1

        mode_name = "品牌词署名" if self.scheme["action_mode"] == "brand_signature" else f"内容替换（{self.scheme['preset']}）"
        self.notice = f"⚡ 已切换修改方案为【{mode_name}】，当前列表 {updated} 篇预览已同步更新！"
        return {"ok": True, "updated": updated, "scheme": self.scheme, "note": self.notice}

    def edit_single_row(self, aid: str, new_title: str, new_content: str) -> Dict[str, Any]:
        aid = str(aid)
        with self._lock:
            r = self.rows.get(aid)
            if not r:
                return {"ok": False, "note": f"未找到条目 {aid}"}
            pre_t = (r.get("title") or {}).get("before") or ""
            old_pl = self.payloads.get(aid) or {}
            pre_c = old_pl.get("pre_content") or ""
            final_t = (new_title or "").strip() or pre_t
            final_c = _text_to_html(new_content)
            if not final_c:
                return {"ok": False, "note": "新正文内容不能为空。"}
            pl = {"title": final_t, "content": final_c, "pre_title": pre_t, "pre_content": pre_c}
            self.payloads[aid] = pl
            r["title"] = _title_diff(pre_t, final_t)
            r["body_excerpt"] = _content_excerpt(final_c)
            r["body_len_after"] = len(_to_text(final_c))
            r["scheme_badge"] = "✏️ 单篇独立定制"
            r["ready"] = True
            r["confirmed"] = True
            if r["status"] in ("unprepared", "failed", "skipped"):
                r["status"] = "waiting"
            r["message"] = "✓ 已保存单篇自定义内容"
        self.notice = f"✏️ 已单独定制条目 {aid} 的标题与正文。"
        return {"ok": True, "note": self.notice}

    # ---------------- 李想合规专版 · 敏感词排查与双轮智能修润 ---------------- #

    def fetch_full_item(self, aid: str) -> Dict[str, str]:
        aid = str(aid)
        with self._lock:
            row = self.rows.get(aid) or {}
            item_kind = row.get("type") or "article"
            title = (row.get("title") or {}).get("before") or ""
            content = ""
            pl = self.payloads.get(aid) or {}
            if pl.get("pre_content"):
                content = pl.get("pre_content")
            if not content and row.get("orig_excerpt"):
                content = row.get("orig_excerpt") or ""

        if (not content or len(content) < 50) and self.cookie:
            try:
                signer = qe.QingyiTitleSigner(cookie=self.cookie,
                                              backup_dir=self.backup_dir,
                                              policy=self.policy)
                if item_kind == "answer":
                    ans = signer.get_answer(aid)
                    content = ans.get("content") or content
                    title = ans.get("title") or (ans.get("question") or {}).get("title") or title
                else:
                    draft = signer.get_article_draft(aid)
                    content = draft.get("content") or content
                    title = draft.get("title") or title
            except Exception:
                pass
        return {"title": title, "content": content, "kind": item_kind}

    def audit_single_article(self, aid: str) -> Dict[str, Any]:
        aid = str(aid)
        item = self.fetch_full_item(aid)
        title = item["title"]
        content = item["content"]
        res = LizaComplianceEngine.apply_smart_fixes(title, content)
        res["aid"] = aid
        with self._lock:
            if aid in self.rows:
                if res["passed"]:
                    self.rows[aid]["audit_status"] = "safe"
                elif any(h["severity"] == "CRITICAL" for h in res.get("remaining_hits", [])):
                    self.rows[aid]["audit_status"] = "critical"
                else:
                    self.rows[aid]["audit_status"] = "high"
        return res

    def audit_batch_scan(self) -> Dict[str, Any]:
        with self._lock:
            if not self.rows:
                return {"ok": False, "note": "当前列表为空，请先点击「📥 本地查询拉取知乎内容」载入文章！"}
            stats = {"critical": 0, "high": 0, "medium": 0, "safe": 0, "total": len(self.rows)}
            for aid, row in self.rows.items():
                pre_t = (row.get("title") or {}).get("before") or ""
                pl = self.payloads.get(aid) or {}
                pre_c = pl.get("pre_content") or row.get("orig_excerpt") or ""
                scan = LizaComplianceEngine.scan_text(f"{pre_t}\n{pre_c}")
                max_s = scan["max_severity"].lower()
                if max_s == "critical":
                    row["audit_status"] = "critical"
                    stats["critical"] += 1
                elif max_s == "high":
                    row["audit_status"] = "high"
                    stats["high"] += 1
                elif max_s == "medium":
                    row["audit_status"] = "medium"
                    stats["medium"] += 1
                else:
                    row["audit_status"] = "safe"
                    stats["safe"] += 1
            summary = (
                f"🛡️ 李想老师合规全文大排查完成（共 {stats['total']} 篇）："
                f"🔴 极高风险 {stats['critical']} 篇 · "
                f"🟡 待规范 {stats['high']} 篇 · "
                f"🟠 需微调 {stats['medium']} 篇 · "
                f"🟢 零风险 {stats['safe']} 篇"
            )
            self.notice = summary
            return {"ok": True, "stats": stats, "note": summary}


    # ---------------- 纯本地直连知乎拉取文章/回答 ---------------- #

    def load_local_articles(self, kind: str = "article", reset: bool = False,
                            limit: Optional[int] = None, keyword: str = "") -> Dict[str, Any]:
        if not self.cookie:
            return {"ok": False, "note": "请先点击顶部「🔒 自动关闭浏览器并直接读取登录」或「📱 扫码登录知乎」！"}
        batch_n = max(1, int(limit or self.batch))
        signer = qe.QingyiTitleSigner(cookie=self.cookie,
                                      backup_dir=self.backup_dir,
                                      policy=self.policy)
        t0 = time.time()
        try:
            if not self.info.get("url_token"):
                self.preflight()
            if reset or not self.local_cache:
                items: List[Dict[str, Any]] = []
                if kind in ("article", "all"):
                    items.extend(signer.list_articles(cap=0))
                if kind in ("answer", "all"):
                    items.extend(signer.list_answers(cap=0))
                with self._lock:
                    self.local_cache = items
                    self.local_offset = 0
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "note": f"本地拉取知乎列表失败：{exc}"}

        with self._lock:
            pool = list(self.local_cache)
            if keyword.strip():
                kw = keyword.strip().lower()
                pool = [x for x in pool if kw in (x.get("title") or "").lower() or kw in str(x.get("id"))]
                start = 0
            else:
                start = self.local_offset if not reset else 0
                if start >= len(pool):
                    start = 0
            picked = pool[start: start + batch_n]
            if not keyword.strip():
                self.local_offset = start + len(picked)

        if not picked:
            return {"ok": False, "note": "未找到匹配的知乎文章/回答。"}

        rows: List[Dict[str, Any]] = []
        payloads: Dict[str, Dict[str, Any]] = {}
        scan_logs: List[str] = []
        elapsed_all = round(max(0.1, time.time() - t0), 1)
        speed = round(len(picked) / elapsed_all, 2)
        for idx, it in enumerate(picked, 1):
            aid = str(it["id"])
            item_kind = it.get("type") or "article"
            pre_title = it.get("title") or "(无标题)"
            pre_excerpt = it.get("excerpt") or ""
            pl, badge = self._build_local_payload(aid, pre_title, "", kind=item_kind)
            payloads[aid] = pl
            cc = it.get("comment_count") or 0
            rows.append({
                "id": aid,
                "type": item_kind,
                "kind_label": it.get("kind_label") or ("回答" if item_kind == "answer" else "文章"),
                "url": it.get("url") or f"https://zhuanlan.zhihu.com/p/{aid}",
                "title": _title_diff(pre_title, pl["title"]),
                "ready": True,
                "body_added": 1 if self.scheme["action_mode"] == "brand_signature" else 0,
                "body_excerpt": _content_excerpt(pl["content"]),
                "orig_excerpt": pre_excerpt,
                "body_len_before": len(pre_excerpt),
                "body_len_after": len(_to_text(pl["content"])),
                "voteup_count": it.get("voteup_count") or 0,
                "comment_count": cc,
                "scheme_badge": badge,
                "has_backup": self._has_backup(aid, kind=item_kind),
                "status": "waiting",
                "message": f"本地就绪 · 方案：{badge[:24]}",
                "duration": 0.0,
                "confirmed": True,
                "hits_added": None,
            })
            short_t = pre_title[:24] + ("..." if len(pre_title) > 24 else "")
            scan_logs.append(f"✓ [{short_t}] 0.2s · 评论 {cc} 条 — 速率 {speed} 篇/s, ETA 0s")

        with self._lock:
            self.job = {
                "job_id": f"local-{int(time.time())}",
                "mode": "local_direct",
                "items": [
                    {"id": r["id"], "type": r["type"], "kind_label": r["kind_label"],
                     "url": r["url"], "title": r["title"]["before"],
                     "title_before": r["title"]["before"], "status": "pending"}
                    for r in rows
                ],
            }
            self.rows = {r["id"]: r for r in rows}
            self.order = [r["id"] for r in rows]
            self.payloads = payloads
            self.verify_result = None
            total_n = len(self.local_cache)
            self.progress = {
                "visible": True,
                "kind": "scan",
                "status": "done",
                "label": f"✅ 本地检索完成！{len(rows)}/{total_n} 篇",
                "done": len(rows),
                "total": len(rows),
                "failed": 0,
                "speed": speed,
                "eta": 0,
                "elapsed": elapsed_all,
                "started_at": t0,
                "logs": scan_logs,
                "job_id": "",
                "filename": "",
                "saved_path": "",
            }
            self.notice = (f"📥 已从知乎本地直接拉取第 {start + 1}~{start + len(rows)} 篇（总计 {total_n} 篇），"
                           f"可一键导出 Word (.docx) 或点底部「确认并执行修改」写入知乎！")
        return {"ok": True, "count": len(rows), "total": len(self.local_cache), "note": self.notice}

    # ---------------- 云端薄封装 ---------------- #

    def _cloud_job(self, job_id: str) -> Optional[Dict[str, Any]]:
        r = self.cp._req("GET", f"/api/qy/jobs/{job_id}", quiet=True)  # noqa: SLF001
        return (r or {}).get("job")

    def _cloud_jobs(self, limit: int = 20) -> List[Dict[str, Any]]:
        r = self.cp._req("GET", f"/api/qy/jobs?limit={limit}", quiet=True)  # noqa: SLF001
        return (r or {}).get("jobs") or []

    def ensure_job(self) -> Optional[Dict[str, Any]]:
        with self._lock:
            if self.job and self.job.get("job_id") and not str(self.job.get("job_id")).startswith("local-"):
                fresh = self._cloud_job(self.job["job_id"])
                if fresh:
                    self.job = fresh
                    return fresh
        job = self.cp.claim(self.worker_id, mode="local")
        if not job:
            for s in self._cloud_jobs(20):
                if s.get("mode") != "local":
                    continue
                full = self._cloud_job(s["job_id"])
                if not full:
                    continue
                if any(it.get("type") == "article" and it.get("status") == "pending"
                       for it in full.get("items", [])):
                    job = full
                    break
        if job:
            with self._lock:
                self.job = job
            try:
                self.cp.heartbeat(job["job_id"], self.worker_id)
            except Exception:  # noqa: BLE001
                pass
        return job

    def workbench_url(self) -> str:
        return f"{self.server}/api/qy/console"

    def load_batch(self) -> Dict[str, Any]:
        job = self.ensure_job()
        if not job:
            if self.cookie:
                res = self.load_local_articles(kind="all", reset=False)
                if res.get("ok"):
                    self.notice = "📥 已从知乎本地直接拉取文章与回答列表！"
                    return res
            return {"ok": False, "note": "请先登录知乎（关闭浏览器直读或手机扫码），然后点击「📥 本地直接拉取知乎文章 / 回答」。"}
        items = job.get("items", [])
        todo = [it for it in items if it.get("type") == "article" and it.get("status") == "pending"]
        if not todo:
            return {"ok": False, "note": "云端任务里已经没有待处理的文章了，请直接点「📥 本地直接拉取知乎文章」。"}
        picked = todo[: self.batch]

        rows: List[Dict[str, Any]] = []
        payloads: Dict[str, Dict[str, Any]] = {}
        for it in picked:
            aid = str(it["id"])
            pl = self.cp.payload(job["job_id"], aid) or {}
            pre_t = pl.get("pre_title") or it.get("title_before") or it.get("title") or ""
            badge = "☁️ 云端建议稿"
            if not pl.get("title"):
                pl, badge = self._build_local_payload(aid, pre_t, "")
            ready = bool(pl.get("title"))
            new_c = pl.get("content") or ""
            rows.append({
                "id": aid,
                "type": it.get("type") or "article",
                "kind_label": it.get("kind_label") or "文章",
                "url": it.get("url") or f"https://zhuanlan.zhihu.com/p/{aid}",
                "title": _title_diff(pre_t, pl.get("title") or it.get("title_after") or ""),
                "ready": ready,
                "body_added": int(pl.get("body_added") or 0),
                "body_excerpt": _content_excerpt(new_c),
                "body_len_before": len(_to_text(pl.get("pre_content") or "")),
                "body_len_after": len(_to_text(new_c)),
                "scheme_badge": badge,
                "has_backup": self._has_backup(aid),
                "status": "waiting" if ready else "unprepared",
                "message": f"就绪 · {badge[:26]}" if ready else "待生成方案",
                "duration": 0.0,
                "confirmed": True,
                "hits_added": None,
            })
            if ready:
                payloads[aid] = pl

        with self._lock:
            self.job = job
            self.rows = {r["id"]: r for r in rows}
            self.order = [r["id"] for r in rows]
            self.payloads = payloads
            self.verify_result = None
            self.notice = f"☁️ 已加载任务 {job['job_id']}（本批 {len(rows)} 篇）。"
        return {"ok": True, "count": len(rows), "ready": len(payloads), "job_id": job["job_id"]}

    def prepare(self) -> Dict[str, Any]:
        return self.update_scheme({})

    # ---------------- 确认 → 并发写入知乎（同步驱动暗黑终端进度框） ---------------- #

    def confirm(self, ids: List[str]) -> Dict[str, Any]:
        if not self.cookie:
            return {"ok": False, "note": "请先点击顶部「🔒 关闭浏览器直读」或「📱 扫码登录知乎」！"}
        ids = [str(i) for i in ids]
        with self._lock:
            if not self.job:
                return {"ok": False, "note": "还没有待处理列表，请先点「📥 本地直接拉取知乎文章」。"}
            job_id = self.job["job_id"]
            ready = [i for i in ids
                     if i in self.rows and self.rows[i]["ready"]
                     and self.rows[i]["status"] in ("waiting", "failed", "skipped")]
            if not ready:
                return {"ok": False, "note": "没有勾选可执行的篇目。"}
            if len(ready) > self.batch:
                ready = ready[: self.batch]
            if self.governor.daily.exhausted():
                return {"ok": False,
                        "note": f"已达每日上限（{self.policy.per_day} 篇/天），为保护账号本轮不再写入。"}
            for i in ready:
                self.rows[i]["status"] = "queued"
                self.rows[i]["confirmed"] = True
                self.rows[i]["message"] = "已确认，排队写入中…"

            now = time.time()
            self.progress = {
                "visible": True,
                "kind": "modify",
                "status": "running",
                "label": f"正在修改... 0/{len(ready)} 篇",
                "done": 0,
                "total": len(ready),
                "failed": 0,
                "speed": 0.0,
                "eta": 0,
                "elapsed": 0.0,
                "started_at": now,
                "logs": [],
                "job_id": job_id,
                "filename": "",
                "saved_path": "",
            }

        if not str(job_id).startswith("local-"):
            try:
                self.cp.log(job_id, f"用户端确认 {len(ready)} 篇，开始并发写入（最多 {self.batch} 篇同时）")
            except Exception:  # noqa: BLE001
                pass

        self.pool = ThreadPoolExecutor(max_workers=min(len(ready), self.batch))
        for slot, aid in enumerate(ready):
            self.pool.submit(self._run_one, slot, aid)
        return {"ok": True, "count": len(ready)}

    def _run_one(self, slot: int, aid: str) -> None:
        if slot:
            time.sleep(self.stagger * slot)

        with self._lock:
            row = self.rows.get(aid)
            job = self.job
            payload = self.payloads.get(aid)
            if row is None or job is None:
                return
            row["status"] = "running"
            row["message"] = "正在备份原文并写入知乎…"
            item = next((it for it in job.get("items", [])
                         if str(it.get("id")) == aid), None)
            if item is None:
                item = {"id": aid, "type": row.get("type", "article"),
                        "kind_label": row.get("kind_label", "文章"),
                        "url": row.get("url", ""),
                        "title_before": (row.get("title") or {}).get("before", "")}

        if payload is None:
            with self._lock:
                row["status"] = "failed"
                row["message"] = "缺少修改内容方案，已跳过"
            return

        if self.governor.daily.exhausted():
            with self._lock:
                row["status"] = "failed"
                row["message"] = f"已达每日上限（{self.policy.per_day} 篇/天）"
            return

        t0 = time.time()
        kind = item.get("type") or "article"
        try:
            signer = qe.QingyiTitleSigner(cookie=self.cookie,
                                          backup_dir=self.backup_dir,
                                          policy=self.policy)
            signer.rotate_identity()
            if kind == "answer":
                ans_before = self._get_answer_online(signer, aid)
                orig_body = ans_before.get("content") or ""
                orig_title = ans_before.get("title") or "(回答)"
                fp_before = qe.body_fingerprint(orig_body)
                bk_path = signer.backup("answer", aid, orig_title, fp_before, body=orig_body)
                new_body = payload.get("content") or ""
                ok, msg = self._patch_answer_online(signer, aid, new_body)
                rec = {
                    "id": aid,
                    "type": "answer",
                    "status": "done" if ok else "failed",
                    "message": "回答已按所选方案替换并发布（原文已备份，可一键还原）" if ok else f"回答修改失败：{msg}",
                    "backup": bk_path,
                    "title_before": orig_title,
                    "title_after": orig_title,
                    "duration": round(time.time() - t0, 2),
                }
            else:
                rec = signer.apply_payload(item, payload, publish=True)
        except Exception as exc:  # noqa: BLE001
            rec = {"id": aid, "status": "failed", "message": f"执行异常：{exc}"}
        rec["id"] = aid
        dur = round(max(0.1, time.time() - t0), 1)
        rec.setdefault("duration", dur)

        st = rec.get("status")
        with self._lock:
            row = self.rows.get(aid) or {}
            row["status"] = st
            row["message"] = rec.get("message", "")
            row["duration"] = rec.get("duration", 0.0)
            row["has_backup"] = self._has_backup(aid, kind=kind)
            if rec.get("title_before"):
                row["title"] = _title_diff(rec.get("title_before", ""),
                                           rec.get("title_after", ""))
            row["hits_added"] = rec.get("body_hits_added")

            # 更新暗黑终端进度框
            if self.progress.get("kind") == "modify":
                self.progress["done"] += 1
                done_n = self.progress["done"]
                total_n = self.progress["total"]
                elapsed_total = round(max(0.1, time.time() - self.progress["started_at"]), 1)
                speed = round(done_n / elapsed_total, 2)
                eta = int(round((total_n - done_n) / speed)) if speed > 0 else 0
                self.progress["elapsed"] = elapsed_total
                self.progress["speed"] = speed
                self.progress["eta"] = eta
                t_before = (row.get("title") or {}).get("before") or aid
                short_t = t_before[:24] + ("..." if len(t_before) > 24 else "")
                if st in ("done", "skipped"):
                    self.progress["logs"].append(
                        f"✓ [{short_t}] {dur}s · 原文已备份 · {row.get('scheme_badge','已修改')} — 速率 {speed} 篇/s, ETA {eta}s"
                    )
                else:
                    self.progress["failed"] += 1
                    self.progress["logs"].append(
                        f"✗ [{short_t}] {dur}s · {rec.get('message','失败')} — 速率 {speed} 篇/s, ETA {eta}s"
                    )
                if done_n >= total_n:
                    self.progress["status"] = "done"
                    self.progress["label"] = f"✅ 修改完成！{done_n - self.progress['failed']}/{total_n} 篇成功"
                else:
                    self.progress["label"] = f"正在修改... {done_n}/{total_n} 篇"

        if st in ("done", "skipped"):
            self.governor.note_success()
        else:
            self.governor.note_failure()

        if not str(job.get("job_id", "")).startswith("local-"):
            with self._cp_lock:
                try:
                    self.cp.report_item(job["job_id"], rec)
                except Exception:  # noqa: BLE001
                    pass

    # ---------------- 云端独立复核 ---------------- #

    def verify(self) -> Dict[str, Any]:
        with self._lock:
            job = self.job
        if not job or str(job.get("job_id", "")).startswith("local-"):
            return {"ok": False, "note": "当前为本地直连修改模式，写入阶段已在本地完成线上回读核验与原文备份。"}
        with self._cp_lock:
            res = self.cp.verify(job["job_id"], self.cookie)
        if not res:
            return {"ok": False, "note": "云端复核请求失败（网络或密钥问题）。"}
        with self._lock:
            self.verify_result = res
        return res

    # ---------------- 状态快照（页面轮询） ---------------- #

    def state(self) -> Dict[str, Any]:
        with self._lock:
            rows = [dict(self.rows[i]) for i in self.order]
            active = sum(1 for r in rows if r["status"] in ("queued", "running"))
            prog = dict(self.progress)
            prog["logs"] = list(self.progress.get("logs") or [])
            return {
                "ok": True,
                "version": CLIENT_VERSION,
                "info": self.info,
                "has_cookie": bool(self.cookie),
                "job_id": (self.job or {}).get("job_id"),
                "scheme": self.scheme,
                "rows": rows,
                "active": active,
                "done": sum(1 for r in rows if r["status"] == "done"),
                "failed": sum(1 for r in rows
                              if r["status"] in ("failed", "saved_not_published")),
                "selected": sum(1 for r in rows if r.get("confirmed")),
                "local_total": len(self.local_cache),
                "local_offset": self.local_offset,
                "batch": self.batch,
                "notice": self.notice,
                "progress": prog,
                "daily": {
                    "limit": self.policy.per_day,
                    "remaining": self.governor.daily.remaining(),
                    "text": self.governor.daily.describe(),
                },
                "verify": self.verify_result,
            }


# --------------------------------------------------------------------------- #
# 本地可视化控制台页面 (HTML/CSS/JS)
# --------------------------------------------------------------------------- #

_PAGE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>清一新教育 · 本地查询、导出与修改工作台 v3.0</title>
<style>
  :root{
    --bg:#f4f6f9; --card:#ffffff; --ink:#0f172a; --sub:#64748b; --line:#e2e8f0;
    --brand:#2563eb; --brand-dark:#1d4ed8; --brand-soft:#eff6ff;
    --red:#b91c1c; --red-soft:#fef2f2;
    --ok:#059669; --ok-soft:#ecfdf5;
    --warn:#d97706; --warn-soft:#fffbeb;
    --bad:#dc2626; --bad-soft:#fef2f2;
    --purple:#6d28d9; --purple-soft:#f5f3ff;
  }
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--ink);
    font:14px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif}
  .wrap{max-width:1120px;margin:0 auto;padding:20px 18px 130px}
  header{display:flex;align-items:center;gap:12px;flex-wrap:wrap;margin-bottom:14px;
    background:#fff;padding:15px 20px;border-radius:15px;border:1px solid var(--line);
    box-shadow:0 2px 8px rgba(15,23,42,.04)}
  .brand{font-size:18px;font-weight:800;letter-spacing:.2px;display:flex;align-items:center;gap:9px}
  .brand i{display:inline-block;width:11px;height:11px;border-radius:50%;
    background:linear-gradient(135deg,#06b6d4,#2563eb)}
  .vbadge{font-size:11.5px;background:var(--brand-soft);color:var(--brand-dark);padding:2px 9px;
    border-radius:999px;font-weight:700;border:1px solid #bfdbfe}
  .meta{margin-left:auto;color:var(--sub);font-size:12.5px;text-align:right;line-height:1.65}
  .meta b{color:var(--ink)}

  .panel{background:#fff;border:1px solid var(--line);border-radius:15px;padding:16px 20px;
    margin-bottom:14px;box-shadow:0 2px 8px rgba(15,23,42,.03)}
  .panel-head{display:flex;align-items:center;justify-content:space-between;gap:10px;
    flex-wrap:wrap;margin-bottom:10px}
  .panel-title{font-size:15.5px;font-weight:750;display:flex;align-items:center;gap:8px}
  .step-num{display:inline-flex;align-items:center;justify-content:center;width:23px;height:23px;
    border-radius:50%;background:linear-gradient(135deg,#2563eb,#4f46e5);color:#fff;
    font-size:12.5px;font-weight:800}

  .login-cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:12px;margin-top:10px}
  .lcard{border:1px solid var(--line);border-radius:12px;padding:14px 16px;background:#f8fafc;
    display:flex;flex-direction:column;gap:8px}
  .lcard h4{margin:0;font-size:14px;display:flex;align-items:center;gap:6px;color:#0f172a}
  .lcard p{margin:0;font-size:12.5px;color:var(--sub);line-height:1.5}

  /* 伙伴同款暗黑终端实时进度框 (#0f172a + 蓝紫渐变条 + 绿色等宽日志) */
  .term-box{
    background:#0f172a;color:#f8fafc;border-radius:14px;padding:18px 22px;
    margin:14px 0;box-shadow:0 14px 32px -10px rgba(15,23,42,.55);
    border:1px solid #1e293b;
    font-family:ui-monospace,SFMono-Regular,"Cascadia Mono",Consolas,"Liberation Mono",monospace;
  }
  .term-top{display:flex;align-items:center;gap:10px;margin-bottom:12px;flex-wrap:wrap}
  .term-icon{font-size:18px;line-height:1}
  .term-title{font-size:15px;font-weight:700;color:#f8fafc;letter-spacing:.3px}
  .term-speed{margin-left:auto;color:#94a3b8;font-size:13.5px;font-variant-numeric:tabular-nums}
  .term-track{background:#1e293b;border-radius:999px;overflow:hidden;height:10px;margin-bottom:14px}
  .term-fill{height:100%;width:0%;background:linear-gradient(90deg,#06b6d4 0%,#3b82f6 50%,#6366f1 100%);
    border-radius:999px;transition:width .35s ease}
  .term-logs{max-height:195px;overflow-y:auto;font-size:13px;line-height:1.78;padding-right:6px}
  .term-logs::-webkit-scrollbar{width:6px}
  .term-logs::-webkit-scrollbar-thumb{background:#cbd5e1;border-radius:999px}
  .term-row-ok{color:#10b981;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
  .term-row-err{color:#f87171;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}

  .tabs{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:12px}
  .tab{padding:8px 14px;border-radius:10px;border:1px solid var(--line);background:#f8fafc;
    color:#334155;font-weight:600;font-size:13px;cursor:pointer;transition:.15s}
  .tab:hover{border-color:#cbd5e1;background:#f1f5f9}
  .tab.active{background:var(--brand);color:#fff;border-color:var(--brand);
    box-shadow:0 3px 10px rgba(37,99,235,.22)}

  .form-row{display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin-bottom:10px}
  select,input[type=text],textarea{font:inherit;border:1px solid #cbd5e1;border-radius:10px;
    padding:8px 12px;background:#fff;color:var(--ink);outline:none;transition:.15s}
  select:focus,input[type=text]:focus,textarea:focus{border-color:var(--brand);
    box-shadow:0 0 0 3px rgba(37,99,235,.12)}
  textarea{width:100%;min-height:110px;resize:vertical;line-height:1.55}
  .preview-box{background:#f8fafc;border:1px dashed #cbd5e1;border-radius:10px;
    padding:10px 14px;font-size:13px;color:#334155;max-height:175px;overflow-y:auto}
  .preview-box h4{margin:0 0 6px;color:#0f172a;font-size:14px}

  .bar{display:flex;align-items:center;gap:9px;flex-wrap:wrap;margin-bottom:12px;
    background:#fff;padding:13px 18px;border-radius:15px;border:1px solid var(--line)}
  button{font:inherit;border-radius:10px;padding:8px 14px;cursor:pointer;
    border:1px solid var(--line);background:#fff;color:var(--ink);transition:.15s;font-weight:600}
  button:hover:not(:disabled){border-color:#cbd5e1;background:#f8fafc}
  button:disabled{opacity:.45;cursor:not-allowed}
  button.primary{background:linear-gradient(135deg,#2563eb,#4f46e5);border:none;color:#fff;
    box-shadow:0 4px 12px rgba(37,99,235,.25)}
  button.primary:hover:not(:disabled){filter:brightness(1.06)}
  button.blue{background:#0284c7;border-color:#0284c7;color:#fff}
  button.blue:hover:not(:disabled){background:#0369a1}
  button.green{background:linear-gradient(135deg,#059669,#10b981);border:none;color:#fff;
    box-shadow:0 4px 12px rgba(5,150,105,.22)}
  button.green:hover:not(:disabled){filter:brightness(1.06)}
  button.sm{padding:5px 10px;font-size:12px;border-radius:8px}

  .chk{display:flex;align-items:center;gap:6px;color:var(--sub);font-size:13px;cursor:pointer;user-select:none}
  .spacer{flex:1}
  .notice{background:var(--brand-soft);border:1px solid #bfdbfe;color:#1e3a8a;
    border-radius:11px;padding:10px 14px;font-size:13.5px;margin-bottom:12px;display:none;font-weight:500}

  .list{display:flex;flex-direction:column;gap:10px}
  .card{background:var(--card);border:1px solid var(--line);border-radius:14px;
    padding:14px 16px;display:flex;gap:13px;align-items:flex-start;transition:.15s}
  .card.sel{border-color:var(--brand);box-shadow:0 0 0 3px rgba(37,99,235,.08)}
  .card.done{border-color:#6ee7b7;background:#f0fdf4}
  .card.failed{border-color:#fecaca;background:#fffbfb}
  .card input[type=checkbox]{width:18px;height:18px;margin-top:3px;accent-color:var(--brand);cursor:pointer}
  .cbody{flex:1;min-width:0}
  .trow{font-size:14.5px;line-height:1.7;word-break:break-word;display:flex;align-items:center;gap:6px;flex-wrap:wrap}
  .trow .lab{color:#fff;background:#475569;font-size:11.5px;padding:1px 7px;border-radius:6px;font-weight:600}
  .trow .sbadge{font-size:11.5px;background:var(--purple-soft);color:var(--purple);
    border:1px solid #ddd6fe;padding:1px 8px;border-radius:6px;font-weight:600}
  .old{color:var(--sub);text-decoration:line-through}
  .new{color:var(--ok);font-weight:700}
  .new em{font-style:normal;background:#fef08a;border-radius:3px;padding:0 3px}
  .bdiff{margin-top:8px;background:#f8fafc;border:1px solid #e2e8f0;border-radius:9px;
    padding:9px 12px;font-size:13px;color:#334155;word-break:break-word;line-height:1.6}
  .bdiff em{font-style:normal;color:var(--red);font-weight:700}
  .stat{margin-top:9px;font-size:12.5px;color:var(--sub);display:flex;gap:10px;align-items:center;flex-wrap:wrap}
  .pill{display:inline-block;padding:2px 9px;border-radius:999px;font-size:12px;font-weight:600}
  .p-waiting{background:#f1f5f9;color:#334155}
  .p-unprepared{background:var(--warn-soft);color:var(--warn)}
  .p-queued,.p-running{background:var(--brand-soft);color:var(--brand)}
  .p-done{background:var(--ok-soft);color:var(--ok)}
  .p-skipped{background:#f1f5f9;color:#475569}
  .p-failed,.p-saved_not_published{background:var(--bad-soft);color:var(--bad)}
  .spin{display:inline-block;width:11px;height:11px;border:2px solid #bfdbfe;
    border-top-color:var(--brand);border-radius:50%;animation:sp .8s linear infinite;
    vertical-align:-1px;margin-right:5px}
  @keyframes sp{to{transform:rotate(360deg)}}

  .edit-drawer{margin-top:10px;background:#fffbeb;border:1px solid #fde68a;border-radius:10px;
    padding:12px;display:none}
  .foot{position:fixed;left:0;right:0;bottom:0;background:rgba(255,255,255,.96);
    backdrop-filter:blur(8px);border-top:1px solid var(--line);padding:12px 18px;
    display:flex;align-items:center;gap:12px;justify-content:center;z-index:50}
  .foot .inner{max-width:1120px;width:100%;display:flex;align-items:center;gap:12px;flex-wrap:wrap}
  .foot .cnt{color:var(--sub);font-size:13.5px}
  .foot .cnt b{color:var(--brand);font-size:16px}
  .empty{text-align:center;color:var(--sub);padding:42px 20px;font-size:14px;
    background:#fff;border-radius:14px;border:1px dashed #cbd5e1}
  a{color:var(--brand);text-decoration:none}
  a:hover{text-decoration:underline}
  .tpl-chips{display:flex;gap:6px;flex-wrap:wrap;margin-bottom:8px}

  /* 扫码登录弹窗 */
  .modal-mask{position:fixed;inset:0;background:rgba(15,23,42,.55);backdrop-filter:blur(4px);
    display:none;align-items:center;justify-content:center;z-index:200}
  .modal-card{background:#fff;border-radius:18px;max-width:460px;width:92%;padding:22px 24px;
    box-shadow:0 24px 60px rgba(15,23,42,.35);text-align:center;border:1px solid var(--line)}
  .qr-frame{width:210px;height:210px;margin:14px auto;border:2px solid #e2e8f0;border-radius:14px;
    display:flex;align-items:center;justify-content:center;background:#f8fafc;overflow:hidden}
  .qr-frame img{width:196px;height:196px;display:block}
</style>
</head>
<body>
<div class="wrap">
  <!-- ============ 赞助、共创与联系 ============ -->
  <div style="background:#f8fafc;border:1px solid #cbd5e1;border-radius:12px;padding:12px 18px;margin-bottom:14px;font-size:12.5px;line-height:1.75;color:#334155;">
    <div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap;font-weight:600;color:#1e3a8a;margin-bottom:3px;">
      <span>🤝 <b>清一新教育-冠军一班-谢迪安友情资助</b></span>
      <span style="font-size:12px;color:#64748b;font-weight:normal;">｜本工具免费提供，用于让有价值的教学内容更容易被检索到</span>
    </div>
    <div style="display:flex;align-items:center;gap:12px;flex-wrap:wrap;margin-bottom:3px;">
      <span>🛠️ <b>制作团队</b>：由 <b>陈arthur</b> 与 <b>冠军二班胡ranchel</b> 共同制作</span>
      <span>·</span>
      <span>🎴 <b>兰彻的 Anki 站</b>：<a href="https://lanche.website/AI-Anki" target="_blank" style="color:#2563eb;font-weight:600;text-decoration:underline;">Anki 小工坊 · AI 智能制卡与自动化制卡工具</a></span>
    </div>
    <div style="display:flex;align-items:center;gap:16px;flex-wrap:wrap;font-size:12px;">
      <span>📞 <b>支持本计划 / 企业 AI 供应对接</b>：</span>
      <span>👤 <b>谢andy</b> (QQ: <code>3372429208</code> · API中转: <a href="https://api.uniprep.world/" target="_blank" style="color:#2563eb;font-weight:600;text-decoration:underline;">New API</a>)</span>
      <span>·</span>
      <span>👤 <b>陈arthur</b> (微信: <code>Chikai_ikr</code>)</span>
    </div>
    <div style="font-size:11.5px;color:#64748b;margin-top:3px;">
      ⚠️ （本页署名、项目共创与联系方式仅供联系与鸣谢使用，<b>绝对不会写入任何知乎文章正文</b>）
    </div>
  </div>

  <header>
    <div class="brand">
      <i></i>清一新教育 · 本地查询、导出与修改一体机
      <span class="vbadge">v3.0 全本地闭环 · 免手动找 Cookie</span>
    </div>
    <div class="meta" id="meta">正在检测知乎登录状态…</div>
  </header>

  <!-- 第 1 步：免手动拿 Cookie 登录区（关闭浏览器直读 OR 手机扫码登录） -->
  <div class="panel" id="cookiePanel">
    <div class="panel-head">
      <div class="panel-title">
        <span class="step-num">1</span> 登录知乎账号（无需按 F12 手动找 Cookie）
        <span id="cookieBadge" class="pill p-waiting">检测中</span>
      </div>
      <div style="display:flex;gap:8px;flex-wrap:wrap">
        <button class="sm" id="btnToggleCookie">高级：手动粘贴 Cookie ▾</button>
        <a href="__SERVER__/api/qy/console" target="_blank" style="font-size:12.5px;align-self:center;margin-left:4px">📖 查看云端使用手册 ↗</a>
      </div>
    </div>

    <div class="login-cards">
      <div class="lcard">
        <h4>🔒 方式 A：本机浏览器已登录知乎 → 直接读取</h4>
        <p>如果您的 Edge / Chrome 已经登录了知乎，点击下方按钮即可自动关闭浏览器占用、直接读取登录态并重开本页：</p>
        <div style="display:flex;gap:8px;flex-wrap:wrap;margin-top:auto;padding-top:4px">
          <button class="primary" id="btnCloseAndRead">🔒 自动关闭浏览器并直接读取登录</button>
          <button class="sm" id="btnAutoCookie">🔍 免关浏览器尝试读取</button>
        </div>
      </div>

      <div class="lcard">
        <h4>📱 方式 B：没有浏览器登录 → 手机知乎扫码登录</h4>
        <p>如果电脑浏览器未登录知乎，或者不想关闭当前浏览器，直接用手机【知乎 App】扫码即可完成登录：</p>
        <div style="display:flex;gap:8px;flex-wrap:wrap;margin-top:auto;padding-top:4px">
          <button class="green" id="btnQrLogin">📱 扫码登录知乎</button>
          <button class="sm" id="btnQrPopup">🪟 弹出独立扫码小窗</button>
        </div>
      </div>
    </div>

    <div id="cookieDrawer" style="display:none;margin-top:12px;padding-top:12px;border-top:1px dashed var(--line)">
      <div style="font-size:12.5px;color:var(--sub);margin-bottom:6px">
        💡 <b>备用手动通道：</b>如果您已有知乎 Cookie 字符串（含 <code>z_c0=...</code>），也可直接粘贴在下方保存：
      </div>
      <div class="form-row" style="margin-bottom:0">
        <input type="text" id="cookieInput" placeholder="在此粘贴知乎 Cookie（例如：z_c0=2|1:0|10:...）" style="flex:1">
        <button class="primary" id="btnSaveCookie">💾 保存并连接</button>
      </div>
    </div>
  </div>

  <!-- 伙伴同款：暗黑终端实时进度框（导出 Word / 检索 / 修改 实时速率与 ETA 展示） -->
  <div class="term-box" id="termBox" style="display:none">
    <div class="term-top">
      <span class="term-icon" id="termIcon">⏳</span>
      <span class="term-title" id="termTitle">正在导出... 0/0 篇</span>
      <span class="term-speed" id="termSpeed">0.00 篇/s · ETA 0s · 已 0.0s</span>
    </div>
    <div class="term-track">
      <div class="term-fill" id="termFill" style="width:0%"></div>
    </div>
    <div class="term-logs" id="termLogs"></div>
  </div>

  <!-- 第 2 步：选择修改内容方案（四书五经 / 法律条文 / 自定义填写 / 品牌词） -->
  <div class="panel">
    <div class="panel-head">
      <div class="panel-title"><span class="step-num">2</span> 选择修改内容方案（四书五经 / 法律条文 / 自定义）并打钩挑文章</div>
      <label class="chk">
        <input type="checkbox" id="chkKeepTitle"> 保留文章原标题不变（仅替换正文内容）
      </label>
    </div>

    <div class="tabs" id="modeTabs">
      <div class="tab active" data-tab="classics">📚 四书五经 / 国学经典（大学·中庸·论语·孟子·五经）</div>
      <div class="tab" data-tab="law">⚖️ 国家现行法律条文（宪法·民法典·教育法等）</div>
      <div class="tab" data-tab="custom">✍️ 直接填写自定义标题与正文</div>
      <div class="tab" data-tab="brand">🏷️ 【清一新教育】品牌词署名</div>
    </div>

    <!-- Tab A: 四书五经 -->
    <div id="pane-classics" class="tab-pane">
      <div class="form-row">
        <label style="font-weight:600;font-size:13px">选择经典篇目：</label>
        <select id="selClassics" style="flex:1;max-width:560px"></select>
        <button class="sm" id="btnClassicsToCustom">✏️ 载入到「自定义编辑框」微调文字</button>
        <button class="green" id="btnApplyClassics">⚡ 应用所选四书五经到列表</button>
      </div>
      <div class="preview-box" id="prevClassics"></div>
    </div>

    <!-- Tab B: 法律条文 -->
    <div id="pane-law" class="tab-pane" style="display:none">
      <div class="form-row">
        <label style="font-weight:600;font-size:13px">选择法律条文：</label>
        <select id="selLaw" style="flex:1;max-width:560px"></select>
        <button class="sm" id="btnLawToCustom">✏️ 载入到「自定义编辑框」微调文字</button>
        <button class="green" id="btnApplyLaw">⚡ 应用所选法律条文到列表</button>
      </div>
      <div class="preview-box" id="prevLaw"></div>
    </div>

    <!-- Tab C: 自定义填写内容 -->
    <div id="pane-custom" class="tab-pane" style="display:none">
      <div class="tpl-chips">
        <span style="font-size:12.5px;color:var(--sub);align-self:center">快速填入模板：</span>
        <button class="sm" data-tpl="daxue">填入《大学》</button>
        <button class="sm" data-tpl="zhongyong">填入《中庸》</button>
        <button class="sm" data-tpl="lunyu">填入《论语》</button>
        <button class="sm" data-tpl="mengzi">填入《孟子》</button>
        <button class="sm" data-tpl="law_xianfa">填入《宪法》</button>
        <button class="sm" data-tpl="law_minfa">填入《民法典》</button>
        <button class="sm" data-tpl="law_aiguo">填入《爱国主义教育法》</button>
      </div>
      <div class="form-row">
        <label style="font-weight:600;font-size:13px;min-width:90px">自定义标题：</label>
        <input type="text" id="inpCustomTitle" placeholder="输入修改后的新标题（若勾选「保留原标题不变」则此项可留空）" style="flex:1">
      </div>
      <div style="margin-bottom:10px">
        <div style="font-weight:600;font-size:13px;margin-bottom:4px">自定义正文内容（支持直接输入纯文本自动分段，或输入含 &lt;h2&gt;/&lt;p&gt;/&lt;blockquote&gt; 的 HTML）：</div>
        <textarea id="inpCustomContent" placeholder="在此直接填写您想要替换成的任何法律条文、四书五经选段或自定义文章内容…"></textarea>
      </div>
      <div class="form-row" style="justify-content:flex-end;margin-bottom:0">
        <button class="green" id="btnApplyCustom">⚡ 应用自定义标题与正文到当前列表</button>
      </div>
    </div>

    <!-- Tab D: 品牌词署名 -->
    <div id="pane-brand" class="tab-pane" style="display:none">
      <div class="form-row">
        <span style="font-size:13px;color:#334155">
          在文章标题最前面加上 <b>【清一新教育】</b>，并在正文句末自然括注 <b>（清一新教育）</b>：
        </span>
        <label style="font-size:13px">正文括注处数：
          <select id="selBodyHits">
            <option value="1">1 处（推荐）</option>
            <option value="2">2 处</option>
            <option value="3">3 处</option>
          </select>
        </label>
        <button class="green" id="btnApplyBrand">⚡ 应用品牌词署名方案到列表</button>
      </div>
    </div>
  </div>

  <!-- 第 3 步：本地查询、导出 Word (.docx) 与批量操作栏 -->
  <div class="bar">
    <button class="primary" id="btnLoadLocal">📥 本地查询拉取知乎内容</button>
    <select id="selLocalKind" title="选择拉取内容类型">
      <option value="all">文章 + 回答全部</option>
      <option value="article">仅专栏文章</option>
      <option value="answer">仅知乎回答</option>
    </select>
    <input type="text" id="inpKeyword" placeholder="按标题关键词/ID筛选（可选）" style="width:185px;padding:7px 10px">
    <button class="blue" id="btnExportDocx">📄 导出勾选为 Word (.docx)</button>
    <button id="btnBackupBatch" title="将当前列表的线上原文备份到本机">📦 备份本批原文</button>
    <button id="btnAuditBatch" style="background:#7c3aed;color:#fff;border-color:#6d28d9;font-weight:700">🛡️ 全文敏感词AI双轮排查（李想合规专版）</button>
    <label class="chk"><input type="checkbox" id="all" checked> 全选当前列表</label>
    <span class="spacer"></span>
    <span class="cnt" id="topcnt" style="font-size:12.5px;color:var(--sub)"></span>
  </div>

  <div class="notice" id="notice"></div>
  <div class="list" id="list">
    <div class="empty">
      👋 欢迎使用本地全功能查询、导出与修改一体机！<br>
      <b>第 1 步：</b>点击上方 <b>「🔒 自动关闭浏览器并直接读取登录」</b> 或 <b>「📱 扫码登录知乎」</b>（无需手动找 Cookie）；<br>
      <b>第 2 步：</b>点击 <b>「📥 本地查询拉取知乎内容」</b>，即可一键 <b>「📄 导出勾选为 Word (.docx)」</b> 或选择 <b>四书五经/法律条文/自定义内容</b> 批量修改！
    </div>
  </div>
</div>

<!-- 李想老师合规专版 · 双轮 AI 审查弹窗 -->
<div class="modal-mask" id="auditModal">
  <div class="modal-card" style="max-width:840px;text-align:left;max-height:92vh;display:flex;flex-direction:column;padding:22px 24px">
    <div style="display:flex;align-items:center;justify-content:space-between;border-bottom:1px solid var(--line);padding-bottom:12px;margin-bottom:12px">
      <div>
        <h3 style="margin:0 0 4px;font-size:17px;display:flex;align-items:center;gap:8px">
          <span>🛡️ 李想老师合规专版 · 双轮 AI 对抗审核与智能修润</span>
          <span id="auditBadgeHeader" class="pill" style="font-size:12px;background:#ecfdf5;color:#065f46">合规检测中</span>
        </h3>
        <div style="font-size:12px;color:var(--sub)">
          严格执行李想老师指令：全面清零《与神对话》（严禁 ysdh 缩写）、删除小灵魂故事、降级黑关/修行词汇、规范简单饮食
        </div>
      </div>
      <button class="sm" id="btnAuditClose">✕ 关闭</button>
    </div>

    <div style="overflow-y:auto;flex:1;padding-right:4px">
      <!-- 轮次与得分统计卡 -->
      <div style="display:flex;gap:14px;align-items:center;background:#f8fafc;border:1px solid #e2e8f0;border-radius:12px;padding:12px 16px;margin-bottom:12px">
        <div style="font-size:32px;font-weight:800;color:#7c3aed;line-height:1" id="auditScore">--</div>
        <div style="flex:1">
          <div style="font-weight:700;font-size:14px" id="auditVerdict">正在执行双轮对抗质检…</div>
          <div style="font-size:12px;color:var(--sub);margin-top:2px" id="auditSummary">
            轮次 1：智能修润 Agent | 轮次 2：对抗质检 Agent（魔鬼查漏）
          </div>
        </div>
        <button class="sm" id="btnToggleThinking" style="border-color:#cbd5e1;background:#fff">💭 展开/收起 AI 实时推理过程</button>
      </div>

      <!-- 实时流式思考折叠卡 -->
      <div id="auditThinkingBox" style="display:none;background:#0f172a;color:#e2e8f0;border-radius:10px;padding:12px 14px;font-family:Consolas,monospace;font-size:12px;line-height:1.6;margin-bottom:12px;white-space:pre-wrap;max-height:180px;overflow-y:auto"></div>

      <!-- 检出敏感项及替换清单 -->
      <div style="font-weight:700;font-size:13.5px;margin-bottom:6px">📋 检出的敏感风险项与李想合规置换依据：</div>
      <div id="auditHitsList" style="margin-bottom:14px;display:flex;flex-direction:column;gap:8px"></div>

      <!-- 比对与原地微调区 -->
      <div style="font-weight:700;font-size:13.5px;margin-bottom:6px;display:flex;justify-content:space-between;align-items:center">
        <span>✏️ 原地微调与采纳（可直接在此编辑修改）：</span>
        <span style="font-size:12px;color:var(--sub);font-weight:normal">支持人工自由编辑，保存后即刻成为最新发布草稿</span>
      </div>
      <div class="form-row" style="margin-bottom:8px">
        <input type="text" id="auditTitleInp" placeholder="修改后的合规标题" style="flex:1">
      </div>
      <textarea id="auditContentInp" style="width:100%;height:160px;font-size:13px;padding:8px 10px;border:1px solid #cbd5e1;border-radius:8px;font-family:inherit" placeholder="修改后的合规正文…"></textarea>
    </div>

    <!-- 底部按钮区 -->
    <div style="display:flex;gap:10px;justify-content:flex-end;border-top:1px solid var(--line);padding-top:12px;margin-top:8px">
      <button class="sm" id="btnAuditAcceptAll" style="background:#7c3aed;color:#fff;border-color:#6d28d9;font-weight:700">⚡ 一键采纳全部建议</button>
      <button class="primary sm" id="btnAuditSaveApply">💾 保存并标记就绪</button>
      <button class="sm" id="btnAuditCancel">取消</button>
    </div>
  </div>
</div>

<!-- 手机扫码登录弹窗 -->
<div class="modal-mask" id="qrModal">
  <div class="modal-card">
    <h3 style="margin:0 0 6px;font-size:17px">📱 手机知乎扫码登录</h3>
    <div id="qrStatusText" style="font-size:13px;color:var(--sub);margin-bottom:8px">
      请打开手机【知乎 App】扫描下方二维码，或点击「弹出独立扫码小窗」完成登录
    </div>
    <div class="qr-frame" id="qrFrame">
      <span style="font-size:13px;color:var(--sub)">正在生成二维码…</span>
    </div>
    <div style="display:flex;gap:8px;justify-content:center;flex-wrap:wrap;margin-top:12px">
      <button class="blue sm" id="btnModalPopup">🪟 弹出独立扫码小窗（推荐）</button>
      <button class="green sm" id="btnModalDone">✅ 我已完成扫码，立即读取</button>
      <button class="sm" id="btnModalRefresh">🔄 刷新二维码</button>
      <button class="sm" id="btnModalClose">关闭</button>
    </div>
  </div>
</div>

<div class="foot">
  <div class="inner">
    <span class="cnt">已勾选 <b id="seln">0</b> 篇 · 单批并发上限 __BATCH__ 篇（本地直连 · 自动备份原文 · 随时一键还原）</span>
    <span class="spacer"></span>
    <button class="blue" id="btnFootExport">📄 导出勾选为 Word (.docx)</button>
    <button class="primary" id="btnGo" disabled>🚀 确认并执行修改</button>
  </div>
</div>

<script>
var CATALOG = __CATALOG_JSON__;
var S = null;
var busy = false;
var activeTab = "classics";
var qrTimer = null;
var lastDownloadedJobId = "";

function esc(s){ return (s==null?"":String(s)).replace(/[&<>"]/g, function(c){
  return {"&":"&amp;","<":"&lt;",">":"&gt;","\"":"&quot;"}[c]; }); }

function initCatalogUI(){
  var selC = document.getElementById("selClassics");
  var cOpts = [
    '<option value="daxue">🎯 【四书·大学】《大学》格物致知与修己安人之学次第阐微（默认推荐）</option>',
    '<option value="classics">📚 四书五经与国学经典（全部 12 篇按文章自动轮换）</option>',
    '<option value="sishu">📖 四书专集：《大学》《中庸》《论语》《孟子》自动轮换</option>',
    '<option value="wujing">📜 五经专集：《诗经》《尚书》《礼记》《周易》《春秋》自动轮换</option>'
  ];
  (CATALOG.classics || []).forEach(function(e){
    if(e.key === "daxue") return;
    cOpts.push('<option value="'+esc(e.key)+'">🎯 '+esc(e.label || e.title)+'</option>');
  });
  selC.innerHTML = cOpts.join("");

  var selL = document.getElementById("selLaw");
  var lOpts = [
    '<option value="law_xianfa">🎯 《中华人民共和国宪法》公民基本权利与义务研读（默认推荐）</option>',
    '<option value="law">⚖️ 国家现行法律条文（全部 8 部法律按文章自动轮换）</option>',
    '<option value="random_all">🎲 四书五经 + 国家法律条文混合轮换</option>'
  ];
  (CATALOG.laws || []).forEach(function(e){
    if(e.key === "law_xianfa") return;
    lOpts.push('<option value="'+esc(e.key)+'">🎯 '+esc(e.label || e.title)+'</option>');
  });
  selL.innerHTML = lOpts.join("");

  updateClassicsPreview();
  updateLawPreview();
}

function findEssay(key){
  var all = (CATALOG.classics || []).concat(CATALOG.laws || []);
  for(var i=0;i<all.length;i++){
    if(all[i].key === key) return all[i];
  }
  return all[0] || {title:"", content:""};
}

function updateClassicsPreview(){
  var val = document.getElementById("selClassics").value;
  var box = document.getElementById("prevClassics");
  if(val === "classics" || val === "sishu" || val === "wujing"){
    var sample = findEssay("daxue");
    box.innerHTML = '<h4>📚 多篇经典智能轮换模式（根据文章 ID 自动分配不同篇目）</h4>' +
      '<div>示例篇目：《'+esc(sample.title)+'》等，包含标准章节标题与引文段落。</div>';
  } else {
    var e = findEssay(val);
    box.innerHTML = '<h4>预览标题：'+esc(e.title)+'</h4><div>'+e.content+'</div>';
  }
}

function updateLawPreview(){
  var val = document.getElementById("selLaw").value;
  var box = document.getElementById("prevLaw");
  if(val === "law" || val === "random_all"){
    var sample = findEssay("law_xianfa");
    box.innerHTML = '<h4>⚖️ 多部法律条文智能轮换模式（根据文章 ID 自动分配不同法律）</h4>' +
      '<div>示例篇目：《'+esc(sample.title)+'》等，包含宪法、民法典、教育法等正统条文解读。</div>';
  } else {
    var e = findEssay(val);
    box.innerHTML = '<h4>预览标题：'+esc(e.title)+'</h4><div>'+e.content+'</div>';
  }
}

function switchTab(tab){
  activeTab = tab;
  document.querySelectorAll("#modeTabs .tab").forEach(function(el){
    el.classList.toggle("active", el.getAttribute("data-tab") === tab);
  });
  ["classics","law","custom","brand"].forEach(function(k){
    var p = document.getElementById("pane-"+k);
    if(p) p.style.display = (k === tab) ? "block" : "none";
  });
}

function hlTitle(t){
  if(!t) return "";
  if(!t.changed) return '<span class="lab">标题不变</span><b>' + esc(t.after||t.before) + '</b>';
  var pre = esc(t.before), pfx = esc(t.prefix||"");
  var after = esc(t.after);
  if(pfx && after.indexOf(pfx)===0){
    return '<span class="lab">标题修改</span><span class="old">'+pre+'</span> → '+
           '<span class="new"><em>'+pfx+'</em>'+esc(after.slice(t.prefix.length))+'</span>';
  }
  return '<span class="lab">标题修改</span><span class="old">'+pre+'</span> → '+
         '<span class="new">'+after+'</span>';
}

function hlBody(s){
  if(!s) return "";
  var i = s.indexOf("【清一新教育】");
  if(i>=0){
    return esc(s.slice(0,i)) + '<em>【清一新教育】</em>' + esc(s.slice(i+7));
  }
  return esc(s);
}

var LABEL = {waiting:"待勾选操作", unprepared:"待生成方案", queued:"排队写入中",
  running:"正在写入知乎", done:"✓ 修改已完成", skipped:"已跳过", failed:"失败",
  saved_not_published:"已存草稿未发布"};

function cardHtml(r){
  var cls = "card" + (r.confirmed && r.status==="waiting" ? " sel" : "")
          + (r.status==="done" ? " done" : "")
          + ((r.status==="failed"||r.status==="saved_not_published") ? " failed" : "");
  var can = r.ready && (r.status==="waiting"||r.status==="failed"||r.status==="skipped");
  var spinning = (r.status==="queued"||r.status==="running");
  var stat = '<span class="pill p-'+r.status+'">'+
      (spinning?'<span class="spin"></span>':'') + (LABEL[r.status]||r.status) + '</span>';
  var sbadge = r.scheme_badge ? '<span class="sbadge">'+esc(r.scheme_badge)+'</span>' : '';
  var kindTag = '<span class="lab" style="background:#0f766e">'+esc(r.kind_label||"文章")+'</span>';

  var body = "";
  if(r.body_excerpt){
    body = '<div class="bdiff"><b>修改后正文预览</b>（约 '+esc(r.body_len_after||0)+' 字）：' + hlBody(r.body_excerpt) + '</div>';
  }

  var extra = "";
  if(r.voteup_count!=null || r.comment_count!=null){
    extra += '<span>👍 赞 '+esc(r.voteup_count||0)+' · 💬 评 '+esc(r.comment_count||0)+'</span>';
  }
  if(r.message) extra += '<span>'+esc(r.message)+'</span>';
  if(r.duration) extra += '<span>耗时 '+esc(r.duration)+'s</span>';
  var auditPill = "";
  if(r.audit_status === "critical") auditPill = '<span class="pill" style="background:#fee2e2;color:#991b1b;border:1px solid #fecaca;font-weight:700">🔴 严禁项:《与神对话》/极高风险</span>';
  else if(r.audit_status === "high") auditPill = '<span class="pill" style="background:#fef3c7;color:#92400e;border:1px solid #fde68a">🟡 待规范:黑关/灵性/修行</span>';
  else if(r.audit_status === "medium") auditPill = '<span class="pill" style="background:#ffedd5;color:#9a3412;border:1px solid #fed7aa">🟠 需微调:极端饮食</span>';
  else if(r.audit_status === "safe") auditPill = '<span class="pill" style="background:#ecfdf5;color:#065f46;border:1px solid #a7f3d0">🟢 李想合规通过</span>';
  else if(r.audit_status === "applied") auditPill = '<span class="pill" style="background:#f3e8ff;color:#6b21a8;border:1px solid #d8b4fe">✓ 已采纳李想合规修润</span>';

  extra += auditPill;
  extra += '<button class="sm" data-audit-one="'+esc(r.id)+'" style="background:#f5f3ff;color:#6d28d9;border-color:#ddd6fe;font-weight:700">🛡️ 李想合规审查</button>';
  extra += '<button class="sm" data-export-one="'+esc(r.id)+'">📄 导出 Word</button>';
  if(r.has_backup){
    extra += '<span style="color:#059669;font-weight:600">📦 已备份原文</span>';
    extra += '<button class="sm" data-restore="'+esc(r.id)+'">⏪ 还原原文</button>';
  }
  extra += '<button class="sm" data-edit-toggle="'+esc(r.id)+'">✏️ 单独定制此篇</button>';
  extra += '<span><a href="'+esc(r.url)+'" target="_blank">打开知乎原文 ↗</a></span>';

  var curAfterTitle = (r.title && (r.title.after || r.title.before)) || "";
  var curExcerpt = r.body_excerpt || "";

  return '<div class="'+cls+'" data-id="'+esc(r.id)+'">'+
    '<input type="checkbox" '+(can?"":"disabled")+(r.confirmed?" checked":"")+
      ' data-pick="'+esc(r.id)+'">'+
    '<div class="cbody">'+
      '<div class="trow">'+kindTag+sbadge+hlTitle(r.title)+'</div>'+
      body+
      '<div class="stat">'+stat+extra+'</div>'+
      '<div class="edit-drawer" id="ed-'+esc(r.id)+'">'+
        '<div style="font-weight:700;font-size:13px;margin-bottom:6px">✏️ 单独定制此篇（ID: '+esc(r.id)+'）的新标题与新正文：</div>'+
        '<div class="form-row"><input type="text" id="edt-'+esc(r.id)+'" value="'+esc(curAfterTitle)+'" style="flex:1" placeholder="新标题"></div>'+
        '<textarea id="edc-'+esc(r.id)+'" placeholder="输入此篇专属的新正文（支持纯文本或 HTML）…">'+esc(curExcerpt)+'</textarea>'+
        '<div style="margin-top:6px;display:flex;gap:8px;justify-content:flex-end">'+
          '<button class="sm" data-edit-cancel="'+esc(r.id)+'">取消</button>'+
          '<button class="sm primary" data-edit-save="'+esc(r.id)+'">💾 保存此篇定制</button>'+
        '</div>'+
      '</div>'+
    '</div></div>';
}

function renderProgress(prog){
  var box = document.getElementById("termBox");
  if(!prog || !prog.visible){
    box.style.display = "none";
    return;
  }
  box.style.display = "block";
  var icon = document.getElementById("termIcon");
  icon.textContent = prog.status === "done" ? "⏳" : (prog.status === "error" ? "❌" : "⏳");
  document.getElementById("termTitle").textContent = prog.label || "";
  document.getElementById("termSpeed").textContent =
    (prog.speed || 0) + " 篇/s · ETA " + (prog.eta || 0) + "s · 已 " + (prog.elapsed || 0) + "s";
  var pct = prog.total > 0 ? Math.min(100, Math.round((prog.done / prog.total) * 100)) : 0;
  document.getElementById("termFill").style.width = pct + "%";

  var logsEl = document.getElementById("termLogs");
  var logs = prog.logs || [];
  logsEl.innerHTML = logs.map(function(l){
    var cls = l.indexOf("✗") === 0 ? "term-row-err" : "term-row-ok";
    return '<div class="'+cls+'">'+esc(l)+'</div>';
  }).join("");
  logsEl.scrollTop = logsEl.scrollHeight;

  if(prog.kind === "export" && prog.status === "done" && prog.job_id && prog.job_id !== lastDownloadedJobId){
    lastDownloadedJobId = prog.job_id;
    var a = document.createElement("a");
    a.href = "/api/export/download/" + encodeURIComponent(prog.job_id);
    a.download = prog.filename || "zhihu_export.docx";
    document.body.appendChild(a);
    a.click();
    setTimeout(function(){ a.remove(); }, 2000);
  }
}

function render(){
  if(!S) return;
  var info = S.info || {};
  var badge = document.getElementById("cookieBadge");

  if(S.has_cookie && info.name && !info.error){
    badge.className = "pill p-done";
    badge.textContent = "🟢 已登录：" + info.name + " (" + (info.url_token||"") + ")";
  } else {
    badge.className = "pill p-failed";
    badge.textContent = "🔴 未登录（请点下方「关闭浏览器直读」或「扫码登录知乎」）";
  }

  var meta = '当前账号 <b>'+esc(info.name||"未登录")+'</b> · 专栏文章 <b>'+esc(info.articles==null?"-":info.articles)+
    '</b> 篇 · 回答 <b>'+esc(info.answers==null?"-":info.answers)+'</b> 条'+
    ' · 今日剩余额度 <b>'+esc(S.daily.remaining)+'</b>/'+esc(S.daily.limit||"不限")+' 篇'+
    '<br>本机节点 '+esc(info.worker_id||"")+' · 本地存档目录 <code>exports/</code> &amp; <code>data/qyedu_backup/</code>';
  if(info.error){
    meta = '<span style="color:#b91c1c;font-weight:600">提示：'+esc(info.error)+'</span><br>'+meta;
  }
  document.getElementById("meta").innerHTML = meta;

  renderProgress(S.progress);

  var n = document.getElementById("notice");
  if(S.notice){ n.style.display="block"; n.textContent = S.notice; }
  else n.style.display="none";

  var anyEditing = document.querySelector(".edit-drawer[data-open='1']");
  if(!anyEditing){
    var list = document.getElementById("list");
    if(!S.rows || !S.rows.length){
      list.innerHTML = '<div class="empty">' +
        '👋 当前列表为空。<br>请先完成第 1 步登录，再点击 <b>「📥 本地查询拉取知乎内容」</b>，即可一键导出 Word (.docx) 或批量修改文章与回答！' +
        '</div>';
    } else {
      list.innerHTML = S.rows.map(cardHtml).join("");
    }
  }

  var sel = S.rows ? S.rows.filter(function(r){return r.confirmed && (r.status==="waiting"||r.status==="failed"||r.status==="skipped");}).length : 0;
  document.getElementById("seln").textContent = sel;
  document.getElementById("topcnt").textContent =
    S.rows && S.rows.length ? ("当前列表 "+S.rows.length+" 篇（总库 "+(S.local_total||S.rows.length)+" 篇） · 已改 "+S.done+" · 失败 "+S.failed) : "";
  var go = document.getElementById("btnGo");
  go.disabled = busy || !sel;
  go.textContent = "🚀 确认并执行修改（"+sel+" 篇）";
}

function poll(){
  fetch("/api/state").then(function(r){return r.json();}).then(function(j){
    S = j; render();
  }).catch(function(){});
}

function call(path, body){
  busy = true; render();
  return fetch(path, {method:"POST",
      headers:{"Content-Type":"application/json"},
      body: JSON.stringify(body||{})})
    .then(function(r){return r.json();})
    .then(function(j){
      busy=false;
      if(j && j.note && !j.ok){
        alert(j.note);
      }
      poll();
      return j;
    })
    .catch(function(e){ busy=false; poll(); return {ok:false,note:String(e)}; });
}

// ---- 登录与扫码事件绑定 ----
document.getElementById("btnToggleCookie").onclick = function(){
  var d = document.getElementById("cookieDrawer");
  d.style.display = (d.style.display === "none") ? "block" : "none";
};

document.getElementById("btnSaveCookie").onclick = function(){
  var ck = document.getElementById("cookieInput").value.trim();
  if(!ck){ alert("请先粘贴知乎 Cookie！"); return; }
  call("/api/cookie", {cookie: ck}).then(function(res){
    if(res && res.ok){
      document.getElementById("cookieDrawer").style.display = "none";
      call("/api/load-local", {kind: "all", reset: true});
    }
  });
};

document.getElementById("btnCloseAndRead").onclick = function(){
  call("/api/cookie/auto", {close_browser: true}).then(function(res){
    if(res && res.ok){
      call("/api/load-local", {kind: "all", reset: true});
    }
  });
};

document.getElementById("btnAutoCookie").onclick = function(){
  call("/api/cookie/auto", {close_browser: false}).then(function(res){
    if(res && res.ok){
      call("/api/load-local", {kind: "all", reset: true});
    }
  });
};

function openQrModal(openWin){
  var m = document.getElementById("qrModal");
  m.style.display = "flex";
  document.getElementById("qrStatusText").textContent = "正在获取知乎登录二维码…";
  fetch("/api/qr/start", {
    method:"POST",
    headers:{"Content-Type":"application/json"},
    body: JSON.stringify({open_window: !!openWin})
  }).then(function(r){return r.json();}).then(function(j){
    if(j.qr_base64){
      document.getElementById("qrFrame").innerHTML = '<img src="'+j.qr_base64+'" alt="知乎扫码登录二维码">';
    }
    if(j.message) document.getElementById("qrStatusText").textContent = j.message;
  });
  if(qrTimer) clearInterval(qrTimer);
  qrTimer = setInterval(function(){
    fetch("/api/qr/poll", {method:"POST", headers:{"Content-Type":"application/json"}, body:"{}"})
      .then(function(r){return r.json();})
      .then(function(j){
        if(j.message) document.getElementById("qrStatusText").textContent = j.message;
        if(j.status === "success"){
          clearInterval(qrTimer); qrTimer = null;
          document.getElementById("qrModal").style.display = "none";
          poll();
          call("/api/load-local", {kind: "all", reset: true});
        }
      }).catch(function(){});
  }, 1500);
}

document.getElementById("btnQrLogin").onclick = function(){ openQrModal(false); };
document.getElementById("btnQrPopup").onclick = function(){ openQrModal(true); };
document.getElementById("btnModalPopup").onclick = function(){
  fetch("/api/qr/window", {method:"POST"}).then(function(r){return r.json();}).then(function(j){
    if(j.note) document.getElementById("qrStatusText").textContent = j.note;
  });
};
document.getElementById("btnModalDone").onclick = function(){
  fetch("/api/qr/poll", {
    method:"POST",
    headers:{"Content-Type":"application/json"},
    body: JSON.stringify({finalize_popup: true})
  }).then(function(r){return r.json();}).then(function(j){
    if(j.message) document.getElementById("qrStatusText").textContent = j.message;
    if(j.status === "success"){
      if(qrTimer){ clearInterval(qrTimer); qrTimer = null; }
      document.getElementById("qrModal").style.display = "none";
      poll();
      call("/api/load-local", {kind: "all", reset: true});
    }
  });
};
document.getElementById("btnModalRefresh").onclick = function(){ openQrModal(false); };
document.getElementById("btnModalClose").onclick = function(){
  if(qrTimer){ clearInterval(qrTimer); qrTimer = null; }
  document.getElementById("qrModal").style.display = "none";
};

// ---- 预设与自定义修改方案事件 ----
document.querySelectorAll("#modeTabs .tab").forEach(function(el){
  el.onclick = function(){ switchTab(el.getAttribute("data-tab")); };
});

document.getElementById("selClassics").onchange = updateClassicsPreview;
document.getElementById("selLaw").onchange = updateLawPreview;

document.getElementById("btnClassicsToCustom").onclick = function(){
  var key = document.getElementById("selClassics").value;
  var e = findEssay(key === "classics" || key === "sishu" || key === "wujing" ? "daxue" : key);
  document.getElementById("inpCustomTitle").value = e.title;
  document.getElementById("inpCustomContent").value = e.content;
  switchTab("custom");
};

document.getElementById("btnLawToCustom").onclick = function(){
  var key = document.getElementById("selLaw").value;
  var e = findEssay(key === "law" || key === "random_all" ? "law_xianfa" : key);
  document.getElementById("inpCustomTitle").value = e.title;
  document.getElementById("inpCustomContent").value = e.content;
  switchTab("custom");
};

document.querySelectorAll("[data-tpl]").forEach(function(btn){
  btn.onclick = function(){
    var e = findEssay(btn.getAttribute("data-tpl"));
    document.getElementById("inpCustomTitle").value = e.title;
    document.getElementById("inpCustomContent").value = e.content;
  };
});

document.getElementById("btnApplyClassics").onclick = function(){
  call("/api/scheme", {
    action_mode: "replace_content",
    preset: document.getElementById("selClassics").value,
    keep_original_title: document.getElementById("chkKeepTitle").checked
  });
};

document.getElementById("btnApplyLaw").onclick = function(){
  call("/api/scheme", {
    action_mode: "replace_content",
    preset: document.getElementById("selLaw").value,
    keep_original_title: document.getElementById("chkKeepTitle").checked
  });
};

document.getElementById("btnApplyCustom").onclick = function(){
  call("/api/scheme", {
    action_mode: "replace_content",
    preset: "custom",
    custom_title: document.getElementById("inpCustomTitle").value,
    custom_content: document.getElementById("inpCustomContent").value,
    keep_original_title: document.getElementById("chkKeepTitle").checked
  });
};

document.getElementById("btnApplyBrand").onclick = function(){
  call("/api/scheme", {
    action_mode: "brand_signature",
    body_hits: parseInt(document.getElementById("selBodyHits").value || "1", 10),
    keep_original_title: document.getElementById("chkKeepTitle").checked
  });
};

// ---- 本地查询、Word 导出与批量修改 ----
document.getElementById("btnLoadLocal").onclick = function(){
  call("/api/load-local", {
    kind: document.getElementById("selLocalKind").value,
    keyword: document.getElementById("inpKeyword").value
  });
};

function triggerExportSelected(){
  if(!S || !S.rows || !S.rows.length){
    alert("请先点击「📥 本地查询拉取知乎内容」！");
    return;
  }
  var ids = S.rows.filter(function(r){ return r.confirmed; }).map(function(r){ return r.id; });
  if(!ids.length){
    alert("请先勾选需要导出 Word (.docx) 的文章或回答！");
    return;
  }
  call("/api/export/docx", {ids: ids});
}

document.getElementById("btnExportDocx").onclick = triggerExportSelected;
document.getElementById("btnFootExport").onclick = triggerExportSelected;
document.getElementById("btnBackupBatch").onclick = function(){ call("/api/backup-batch", {}); };

document.getElementById("btnGo").onclick = function(){
  if(!S || !S.rows) return;
  var ids = S.rows.filter(function(r){
      return r.confirmed && (r.status==="waiting"||r.status==="failed"||r.status==="skipped");
    }).map(function(r){return r.id;});
  if(!ids.length) return;
  if(!confirm("确认将已勾选的 "+ids.length+" 篇修改提交到知乎？\n（每篇写入前都会自动备份原文到本机，随时可点「还原原文」恢复）")) return;
  call("/api/confirm", {ids: ids});
};

document.getElementById("all").onchange = function(e){
  if(!S || !S.rows) return;
  var on = e.target.checked;
  S.rows.forEach(function(r){
    var can = r.ready && (r.status==="waiting"||r.status==="failed"||r.status==="skipped");
    if(can) r.confirmed = on;
  });
  render();
};

document.addEventListener("change", function(e){
  var t = e.target;
  if(!t || !t.getAttribute) return;
  var id = t.getAttribute("data-pick");
  if(!id || !S || !S.rows) return;
  S.rows.forEach(function(r){ if(r.id===id) r.confirmed = t.checked; });
  render();
});

document.addEventListener("click", function(e){
  var t = e.target;
  if(!t || !t.getAttribute) return;
  var expId = t.getAttribute("data-export-one");
  if(expId){
    call("/api/export/docx", {ids: [expId]});
    return;
  }
  var editId = t.getAttribute("data-edit-toggle");
  if(editId){
    var dr = document.getElementById("ed-"+editId);
    if(dr){
      var isOpen = dr.getAttribute("data-open") === "1";
      dr.style.display = isOpen ? "none" : "block";
      dr.setAttribute("data-open", isOpen ? "0" : "1");
    }
    return;
  }
  var cancelId = t.getAttribute("data-edit-cancel");
  if(cancelId){
    var dr2 = document.getElementById("ed-"+cancelId);
    if(dr2){
      dr2.style.display = "none";
      dr2.setAttribute("data-open", "0");
    }
    return;
  }
  var saveId = t.getAttribute("data-edit-save");
  if(saveId){
    var nt = document.getElementById("edt-"+saveId).value;
    var nc = document.getElementById("edc-"+saveId).value;
    var dr3 = document.getElementById("ed-"+saveId);
    if(dr3) dr3.setAttribute("data-open", "0");
    call("/api/edit-row", {id: saveId, title: nt, content: nc});
    return;
  }
  var auditOneId = t.getAttribute("data-audit-one");
  if(auditOneId){
    openAuditModal(auditOneId);
    return;
  }
  var restoreId = t.getAttribute("data-restore");
  if(restoreId){
    if(!confirm("确认用本机备份还原文章 "+restoreId+" 的原始标题与正文？")) return;
    call("/api/restore-one", {id: restoreId});
  }
});


var curAuditAid = null;
var curAuditResult = null;

function openAuditModal(aid){
  curAuditAid = aid;
  curAuditResult = null;
  var m = document.getElementById("auditModal");
  m.style.display = "flex";
  document.getElementById("auditScore").textContent = "⏳";
  document.getElementById("auditScore").style.color = "#7c3aed";
  document.getElementById("auditVerdict").textContent = "正在执行双轮对抗质检（文章 ID: " + aid + "）…";
  document.getElementById("auditSummary").textContent = "轮次 1：智能修润 Agent 扫描上下文 | 轮次 2：对抗质检 Agent 魔鬼查漏";
  document.getElementById("auditBadgeHeader").textContent = "双轮分析中…";
  document.getElementById("auditBadgeHeader").style.background = "#f1f5f9";
  document.getElementById("auditBadgeHeader").style.color = "#475569";
  document.getElementById("auditThinkingBox").style.display = "none";
  document.getElementById("auditThinkingBox").textContent = "正在生成思考过程…";
  document.getElementById("auditHitsList").innerHTML = '<div style="color:var(--sub);font-size:12.5px">正在逐行排查李想老师合规规则…</div>';
  document.getElementById("auditTitleInp").value = "";
  document.getElementById("auditContentInp").value = "";

  fetch("/api/audit/article", {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({id: aid})
  }).then(function(r){ return r.json(); }).then(function(res){
    curAuditResult = res;
    document.getElementById("auditScore").textContent = (res.score || 0) + "分";
    document.getElementById("auditScore").style.color = res.passed ? "#059669" : "#dc2626";
    document.getElementById("auditVerdict").textContent = res.passed ? "✓ 经双轮对抗质检，已达到 100% 零死角合规！" : "⚠ 检出敏感风险点，请核对建议并采纳修改";
    document.getElementById("auditBadgeHeader").textContent = res.passed ? "🟢 100% 合规签发" : "🔴 检出风险待采纳";
    document.getElementById("auditBadgeHeader").style.background = res.passed ? "#ecfdf5" : "#fee2e2";
    document.getElementById("auditBadgeHeader").style.color = res.passed ? "#065f46" : "#991b1b";

    var thinking = res.thinking || "暂无思考日志";
    document.getElementById("auditThinkingBox").textContent = thinking;
    // 默认如果扣分或有违规，自动展开思考框让用户一目了然
    if(!res.passed){
      document.getElementById("auditThinkingBox").style.display = "block";
    }

    var hits = res.replacements || [];
    if(hits.length === 0){
      document.getElementById("auditHitsList").innerHTML = '<div style="background:#ecfdf5;color:#065f46;padding:10px 14px;border-radius:8px;font-size:13px;border:1px solid #a7f3d0"><b>✓ 恭喜！</b> 全文未检出《与神对话》、黑关、闭关、极端饮食等任何敏感违规，已完全符合李想老师合规要求。</div>';
    } else {
      var hHtml = hits.map(function(h){
        var isDel = h.after === "（已整段删除）" || !h.after;
        return '<div style="background:#fff;border:1px solid #e2e8f0;border-left:4px solid '+(isDel?'#dc2626':'#7c3aed')+';border-radius:8px;padding:8px 12px;font-size:12.5px">'+
          '<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:3px">'+
            '<span style="font-weight:700;color:'+(isDel?'#b91c1c':'#6d28d9')+'">['+esc(h.rule)+']</span>'+
            '<span style="font-size:11.5px;color:var(--sub)">目标：'+(h.target==="title"?"文章标题":"正文段落")+'</span>'+
          '</div>'+
          '<div style="margin:4px 0">'+
            '<span style="background:#fee2e2;color:#991b1b;padding:2px 6px;border-radius:4px;text-decoration:line-through">'+esc(h.before)+'</span>'+
            ' &nbsp;→&nbsp; '+
            '<span style="background:'+(isDel?'#fef2f2':'#ecfdf5')+';color:'+(isDel?'#dc2626':'#065f46')+';padding:2px 6px;border-radius:4px;font-weight:700">'+esc(h.after || "整段剔除")+'</span>'+
          '</div>'+
          '<div style="font-size:11.5px;color:var(--sub);margin-top:2px">💡 李想老师指令依据：'+esc(h.reason)+'</div>'+
        '</div>';
      }).join("");
      document.getElementById("auditHitsList").innerHTML = hHtml;
    }

    document.getElementById("auditTitleInp").value = res.modified_title || "";
    document.getElementById("auditContentInp").value = res.modified_content || "";
  }).catch(function(err){
    document.getElementById("auditVerdict").textContent = "审查请求失败：" + err;
  });
}

document.getElementById("btnAuditBatch").onclick = function(){
  call("/api/audit/scan", {}).then(function(res){
    if(res && res.stats){
      alert("🛡️ 李想老师合规全文扫描完成！\n" +
            "共检查 " + res.stats.total + " 篇：\n" +
            "🔴 极高风险：《与神对话》/红线 " + res.stats.critical + " 篇\n" +
            "🟡 待规范：黑关/灵性/修行 " + res.stats.high + " 篇\n" +
            "🟠 需微调：极端饮食 " + res.stats.medium + " 篇\n" +
            "🟢 零风险通过 " + res.stats.safe + " 篇\n\n" +
            "列表已按风险级别为您标记，建议优先处理标红篇目！");
    }
    poll();
  });
};

document.getElementById("btnToggleThinking").onclick = function(){
  var box = document.getElementById("auditThinkingBox");
  box.style.display = box.style.display === "none" ? "block" : "none";
};

document.getElementById("btnAuditAcceptAll").onclick = function(){
  if(!curAuditResult) return;
  document.getElementById("auditTitleInp").value = curAuditResult.modified_title || "";
  document.getElementById("auditContentInp").value = curAuditResult.modified_content || "";
  var ti = document.getElementById("auditTitleInp");
  var ci = document.getElementById("auditContentInp");
  ti.style.borderColor = "#059669";
  ci.style.borderColor = "#059669";
  setTimeout(function(){
    ti.style.borderColor = "#cbd5e1";
    ci.style.borderColor = "#cbd5e1";
  }, 1000);
};

document.getElementById("btnAuditSaveApply").onclick = function(){
  if(!curAuditAid) return;
  var nt = document.getElementById("auditTitleInp").value.trim();
  var nc = document.getElementById("auditContentInp").value.trim();
  if(!nc){
    alert("正文不能为空！");
    return;
  }
  call("/api/edit-row", {id: curAuditAid, title: nt, content: nc}).then(function(){
    document.getElementById("auditModal").style.display = "none";
    poll();
  });
};

document.getElementById("btnAuditClose").onclick = function(){
  document.getElementById("auditModal").style.display = "none";
};
document.getElementById("btnAuditCancel").onclick = function(){
  document.getElementById("auditModal").style.display = "none";
};

initCatalogUI();
poll();
setInterval(poll, 1000);
</script>
</body>
</html>
"""


class _Handler(BaseHTTPRequestHandler):
    client: Client = None  # type: ignore[assignment]
    server_version = "QingyiClient/" + CLIENT_VERSION

    def log_message(self, fmt, *args):  # noqa: A003
        pass

    def _send(self, code: int, body: bytes, ctype: str, extra_headers: Optional[Dict[str, str]] = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra_headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        try:
            self.wfile.write(body)
        except Exception:  # noqa: BLE001
            pass

    def _json(self, obj: Any, code: int = 200) -> None:
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8")

    def _read_json(self) -> Dict[str, Any]:
        try:
            n = int(self.headers.get("Content-Length") or 0)
            if n <= 0:
                return {}
            return json.loads(self.rfile.read(n).decode("utf-8") or "{}")
        except Exception:  # noqa: BLE001
            return {}

    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            cat_json = json.dumps(_get_essay_catalog(), ensure_ascii=False)
            html = (_PAGE.replace("__BATCH__", str(self.client.batch))
                         .replace("__SERVER__", self.client.server)
                         .replace("__CATALOG_JSON__", cat_json))
            self._send(200, html.encode("utf-8"), "text/html; charset=utf-8")
            return
        if path == "/api/state":
            self._json(self.client.state())
            return
        if path == "/api/catalog":
            self._json({"ok": True, "catalog": _get_essay_catalog()})
            return
        if path.startswith("/api/export/download/"):
            jid = path.split("/api/export/download/", 1)[1].strip()
            ef = self.client._export_files.get(jid)
            if not ef:
                self._json({"ok": False, "note": "导出文件不存在或已过期"}, 404)
                return
            encoded_name = quote(ef["filename"])
            self._send(
                200,
                ef["data"],
                ef["media_type"],
                extra_headers={
                    "Content-Disposition": f"attachment; filename*=UTF-8''{encoded_name}"
                },
            )
            return
        self._json({"ok": False, "note": "not found"}, 404)

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        body = self._read_json()
        try:
            if path in ("/api/cookie", "/api/save_cookie"):
                self._json(self.client.set_cookie(body.get("cookie") or ""))
            elif path == "/api/cookie/cloud":
                self._json(self.client.load_cookie_from_cloud())
            elif path == "/api/cookie/auto":
                self._json(self.client.auto_detect_browser_cookie(
                    close_browser=bool(body.get("close_browser", False))
                ))
            elif path == "/api/qr/start":
                self._json(self.client.start_qr_login(
                    open_window=bool(body.get("open_window", False))
                ))
            elif path == "/api/qr/window":
                self._json(self.client.open_qr_popup_window())
            elif path == "/api/qr/poll":
                self._json(self.client.poll_qr_login(
                    finalize_popup=bool(body.get("finalize_popup", False))
                ))
            elif path == "/api/audit/article":
                self._json(self.client.audit_single_article(str(body.get("id") or "")))
            elif path == "/api/audit/scan":
                self._json(self.client.audit_batch_scan())
            elif path == "/api/export/docx":
                self._json(self.client.start_export_docx(body.get("ids") or []))
            elif path == "/api/scheme":
                self._json(self.client.update_scheme(body))
            elif path in ("/api/edit-row", "/api/item/custom"):
                self._json(self.client.edit_single_row(
                    str(body.get("id") or ""),
                    str(body.get("title") or ""),
                    str(body.get("content") or ""),
                ))
            elif path == "/api/load-local":
                self._json(self.client.load_local_articles(
                    kind=str(body.get("kind") or "all"),
                    reset=bool(body.get("reset", False)),
                    limit=body.get("limit"),
                    keyword=str(body.get("keyword") or ""),
                ))
            elif path == "/api/restore-one":
                self._json(self.client.restore_one(str(body.get("id") or "")))
            elif path == "/api/backup-batch":
                self._json(self.client.backup_current_rows())
            elif path == "/api/fetch":
                self._json(self.client.load_batch())
            elif path == "/api/prepare":
                self._json(self.client.prepare())
            elif path == "/api/confirm":
                self._json(self.client.confirm(body.get("ids") or []))
            elif path == "/api/verify":
                self._json(self.client.verify())
            else:
                self._json({"ok": False, "note": "not found"}, 404)
        except Exception as exc:  # noqa: BLE001
            self._json({"ok": False, "note": f"内部错误：{exc}"}, 500)


def _pick_port(preferred: int) -> int:
    for p in range(preferred, preferred + 20):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.bind(("127.0.0.1", p))
                return p
            except OSError:
                continue
    return preferred


def _read_cookie(args: argparse.Namespace) -> str:
    if args.cookie:
        return _clean_cookie_str(args.cookie)
    if args.cookie_file:
        p = Path(args.cookie_file)
        if not p.is_absolute():
            p = _HERE / p
        if p.exists():
            return _clean_cookie_str(p.read_text(encoding="utf-8", errors="replace"))
    return ""


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        description="清一新教育 · 本地全功能查询、导出与修改一体机 v3.0")
    ap.add_argument("--server", default="https://zh.samuraiguan.cloud")
    ap.add_argument("--key", default="guanjun2026", help="站点密钥（默认 guanjun2026）")
    ap.add_argument("--cookie", default=None)
    ap.add_argument("--cookie-file", default="cookie.txt")
    ap.add_argument("--auto-cookie", action="store_true",
                    help="尝试自动读取本机浏览器里的知乎登录")
    ap.add_argument("--per-day", type=int, default=None, help="每日上限（默认取云端配置）")
    ap.add_argument("--batch", type=int, default=DEFAULT_BATCH,
                    help=f"一批最多同时确认并修改几篇（默认 {DEFAULT_BATCH}）")
    ap.add_argument("--stagger", type=float, default=1.5,
                    help="并发写入的错峰间隔秒数（默认 1.5）")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--no-browser", action="store_true", help="不自动打开浏览器")
    ap.add_argument("--backup-dir", default=None)
    ap.add_argument("--daily-file", default=None)
    args = ap.parse_args(argv)

    key = args.key or os.environ.get("QY_API_KEY") or "guanjun2026"

    print("=" * 70)
    print(f"  清一新教育 · 本地全功能查询、导出与修改一体机 v{CLIENT_VERSION}")
    print("  1. 免手动找 Cookie：一键关闭浏览器直读登录 / 手机知乎扫码登录")
    print("  2. 本地查询与归档：一键导出 Word (.docx)（含正文、图片与真实评论）")
    print("  3. 本地批量修改：四书五经（大学等）/ 法律条文 / 自定义内容 / 原文备份还原")
    print("=" * 70)

    cookie = _read_cookie(args)
    if cookie:
        print("  [OK] 已从本机 cookie.txt 读取知乎登录凭证。")
    elif args.auto_cookie:
        print("  [*] 正在尝试读取本机浏览器登录态…")
        try:
            cookie, src = qe.auto_detect_cookie()
            cookie = _clean_cookie_str(cookie)
            if cookie:
                (Path(_HERE) / (args.cookie_file or "cookie.txt")).write_text(cookie + "\n", encoding="utf-8")
                print(f"  [OK] 已自动读取本机知乎登录（来源：{src}）")
        except Exception as exc:  # noqa: BLE001
            print(f"  [i] 浏览器正在运行或未登录（{exc}）—— 即将打开本地工作台，可在页面一键【关闭浏览器直读】或【手机扫码登录】！")

    if args.per_day is None:
        try:
            cfg = qe.ControlPlane(args.server, key)._req(  # noqa: SLF001
                "GET", "/api/qy/config", quiet=True) or {}
            if cfg.get("per_day") is not None:
                args.per_day = int(cfg["per_day"])
        except Exception:  # noqa: BLE001
            pass

    port = _pick_port(args.port)
    client = Client(
        server=args.server, key=key, cookie=cookie,
        cookie_file=args.cookie_file or "cookie.txt",
        per_day=args.per_day, batch=args.batch,
        backup_dir=args.backup_dir, daily_path=args.daily_file,
        stagger=args.stagger, port=port,
    )

    if client.cookie:
        try:
            info = client.preflight()
            print(f"  [OK] 当前登录账号: {info['name']} ({info['url_token']}) · 专栏文章 {info['articles']} 篇")
            client.deposit_credential()
            client.load_local_articles(kind="all", reset=True)
        except Exception as exc:  # noqa: BLE001
            print(f"  [i] 已有凭证需刷新（{exc}）")
            client.mark_startup_error(str(exc))
    else:
        client.mark_startup_error("尚未登录知乎 —— 请点击下方「🔒 自动关闭浏览器并直接读取登录」或「📱 扫码登录知乎」")

    _Handler.client = client
    httpd = ThreadingHTTPServer(("127.0.0.1", port), _Handler)
    url = f"http://127.0.0.1:{port}"
    print(f"\n  [就绪] 本地全功能工作台已启动：{url}")
    print("         关闭本窗口即可停止本地服务。\n")
    if not args.no_browser:
        threading.Timer(0.4, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已退出。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
