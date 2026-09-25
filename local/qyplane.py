#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""qyplane —— 本地控制面：把云端的活儿全部搬回本机。

原来云端做四件事，现在本地做：

    云端接口                      本地替代
    ---------------------------------------------------------------
    GET  /api/qy/inspect      →   LocalPlane.inspect()      列文章 + 正文
    GET  /api/qy/agent/payload →  LocalPlane.payload()      本地算建议稿
    POST /api/qy/verify/{job}  →  LocalPlane.verify()       本地回读复核
    GET  /api/qy/config        →  LocalPlane.config()       本地配置
    任务领取/心跳/汇报/日志      →  LocalPlane.claim/heartbeat/report_item/...

关键洞察：**这四件事的原料本地早就有了。**
`qingyi_executor.py` 里已经有全部知乎读写（list_articles / get_article_draft /
patch_draft / publish_article）和全部植入算法（plan_title / scan_scenes /
apply_scenes）。云端唯一不可替代的东西是「服务器上跑着的那个大模型」，
而用户已经决定「AI 用他自己的」—— 所以云端没有剩下任何必需的能力。

接口签名刻意与 `ControlPlane` 保持一致（claim/heartbeat/report_item/log/
finish/payload/verify/brief），这样 `qingyi_client.py` 那套流程可以原地换芯。
"""
from __future__ import annotations

import json
import os
import random
import re
import sys
import time
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent
for p in (str(_HERE), str(_ROOT)):
    if p not in sys.path:
        sys.path.insert(0, p)


# --------------------------------------------------------------------------- #
# 零依赖引导：把 qynet 顶替掉 requests
# --------------------------------------------------------------------------- #

def ensure_requests(force: bool = True) -> str:
    """让 `import requests` 拿到 qynet（除非显式要求用真 requests）。

    默认强制顶替 —— 我们要的是「行为确定、不依赖用户装了什么」，
    而不是「用户恰好装了 requests 就用它」。
    设 QY_USE_REAL_REQUESTS=1 可切回真 requests（调试用）。
    """
    if os.environ.get("QY_USE_REAL_REQUESTS") == "1":
        try:
            import requests  # noqa: F401
            return "requests"
        except ImportError:
            pass
    import qynet
    qynet.install(force=force)
    return "qynet"


BACKEND = ensure_requests()

import qingyi_executor as qe  # noqa: E402  必须在 ensure_requests 之后
import qystore as qs           # noqa: E402

__version__ = "1.0.0"

BRAND = "清一新教育"
PREFIX = f"【{BRAND}】"

# 每日上限的默认值（本地配置，不再问云端）
DEFAULT_CONFIG = {
    "per_day": 120,
    "per_hour": 12,
    "gap_min": 25.0,
    "gap_max": 75.0,
    "batch": 5,
    "publish": True,
}


# --------------------------------------------------------------------------- #
# 本地控制面
# --------------------------------------------------------------------------- #

class LocalPlane:
    """本地控制面。替代 `qingyi_executor.ControlPlane`。

    用法：
        plane = LocalPlane(cookie)
        plane.inspect("article", with_body=True)     # 同步到本地库
        job = plane.make_job(doc_ids)                # 建本地任务
        pl  = plane.payload(job["job_id"], "123")    # 本地算建议稿
        ...
        plane.verify(job["job_id"])                  # 本地回读复核
    """

    base = "local://127.0.0.1"

    def __init__(self, cookie: str,
                 store: Optional[qs.Store] = None,
                 backup_dir: Optional[Path | str] = None,
                 policy: Optional[qe.RatePolicy] = None,
                 config: Optional[Dict[str, Any]] = None) -> None:
        self.cookie = cookie or ""
        self.store = store or qs.Store()
        self.backup_dir = Path(backup_dir) if backup_dir else _ROOT / "data" / "qyedu_backup"
        self.backup_dir.mkdir(parents=True, exist_ok=True)
        self.policy = policy or qe.RatePolicy()
        self.cfg = dict(DEFAULT_CONFIG)
        self.cfg.update(config or {})
        self._apply_config()
        self._read_signers: Dict[str, qe.QingyiTitleSigner] = {}

    def _apply_config(self) -> None:
        c = self.cfg
        self.policy.per_day = int(c.get("per_day", self.policy.per_day))
        self.policy.per_hour = int(c.get("per_hour", self.policy.per_hour))
        self.policy.gap_min = float(c.get("gap_min", self.policy.gap_min))
        self.policy.gap_max = float(c.get("gap_max", self.policy.gap_max))

    # ---------------- 本地配置（替代 /api/qy/config） ---------------- #

    def config(self, **kw: Any) -> Dict[str, Any]:
        if kw:
            self.cfg.update(kw)
            self._apply_config()
        return dict(self.cfg)

    # ---------------- 读会话 ---------------- #

    def _reader(self, tag: str = "read") -> qe.QingyiTitleSigner:
        """读操作共用少量会话，避免每篇都新建 session 造成 TLS 开销。"""
        if tag not in self._read_signers:
            self._read_signers[tag] = qe.QingyiTitleSigner(
                cookie=self.cookie, backup_dir=self.backup_dir,
                policy=self.policy)
        return self._read_signers[tag]

    def account(self) -> Dict[str, Any]:
        return self._reader().verify()

    # ---------------- inspect（替代 /api/qy/inspect） ---------------- #

    def inspect(self, kinds: Sequence[str] = ("article",),
                cap: int = 0,
                with_body: bool = False,
                body_limit: int = 0,
                progress: Optional[Callable[[str, int, int], None]] = None
                ) -> Dict[str, Any]:
        """列出账号下的内容并写入本地库。

        with_body=False 时只同步元信息（快，N 次列表请求）。
        with_body=True  时逐篇拉正文（慢，+N 次请求），这是敏感内容检查的前提。
        """
        s = self._reader()
        got: List[Dict[str, Any]] = []
        for kind in kinds:
            if kind == "article":
                got += s.list_articles(cap=cap)
            elif kind == "answer":
                got += s.list_answers(cap=cap)
            elif kind == "pin":
                got += s.list_pins(cap=cap)
        self.store.log("inspect", None,
                       f"kinds={list(kinds)} 取回 {len(got)} 条 with_body={with_body}")

        for it in got:
            self.store.upsert_document(it)

        body_ok = body_fail = 0
        if with_body:
            arts = [x for x in got if x.get("type") == "article"]
            if body_limit:
                arts = arts[:body_limit]
            for i, it in enumerate(arts, 1):
                if progress:
                    progress(it.get("title") or str(it.get("id")), i, len(arts))
                try:
                    d = s.get_article_draft(str(it["id"]))
                    body = d.get("content") or ""
                    title = d.get("title") or it.get("title") or ""
                    it2 = dict(it, title=title)
                    self.store.upsert_document(it2, body_html=body)
                    if not self.store.latest_snapshot(str(it["id"]), "original"):
                        self.store.add_snapshot(str(it["id"]), title, body,
                                                "original")
                    body_ok += 1
                except Exception as exc:  # noqa: BLE001
                    body_fail += 1
                    self.store.log("inspect.body_fail", str(it.get("id")),
                                   str(exc)[:200])
                if i < len(arts):
                    time.sleep(random.uniform(0.35, 0.85))
        return {"ok": True, "documents": len(got), "body_ok": body_ok,
                "body_fail": body_fail, "backend": BACKEND}

    def sync_body(self, doc_ids: Sequence[str],
                  progress: Optional[Callable[[str, int, int], None]] = None
                  ) -> Dict[str, Any]:
        """给指定文档补拉正文（用于「检查这一批」）。"""
        s = self._reader()
        ok = fail = 0
        ids = [str(i) for i in doc_ids]
        for i, did in enumerate(ids, 1):
            doc = self.store.get_document(did) or {}
            if progress:
                progress(doc.get("title_now") or did, i, len(ids))
            try:
                d = s.get_article_draft(did)
                body = d.get("content") or ""
                title = d.get("title") or doc.get("title_now") or ""
                self.store.upsert_document(
                    {"id": did, "type": doc.get("kind") or "article",
                     "title": title, "url": doc.get("url") or "",
                     "comment_count": doc.get("comment_count"),
                     "voteup_count": doc.get("voteup_count")},
                    body_html=body)
                if not self.store.latest_snapshot(did, "original"):
                    self.store.add_snapshot(did, title, body, "original")
                ok += 1
            except Exception as exc:  # noqa: BLE001
                fail += 1
                self.store.log("sync.body_fail", did, str(exc)[:200])
            if i < len(ids):
                time.sleep(random.uniform(0.35, 0.85))
        return {"ok": True, "ok_count": ok, "fail": fail}

    # ---------------- 建议稿（替代 /api/qy/agent/payload） ---------------- #

    def compute_payload(self, item: Dict[str, Any], title: str, body: str,
                        mode: str = "brand",
                        preset: str = "",
                        allow_image_loss: bool = False) -> Dict[str, Any]:
        """本地算一篇的最终稿。纯函数式：不改线上，只产出方案 + 替换记录。

        mode:
          brand           标题前置品牌词 + 正文句末署名括注（默认，幂等）
          title_only      只改标题
          replace_content 整文替换为高价值文库（用户选择保留该模式）
        """
        aid = str(item.get("id") or "")
        new_title, title_change, title_reason = qe.plan_title(title)
        payload: Dict[str, Any] = {
            "title": new_title,
            "content": None,
            "pre_title": title,
            "pre_content": body,
            "body_added": 0,
            "source": "local",
            "mode": mode,
            "revisions": [],
        }

        if mode == "replace_content":
            essay = None
            try:
                from high_value_essays import get_essay_by_preset
                essay = get_essay_by_preset(preset) if preset else None
            except Exception:  # noqa: BLE001
                essay = None
            if not essay:
                payload["note"] = f"未找到预设文库 {preset!r}，已退回 brand 模式"
                payload["mode"] = mode = "brand"
            else:
                imgs = qs.image_manifest(body)
                # 安全闸门：replace_content 会把标题+正文整体换成范文，
                # 图片和作者本人的文字都会消失。用户明确选择保留此模式，
                # 但至少不能在「有图」的文档上悄悄发生。
                if imgs and not allow_image_loss:
                    payload["blocked"] = True
                    payload["note"] = (
                        f"该篇有 {len(imgs)} 张图片，replace_content 会整篇替换、"
                        f"图片全部丢失。已拦截。如确要替换，请显式允许图片丢失。")
                    payload["content"] = None
                    payload["title"] = title
                    return payload
                et = essay.get("title") if isinstance(essay, dict) else None
                ec = essay.get("content") or essay.get("body") \
                    if isinstance(essay, dict) else None
                if not ec:
                    payload["note"] = "预设文库内容为空，已退回 brand 模式"
                    payload["mode"] = mode = "brand"
                else:
                    payload["title"] = et or new_title
                    payload["content"] = ec
                    payload["mode"] = "replace_content"
                    payload["preset"] = preset
                    payload["revisions"].append({
                        "kind": "replace_content", "field": "content",
                        "rule_id": "RC01", "rule_label": f"整文替换（{preset}）",
                        "before": body, "after": ec, "applied": False,
                    })
                    if title != payload["title"]:
                        payload["revisions"].append({
                            "kind": "title", "field": "title",
                            "rule_id": "RC02", "rule_label": "标题随替换稿",
                            "before": title, "after": payload["title"],
                            "applied": False,
                        })
                    return payload

        # ---- brand 模式 ----
        if title_change:
            payload["revisions"].append({
                "kind": "title", "field": "title", "rule_id": "T01",
                "rule_label": title_reason, "before": title,
                "after": new_title, "applied": False})

        if mode != "title_only":
            scenes = qe.scan_scenes(body, limit=1)
            if scenes:
                new_body = qe.apply_scenes(body, scenes)
                if new_body != body:
                    payload["content"] = new_body
                    payload["body_added"] = qe.inserted_count(body, new_body)
                    for sc in scenes:
                        payload["revisions"].append({
                            "kind": "body", "field": "content",
                            "rule_id": "B01",
                            "rule_label": f"正文句末署名括注（{sc.reason}）",
                            "before": sc.para_text,
                            "after": sc.proposed[:400],
                            "applied": False})

        if not payload["revisions"]:
            payload["note"] = "标题与正文都已含品牌词，无需改动（幂等）"
        return payload

    def payload(self, job_id: str, item_id: str) -> Optional[Dict[str, Any]]:
        """从本地库取出（或首次计算并缓存）该篇的建议稿。"""
        row = None
        for it in (self.store.get_job(job_id) or {}).get("items", []):
            if str(it.get("doc_id")) == str(item_id):
                row = it
                break
        if row is None:
            return None
        if row.get("payload"):
            try:
                return json.loads(row["payload"])
            except Exception:  # noqa: BLE001
                pass

        doc = self.store.get_document(str(item_id)) or {}
        snap = self.store.latest_snapshot(str(item_id))
        title = doc.get("title_now") or ""
        body = (snap or {}).get("content") or ""
        if not title and not body:
            return None
        item = {"id": str(item_id), "type": doc.get("kind") or "article",
                "url": doc.get("url") or "", "title": title}
        pl = self.compute_payload(item, title, body,
                                  mode=self.cfg.get("mode", "brand"),
                                  preset=self.cfg.get("preset", ""),
                                  allow_image_loss=bool(
                                      self.cfg.get("allow_image_loss")))
        self.store.set_job_item(job_id, str(item_id), row.get("status") or "pending",
                                row.get("message") or "", payload=pl)
        return pl

    # ---------------- 任务（替代 claim/heartbeat/report/log/finish） ---------------- #

    def make_job(self, doc_ids: Sequence[str], note: str = "",
                 mode: str = "brand", preset: str = "",
                 allow_image_loss: bool = False) -> Dict[str, Any]:
        jid = f"local-{time.strftime('%Y%m%d-%H%M%S')}-{random.randint(100, 999)}"
        self.cfg["mode"] = mode
        self.cfg["preset"] = preset
        self.cfg["allow_image_loss"] = allow_image_loss
        self.store.create_job(jid, doc_ids, note=note or f"{len(doc_ids)} 篇")
        return self.store.get_job(jid) or {"job_id": jid, "items": []}

    def claim(self, worker_id: str, mode: str = "local") -> Optional[Dict[str, Any]]:
        """取最近一个还有 pending 的本地任务。"""
        for j in self.store.list_jobs(20):
            full = self.store.get_job(j["job_id"]) or {}
            if any(it.get("status") == "pending" for it in full.get("items", [])):
                return full
        return None

    def heartbeat(self, job_id: str, worker_id: str) -> None:
        self.store.log("worker.heartbeat", None, f"{job_id} / {worker_id}")

    def log(self, job_id: str, msg: str) -> None:
        self.store.log("worker.log", None, f"{job_id}: {msg}")

    def report_item(self, job_id: str, record: Dict[str, Any]) -> None:
        did = str(record.get("id") or "")
        self.store.set_job_item(job_id, did,
                                record.get("status") or "done",
                                record.get("message") or "",
                                record=record)
        self.store.set_document_status(
            did,
            upload_status=("uploaded" if record.get("status") in ("done", "skipped")
                           else "failed"),
            upload_at=int(time.time()),
            title_now=record.get("title_after") or None)
        self.store.log("worker.report", did,
                       f"{record.get('status')} {record.get('message','')}"[:300])

    def finish(self, job_id: str, summary: Dict[str, Any]) -> None:
        self.store.finish_job(job_id, "done", summary)

    # ---------------- 独立复核（替代 /api/qy/verify/{job}） ---------------- #

    def verify(self, job_id: str, cookie: str = "") -> Dict[str, Any]:
        """本地回读线上，核对「我们记录的」与「线上真实状态」是否一致。

        云端复核的价值在于「不信本地自述」；本地做同一件事仍然成立 ——
        因为复核读的是知乎的线上数据，而不是我们自己的记录。
        """
        job = self.store.get_job(job_id)
        if not job:
            return {"ok": False, "note": "找不到这个任务"}
        s = self._reader()
        details: List[Dict[str, Any]] = []
        passed = failed = skipped = 0
        for it in job.get("items", []):
            did = str(it.get("doc_id"))
            rec = {}
            try:
                rec = json.loads(it.get("record") or "{}")
            except Exception:  # noqa: BLE001
                rec = {}
            if it.get("status") not in ("done", "skipped"):
                skipped += 1
                details.append({"id": did, "verify": "skipped",
                                "reasons": [f"任务状态为 {it.get('status')}"]})
                continue
            try:
                d = s.get_article_draft(did)
            except Exception as exc:  # noqa: BLE001
                failed += 1
                details.append({"id": did, "verify": "fail",
                                "reasons": [f"回读失败：{exc}"]})
                continue
            live_title = (d.get("title") or "").strip()
            live_body = d.get("content") or ""
            reasons: List[str] = []

            want_title = (rec.get("title_after") or "").strip()
            if want_title and live_title != want_title:
                reasons.append(f"标题不一致：线上「{live_title[:30]}」"
                               f"≠ 记录「{want_title[:30]}」")

            if BRAND not in live_title:
                reasons.append("线上标题不含品牌词")

            # 图片一致性：这是用户最在意的一条
            base = self.store.latest_snapshot(did, "original")
            if base and base.get("content"):
                diff = qs.image_diff(base["content"], live_body)
                if not diff["same"]:
                    if diff["lost"]:
                        reasons.append(
                            f"图片丢失 {len(diff['lost'])} 张（原有 "
                            f"{diff['before_count']}，现有 {diff['after_count']}）")
                    elif diff["added"]:
                        reasons.append(
                            f"图片增加 {len(diff['added'])} 张（原有 "
                            f"{diff['before_count']}，现有 {diff['after_count']}）")

            want_hash = rec.get("body_sha256_after") or ""
            if want_hash and qs.text_hash(live_body) != want_hash:
                reasons.append("正文可见文本与记录不一致（可能被平台二次编辑）")

            # 逐条替换复核：每一条 applied 的替换，其 after 片段应能在线上找到
            for rv in self.store.list_revisions(did):
                if not rv.get("applied"):
                    continue
                frag = (rv.get("after_text") or "").strip()
                if not frag or len(frag) < 8:
                    continue
                if rv.get("field") == "title":
                    ok = frag[:40] in live_title
                else:
                    ok = frag[:40] in qs.html_to_text(live_body)
                self.store.mark_verified(
                    int(rv["rev_id"]), ok,
                    "线上可检索到" if ok else "线上检索不到该片段")
                if not ok:
                    reasons.append(
                        f"替换 #{rv['rev_id']}（{rv.get('rule_label')}）"
                        f"在线上检索不到")

            if reasons:
                failed += 1
                details.append({"id": did, "verify": "fail",
                                "title": live_title, "reasons": reasons})
            else:
                passed += 1
                details.append({"id": did, "verify": "pass", "title": live_title})
            time.sleep(random.uniform(0.3, 0.7))

        res = {"ok": True, "job_id": job_id,
               "summary": {"checked": passed + failed, "passed": passed,
                           "failed": failed, "skipped": skipped},
               "details": details,
               "checked_at": int(time.time())}
        self.store.log("verify", None, json.dumps(res["summary"], ensure_ascii=False))
        return res

    def brief(self, job_id: str = "") -> Dict[str, Any]:
        st = self.store.stats()
        if job_id:
            j = self.store.get_job(job_id) or {}
            items = j.get("items", [])
            st["job"] = {
                "job_id": job_id, "total": len(items),
                "pending": sum(1 for i in items if i["status"] == "pending"),
                "done": sum(1 for i in items if i["status"] == "done"),
                "failed": sum(1 for i in items if i["status"] == "failed"),
            }
        return st

    # ---------------- 一致性审计（抓「搞混」的现场） ---------------- #

    def audit(self, limit: int = 0) -> Dict[str, Any]:
        """全库体检，专抓**半应用 / 不一致**状态。

        为什么需要这个
        --------------
        实测发现真实账号里存在这样的文档：**正文已经有「（清一新教育）」署名括注，
        标题却没有品牌词**。这不是设计如此，而是历史批次只跑完一半留下的残骸。
        这类「半应用状态」正是用户最怕的「搞混」——而且它不会自己暴露，
        必须主动扫。

        返回的问题分类：
          half_applied   正文有品牌、标题没有（或反之）→ 历史残骸
          title_only     只有标题有品牌（正文植入失败或未执行）
          unverified     有已应用但未复核的替换记录
          blocked_pending 检查定级 block 但仍在待上传队列
          image_drift    当前图片数与最初快照不一致
        """
        issues: List[Dict[str, Any]] = []
        docs = self.store.list_documents(limit=limit)
        for d in docs:
            did = d["doc_id"]
            title = d.get("title_now") or ""
            t_has = BRAND in title
            snap = self.store.latest_snapshot(did)
            b_has = False
            if snap and snap.get("content"):
                b_has = BRAND in qs.html_to_text(snap["content"])
            elif d.get("kind") == "article":
                b_has = None          # 没拉过正文，无法判断

            if b_has is True and not t_has:
                issues.append({
                    "doc_id": did, "kind": "half_applied", "level": "warn",
                    "title": title, "url": d.get("url"),
                    "note": "正文已含品牌词，标题没有 —— 历史批次只跑完一半",
                    "fix": "重跑本篇（只补标题）即可对齐"})
            elif t_has and b_has is False:
                issues.append({
                    "doc_id": did, "kind": "title_only", "level": "info",
                    "title": title, "url": d.get("url"),
                    "note": "标题已含品牌词，正文没有",
                    "fix": "若正文需植入，重跑本篇（只补正文）"})

            uv = self.store.unverified_revisions(did)
            if uv:
                issues.append({
                    "doc_id": did, "kind": "unverified", "level": "warn",
                    "title": title, "url": d.get("url"),
                    "note": f"{len(uv)} 条替换已应用但未复核",
                    "fix": "跑一次独立复核（本地回读线上）"})

            if d.get("check_status") == "block" and \
                    d.get("upload_status") in ("untouched", "dirty"):
                issues.append({
                    "doc_id": did, "kind": "blocked_pending", "level": "block",
                    "title": title, "url": d.get("url"),
                    "note": f"检查定级 block（{d.get('check_hits')} 处），"
                            f"但状态是 {d.get('upload_status')}",
                    "fix": "先处理敏感内容，再谈上传"})

            if snap and d.get("image_count") is not None \
                    and snap.get("image_count") != d.get("image_count"):
                issues.append({
                    "doc_id": did, "kind": "image_drift", "level": "warn",
                    "title": title, "url": d.get("url"),
                    "note": f"图片数变化：快照 {snap.get('image_count')} → "
                            f"当前 {d.get('image_count')}",
                    "fix": "核对是否被平台或历史批次改动"})

        by_kind: Dict[str, int] = {}
        for i in issues:
            by_kind[i["kind"]] = by_kind.get(i["kind"], 0) + 1
        return {
            "ok": True,
            "documents_scanned": len(docs),
            "issues": len(issues),
            "by_kind": by_kind,
            "items": issues,
            "note": ("没有发现问题" if not issues
                     else f"发现 {len(issues)} 处需要人工过目的不一致"),
        }

    def restore(self, doc_id: str, signer: Optional[qe.QingyiTitleSigner] = None,
                publish: bool = True) -> Dict[str, Any]:
        """一键还原到最初快照。这是「可随时完整还原」的可执行版本。"""
        snap = self.store.latest_snapshot(doc_id, "original")
        if not snap:
            return {"ok": False, "note": "没有最初快照，无法还原"}
        s = signer or qe.QingyiTitleSigner(
            cookie=self.cookie, backup_dir=self.backup_dir, policy=self.policy)
        aid = str(doc_id)
        ok, msg = s.patch_draft(aid, snap.get("title") or "", snap.get("content") or "")
        if not ok:
            return {"ok": False, "note": f"保存失败：{msg}"}
        if publish:
            okp, msgp = s.publish_article(aid, snap.get("title") or "",
                                          snap.get("content") or "")
            if not okp:
                return {"ok": False, "note": f"已保存，发布未确认：{msgp}"}
        self.store.add_snapshot(doc_id, snap.get("title") or "",
                                snap.get("content") or "", "restored")
        self.store.set_document_status(doc_id, upload_status="untouched")
        self.store.log("restore", doc_id, f"已还原到 original 快照（{msg}）")
        return {"ok": True, "note": "已还原到最初版本"}


# --------------------------------------------------------------------------- #
# 自检（只验本地逻辑与零依赖，不触网）
# --------------------------------------------------------------------------- #

def _selftest() -> int:
    import tempfile
    tmp = Path(tempfile.mkdtemp())
    print("网络后端    :", BACKEND, "（qynet = 纯标准库）")
    print("第三方依赖  :", end=" ")
    try:
        import requests  # noqa: F401
        print("本机装了 requests，但已按设计被 qynet 顶替"
              if BACKEND == "qynet" else "使用真 requests（QY_USE_REAL_REQUESTS=1）")
    except ImportError:
        print("无（requests 未安装，走 qynet）")

    st = qs.Store(tmp / "t.db")
    plane = LocalPlane("", store=st, backup_dir=tmp / "bk")
    print("本地配置    :", json.dumps(plane.config(), ensure_ascii=False))

    body = ("<p>这几年我最大的收获是学会了怎么学习，也明白了教育到底在做什么。</p>"
            "<p>第二段讲方法。</p>"
            '<img src="https://pic1.zhimg.com/v2-abc1234567890abcdef1234_1440w.jpg">')
    st.upsert_document({"id": "100", "type": "article",
                        "title": "我的学习心得",
                        "url": "https://zhuanlan.zhihu.com/p/100"}, body)
    st.add_snapshot("100", "我的学习心得", body, "original")

    print()
    print("== 本地算建议稿（brand 模式） ==")
    pl = plane.compute_payload({"id": "100"}, "我的学习心得", body)
    print("  标题  :", repr(pl["pre_title"]), "→", repr(pl["title"]))
    print("  正文  :", "有改动" if pl["content"] else "无改动",
          f"| 新增品牌词 {pl['body_added']} 处")
    print("  替换记录:")
    for r in pl["revisions"]:
        print(f"    {r['rule_id']} {r['rule_label']}")
    print("  幂等检查（再算一次应无改动）:")
    pl2 = plane.compute_payload({"id": "100"}, pl["title"], pl["content"])
    print("   ", pl2.get("note"), "| revisions =", len(pl2["revisions"]))

    print()
    print("== replace_content 安全闸门（该篇有 1 张图） ==")
    pl3 = plane.compute_payload({"id": "100"}, "标题", body,
                                mode="replace_content", preset="whatever")
    print("  blocked:", pl3.get("blocked"), "|", pl3.get("note"))

    print()
    print("== 本地任务闭环 ==")
    job = plane.make_job(["100"], note="自检")
    print("  job:", job["job_id"], "| items:", len(job["items"]))
    got = plane.payload(job["job_id"], "100")
    print("  payload 取回:", "成功" if got else "失败",
          "| 标题 →", repr((got or {}).get("title")))
    plane.report_item(job["job_id"], {"id": "100", "status": "done",
                                      "title_after": (got or {}).get("title"),
                                      "message": "自检"})
    plane.finish(job["job_id"], {"done": 1})
    print("  brief:", json.dumps(plane.brief(job["job_id"]).get("job"),
                                ensure_ascii=False))
    print("  文档状态:", (st.get_document("100") or {}).get("upload_status"))
    print()
    print("== 时间线（「有记录」的可视化原料） ==")
    for e in st.timeline("100")[:6]:
        print("  ", json.dumps(e, ensure_ascii=False)[:150])
    return 0


if __name__ == "__main__":
    raise SystemExit(_selftest())
