#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""清一新教育 · 用户端（本地可视化修改工作台 v2.0）

核心能力升级
------------
1. 手动/自动/云端多通道凭证：
   - 即使 Edge / Chrome 正在运行并锁定了 Cookie 数据库，程序也绝不退出或卡死；
   - 直接打开本地控制台（http://127.0.0.1:8765），支持在网页内直接手动粘贴 Cookie、
     从云端凭证柜一键载入、或按需自动检测本机浏览器。
2. 纯本地直连拉取 + 云端任务双模式（参考 zh_local 工具包并全面升级）：
   - 支持「📥 本地直接拉取知乎文章 / 回答」（不经过任何第三方服务器，直连知乎）；
   - 同时保留「☁️ 从云端工作台领取任务」与「云端独立复核」能力。
3. 软件内自定义修改内容与正统文库（四书五经 / 法律条文 / 自定义填写 / 品牌署名）：
   - 📚 四书五经与国学经典：支持选《大学》《中庸》《论语》《孟子》《诗经》《尚书》
     《礼记·学记》《礼记·儒行》《周易》《春秋左传》《荀子·劝学》《韩愈·师说》单篇或轮换；
   - ⚖️ 国家现行法律条文：支持选《宪法》《民法典》《爱国主义教育法》《义务教育法》
     《未成年人保护法》《科学技术进步法》《著作权法》《家庭教育促进法》单篇或轮换；
   - ✍️ 直接填写自定义内容：在软件内直接输入任意新标题与新正文（支持纯文本自动分段或 HTML），
     并支持对列表中任意单篇文章/回答点「✏️ 单独编辑」独立定制；
   - 🏷️ 品牌词署名：保留【清一新教育】标题前缀 + 正文括注模式。
4. 本地自动备份与一键还原：
   - 每一篇修改写入前自动将原始标题与 HTML 正文备份到本机 `data/qyedu_backup/`；
   - 页面上随时可点「⏪ 还原原文」一键恢复修改前状态。
