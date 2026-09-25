#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""qyapp —— 清一新教育 · 本地文章工作台（零依赖 GUI）

为什么用 http.server + 浏览器，而不是 Tkinter：
  1. 零第三方依赖（tkinter 在部分 Python 发行版里反而缺失）；
  2. 前端可以用原生 HTML/CSS 做出 Claude 那种克制的观感；
  3. 图片预览、富文本、多栏布局浏览器天生就会，Tkinter 要手写。

安全设计（三条硬约束）：
  * 只监听 127.0.0.1 —— 不绑 0.0.0.0，局域网里的别人连不上。
  * 所有写操作要求 X-QY-Token 头 —— 防止你随便打开一个网页就被
    恶意 JS 往 localhost 发请求（CSRF）。
  * 默认**只读**：不加 --allow-write 时，任何写回知乎的接口直接拒绝。

启动：
    python3 local/qyapp.py                # 只读，自动开浏览器
    python3 local/qyapp.py --allow-write  # 允许写回知乎
"""
from __future__ import annotations

import argparse
import json
import mimetypes
import os
import re
import secrets
import sys
import threading
import time
import traceback
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence
from urllib.parse import parse_qs, unquote, urlparse

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent
for _p in (str(_HERE), str(_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

# 必须在任何 `import requests` 之前顶替掉它
import qynet  # noqa: E402

qynet.install()

import qystore as qs  # noqa: E402
import qycheck as qc  # noqa: E402
import qydocx as qd  # noqa: E402
import qyplane as qp  # noqa: E402
import qyai  # noqa: E402
import qyauth  # noqa: E402
import qyupdate  # noqa: E402

WEB_DIR = _HERE / "web"
EXPORT_DIR = _ROOT / "exports"
COOKIE_FILE = _ROOT / "cookie.txt"
DB_FILE = _ROOT / "data" / "qyedu.db"
IMG_CACHE = _ROOT / "data" / "imgcache"

APP_NAME = "清一新教育 · 文章工作台"
VERSION = "1.1.0"


def _now() -> int:
    return int(time.time())


def _hhmmss(ts: Optional[int]) -> str:
    if not ts:
        return ""
    return time.strftime("%m-%d %H:%M", time.localtime(ts))


# --------------------------------------------------------------------------- #
# 后台任务：长耗时操作不能卡住界面
# --------------------------------------------------------------------------- #

class TaskRunner:
    """极简后台任务表。前端拿 task_id 轮询 /api/task/<id>。"""

    def __init__(self) -> None:
        self._d: Dict[str, Dict[str, Any]] = {}
        self._lock = threading.RLock()

    def spawn(self, name: str, fn: Callable[[Dict[str, Any]], Any]) -> str:
        tid = uuid.uuid4().hex[:12]
        t: Dict[str, Any] = {
            "id": tid, "name": name, "state": "running",
            "done": 0, "total": 0, "current": "", "log": [],
            "result": None, "error": None, "t0": time.time(), "elapsed": 0.0,
        }
        with self._lock:
            self._d[tid] = t

        def _run() -> None:
            try:
                t["result"] = fn(t)
                t["state"] = "done"
            except Exception as exc:  # noqa: BLE001
                t["state"] = "error"
                t["error"] = f"{type(exc).__name__}: {exc}"
                t["log"].append(traceback.format_exc()[-1500:])
            finally:
                t["elapsed"] = round(time.time() - t["t0"], 1)
                t["current"] = ""

        threading.Thread(target=_run, daemon=True).start()
        return tid

    def say(self, t: Dict[str, Any], msg: str) -> None:
        with self._lock:
            t["log"].append(f"{time.strftime('%H:%M:%S')}  {msg}")
            if len(t["log"]) > 500:
                del t["log"][:-400]

    def step(self, t: Dict[str, Any], i: int, total: int,
             current: str = "") -> None:
        with self._lock:
            t["done"] = i
            t["total"] = total
            t["current"] = current

    def get(self, tid: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            t = self._d.get(tid)
            if not t:
                return None
            out = dict(t)
            out["log"] = list(t["log"])[-120:]
            return out

    def reap(self, keep: int = 40) -> None:
        with self._lock:
            if len(self._d) <= keep:
                return
            order = sorted(self._d.values(), key=lambda x: x["t0"])
            for t in order[:-keep]:
                self._d.pop(t["id"], None)


# --------------------------------------------------------------------------- #
# 工作台本体
# --------------------------------------------------------------------------- #

class Workbench:
    def __init__(self, allow_write: bool = False,
                 db: Optional[Path] = None,
                 server_url: str = "https://zh.samuraiguan.cloud") -> None:
        self.allow_write = bool(allow_write)
        self.server_url = server_url.rstrip("/")
        self.store = qs.Store(db or DB_FILE)
        self.tasks = TaskRunner()
        self.token = secrets.token_urlsafe(24)
        self._plane: Optional[qp.LocalPlane] = None
        self._lock = threading.RLock()
        self._fetcher = qd.ImageFetcher(cache_dir=IMG_CACHE)
        self._cookie = self._read_cookie()
        EXPORT_DIR.mkdir(parents=True, exist_ok=True)
        IMG_CACHE.mkdir(parents=True, exist_ok=True)

    # ---------------- cookie / 会话 ---------------- #

    def _read_cookie(self) -> str:
        try:
            if COOKIE_FILE.exists():
                return qyauth.clean_cookie_str(COOKIE_FILE.read_text(encoding="utf-8").strip())
        except Exception:  # noqa: BLE001
            pass
        return ""

    def set_cookie(self, cookie: str) -> Dict[str, Any]:
        cookie = qyauth.clean_cookie_str(cookie)
        if not cookie:
            return {"ok": False, "error": "Cookie 为空"}
        if "z_c0" not in cookie:
            return {"ok": False,
                    "error": "这段文本里没有 z_c0，多半不是知乎的 Cookie"}
        with self._lock:
            qyauth.save_cookie_to_disk(cookie)
            try:
                os.chmod(COOKIE_FILE, 0o600)
            except Exception:  # noqa: BLE001
                pass
            self._cookie = cookie
            self._plane = None
        return {"ok": True}

    # ---------------- 智能免 F12 登录 ---------------- #

    def auth_auto_detect(self, close_browser: bool = False) -> Dict[str, Any]:
        """一键从 Edge/Chrome 自动读取知乎登录态。"""
        res = qyauth.auto_detect_browser_cookie(close_browser=close_browser)
        if res.get("ok") and res.get("cookie"):
            set_res = self.set_cookie(res["cookie"])
            if set_res.get("ok"):
                try:
                    res["account"] = self.plane().account()
                except Exception as exc:
                    res["account"] = {"error": str(exc)[:200]}
        return res

    def auth_qr_start(self) -> Dict[str, Any]:
        """生成知乎官方扫码登录二维码。"""
        return qyauth.start_qr_login()

    def auth_qr_poll(self, token: str) -> Dict[str, Any]:
        """轮询知乎扫码状态。"""
        res = qyauth.poll_qr_login(token)
        if res.get("ok") and res.get("status") == "success" and res.get("cookie"):
            set_res = self.set_cookie(res["cookie"])
            if set_res.get("ok"):
                try:
                    res["account"] = self.plane().account()
                except Exception as exc:
                    res["account"] = {"error": str(exc)[:200]}
        return res

    def auth_cloud_sync(self) -> Dict[str, Any]:
        """从云端凭证柜同步知乎 Cookie。"""
        res = qyauth.sync_cloud_credential(self.server_url)
        if res.get("ok") and res.get("cookie"):
            set_res = self.set_cookie(res["cookie"])
            if set_res.get("ok"):
                try:
                    res["account"] = self.plane().account()
                except Exception as exc:
                    res["account"] = {"error": str(exc)[:200]}
        return res

    # ---------------- 远程检查更新与自动热更新 ---------------- #

    def check_update(self) -> Dict[str, Any]:
        return qyupdate.check_remote_update(VERSION, self.server_url)

    def apply_update(self, download_url: str, t: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        def _cb(pct: int, msg: str) -> None:
            if t:
                self.tasks.step(t, pct, 100, msg)
                self.tasks.say(t, msg)
        return qyupdate.perform_background_update(download_url, progress_cb=_cb)

    def plane(self) -> qp.LocalPlane:
        with self._lock:
            if self._plane is None:
                if not self._cookie:
                    raise RuntimeError(
                        "还没有登录。请点击左下角「登录认证」使用自动读取或扫码登录。")
                self._plane = qp.LocalPlane(
                    self._cookie, store=self.store,
                    backup_dir=_ROOT / "data" / "qyedu_backup")
            return self._plane

    # ---------------- 状态 ---------------- #

    def status(self) -> Dict[str, Any]:
        st = self.store.stats()
        acct: Dict[str, Any] = {}
        if self._cookie:
            try:
                acct = self.plane().account() or {}
            except Exception as exc:  # noqa: BLE001
                acct = {"error": str(exc)[:200]}
        return {
            "app": APP_NAME, "version": VERSION,
            "server_url": self.server_url,
            "remote_connected": True,
            "logged_in": bool(self._cookie),
            "cookie_len": len(self._cookie),
            "account": acct,
            "stats": st,
            "db_path": str(self.store.path),
            "db_bytes": st.get("db_bytes", 0),
            "export_dir": str(EXPORT_DIR),
            "allow_write": self.allow_write,
            "backend": qp.BACKEND,
            "config": self.plane().config() if self._cookie else {},
            "rules": len(qc.RULES),
            "web_dir": str(WEB_DIR),
        }

    # ---------------- 列表 ---------------- #

    def articles(self, only: str = "", q: str = "", order: str = "",
                 limit: int = 0) -> List[Dict[str, Any]]:
        rows = self.store.list_documents(
            kind=None, order=order or "updated_at DESC", limit=0, only=only or None)
        out: List[Dict[str, Any]] = []
        ql = (q or "").strip().lower()
        for r in rows:
            title = r.get("title_now") or ""
            if ql and ql not in title.lower() and ql not in str(r.get("doc_id")):
                continue
            snap = self.store.latest_snapshot(str(r["doc_id"]))
            chk = self.store.latest_check(str(r["doc_id"]))
            out.append({
                "doc_id": r["doc_id"], "kind": r.get("kind"),
                "title": title,
                "title_original": r.get("title_original"),
                "has_brand": "清一新教育" in title,
                "image_count": r.get("image_count"),
                "body_len": r.get("body_len"),
                "check_status": r.get("check_status") or "unchecked",
                "check_hits": r.get("check_hits") or 0,
                "upload_status": r.get("upload_status") or "untouched",
                "synced_at": r.get("synced_at"),
                "synced_text": _hhmmss(r.get("synced_at")),
                "has_body": bool(snap),
                "updated_text": _hhmmss(r.get("updated_at")),
                "note": r.get("note") or "",
                "block": (chk or {}).get("level") == "block",
            })
        if limit:
            out = out[:limit]
        return out

    # ---------------- 单篇详情 ---------------- #

    def article(self, doc_id: str) -> Dict[str, Any]:
        doc = self.store.get_document(doc_id)
        if not doc:
            raise KeyError(f"本地库里没有这篇：{doc_id}")
        snap = self.store.latest_snapshot(doc_id)
        orig = self.store.latest_snapshot(doc_id, "original")
        title = (snap or {}).get("title") or doc.get("title_now") or ""
        body = (snap or {}).get("content") or ""
        imgs = qs.image_manifest(body)
        chk = self.store.latest_check(doc_id)
        return {
            "doc": dict(doc),
            "title": title,
            "body_html": body,
            "text": qc.to_text(body),
            "has_body": bool(snap),
            "snapshot_at": _hhmmss((snap or {}).get("taken_at")),
            "snapshot_label": (snap or {}).get("label"),
            "original": {
                "title": (orig or {}).get("title") or doc.get("title_original"),
                "body_html": (orig or {}).get("content") or "",
                "at": _hhmmss((orig or {}).get("taken_at")),
                "exists": bool(orig),
            },
            "images": imgs,
            "image_count": len(imgs),
            "check": chk,
            "checks": self.store.list_checks(doc_id, 5),
            "revisions": self.store.list_revisions(doc_id, 200),
            "timeline": self.store.timeline(doc_id, 60),
            "consistency": self.store.consistency_report(doc_id),
            "url": doc.get("url") or f"https://zhuanlan.zhihu.com/p/{doc_id}",
        }

    # ---------------- 敏感检查 ---------------- #

    def check(self, doc_ids: Sequence[str], t: Optional[Dict[str, Any]] = None
              ) -> Dict[str, Any]:
        ids = [str(i) for i in doc_ids]
        agg: Dict[str, Any] = {"block": 0, "warn": 0, "pass": 0,
                               "items": [], "errors": []}
        for i, did in enumerate(ids, 1):
            if t:
                self.tasks.step(t, i, len(ids), did)
            try:
                snap = self.store.latest_snapshot(did)
                if not snap:
                    agg["errors"].append(f"{did}：本地没有正文，请先同步正文")
                    continue
                title = snap.get("title") or ""
                body = snap.get("content") or ""
                findings = qc.scan(title, body)
                rep = qc.report(findings)
                self.store.add_check(did, "rule", rep["level"],
                                     rep["findings"])
                lvl = rep["level"]
                agg[lvl] = agg.get(lvl, 0) + 1
                agg["items"].append({
                    "doc_id": did, "level": lvl,
                    "block": rep["block"], "warn": rep["warn"],
                    "info": rep["info"], "hits": rep["hits"],
                    "by_cat": rep["by_cat"], "note": rep.get("note", ""),
                })
                if t:
                    self.tasks.say(
                        t, f"{lvl.upper():5s} {title[:34]} "
                           f"（阻断 {rep['block']} / 提醒 {rep['warn']}）")
            except Exception as exc:  # noqa: BLE001
                agg["errors"].append(f"{did}：{exc}")
                if t:
                    self.tasks.say(t, f"失败 {did}：{exc}")
        return agg

    def check_one(self, doc_id: str) -> Dict[str, Any]:
        return self.check([doc_id])

    # ---------------- AI 提示词 / 回填 ---------------- #

    def ai_prompt(self, doc_id: str, max_chars: int = 60000) -> Dict[str, Any]:
        snap = self.store.latest_snapshot(doc_id)
        if not snap:
            raise RuntimeError("本地没有正文，请先同步正文")
        prompt = qc.build_ai_prompt(snap.get("title") or "",
                                    snap.get("content") or "", max_chars)
        return {"doc_id": doc_id, "prompt": prompt, "chars": len(prompt)}

    def ai_ingest(self, doc_id: str, reply: str) -> Dict[str, Any]:
        """接收用户自己 AI 的回复，解析 + 与规则引擎交叉核对。"""
        snap = self.store.latest_snapshot(doc_id)
        if not snap:
            raise RuntimeError("本地没有正文，请先同步正文")
        title = snap.get("title") or ""
        body = snap.get("content") or ""

        parsed = qc.parse_ai_reply(reply, title, body)
        if not parsed.get("ok"):
            raise RuntimeError(parsed.get("note") or "AI 回话无法解析")
        ai_findings: List[Dict[str, Any]] = list(parsed.get("findings") or [])
        dropped = int(parsed.get("dropped") or 0)

        rule_rep = self.store.latest_check(doc_id) or {}
        rule_findings: List[Dict[str, Any]] = list(rule_rep.get("findings") or [])
        if not rule_findings:
            rule_findings = [f.to_dict() for f in qc.scan(title, body)]

        cc = qc.cross_check(rule_findings, ai_findings)

        # 合并进检查记录：规则命中 + AI 独有命中（去重按 字段+位置）
        merged = list(rule_findings)
        for a in ai_findings:
            dup = any(x.get("field") == a.get("field")
                      and abs(int(x.get("start") or 0)
                              - int(a.get("start") or 0)) <= 12
                      for x in rule_findings)
            if not dup:
                merged.append(dict(a, source="ai"))

        lvl = "pass"
        for f in merged:
            if qc._LEVEL_RANK.get(f.get("level") or "info", 0) > \
                    qc._LEVEL_RANK.get(lvl, 0):
                lvl = f.get("level")
        self.store.add_check(doc_id, "ai", lvl, merged)
        return {"doc_id": doc_id, "level": lvl,
                "ai_count": len(ai_findings),
                "ai_dropped": dropped,
                "rule_count": len(rule_findings),
                "merged_count": len(merged),
                "ai_note": parsed.get("note", ""),
                "cross": cc, "findings": merged}

    # ---------------- 双轮 AI 智能修润与假想敌质检 ---------------- #

    def ai_dual_round(self, doc_id: str, model: Optional[str] = None,
                      t: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        snap = self.store.latest_snapshot(doc_id)
        doc = self.store.get_document(doc_id) or {}
        title = (snap or {}).get("title") or doc.get("title_now") or ""
        body_html = (snap or {}).get("content") or ""
        if not body_html:
            raise RuntimeError("本地尚无该文章正文，请先在左侧点「同步正文」拉取")

        def _log(msg: str) -> None:
            if t:
                self.tasks.say(t, msg)

        res = qyai.run_dual_agent_pipeline(title, body_html, model=model, log_fn=_log)
        if not res.get("ok"):
            raise RuntimeError(res.get("error") or "AI 处理异常")

        # 记录到 revisions 表作为候选建议
        self.store.add_revision(
            doc_id, "ai_dual", "title",
            title, res["modified_title"],
            "AI_TITLE", "双轮AI修润标题", res.get("thinking", "")[:200], False
        )
        self.store.add_revision(
            doc_id, "ai_dual", "content",
            body_html[:4000], res["modified_body"][:4000],
            "AI_BODY", "双轮AI修润正文", res.get("verdict", "")[:200], False
        )
        return res

    def ai_apply(self, doc_id: str, title: str, body_html: str) -> Dict[str, Any]:
        """采纳双轮 AI 修润结果到本地草稿。"""
        snap = self.store.latest_snapshot(doc_id)
        old_title = (snap or {}).get("title") or ""
        old_body = (snap or {}).get("content") or ""

        self.store.add_revision(
            doc_id, "manual", "title",
            old_title, title,
            "AI_APPLY", "采纳双轮AI修润", "用户采纳双轮AI修润结果", True
        )
        self.store.add_revision(
            doc_id, "manual", "content",
            old_body[:4000], body_html[:4000],
            "AI_APPLY", "采纳双轮AI修润", "用户采纳双轮AI修润结果", True
        )
        self.store.upsert_document(
            {"id": str(doc_id), "type": "article", "title": title},
            body_html=body_html
        )
        self.store.add_snapshot(doc_id, title, body_html, "manual")
        return {"ok": True, "doc_id": doc_id, "title": title, "body_len": len(body_html)}

    # ---------------- 保存 / 写回 ---------------- #

    def save(self, doc_id: str, title: str, body_html: str,
             publish: bool = False,
             t: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """把编辑后的标题/正文写回知乎。**这是唯一的破坏性操作。**

        安全网：
          1. 写前存 `pre_upload` 快照（唯一，不覆盖）；
          2. 对比图片清单，丢了图就中止（除非 force）；
          3. 逐字记录 revisions，写完后回读复核。
        """
        if not self.allow_write:
            raise RuntimeError(
                "服务以只读模式启动。要写回知乎，请用 --allow-write 重启。")
        snap = self.store.latest_snapshot(doc_id)
        if not snap:
            raise RuntimeError("本地没有正文，请先同步正文再编辑")
        old_title = snap.get("title") or ""
        old_body = snap.get("content") or ""

        if old_title == title and old_body == body_html:
            return {"ok": True, "changed": False, "note": "内容没有变化"}

        img = qs.image_diff(old_body, body_html)
        if img["lost"]:
            raise RuntimeError(
                f"这次改动会丢掉 {len(img['lost'])} 张图片，已中止。"
                f"丢失的图：{', '.join(img['lost'][:3])}…"
                f"（如确要如此，请在界面上勾选「允许丢图」）")

        self.store.add_snapshot(doc_id, old_title, old_body, "pre_upload")
        if t:
            self.tasks.say(t, f"已存 pre_upload 快照（{len(old_body)} 字节）")

        revs: List[int] = []
        if old_title != title:
            revs.append(self.store.add_revision(
                doc_id, "manual", "title", old_title, title, "M01",
                "人工编辑标题", "", False))
        if old_body != body_html:
            revs.append(self.store.add_revision(
                doc_id, "manual", "content", old_body[:4000],
                body_html[:4000], "M02", "人工编辑正文", "", False))

        signer = self.plane()._reader("write")
        if t:
            self.tasks.say(t, "写入草稿…")
        ok, msg = signer.patch_draft(str(doc_id), title, body_html)
        if not ok:
            self.store.set_document_status(doc_id, upload_status="failed",
                                           note=msg[:200])
            raise RuntimeError(f"保存草稿失败：{msg}")

        if publish:
            if t:
                self.tasks.say(t, "发布…")
            ok2, msg2 = signer.publish_article(str(doc_id), title, body_html)
            if not ok2:
                self.store.set_document_status(doc_id, upload_status="failed",
                                               note=msg2[:200])
                raise RuntimeError(f"发布失败：{msg2}")

        self.store.mark_applied(revs)
        self.store.upsert_document(
            {"id": str(doc_id), "type": "article", "title": title},
            body_html=body_html)
        self.store.set_document_status(doc_id, upload_status="uploaded",
                                       upload_at=_now(), note="")
        if t:
            self.tasks.say(t, "写回成功，正在回读复核…")

        v = self._verify(doc_id, title, body_html)
        if t:
            self.tasks.say(t, "复核：" + ("通过" if v.get("ok") else
                                          f"不一致 —— {v.get('why')}"))
        return {"ok": True, "changed": True, "published": publish,
                "verify": v, "image_diff": img}

    def _verify(self, doc_id: str, title: str, body_html: str) -> Dict[str, Any]:
        """回读线上，逐项核对。不采信「本地说做完了」。"""
        try:
            signer = self.plane()._reader("read")
            d = signer.get_article_draft(str(doc_id))
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "why": f"回读失败：{exc}"}
        live_title = d.get("title") or ""
        live_body = d.get("content") or ""
        same_title = live_title == title
        same_body = qs.text_hash(live_body) == qs.text_hash(body_html)
        img = qs.image_diff(body_html, live_body)
        ok = same_title and same_body and not img["lost"]
        why = []
        if not same_title:
            why.append(f"标题不一致（线上「{live_title[:30]}」）")
        if not same_body:
            why.append("正文文本哈希不一致")
        if img["lost"]:
            why.append(f"线上少了 {len(img['lost'])} 张图")
        if ok:
            for r in self.store.list_revisions(doc_id, 20):
                if r.get("applied") and not r.get("verified"):
                    self.store.mark_verified(int(r["rev_id"]), True, "回读一致")
        return {"ok": ok, "why": "；".join(why), "live_title": live_title,
                "live_image_count": img["after_count"]}

    def restore(self, doc_id: str, push: bool = False) -> Dict[str, Any]:
        """还原到 original 快照。push=True 时同时写回知乎。"""
        orig = self.store.restore_original(str(doc_id))
        if not orig:
            raise RuntimeError("没有 original 快照可还原")
        if push:
            if not self.allow_write:
                raise RuntimeError("只读模式，不能写回。")
            signer = self.plane()._reader("write")
            ok, msg = signer.patch_draft(str(doc_id), orig["title"],
                                         orig["content"])
            if not ok:
                raise RuntimeError(f"还原写回失败：{msg}")
            signer.publish_article(str(doc_id), orig["title"], orig["content"])
            self.store.upsert_document(
                {"id": str(doc_id), "type": "article", "title": orig["title"]},
                body_html=orig["content"])
        return {"ok": True, "title": orig["title"],
                "body_len": len(orig["content"]), "pushed": push}

    # ---------------- 导出 Word（单篇） ---------------- #

    def export(self, doc_ids: Sequence[str], t: Optional[Dict[str, Any]] = None
               ) -> Dict[str, Any]:
        """每篇一个 .docx，不打包。高清原图。"""
        ids = [str(i) for i in doc_ids]
        done: List[Dict[str, Any]] = []
        fails: List[str] = []
        for i, did in enumerate(ids, 1):
            snap = self.store.latest_snapshot(did)
            doc = self.store.get_document(did) or {}
            title = (snap or {}).get("title") or doc.get("title_now") or did
            if t:
                self.tasks.step(t, i, len(ids), title[:40])
            if not snap:
                fails.append(f"{did}：本地无正文")
                continue
            safe = re.sub(r'[\\/:*?"<>|\s]+', "_", title).strip("_")[:60] or did
            path = EXPORT_DIR / f"{safe}.docx"
            n = 1
            while path.exists() and path.stat().st_size > 0 and n < 200:
                path = EXPORT_DIR / f"{safe} ({n}).docx"
                n += 1
            try:
                meta = {
                    "author": doc.get("author") or "清一新教育",
                    "url": doc.get("url") or
                           f"https://zhuanlan.zhihu.com/p/{did}",
                    "exported_at": time.strftime("%Y-%m-%d %H:%M"),
                    "note": f"本地导出 · 图片 {len(qs.image_manifest(snap['content']))} 张",
                }
                r = qd.write_article_docx(path, title, snap["content"],
                                          meta=meta, fetcher=self._fetcher)
                r["title"] = title
                r["doc_id"] = did
                r["file"] = path.name
                done.append(r)
                if t:
                    self.tasks.say(
                        t, f"✓ {path.name}  {r['bytes'] / 1048576:.1f} MB  "
                           f"图 {r['images_embedded']} 张"
                           f"{'（失败 ' + str(r['images_failed']) + '）' if r['images_failed'] else ''}")
            except Exception as exc:  # noqa: BLE001
                fails.append(f"{title[:30]}：{exc}")
                if t:
                    self.tasks.say(t, f"✗ {title[:30]}：{exc}")
        total = sum(d.get("bytes", 0) for d in done)
        return {"ok": True, "count": len(done), "failed": fails,
                "total_bytes": total, "dir": str(EXPORT_DIR),
                "files": done, "img_stats": dict(self._fetcher.stats)}

    # ---------------- 审计 ---------------- #

    def audit(self, limit: int = 0) -> Dict[str, Any]:
        rep = self.plane().audit(limit) if self._cookie else {
            "ok": True, "issues": {}, "note": "未登录，仅本地审计"}
        local = {
            "unchecked": len([d for d in self.store.list_documents()
                              if (d.get("check_status") or "") == "unchecked"]),
            "has_body": len([d for d in self.store.list_documents()
                             if self.store.latest_snapshot(str(d["doc_id"]))]),
            "failed_upload": len([d for d in self.store.list_documents()
                                  if d.get("upload_status") == "failed"]),
            "snapshots": self.store.stats().get("snapshots", 0),
            "revisions": self.store.stats().get("revisions", 0),
        }
        return {"ok": True, "remote": rep, "local": local,
                "audit_tail": self.store.audit_tail(limit=60)}


# --------------------------------------------------------------------------- #
# HTTP 层
# --------------------------------------------------------------------------- #

class Handler(BaseHTTPRequestHandler):
    server_version = "QyApp/" + VERSION
    wb: Workbench  # 由 main() 注入

    def log_message(self, fmt: str, *args: Any) -> None:  # 静音
        pass

    # ---------- 基础 ---------- #

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _json(self, obj: Any, code: int = 200) -> None:
        data = json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8")
        self._send(code, data, "application/json; charset=utf-8")

    def _err(self, exc: Exception, code: int = 400) -> None:
        self._json({"ok": False, "error": str(exc)}, code)

    def _body(self) -> Dict[str, Any]:
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = 0
        if n <= 0:
            return {}
        raw = self.rfile.read(n)
        try:
            return json.loads(raw.decode("utf-8"))
        except Exception:  # noqa: BLE001
            return {}

    def _authed(self) -> bool:
        return self.headers.get("X-QY-Token") == self.wb.token

    # ---------- 路由 ---------- #

    def do_GET(self) -> None:  # noqa: N802
        u = urlparse(self.path)
        p, q = u.path, parse_qs(u.query)
        try:
            if p.startswith("/api/"):
                return self._api_get(p, q)
            return self._static(p)
        except KeyError as exc:
            self._err(exc, 404)
        except Exception as exc:  # noqa: BLE001
            self._err(exc, 500)

    def do_POST(self) -> None:  # noqa: N802
        u = urlparse(self.path)
        p = u.path
        if not p.startswith("/api/"):
            return self._err(RuntimeError("未知路径"), 404)
        if not self._authed():
            return self._err(RuntimeError("令牌无效（请刷新页面）"), 403)
        try:
            return self._api_post(p, self._body())
        except KeyError as exc:
            self._err(exc, 404)
        except Exception as exc:  # noqa: BLE001
            self._err(exc, 500)

    # ---------- 静态 ---------- #

    def _static(self, p: str) -> None:
        rel = "index.html" if p in ("/", "") else unquote(p.lstrip("/"))
        target = (WEB_DIR / rel).resolve()
        if not str(target).startswith(str(WEB_DIR.resolve())):
            return self._err(RuntimeError("越界"), 403)
        if not target.is_file():
            return self._send(404, b"not found", "text/plain; charset=utf-8")
        data = target.read_bytes()
        if target.name == "index.html":
            data = data.replace(b"__QY_TOKEN__",
                                self.wb.token.encode("utf-8"))
        ctype = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        if target.suffix in (".js", ".css", ".html"):
            ctype += "; charset=utf-8"
        self._send(200, data, ctype)

    # ---------- GET API ---------- #

    def _api_get(self, p: str, q: Dict[str, List[str]]) -> None:
        wb = self.wb
        g = lambda k, d="": (q.get(k) or [d])[0]  # noqa: E731

        if p == "/api/status":
            return self._json(wb.status())

        if p == "/api/articles":
            return self._json({
                "ok": True,
                "items": wb.articles(only=g("only"), q=g("q"),
                                     order=g("order"),
                                     limit=int(g("limit", "0") or 0)),
                "stats": wb.store.stats(),
            })

        if p == "/api/task":
            t = wb.tasks.get(g("id"))
            if not t:
                return self._err(RuntimeError("任务不存在"), 404)
            return self._json({"ok": True, "task": t})

        if p == "/api/audit":
            return self._json(wb.audit(int(g("limit", "0") or 0)))

        if p == "/api/rules":
            return self._json({"ok": True, "count": len(qc.RULES),
                               "rules": [{"id": r.id, "cat": r.cat,
                                          "level": r.level, "label": r.label,
                                          "why": r.why, "fix": r.fix,
                                          "scope": r.scope}
                                         for r in qc.RULES]})

        m = re.match(r"^/api/article/([^/]+)$", p)
        if m:
            return self._json({"ok": True, **wb.article(m.group(1))})

        m = re.match(r"^/api/prompt/([^/]+)$", p)
        if m:
            return self._json({"ok": True, **wb.ai_prompt(m.group(1))})

        m = re.match(r"^/api/revisions/([^/]+)$", p)
        if m:
            did = m.group(1)
            return self._json({"ok": True,
                               "revisions": wb.store.list_revisions(did, 300),
                               "timeline": wb.store.timeline(did, 80),
                               "snapshots": wb.store.list_snapshots(did),
                               "consistency": wb.store.consistency_report(did)})

        if p == "/api/ai/config":
            return self._json({"ok": True, "config": qyai.load_ai_config()})

        if p == "/api/auth/qr_poll":
            return self._json(wb.auth_qr_poll(g("token")))

        if p == "/api/auth/cloud_sync":
            return self._json(wb.auth_cloud_sync())

        if p == "/api/system/check_update":
            return self._json(wb.check_update())

        return self._err(RuntimeError(f"未知接口 {p}"), 404)

    # ---------- POST API ---------- #

    def _api_post(self, p: str, b: Dict[str, Any]) -> None:
        wb = self.wb

        if p == "/api/login":
            r = wb.set_cookie(str(b.get("cookie") or ""))
            if r.get("ok"):
                try:
                    r["account"] = wb.plane().account()
                except Exception as exc:  # noqa: BLE001
                    r["account"] = {"error": str(exc)[:200]}
            return self._json(r)

        if p == "/api/auth/auto_detect":
            return self._json(wb.auth_auto_detect(close_browser=bool(b.get("close_browser"))))

        if p == "/api/auth/qr_start":
            return self._json(wb.auth_qr_start())

        if p == "/api/system/apply_update":
            dl_url = str(b.get("download_url") or f"{wb.server_url}/api/qy/download/windows")

            def _job(t: Dict[str, Any]) -> Any:
                wb.tasks.say(t, f"开始连接云端更新服务：{dl_url}")
                return wb.apply_update(dl_url, t=t)

            return self._json({"ok": True,
                               "task_id": wb.tasks.spawn("一键自动更新", _job)})

        if p == "/api/ai/config":
            qyai.save_ai_config(
                str(b.get("api_url") or ""),
                str(b.get("api_key") or ""),
                str(b.get("model") or "")
            )
            return self._json({"ok": True, "config": qyai.load_ai_config()})

        if p == "/api/ai/dual_round":
            doc_id = str(b.get("doc_id") or "")
            model = str(b.get("model") or "")
            if not doc_id:
                return self._err(RuntimeError("doc_id 为空"))

            def _job(t: Dict[str, Any]) -> Any:
                wb.tasks.say(t, f"开始双轮 AI 智能修润与对抗质检（文章 ID: {doc_id}）")
                r = wb.ai_dual_round(doc_id, model=model or None, t=t)
                wb.tasks.say(t, f"双轮质检完成！安全分: {r.get('adversarial_score')} 分")
                return r

            return self._json({"ok": True,
                               "task_id": wb.tasks.spawn("双轮 AI 修润", _job)})

        if p == "/api/ai/apply":
            doc_id = str(b.get("doc_id") or "")
            title = str(b.get("title") or "")
            body_html = str(b.get("body_html") or "")
            if not doc_id or not body_html:
                return self._err(RuntimeError("缺少必需参数"))
            return self._json(wb.ai_apply(doc_id, title, body_html))

        if p == "/api/inspect":
            kinds = b.get("kinds") or ["article"]
            cap = int(b.get("cap") or 0)
            with_body = bool(b.get("with_body"))
            body_limit = int(b.get("body_limit") or 0)

            def _job(t: Dict[str, Any]) -> Any:
                wb.tasks.say(t, f"拉取列表 kinds={kinds} cap={cap or '全部'}")
                plane = wb.plane()
                r = plane.inspect(
                    kinds=kinds, cap=cap, with_body=False,
                    progress=lambda s, i, n: wb.tasks.step(t, i, n, str(s)))
                wb.tasks.say(t, f"列表 {r['documents']} 条，已入库")
                if with_body:
                    ids = [d["doc_id"] for d in wb.store.list_documents(
                        kind="article")]
                    if body_limit:
                        ids = ids[:body_limit]
                    wb.tasks.say(t, f"开始同步正文 {len(ids)} 篇（约 "
                                    f"{max(1, int(len(ids) * 0.6))} 秒）")
                    r2 = plane.sync_body(
                        ids,
                        progress=lambda s, i, n: wb.tasks.step(t, i, n, str(s)))
                    wb.tasks.say(t, f"正文成功 {r2.get('ok', 0)} / "
                                    f"失败 {r2.get('fail', 0)}")
                    r["body"] = r2
                return r

            return self._json({"ok": True, "task_id": wb.tasks.spawn("同步", _job)})

        if p == "/api/check":
            ids = b.get("doc_ids") or []
            if not ids:
                return self._err(RuntimeError("没有选中文章"))

            def _job(t: Dict[str, Any]) -> Any:
                wb.tasks.say(t, f"开始检查 {len(ids)} 篇（{len(qc.RULES)} 条规则）")
                r = wb.check(ids, t)
                wb.tasks.say(t, f"完成：阻断 {r['block']} / 提醒 {r['warn']} / "
                                f"通过 {r['pass']}")
                return r

            return self._json({"ok": True,
                               "task_id": wb.tasks.spawn("敏感检查", _job)})

        if p == "/api/ai_ingest":
            return self._json({"ok": True, **wb.ai_ingest(
                str(b.get("doc_id") or ""), str(b.get("reply") or ""))})

        if p == "/api/export":
            ids = b.get("doc_ids") or []
            if not ids:
                return self._err(RuntimeError("没有选中文章"))

            def _job(t: Dict[str, Any]) -> Any:
                wb.tasks.say(t, f"开始导出 {len(ids)} 篇（高清原图，每篇一个文件）")
                r = wb.export(ids, t)
                wb.tasks.say(t, f"完成 {r['count']} 个文件，共 "
                                f"{r['total_bytes'] / 1048576:.1f} MB → {r['dir']}")
                return r

            return self._json({"ok": True,
                               "task_id": wb.tasks.spawn("导出 Word", _job)})

        if p == "/api/save":
            return self._json({"ok": True, **wb.save(
                str(b.get("doc_id") or ""), str(b.get("title") or ""),
                str(b.get("body_html") or ""), bool(b.get("publish")))})

        if p == "/api/restore":
            return self._json({"ok": True, **wb.restore(
                str(b.get("doc_id") or ""), bool(b.get("push")))})

        if p == "/api/config":
            kw = {k: b[k] for k in ("per_day", "per_hour", "gap_min", "gap_max")
                  if k in b}
            return self._json({"ok": True, "config": wb.plane().config(**kw)})

        if p == "/api/reveal":
            path = str(b.get("path") or EXPORT_DIR)
            try:
                import subprocess
                subprocess.Popen(["open", path])
                return self._json({"ok": True})
            except Exception as exc:  # noqa: BLE001
                return self._err(exc)

        return self._err(RuntimeError(f"未知接口 {p}"), 404)


# --------------------------------------------------------------------------- #
# 入口
# --------------------------------------------------------------------------- #

def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=APP_NAME)
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--host", default="127.0.0.1",
                    help="只建议用 127.0.0.1；绑 0.0.0.0 会暴露到局域网")
    ap.add_argument("--db", default="", help="自定义数据库路径")
    ap.add_argument("--allow-write", action="store_true",
                    help="允许写回知乎（默认只读）")
    ap.add_argument("--no-browser", action="store_true")
    a = ap.parse_args(list(argv) if argv is not None else None)

    if not WEB_DIR.is_dir():
        print(f"[错误] 找不到前端目录：{WEB_DIR}", file=sys.stderr)
        return 2

    wb = Workbench(allow_write=a.allow_write,
                   db=Path(a.db) if a.db else None)
    Handler.wb = wb

    httpd = ThreadingHTTPServer((a.host, a.port), Handler)
    httpd.daemon_threads = True
    url = f"http://{a.host}:{a.port}/"

    print("=" * 62)
    print(f"  {APP_NAME} v{VERSION}")
    print("=" * 62)
    print(f"  地址      {url}")
    print(f"  数据库    {wb.store.path}")
    print(f"  导出目录  {EXPORT_DIR}")
    print(f"  登录      {'已读取 cookie.txt' if wb._cookie else '未登录'}")
    print(f"  写回知乎  {'允许' if a.allow_write else '禁止（只读模式）'}")
    print(f"  规则条数  {len(qc.RULES)}")
    print(f"  后端      {qp.BACKEND}")
    print("=" * 62)
    print("  Ctrl+C 停止")
    print()

    if not a.no_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