"""
from __future__ import annotations

import argparse
import ctypes
import html as _html
import json
import os
import random
import re
import socket
import sys
import threading
import time
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

if sys.platform == "win32":
    try:
        ctypes.windll.kernel32.SetConsoleTitleW("清一新教育 · 用户端本地修改工作台")
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

CLIENT_VERSION = "2.0.0"
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
    """若用户输入的是纯文本，自动转为标准 <p> 段落；若已含 HTML 标签则原样保留。"""
    raw = (s or "").strip()
    if not raw:
        return ""
    if re.search(r"<(p|h[1-6]|blockquote|div|ul|ol|li|br)\b", raw, re.I):
        return raw
    parts = [p.strip() for p in re.split(r"\n\s*\n|\r?\n", raw) if p.strip()]
    return "".join(f"<p>{_html.escape(p)}</p>" for p in parts)


def _brand_excerpt(new_html: str, radius: int = 80) -> str:
    """把「正文里被插入品牌词」的那一小段摘出来，插入处用【】标出。"""
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
    """提取替换后正文的纯文本摘要，供界面清晰预览。"""
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
    """清洗用户粘贴或文件中的 Cookie 字符串（自动剥离 Cookie: 前缀与多余换行）。"""
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
# 本地控制台核心状态机
# --------------------------------------------------------------------------- #

class Client:
    def __init__(self, server: str, key: str, cookie: str,
                 cookie_file: str = "cookie.txt",
                 per_day: Optional[int] = None, batch: int = DEFAULT_BATCH,
                 backup_dir: Optional[str] = None,
                 daily_path: Optional[str] = None,
                 stagger: float = 1.5) -> None:
        self.cookie = _clean_cookie_str(cookie)
        self.cookie_file = Path(cookie_file) if cookie_file else (_HERE / "cookie.txt")
        if not self.cookie_file.is_absolute():
            self.cookie_file = _HERE / self.cookie_file
        self.server = server.rstrip("/")
        self.key = key
        self.batch = max(1, int(batch))
        self.stagger = float(stagger)
        self.cp = qe.ControlPlane(server, key)
        self.cp.s.headers["X-API-Key"] = key
        self.policy = qe.RatePolicy()
        if per_day is not None:
            self.policy.per_day = max(0, int(per_day))
        self.backup_dir = Path(backup_dir) if backup_dir else (_HERE / "data" / "qyedu_backup")
        self.backup_dir.mkdir(parents=True, exist_ok=True)
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

        # 当前软件内选择的修改方案（默认：四书五经《大学》/经典轮换，可在页面随时切换）
        self.scheme: Dict[str, Any] = {
            "action_mode": "replace_content",   # replace_content | brand_signature
            "preset": "daxue",                  # daxue | zhongyong | lunyu | mengzi | classics | law | law_xianfa | custom ...
            "custom_title": "",
            "custom_content": "",
            "keep_original_title": False,
            "body_hits": 1,
        }

    # ---------------- 登录凭证管理（手动 / 云端 / 自动检测） ---------------- #

    def set_cookie(self, raw_cookie: str, source_label: str = "手动输入") -> Dict[str, Any]:
        """更新 Cookie，保存到本机 cookie.txt，立即执行知乎账号自检并同步云端凭证柜。"""
        ck = _clean_cookie_str(raw_cookie)
        if not ck:
            return {"ok": False, "note": "输入的 Cookie 为空或格式无效（需包含 z_c0=... 或完整知乎 Cookie）。"}
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
            self.notice = (f"✓ 已通过【{source_label}】成功绑定知乎账号：{info.get('name')} "
                           f"（文章 {info.get('articles')} 篇），凭证已存至 cookie.txt。")
            return {"ok": True, "info": info, "deposit": dep, "note": self.notice}
        except Exception as exc:  # noqa: BLE001
            self.mark_startup_error(str(exc))
            return {"ok": False, "note": f"Cookie 验证未通过：{exc}"}

    def load_cookie_from_cloud(self) -> Dict[str, Any]:
        """从云端凭证柜（/api/qy/credential-latest）拉取最新凭证。"""
        try:
            r = self.cp._req(  # noqa: SLF001
                "GET", f"/api/qy/credential-latest?key={self.key}", quiet=True) or {}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "note": f"连接云端凭证柜失败：{exc}"}
        if not r.get("ok") or not r.get("cookie"):
            return {"ok": False, "note": r.get("note") or "云端凭证柜暂无可用凭证，请在下方输入框直接手动粘贴知乎 Cookie。"}
        return self.set_cookie(r["cookie"], source_label="云端凭证柜")

    def auto_detect_browser_cookie(self) -> Dict[str, Any]:
        """按需尝试读取本机浏览器 Cookie（失败不卡死，提示手动粘贴即可）。"""
        try:
            ck, src = qe.auto_detect_cookie()
            return self.set_cookie(ck, source_label=f"本机浏览器 {src}")
        except Exception as exc:  # noqa: BLE001
            return {
                "ok": False,
                "note": f"浏览器自动读取未成功（{exc}）。无需关闭浏览器，请直接在上方输入框手动粘贴知乎 Cookie 即可！",
            }

    # ---------------- 启动自检 ---------------- #

    def preflight(self) -> Dict[str, Any]:
        """账号自检。返回的就是 self.info（页面展示用的那份），失败时抛异常。"""
        if not self.cookie:
            raise RuntimeError("尚未配置知乎 Cookie，请在页面顶部「登录凭证设置」中手动粘贴 Cookie 或点载入。")
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
        """自检失败也要让页面有话说，引导用户直接在页面粘贴 Cookie。"""
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
        """用本机备份一键还原指定文章或回答。"""
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
            self.notice = f"✓ 文章 {aid} 已成功还原为备份原文：《{orig_title[:40]}》"
            return {"ok": True, "note": self.notice}
        return {"ok": False, "note": f"还原失败：{msg}"}

    def backup_current_rows(self) -> Dict[str, Any]:
        """仅把当前列表中的文章原文备份到本机（只读不改写知乎，类似 zh_local.py backup）。"""
        if not self.cookie:
            return {"ok": False, "note": "请先配置知乎 Cookie。"}
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
        """根据当前 self.scheme 为单篇内容生成 (payload, preset_badge)。"""
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
                new_body = f"<p>{ _html.escape(pre_title) }（清一新教育）</p>"
            return {"title": new_title, "content": new_body, "pre_title": pre_title, "pre_content": pre_body}, "【清一新教育】品牌署名"

        # replace_content 模式：四书五经 / 法律条文 / 自定义填写
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
        """更新软件内的修改方案，并实时刷新当前列表中所有待确认文章的预览！"""
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
        """对列表中某一篇文章单独设置自定义标题与正文。"""
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

    # ---------------- 纯本地直连知乎拉取文章/回答（参考 zh_local.py） ---------------- #

    def load_local_articles(self, kind: str = "article", reset: bool = False,
                            limit: Optional[int] = None, keyword: str = "") -> Dict[str, Any]:
        """直接用本机 Cookie 从知乎拉取文章或回答列表，并按当前方案生成修改预览。"""
        if not self.cookie:
            return {"ok": False, "note": "请先在页面顶部「🔑 登录凭证设置」中粘贴知乎 Cookie 或从云端载入！"}
        batch_n = max(1, int(limit or self.batch))
        signer = qe.QingyiTitleSigner(cookie=self.cookie,
                                      backup_dir=self.backup_dir,
                                      policy=self.policy)
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
        for it in picked:
            aid = str(it["id"])
            item_kind = it.get("type") or "article"
            pre_title = it.get("title") or "(无标题)"
            pre_excerpt = it.get("excerpt") or ""
            pl, badge = self._build_local_payload(aid, pre_title, "", kind=item_kind)
            payloads[aid] = pl
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
                "comment_count": it.get("comment_count") or 0,
                "scheme_badge": badge,
                "has_backup": self._has_backup(aid, kind=item_kind),
                "status": "waiting",
                "message": f"本地就绪 · 方案：{badge[:24]}",
                "duration": 0.0,
                "confirmed": True,
                "hits_added": None,
            })

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
            self.notice = (f"📥 已从知乎本地直接拉取第 {start + 1}~{start + len(rows)} 篇（共 {total_n} 篇），"
                           f"并套用方案【{rows[0]['scheme_badge']}】。确认无误后点下方「确认并执行」即可写入！")
        return {"ok": True, "count": len(rows), "total": len(self.local_cache), "note": self.notice}

    # ---------------- 云端薄封装（保留云端工作台任务协同） ---------------- #

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
        """从云端领取一批任务；若云端未算好建议稿，自动用本机当前所选方案（四书五经/法律/自定义）补齐！"""
        job = self.ensure_job()
        if not job:
            # 若云端没有待办任务，自动回退到本地直接拉取知乎文章，绝不让用户扑空！
            if self.cookie:
                res = self.load_local_articles(kind="article", reset=False)
                if res.get("ok"):
                    self.notice = "☁️ 云端暂无待办任务，已自动为您从知乎本地直接拉取一批文章！"
                    return res
            return {"ok": False,
                    "note": (f"云端暂无待执行任务。您可以直接点「📥 本地直接拉取知乎文章」在软件内直接选文章修改，"
                             f"或去云端工作台 {self.workbench_url()} 创建任务。")}
        items = job.get("items", [])
        todo = [it for it in items
                if it.get("type") == "article" and it.get("status") == "pending"]
        if not todo:
            return {"ok": False, "note": "云端任务里已经没有待处理的文章了，可点「📥 本地直接拉取知乎文章」。"}
        picked = todo[: self.batch]

        rows: List[Dict[str, Any]] = []
        payloads: Dict[str, Dict[str, Any]] = {}
        for it in picked:
            aid = str(it["id"])
            pl = self.cp.payload(job["job_id"], aid) or {}
            pre_t = pl.get("pre_title") or it.get("title_before") or it.get("title") or ""
            badge = "☁️ 云端建议稿"
            if not pl.get("title"):
                # 自动用本机选择的方案生成，无需等待云端补算！
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
                "message": f"就绪 · {badge[:26]}" if ready else "云端还没算好这篇的建议稿",
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
            self.notice = f"☁️ 已领取云端任务 {job['job_id']}（本批 {len(rows)} 篇）。您也可以在上方直接切换为法律条文、四书五经或自定义内容！"
        return {"ok": True, "count": len(rows), "ready": len(payloads), "job_id": job["job_id"]}

    def prepare(self) -> Dict[str, Any]:
        job = self.ensure_job()
        if not job:
            return {"ok": False, "note": "当前为纯本地模式，直接点上方「⚡ 应用此方案到当前列表」即可在本地生成修改稿！"}
        res = self.cp._req(  # noqa: SLF001
            "POST", "/api/qy/prepare",
            json={"job_id": job["job_id"], "cookie": self.cookie,
                  "limit": self.batch}, timeout=600) or {}
        if not res.get("ok"):
            return {"ok": False, "note": res.get("note") or "云端补算失败"}
        self.notice = (f"云端补算完成：新算 {res.get('prepared', 0)} 篇，"
                       f"无需改动 {res.get('skipped', 0)} 篇，"
                       f"失败 {res.get('failed', 0)} 篇。")
        return {"ok": True, "note": self.notice}

    # ---------------- 确认 → 并发写入知乎 ---------------- #

    def confirm(self, ids: List[str]) -> Dict[str, Any]:
        if not self.cookie:
            return {"ok": False, "note": "请先在页面顶部粘贴或载入知乎 Cookie！"}
        ids = [str(i) for i in ids]
        with self._lock:
            if not self.job:
                return {"ok": False, "note": "还没有待处理列表，请先点「📥 本地直接拉取知乎文章」或「☁️ 从云端领取」。"}
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
        rec.setdefault("duration", round(time.time() - t0, 2))

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
            return {"ok": False, "note": "当前为本地直连修改模式，写入阶段已完成线上回读核验。"}
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
<title>清一新教育 · 本地文章自定义修改工作台 v2.0</title>
<style>
  :root{
    --bg:#f4f6f9; --card:#ffffff; --ink:#111827; --sub:#6b7280; --line:#e5e7eb;
    --brand:#b91c1c; --brand-soft:#fef2f2; --ok:#0f766e; --ok-soft:#ecfdf5;
    --warn:#b45309; --warn-soft:#fffbeb; --bad:#b91c1c; --bad-soft:#fef2f2;
    --blue:#1d4ed8; --blue-soft:#eff6ff; --purple:#6d28d9; --purple-soft:#f5f3ff;
  }
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--ink);
    font:14px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif}
  .wrap{max-width:1080px;margin:0 auto;padding:20px 18px 130px}
  header{display:flex;align-items:center;gap:12px;flex-wrap:wrap;margin-bottom:14px;
    background:#fff;padding:14px 18px;border-radius:14px;border:1px solid var(--line);
    box-shadow:0 1px 3px rgba(0,0,0,.03)}
  .brand{font-size:18px;font-weight:800;letter-spacing:.3px;display:flex;align-items:center;gap:8px}
  .brand i{display:inline-block;width:10px;height:10px;border-radius:50%;background:var(--brand)}
  .vbadge{font-size:11.5px;background:var(--brand-soft);color:var(--brand);padding:2px 8px;
    border-radius:999px;font-weight:700;border:1px solid #fecaca}
  .meta{margin-left:auto;color:var(--sub);font-size:12.5px;text-align:right;line-height:1.65}
  .meta b{color:var(--ink)}

  .panel{background:#fff;border:1px solid var(--line);border-radius:14px;padding:16px 18px;
    margin-bottom:14px;box-shadow:0 1px 3px rgba(0,0,0,.02)}
  .panel-head{display:flex;align-items:center;justify-content:space-between;gap:10px;
    flex-wrap:wrap;margin-bottom:10px}
  .panel-title{font-size:15px;font-weight:700;display:flex;align-items:center;gap:8px}

  .tabs{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:12px}
  .tab{padding:8px 14px;border-radius:10px;border:1px solid var(--line);background:#f9fafb;
    color:#374151;font-weight:600;font-size:13px;cursor:pointer;transition:.15s}
  .tab:hover{border-color:#cbd5e1;background:#f3f4f6}
  .tab.active{background:var(--brand);color:#fff;border-color:var(--brand);
    box-shadow:0 2px 6px rgba(185,28,28,.18)}

  .form-row{display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin-bottom:10px}
  select,input[type=text],textarea{font:inherit;border:1px solid #d1d5db;border-radius:10px;
    padding:8px 12px;background:#fff;color:var(--ink);outline:none;transition:.15s}
  select:focus,input[type=text]:focus,textarea:focus{border-color:var(--brand);
    box-shadow:0 0 0 3px rgba(185,28,28,.1)}
  textarea{width:100%;min-height:110px;resize:vertical;line-height:1.55}
  .preview-box{background:#f8fafc;border:1px dashed #cbd5e1;border-radius:10px;
    padding:10px 14px;font-size:13px;color:#334155;max-height:180px;overflow-y:auto}
  .preview-box h4{margin:0 0 6px;color:#0f172a;font-size:14px}

  .bar{display:flex;align-items:center;gap:9px;flex-wrap:wrap;margin-bottom:12px;
    background:#fff;padding:12px 16px;border-radius:14px;border:1px solid var(--line)}
  button{font:inherit;border-radius:10px;padding:8px 14px;cursor:pointer;
    border:1px solid var(--line);background:#fff;color:var(--ink);transition:.15s;font-weight:500}
  button:hover:not(:disabled){border-color:#cbd5e1;background:#f9fafb}
  button:disabled{opacity:.45;cursor:not-allowed}
  button.primary{background:var(--brand);border-color:var(--brand);color:#fff;font-weight:600}
  button.primary:hover:not(:disabled){background:#991b1b}
  button.blue{background:var(--blue);border-color:var(--blue);color:#fff;font-weight:600}
  button.blue:hover:not(:disabled){background:#1e40af}
  button.green{background:var(--ok);border-color:var(--ok);color:#fff;font-weight:600}
  button.green:hover:not(:disabled){background:#115e59}
  button.sm{padding:4px 10px;font-size:12px;border-radius:8px}

  .chk{display:flex;align-items:center;gap:6px;color:var(--sub);font-size:13px;cursor:pointer;user-select:none}
  .spacer{flex:1}
  .notice{background:var(--blue-soft);border:1px solid #bfdbfe;color:#1e3a8a;
    border-radius:11px;padding:10px 14px;font-size:13.5px;margin-bottom:12px;display:none;font-weight:500}

  .list{display:flex;flex-direction:column;gap:10px}
  .card{background:var(--card);border:1px solid var(--line);border-radius:14px;
    padding:14px 16px;display:flex;gap:13px;align-items:flex-start;transition:.15s}
  .card.sel{border-color:var(--brand);box-shadow:0 0 0 3px rgba(185,28,28,.07)}
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
  .bdiff em{font-style:normal;color:var(--brand);font-weight:700}
  .stat{margin-top:9px;font-size:12.5px;color:var(--sub);display:flex;gap:10px;align-items:center;flex-wrap:wrap}
  .pill{display:inline-block;padding:2px 9px;border-radius:999px;font-size:12px;font-weight:600}
  .p-waiting{background:#f1f5f9;color:#334155}
  .p-unprepared{background:var(--warn-soft);color:var(--warn)}
  .p-queued,.p-running{background:var(--blue-soft);color:var(--blue)}
  .p-done{background:var(--ok-soft);color:var(--ok)}
  .p-skipped{background:#f1f5f9;color:#475569}
  .p-failed,.p-saved_not_published{background:var(--bad-soft);color:var(--bad)}
  .spin{display:inline-block;width:11px;height:11px;border:2px solid #bfdbfe;
    border-top-color:var(--blue);border-radius:50%;animation:sp .8s linear infinite;
    vertical-align:-1px;margin-right:5px}
  @keyframes sp{to{transform:rotate(360deg)}}

  .edit-drawer{margin-top:10px;background:#fffbeb;border:1px solid #fde68a;border-radius:10px;
    padding:12px;display:none}
  .foot{position:fixed;left:0;right:0;bottom:0;background:rgba(255,255,255,.96);
    backdrop-filter:blur(8px);border-top:1px solid var(--line);padding:12px 18px;
    display:flex;align-items:center;gap:12px;justify-content:center;z-index:50}
  .foot .inner{max-width:1080px;width:100%;display:flex;align-items:center;gap:12px}
  .foot .cnt{color:var(--sub);font-size:13.5px}
  .foot .cnt b{color:var(--brand);font-size:16px}
  .verify{margin-top:16px;background:#fff;border:1px solid var(--line);border-radius:12px;
    padding:13px 16px;font-size:13px;display:none}
  .empty{text-align:center;color:var(--sub);padding:42px 20px;font-size:14px;
    background:#fff;border-radius:14px;border:1px dashed #cbd5e1}
  a{color:var(--blue);text-decoration:none}
  a:hover{text-decoration:underline}
  .tpl-chips{display:flex;gap:6px;flex-wrap:wrap;margin-bottom:8px}
</style>
</head>
<body>
<div class="wrap">
  <header>
    <div class="brand">
      <i></i>清一新教育 · 本地可视化修改工作台
      <span class="vbadge">v2.0 支持四书五经 / 法律条文 / 自定义内容</span>
    </div>
    <div class="meta" id="meta">正在检测登录凭证…</div>
  </header>

  <!-- 1. 知乎登录凭证（Cookie）手动/自动/云端配置面板 -->
  <div class="panel" id="cookiePanel">
    <div class="panel-head">
      <div class="panel-title">
        🔑 1. 知乎登录凭证（Cookie）配置
        <span id="cookieBadge" class="pill p-waiting">检测中</span>
      </div>
      <div style="display:flex;gap:8px;flex-wrap:wrap">
        <button class="sm" id="btnToggleCookie">展开/收起手动输入</button>
        <button class="sm" id="btnCloudCookie">☁️ 从云端凭证柜载入</button>
        <button class="sm" id="btnAutoCookie">🔍 自动读取本机浏览器</button>
        <a href="__SERVER__/api/qy/console" target="_blank" style="font-size:12.5px;align-self:center;margin-left:4px">打开云端工作台 ↗</a>
      </div>
    </div>
    <div id="cookieDrawer" style="display:none;margin-top:8px">
      <div style="font-size:12.5px;color:var(--sub);margin-bottom:6px">
        💡 <b>无需关闭 Edge / Chrome 浏览器！</b>您可直接将知乎 Cookie 粘贴在下方（获取方式：浏览器打开 zhihu.com → 按 F12 → Network → 点任意请求复制 Request Headers 中的 <code>Cookie</code> 整行或 <code>z_c0=...</code>），保存后自动写入本机 <code>cookie.txt</code>：
      </div>
      <div class="form-row">
        <input type="text" id="cookieInput" placeholder="在此粘贴知乎 Cookie（例如：z_c0=2|1:0|10:... 或整串 Cookie）" style="flex:1">
        <button class="primary" id="btnSaveCookie">💾 保存并连接知乎账号</button>
      </div>
    </div>
  </div>

  <!-- 2. 软件内修改内容与预设选择台（四书五经 / 法律条文 / 自定义内容 / 品牌署名） -->
  <div class="panel">
    <div class="panel-head">
      <div class="panel-title">📝 2. 选择或自定义修改内容（直接在软件内设置标题与正文）</div>
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

  <!-- 3. 文章拉取与批量操作栏 -->
  <div class="bar">
    <button class="blue" id="btnLoadLocal">📥 本地直接拉取知乎文章</button>
    <select id="selLocalKind" title="选择拉取内容类型">
      <option value="article">仅拉取专栏文章</option>
      <option value="answer">仅拉取知乎回答</option>
      <option value="all">文章 + 回答全部</option>
    </select>
    <input type="text" id="inpKeyword" placeholder="按标题关键词/ID筛选（可选）" style="width:190px;padding:7px 10px">
    <button id="btnFetch" title="从云端工作台领取已编排的任务">☁️ 从云端任务领取</button>
    <button id="btnBackupBatch" title="将当前列表的线上原文备份到本机">📦 备份本批原文</button>
    <label class="chk"><input type="checkbox" id="all" checked> 全选可执行</label>
    <span class="spacer"></span>
    <span class="cnt" id="topcnt" style="font-size:12.5px;color:var(--sub)"></span>
  </div>

  <div class="notice" id="notice"></div>
  <div class="list" id="list">
    <div class="empty">
      👋 欢迎使用本地修改工作台！<br>
      第 1 步：确认顶部知乎 Cookie 已连接（可手动粘贴或从云端载入）；<br>
      第 2 步：在「修改内容设置」中选择<b>四书五经（如《大学》）</b>、<b>法律条文（如《宪法》）</b>或<b>直接填写自定义内容</b>；<br>
      第 3 步：点击 <b>「📥 本地直接拉取知乎文章」</b> 预览修改效果，勾选后点底部 <b>「确认并执行」</b> 即可写入！
    </div>
  </div>
  <div class="verify" id="verify"></div>
</div>

<div class="foot">
  <div class="inner">
    <span class="cnt">已勾选 <b id="seln">0</b> 篇 · 单批并发上限 __BATCH__ 篇（每篇写入前自动本地备份，随时可还原）</span>
    <span class="spacer"></span>
    <button id="btnVerify" style="margin-right:6px">云端复核</button>
    <button class="primary" id="btnGo" disabled>确认并执行修改</button>
  </div>
</div>

<script>
var CATALOG = __CATALOG_JSON__;
var S = null;
var busy = false;
var activeTab = "classics";

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

var LABEL = {waiting:"待你勾选确认", unprepared:"待生成方案", queued:"排队写入中",
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
    body = '<div class="bdiff"><b>新正文预览</b>（约 '+esc(r.body_len_after||0)+' 字）：' + hlBody(r.body_excerpt) + '</div>';
  } else if(r.ready){
    body = '<div class="bdiff">正文保持不变（仅修改标题）</div>';
  }

  var extra = "";
  if(r.voteup_count!=null || r.comment_count!=null){
    extra += '<span>👍 '+esc(r.voteup_count||0)+' · 💬 '+esc(r.comment_count||0)+'</span>';
  }
  if(r.message) extra += '<span>'+esc(r.message)+'</span>';
  if(r.duration) extra += '<span>耗时 '+esc(r.duration)+'s</span>';
  if(r.has_backup){
    extra += '<span style="color:#0f766e;font-weight:600">📦 已备份原文</span>';
    extra += '<button class="sm" data-restore="'+esc(r.id)+'">⏪ 还原原文</button>';
  }
  extra += '<button class="sm" data-edit-toggle="'+esc(r.id)+'">✏️ 单独编辑此篇</button>';
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

function render(){
  if(!S) return;
  var info = S.info || {};
  var badge = document.getElementById("cookieBadge");
  var drawer = document.getElementById("cookieDrawer");

  if(S.has_cookie && info.name && !info.error){
    badge.className = "pill p-done";
    badge.textContent = "🟢 已连接：" + info.name + " (" + (info.url_token||"") + ")";
  } else {
    badge.className = "pill p-failed";
    badge.textContent = "🔴 未配置或需更新 Cookie（请手动粘贴）";
    if(!drawer.hasAttribute("data-user-toggled")){
      drawer.style.display = "block";
    }
  }

  var meta = '账号 <b>'+esc(info.name||"未连接")+'</b> · 专栏文章 <b>'+esc(info.articles==null?"-":info.articles)+
    '</b> 篇 · 回答 <b>'+esc(info.answers==null?"-":info.answers)+'</b> 条'+
    ' · 今日剩余额度 <b>'+esc(S.daily.remaining)+'</b>/'+esc(S.daily.limit||"不限")+' 篇'+
    '<br>本机节点 '+esc(info.worker_id||"")+' · 当前批次 '+esc(S.job_id||"未拉取");
  if(info.error){
    meta = '<span style="color:#b91c1c;font-weight:600">提示：'+esc(info.error)+'</span><br>'+meta;
  }
  document.getElementById("meta").innerHTML = meta;

  var n = document.getElementById("notice");
  if(S.notice){ n.style.display="block"; n.textContent = S.notice; }
  else n.style.display="none";

  // 避免用户在单篇编辑框输入时被轮询冲掉
  var anyEditing = document.querySelector(".edit-drawer[data-open='1']");
  if(!anyEditing){
    var list = document.getElementById("list");
    if(!S.rows || !S.rows.length){
      list.innerHTML = '<div class="empty">' +
        '👋 当前列表为空。<br>请点击上方 <b>「📥 本地直接拉取知乎文章」</b>（直接读取您的知乎文章并生成四书五经/法律条文/自定义内容预览），或点 <b>「☁️ 从云端任务领取」</b>。' +
        '</div>';
    } else {
      list.innerHTML = S.rows.map(cardHtml).join("");
    }
  }

  var sel = S.rows ? S.rows.filter(function(r){return r.confirmed && (r.status==="waiting"||r.status==="failed"||r.status==="skipped");}).length : 0;
  document.getElementById("seln").textContent = sel;
  document.getElementById("topcnt").textContent =
    S.rows && S.rows.length ? ("当前显示 "+S.rows.length+" 篇（总库 "+(S.local_total||S.rows.length)+" 篇） · 已完成 "+S.done+" · 失败 "+S.failed) : "";
  var go = document.getElementById("btnGo");
  go.disabled = busy || !sel;
  go.textContent = "确认并执行修改（"+sel+" 篇）";

  var v = document.getElementById("verify");
  if(S.verify && S.verify.ok && S.verify.summary){
    var s = S.verify.summary;
    var html = '<h4>云端独立复核（云端回读线上文章）</h4>校验 '+s.checked+
      ' 篇 · 通过 '+s.passed+' · 不通过 '+s.failed+' · 未校验 '+s.skipped;
    var ds = (S.verify.details||[]).filter(function(d){return d.verify==="fail";});
    if(ds.length){
      html += ds.map(function(d){
        return '<div class="r">✗ '+esc(d.id)+'：'+esc((d.reasons||[]).join("；"))+'</div>';
      }).join("");
    }
    v.style.display = "block"; v.innerHTML = html;
  } else if(S.verify && S.verify.note){
    v.style.display="block"; v.innerHTML = '<h4>复核结果</h4>'+esc(S.verify.note);
  } else { v.style.display="none"; }
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

// ---- 事件绑定 ----
document.getElementById("btnToggleCookie").onclick = function(){
  var d = document.getElementById("cookieDrawer");
  d.setAttribute("data-user-toggled", "1");
  d.style.display = (d.style.display === "none") ? "block" : "none";
};

document.getElementById("btnSaveCookie").onclick = function(){
  var ck = document.getElementById("cookieInput").value.trim();
  if(!ck){ alert("请先在输入框内粘贴知乎 Cookie！"); return; }
  call("/api/cookie", {cookie: ck}).then(function(res){
    if(res && res.ok){
      document.getElementById("cookieDrawer").style.display = "none";
    }
  });
};

document.getElementById("btnCloudCookie").onclick = function(){
  call("/api/cookie/cloud", {});
};

document.getElementById("btnAutoCookie").onclick = function(){
  call("/api/cookie/auto", {});
};

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

document.getElementById("btnLoadLocal").onclick = function(){
  call("/api/load-local", {
    kind: document.getElementById("selLocalKind").value,
    keyword: document.getElementById("inpKeyword").value
  });
};

document.getElementById("btnFetch").onclick = function(){ call("/api/fetch", {}); };
document.getElementById("btnBackupBatch").onclick = function(){ call("/api/backup-batch", {}); };
document.getElementById("btnVerify").onclick = function(){ call("/api/verify", {}); };

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
  var restoreId = t.getAttribute("data-restore");
  if(restoreId){
    if(!confirm("确认用本机备份还原文章 "+restoreId+" 的原始标题与正文？")) return;
    call("/api/restore-one", {id: restoreId});
  }
});

initCatalogUI();
poll();
setInterval(poll, 1500);
</script>
</body>
</html>
"""


class _Handler(BaseHTTPRequestHandler):
    client: Client = None  # type: ignore[assignment]
    server_version = "QingyiClient/" + CLIENT_VERSION

    def log_message(self, fmt, *args):  # noqa: A003
        pass

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
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
        self._json({"ok": False, "note": "not found"}, 404)

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        body = self._read_json()
        try:
            if path == "/api/cookie":
                self._json(self.client.set_cookie(body.get("cookie") or ""))
            elif path == "/api/cookie/cloud":
                self._json(self.client.load_cookie_from_cloud())
            elif path == "/api/cookie/auto":
                self._json(self.client.auto_detect_browser_cookie())
            elif path == "/api/scheme":
                self._json(self.client.update_scheme(body))
            elif path == "/api/edit-row":
                self._json(self.client.edit_single_row(
                    str(body.get("id") or ""),
                    str(body.get("title") or ""),
                    str(body.get("content") or ""),
                ))
            elif path == "/api/load-local":
                self._json(self.client.load_local_articles(
                    kind=str(body.get("kind") or "article"),
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
        description="清一新教育 · 用户端（本地可视化修改工作台：支持四书五经/法律条文/自定义内容）")
    ap.add_argument("--server", default="https://zh.samuraiguan.cloud")
    ap.add_argument("--key", default="guanjun2026", help="站点密钥（默认 guanjun2026）")
    ap.add_argument("--cookie", default=None)
    ap.add_argument("--cookie-file", default="cookie.txt")
    ap.add_argument("--auto-cookie", action="store_true",
                    help="尝试自动读取本机浏览器里的知乎登录（失败不阻断启动）")
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

    print("=" * 68)
    print(f"  清一新教育 · 用户端本地修改工作台 v{CLIENT_VERSION}")
    print("  支持：四书五经（大学/中庸/论语/孟子/五经）· 国家法律条文 · 自定义修改内容")
    print("=" * 68)

    cookie = _read_cookie(args)
    if cookie:
        print("  [OK] 已从本机 cookie.txt 读取知乎登录凭证。")
    elif args.auto_cookie:
        print("  [*] 正在尝试读取本机浏览器登录态（若浏览器锁定将自动跳过，无需关闭浏览器）…")
        try:
            cookie, src = qe.auto_detect_cookie()
            cookie = _clean_cookie_str(cookie)
            if cookie:
                (Path(_HERE) / (args.cookie_file or "cookie.txt")).write_text(cookie + "\n", encoding="utf-8")
                print(f"  [OK] 已自动读取本机知乎登录（来源：{src}）")
        except Exception as exc:  # noqa: BLE001
            print(f"  [i] 浏览器自动检测跳过（{exc}）")

    # 每日上限：命令行没给就问云端要
    if args.per_day is None:
        try:
            cfg = qe.ControlPlane(args.server, key)._req(  # noqa: SLF001
                "GET", "/api/qy/config", quiet=True) or {}
            if cfg.get("per_day") is not None:
                args.per_day = int(cfg["per_day"])
        except Exception:  # noqa: BLE001
            pass

    cli = Client(args.server, key, cookie, cookie_file=args.cookie_file,
                 per_day=args.per_day, batch=args.batch,
                 backup_dir=args.backup_dir, daily_path=args.daily_file,
                 stagger=args.stagger)

    # 若本地没拿到 Cookie，自动尝试从云端凭证柜拉取一次
    if not cli.cookie:
        cres = cli.load_cookie_from_cloud()
        if cres.get("ok"):
            print("  [OK] 已自动从云端凭证柜载入知乎登录态！")

    if cli.cookie:
        dep = cli.deposit_credential()
        try:
            info = cli.preflight()
            print(f"  账号        : {info.get('name')} ({info.get('url_token')})")
            print(f"  文章 / 回答 : {info.get('articles')} 篇 / {info.get('answers')} 条")
            print(f"  凭证同步    : {'✓ ' if dep.get('ok') else '× '}{dep.get('note')}")
        except Exception as exc:  # noqa: BLE001
            cli.mark_startup_error(str(exc))
            print(f"  [!] 凭证自检未通过：{exc}（可在打开的网页顶部直接重新粘贴 Cookie）")
    else:
        cli.mark_startup_error("尚未配置知乎 Cookie，请在上方输入框直接粘贴 Cookie 或点「从云端凭证柜载入」。")
        print("  [i] 当前尚未配置 Cookie，已为您启动本地网页控制台。")
        print("      请在打开的网页顶部直接粘贴知乎 Cookie（或放入 cookie.txt），无需关闭浏览器！")

    print(f"  每日上限    : {cli.policy.per_day} 篇/天（当前 {cli.governor.daily.describe()}）")
    print(f"  同批并发    : 最多 {cli.batch} 篇")
    print(f"  本机备份    : {cli.backup_dir}")
    print(f"  云端工作台  : {cli.workbench_url()}")

    port = _pick_port(args.port)
    url = f"http://127.0.0.1:{port}/"
    print(f"  本地控制台  : {url}")
    print("=" * 68)
    print("  浏览器将自动打开本地控制台页面。按 Ctrl+C 可随时安全退出。")
    print()

    _Handler.client = cli
    httpd = ThreadingHTTPServer(("127.0.0.1", port), _Handler)
    if not args.no_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n用户端已安全退出。")
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
