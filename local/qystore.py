#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""qystore —— 本地文档库（SQLite，标准库 sqlite3，零第三方依赖）。

它解决什么问题
--------------
用户的原话：「修改完之后保证重新编辑上传的时候文档是一致的，图片都在，并且
所有替换内容（什么大学或者法规 都会被更改）一定不能搞混，一定要有记录，这一点切记。」

「不能搞混、一定要有记录」在工程上只有一种可靠实现：**每次改动都留痕，
每条痕都能被机器复算**。所以本模块不做「看起来像日志」的文本记录，而是做三张
可查询、可校验、可回滚的表：

    documents   每个文档一行（当前状态、同步时间、检查状态、上传状态）
    snapshots   每个文档的**内容快照**（含图片清单指纹）—— 一致性校验的基准
    revisions   每一次替换的**逐条记录**（规则号、替换前、替换后、是否已应用、是否已复核）

为什么快照要带「图片清单指纹」
------------------------------
用户明确要求「图片都在」。只比正文文本哈希是不够的：知乎保存时会把
`pic1.zhimg.com/v2-abc_1440w.jpg` 改写成带 `data-pid` 的形态，URL 会漂移，
但**图片本身没变**。所以图片的同一性判定必须锚在「图片 token」上，
而不是锚在整段 HTML 上。见 `image_manifest()`。

一致性判定的三个层次（从宽到严）
--------------------------------
    text_hash    只比可见文本          —— 判定「内容有没有被改坏」
    image_sig    只比图片 token 集合    —— 判定「图片有没有丢/多/换」
    full_hash    原始 HTML 字节         —— 仅在完全同源时相等，知乎回写必然不等

上传前用 text_hash + image_sig 做闸门；full_hash 只用于本地文件之间的比对。
"""
from __future__ import annotations

import hashlib
import html as html_mod
import json
import re
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

__version__ = "1.0.0"

SCHEMA_VERSION = 3

# 默认库文件位置：<项目根>/data/qyedu.db
_DEFAULT_DB = Path(__file__).resolve().parent.parent / "data" / "qyedu.db"

_TAG_RE = re.compile(r"<[^>]+>")
_IMG_RE = re.compile(r"<img\b[^>]*>", re.I)
_ATTR_RE = re.compile(r"""([a-zA-Z_:][-\w:.]*)\s*=\s*("([^"]*)"|'([^']*)'|([^\s"'>]+))""")

# 知乎 CDN 图片 URL 里的稳定指纹：v2-<hash>_<size>.<ext>
_ZHIMG_TOKEN_RE = re.compile(r"(v2-[0-9a-f]{20,})", re.I)


# --------------------------------------------------------------------------- #
# HTML / 图片工具（纯函数，无副作用）
# --------------------------------------------------------------------------- #

def html_to_text(html: str) -> str:
    """富文本 → 可见纯文本。用于「内容有没有被改坏」的判定。"""
    if not html:
        return ""
    s = re.sub(r"<br\s*/?>", "\n", html, flags=re.I)
    s = re.sub(r"</(p|h[1-6]|li|blockquote|div|figure)>", "\n", s, flags=re.I)
    s = re.sub(r"<img\b[^>]*>", " [图片] ", s, flags=re.I)
    s = _TAG_RE.sub("", s)
    s = html_mod.unescape(s)
    s = re.sub(r"[ \t\u00a0]+", " ", s)
    s = re.sub(r"\n\s*\n+", "\n", s)
    return s.strip()


def text_hash(html: str) -> str:
    """可见文本哈希 —— 判定「内容是否被改坏」。"""
    norm = re.sub(r"\s+", " ", html_to_text(html)).strip()
    return hashlib.sha256(norm.encode("utf-8")).hexdigest()


def full_hash(html: str) -> str:
    """原始 HTML 哈希 —— 仅用于本地文件之间比对。"""
    return hashlib.sha256((html or "").encode("utf-8")).hexdigest()


def _attrs(tag: str) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for m in _ATTR_RE.finditer(tag):
        key = m.group(1).lower()
        val = m.group(3) if m.group(3) is not None else (
            m.group(4) if m.group(4) is not None else m.group(5))
        out[key] = html_mod.unescape(val or "")
    return out


def image_manifest(html: str) -> List[Dict[str, Any]]:
    """抽出一篇正文里的全部图片，给出**稳定身份**。

    身份优先级（越靠前越稳）：
      1. `data-original-token`  —— 知乎给的原图 token，最稳
      2. URL 里的 `v2-<hash>`   —— CDN 文件名里的内容指纹，跨尺寸稳定
      3. 完整 URL 去 query      —— 兜底

    这样即使知乎把 `_1440w` 改成 `_b`、给 URL 挂上签名参数，
    同一张图的身份仍然不变，不会误报「图片丢了」。
    """
    out: List[Dict[str, Any]] = []
    for i, m in enumerate(_IMG_RE.finditer(html or "")):
        a = _attrs(m.group(0))
        src = a.get("src") or ""
        original = a.get("data-original") or a.get("data-actualsrc") or ""
        token = a.get("data-original-token") or ""
        if not token:
            hit = _ZHIMG_TOKEN_RE.search(src) or _ZHIMG_TOKEN_RE.search(original)
            if hit:
                token = hit.group(1).lower()
        if not token:
            token = (src or original).split("?")[0].strip()
        out.append({
            "index": i,
            "token": token,
            "src": src,
            "original": original,
            "alt": a.get("alt") or a.get("data-caption") or "",
            "width": a.get("data-rawwidth") or a.get("width") or "",
            "height": a.get("data-rawheight") or a.get("height") or "",
        })
    return out


def image_signature(html: str) -> str:
    """图片集合指纹 —— 判定「图片有没有丢/多/换」。

    注意：**排序后再哈希**，因为知乎保存时可能重排 DOM 顺序，
    但「还是那几张图」这件事不该被判为不一致。
    """
    tokens = sorted(it["token"] for it in image_manifest(html))
    return hashlib.sha256("|".join(tokens).encode("utf-8")).hexdigest()


def image_diff(before_html: str, after_html: str) -> Dict[str, Any]:
    """给出两次内容之间图片的增删明细。上传前的闸门就看这个。"""
    b = image_manifest(before_html)
    a = image_manifest(after_html)
    bs, as_ = {it["token"] for it in b}, {it["token"] for it in a}
    return {
        "before_count": len(b),
        "after_count": len(a),
        "same": bs == as_,
        "lost": sorted(bs - as_),
        "added": sorted(as_ - bs),
        "kept": len(bs & as_),
    }


# --------------------------------------------------------------------------- #
# 数据库
# --------------------------------------------------------------------------- #

_SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS meta (
    k TEXT PRIMARY KEY,
    v TEXT
);

-- 每个文档一行：这是「当前状态」的权威来源
CREATE TABLE IF NOT EXISTS documents (
    doc_id          TEXT PRIMARY KEY,       -- 知乎 id
    kind            TEXT NOT NULL,          -- article / answer / pin
    url             TEXT,
    title_now       TEXT,                   -- 上次同步时线上标题
    title_original  TEXT,                   -- 首次见到时的标题（永不覆盖）
    author          TEXT,
    text_hash       TEXT,                   -- 上次同步时正文可见文本哈希
    image_sig       TEXT,                   -- 上次同步时图片集合指纹
    image_count     INTEGER DEFAULT 0,
    body_len        INTEGER DEFAULT 0,
    comment_count   INTEGER DEFAULT 0,
    voteup_count    INTEGER DEFAULT 0,
    created_at      INTEGER,
    updated_at      INTEGER,
    first_seen      INTEGER,
    synced_at       INTEGER,
    check_status    TEXT DEFAULT 'unchecked',   -- unchecked/pass/warn/block
    check_hits      INTEGER DEFAULT 0,
    upload_status   TEXT DEFAULT 'untouched',   -- untouched/dirty/uploaded/failed
    upload_at       INTEGER,
    note            TEXT DEFAULT ''
);

-- 内容快照：一致性校验的基准。label 说明这份快照是什么时候、为什么存的
CREATE TABLE IF NOT EXISTS snapshots (
    snap_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    doc_id      TEXT NOT NULL,
    label       TEXT NOT NULL,      -- original / pre_upload / post_upload / manual
    taken_at    INTEGER NOT NULL,
    title       TEXT,
    content     TEXT,
    text_hash   TEXT,
    image_sig   TEXT,
    image_count INTEGER DEFAULT 0,
    image_json  TEXT,               -- 完整图片清单（含 URL），用于还原与审计
    body_len    INTEGER DEFAULT 0,
    FOREIGN KEY (doc_id) REFERENCES documents(doc_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS ix_snap_doc ON snapshots(doc_id, taken_at DESC);
CREATE UNIQUE INDEX IF NOT EXISTS ux_snap_original
    ON snapshots(doc_id, label) WHERE label IN ('original', 'pre_upload');

-- 逐条替换记录：用户要的「不能搞混」就靠这张表
CREATE TABLE IF NOT EXISTS revisions (
    rev_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    doc_id      TEXT NOT NULL,
    created_at  INTEGER NOT NULL,
    kind        TEXT NOT NULL,      -- title / body / replace_content / manual_edit
    rule_id     TEXT,               -- 触发的规则号（如 S01 邪教 / T01 品牌前置）
    rule_label  TEXT,
    field       TEXT,               -- title / content
    before_text TEXT,
    after_text  TEXT,
    before_hash TEXT,
    after_hash  TEXT,
    span_start  INTEGER,            -- 在原文中的位置，便于前端高亮
    span_end    INTEGER,
    applied     INTEGER DEFAULT 0,
    applied_at  INTEGER,
    verified    INTEGER,            -- NULL=未复核 / 1=一致 / 0=不一致
    verify_note TEXT,
    FOREIGN KEY (doc_id) REFERENCES documents(doc_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS ix_rev_doc ON revisions(doc_id, created_at DESC);

-- 本地任务（替代云端 job）：一次「批量处理」就是一行
CREATE TABLE IF NOT EXISTS jobs (
    job_id      TEXT PRIMARY KEY,
    created_at  INTEGER NOT NULL,
    updated_at  INTEGER,
    mode        TEXT DEFAULT 'local',
    status      TEXT DEFAULT 'open',    -- open/running/done/aborted
    note        TEXT DEFAULT '',
    summary     TEXT
);

CREATE TABLE IF NOT EXISTS job_items (
    job_id      TEXT NOT NULL,
    doc_id      TEXT NOT NULL,
    status      TEXT DEFAULT 'pending', -- pending/running/done/skipped/failed
    message     TEXT DEFAULT '',
    payload     TEXT,                   -- 本地算好的最终稿（title/content）
    record      TEXT,                   -- 执行结果明细
    updated_at  INTEGER,
    PRIMARY KEY (job_id, doc_id),
    FOREIGN KEY (job_id) REFERENCES jobs(job_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS ix_ji_status ON job_items(job_id, status);

-- 敏感内容检查结果：一篇文章一次检查一行
CREATE TABLE IF NOT EXISTS checks (
    check_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    doc_id      TEXT NOT NULL,
    checked_at  INTEGER NOT NULL,
    engine      TEXT,               -- rules / ai
    level       TEXT,               -- pass/warn/block
    hits        INTEGER DEFAULT 0,
    findings    TEXT,               -- JSON 数组
    FOREIGN KEY (doc_id) REFERENCES documents(doc_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS ix_check_doc ON checks(doc_id, checked_at DESC);

-- 审计流水：所有动作一行，人可读
CREATE TABLE IF NOT EXISTS audit (
    seq         INTEGER PRIMARY KEY AUTOINCREMENT,
    at          INTEGER NOT NULL,
    doc_id      TEXT,
    action      TEXT NOT NULL,
    detail      TEXT
);
CREATE INDEX IF NOT EXISTS ix_audit_doc ON audit(doc_id, at DESC);
"""


class Store:
    """本地文档库。线程安全（单写锁 + WAL 多读）。

    所有方法都是「打开即用」，不需要调用方管理连接。
    """

    def __init__(self, path: Optional[Path | str] = None) -> None:
        self.path = Path(path) if path else _DEFAULT_DB
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._init()

    # ---------------- 底层 ---------------- #

    def _conn(self) -> sqlite3.Connection:
        c = sqlite3.connect(str(self.path), timeout=15.0,
                            check_same_thread=False)
        c.row_factory = sqlite3.Row
        return c

    def _init(self) -> None:
        with self._lock:
            c = self._conn()
            try:
                c.executescript(_SCHEMA)
                c.execute("INSERT OR REPLACE INTO meta(k,v) VALUES('schema',?)",
                          (str(SCHEMA_VERSION),))
                c.execute("INSERT OR IGNORE INTO meta(k,v) VALUES('created',?)",
                          (str(int(time.time())),))
                c.commit()
            finally:
                c.close()

    def _exec(self, sql: str, args: Sequence[Any] = ()) -> sqlite3.Cursor:
        c = self._conn()
        try:
            cur = c.execute(sql, args)
            c.commit()
            return cur
        finally:
            c.close()

    def _rows(self, sql: str, args: Sequence[Any] = ()) -> List[sqlite3.Row]:
        c = self._conn()
        try:
            return list(c.execute(sql, args))
        finally:
            c.close()

    def _one(self, sql: str, args: Sequence[Any] = ()) -> Optional[sqlite3.Row]:
        r = self._rows(sql, args)
        return r[0] if r else None

    # ---------------- 审计 ---------------- #

    def log(self, action: str, doc_id: Optional[str] = None,
            detail: Any = "") -> None:
        if not isinstance(detail, str):
            detail = json.dumps(detail, ensure_ascii=False)[:2000]
        with self._lock:
            self._exec("INSERT INTO audit(at,doc_id,action,detail) VALUES(?,?,?,?)",
                       (int(time.time()), doc_id, action, detail[:2000]))

    def audit_tail(self, doc_id: Optional[str] = None, limit: int = 50) -> List[Dict[str, Any]]:
        if doc_id:
            rows = self._rows(
                "SELECT * FROM audit WHERE doc_id=? ORDER BY seq DESC LIMIT ?",
                (doc_id, limit))
        else:
            rows = self._rows("SELECT * FROM audit ORDER BY seq DESC LIMIT ?",
                              (limit,))
        return [dict(r) for r in rows]

    # ---------------- 文档 ---------------- #

    def upsert_document(self, doc: Dict[str, Any],
                        body_html: str = "") -> str:
        """把一次「线上读取」的结果写入库。title_original 永不覆盖。

        返回 doc_id。
        """
        did = str(doc.get("id") or doc.get("doc_id") or "").strip()
        if not did:
            raise ValueError("doc_id 不能为空")
        now = int(time.time())
        # 没拉正文时必须写 NULL 而不是 0 ——
        # 「0 张图」和「还不知道有几张图」是两件事，混起来会让
        # 按图片数排序、图片漂移审计全部失真。
        has_body = bool(body_html)
        th = text_hash(body_html) if has_body else None
        imgs = image_manifest(body_html) if has_body else []
        isig = image_signature(body_html) if has_body else None
        icnt = len(imgs) if has_body else None
        blen = len(body_html) if has_body else None

        with self._lock:
            old = self._one("SELECT * FROM documents WHERE doc_id=?", (did,))
            if old is None:
                self._exec(
                    """INSERT INTO documents
                       (doc_id,kind,url,title_now,title_original,author,
                        text_hash,image_sig,image_count,body_len,
                        comment_count,voteup_count,created_at,updated_at,
                        first_seen,synced_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (did, doc.get("type") or "article", doc.get("url") or "",
                     doc.get("title") or "", doc.get("title") or "",
                     doc.get("author") or "",
                     th, isig, icnt, blen,
                     int(doc.get("comment_count") or 0),
                     int(doc.get("voteup_count") or 0),
                     int(doc.get("created") or 0), int(doc.get("updated") or 0),
                     now, now))
                self.log("doc.new", did, doc.get("title") or "")
            else:
                self._exec(
                    """UPDATE documents SET
                         kind=?, url=?, title_now=?, author=?,
                         text_hash=?, image_sig=?, image_count=?, body_len=?,
                         comment_count=?, voteup_count=?, created_at=?, updated_at=?,
                         synced_at=?
                       WHERE doc_id=?""",
                    (doc.get("type") or old["kind"], doc.get("url") or old["url"],
                     doc.get("title") or old["title_now"],
                     doc.get("author") or old["author"],
                     th if has_body else old["text_hash"],
                     isig if has_body else old["image_sig"],
                     icnt if has_body else old["image_count"],
                     blen if has_body else old["body_len"],
                     int(doc.get("comment_count") or old["comment_count"]),
                     int(doc.get("voteup_count") or old["voteup_count"]),
                     int(doc.get("created") or old["created_at"]),
                     int(doc.get("updated") or old["updated_at"]),
                     now, did))
        return did

    def get_document(self, doc_id: str) -> Optional[Dict[str, Any]]:
        r = self._one("SELECT * FROM documents WHERE doc_id=?", (str(doc_id),))
        return dict(r) if r else None

    def list_documents(self, kind: Optional[str] = None,
                       order: str = "updated_at DESC",
                       limit: int = 0,
                       only: Optional[str] = None) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM documents"
        args: List[Any] = []
        where: List[str] = []
        if kind:
            where.append("kind=?")
            args.append(kind)
        if only == "has_brand":
            where.append("title_now LIKE '%清一新教育%'")
        elif only == "no_brand":
            where.append("title_now NOT LIKE '%清一新教育%'")
        elif only == "dirty":
            where.append("upload_status='dirty'")
        elif only == "blocked":
            where.append("check_status='block'")
        if where:
            sql += " WHERE " + " AND ".join(where)
        # 白名单排序，避免拼接注入
        if order not in ("updated_at DESC", "updated_at ASC", "title_now ASC",
                         "synced_at DESC", "image_count DESC"):
            order = "updated_at DESC"
        sql += f" ORDER BY {order}"
        if limit:
            sql += " LIMIT ?"
            args.append(int(limit))
        return [dict(r) for r in self._rows(sql, args)]

    def set_document_status(self, doc_id: str, **kw: Any) -> None:
        allowed = {"check_status", "check_hits", "upload_status", "upload_at",
                   "title_now", "note"}
        sets = {k: v for k, v in kw.items() if k in allowed}
        if not sets:
            return
        sql = "UPDATE documents SET " + ",".join(f"{k}=?" for k in sets) \
            + " WHERE doc_id=?"
        with self._lock:
            self._exec(sql, (*sets.values(), str(doc_id)))

    def stats(self) -> Dict[str, Any]:
        g = lambda sql, a=(): (self._one(sql, a) or [0])[0]  # noqa: E731
        return {
            "documents": g("SELECT COUNT(*) FROM documents"),
            "branded": g("SELECT COUNT(*) FROM documents WHERE title_now LIKE '%清一新教育%'"),
            "unbranded": g("SELECT COUNT(*) FROM documents WHERE title_now NOT LIKE '%清一新教育%'"),
            "dirty": g("SELECT COUNT(*) FROM documents WHERE upload_status='dirty'"),
            "uploaded": g("SELECT COUNT(*) FROM documents WHERE upload_status='uploaded'"),
            "failed": g("SELECT COUNT(*) FROM documents WHERE upload_status='failed'"),
            "blocked": g("SELECT COUNT(*) FROM documents WHERE check_status='block'"),
            "warned": g("SELECT COUNT(*) FROM documents WHERE check_status='warn'"),
            "snapshots": g("SELECT COUNT(*) FROM snapshots"),
            "revisions": g("SELECT COUNT(*) FROM revisions"),
            "applied_revisions": g("SELECT COUNT(*) FROM revisions WHERE applied=1"),
            "audit": g("SELECT COUNT(*) FROM audit"),
            "db_path": str(self.path),
            "db_bytes": self.path.stat().st_size if self.path.exists() else 0,
        }

    # ---------------- 快照 ---------------- #

    def add_snapshot(self, doc_id: str, title: str, content: str,
                     label: str = "manual") -> int:
        """存一份内容快照。`original` / `pre_upload` 是**唯一**的：已存在则不覆盖。

        这条唯一约束是「原貌永不丢失」的技术保证 —— 与执行器 backup()
        的 never-overwrite 语义一致。
        """
        now = int(time.time())
        imgs = image_manifest(content)
        with self._lock:
            if label in ("original", "pre_upload"):
                hit = self._one(
                    "SELECT snap_id FROM snapshots WHERE doc_id=? AND label=?",
                    (str(doc_id), label))
                if hit:
                    return int(hit["snap_id"])
            cur = self._exec(
                """INSERT INTO snapshots
                   (doc_id,label,taken_at,title,content,text_hash,image_sig,
                    image_count,image_json,body_len)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (str(doc_id), label, now, title or "", content or "",
                 text_hash(content), image_signature(content), len(imgs),
                 json.dumps(imgs, ensure_ascii=False), len(content or "")))
            sid = int(cur.lastrowid or 0)
            self.log("snap.add", doc_id, f"{label} #{sid} 图 {len(imgs)} 张")
            return sid

    def latest_snapshot(self, doc_id: str,
                        label: Optional[str] = None) -> Optional[Dict[str, Any]]:
        if label:
            r = self._one(
                """SELECT * FROM snapshots WHERE doc_id=? AND label=?
                   ORDER BY taken_at DESC LIMIT 1""", (str(doc_id), label))
        else:
            r = self._one(
                """SELECT * FROM snapshots WHERE doc_id=?
                   ORDER BY taken_at DESC LIMIT 1""", (str(doc_id),))
        if not r:
            return None
        d = dict(r)
        try:
            d["images"] = json.loads(d.get("image_json") or "[]")
        except Exception:  # noqa: BLE001
            d["images"] = []
        return d

    def list_snapshots(self, doc_id: str) -> List[Dict[str, Any]]:
        return [dict(r) for r in self._rows(
            """SELECT snap_id,doc_id,label,taken_at,title,text_hash,image_sig,
                      image_count,body_len
               FROM snapshots WHERE doc_id=? ORDER BY taken_at DESC""",
            (str(doc_id),))]

    def restore_original(self, doc_id: str) -> Optional[Dict[str, Any]]:
        """取回最初快照，供「一键还原」使用。"""
        return self.latest_snapshot(doc_id, "original")

    # ---------------- 替换记录 ---------------- #

    def add_revision(self, doc_id: str, kind: str, field: str,
                     before: str, after: str,
                     rule_id: str = "", rule_label: str = "",
                     span: Optional[Tuple[int, int]] = None,
                     applied: bool = False) -> int:
        """记一条替换。**这是「不能搞混」的核心表**。

        kind: title / body / replace_content / manual_edit
        field: title / content
        """
        now = int(time.time())
        cur = self._exec(
            """INSERT INTO revisions
               (doc_id,created_at,kind,rule_id,rule_label,field,
                before_text,after_text,before_hash,after_hash,
                span_start,span_end,applied,applied_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (str(doc_id), now, kind, rule_id, rule_label, field,
             before or "", after or "", full_hash(before), full_hash(after),
             (span[0] if span else None), (span[1] if span else None),
             1 if applied else 0, now if applied else None))
        return int(cur.lastrowid or 0)

    def mark_applied(self, rev_ids: Iterable[int]) -> None:
        now = int(time.time())
        with self._lock:
            for rid in rev_ids:
                self._exec("UPDATE revisions SET applied=1, applied_at=? WHERE rev_id=?",
                           (now, int(rid)))

    def mark_verified(self, rev_id: int, ok: bool, note: str = "") -> None:
        with self._lock:
            self._exec("UPDATE revisions SET verified=?, verify_note=? WHERE rev_id=?",
                       (1 if ok else 0, note[:500], int(rev_id)))

    def list_revisions(self, doc_id: str, limit: int = 200) -> List[Dict[str, Any]]:
        return [dict(r) for r in self._rows(
            """SELECT * FROM revisions WHERE doc_id=?
               ORDER BY created_at DESC, rev_id DESC LIMIT ?""",
            (str(doc_id), int(limit)))]

    def unverified_revisions(self, doc_id: str) -> List[Dict[str, Any]]:
        return [dict(r) for r in self._rows(
            """SELECT * FROM revisions
               WHERE doc_id=? AND applied=1 AND verified IS NULL
               ORDER BY rev_id""", (str(doc_id),))]

    def timeline(self, doc_id: str, limit: int = 60) -> List[Dict[str, Any]]:
        """一个文档的完整时间线：快照 + 替换 + 检查 + 审计，按时间合并。"""
        items: List[Dict[str, Any]] = []
        for s in self.list_snapshots(doc_id)[:limit]:
            items.append({"at": s["taken_at"], "type": "snapshot",
                          "label": s["label"], "text_hash": s["text_hash"],
                          "image_count": s["image_count"],
                          "title": s.get("title")})
        for r in self.list_revisions(doc_id, limit)[:limit]:
            items.append({"at": r["created_at"], "type": "revision",
                          "kind": r["kind"], "rule_id": r["rule_id"],
                          "rule_label": r["rule_label"], "field": r["field"],
                          "before": (r["before_text"] or "")[:200],
                          "after": (r["after_text"] or "")[:200],
                          "applied": bool(r["applied"]),
                          "verified": r["verified"]})
        for c in self.list_checks(doc_id, limit)[:limit]:
            items.append({"at": c["checked_at"], "type": "check",
                          "engine": c["engine"], "level": c["level"],
                          "hits": c["hits"]})
        items.sort(key=lambda x: x["at"] or 0, reverse=True)
        return items[:limit]

    # ---------------- 检查结果 ---------------- #

    def add_check(self, doc_id: str, engine: str, level: str,
                  findings: List[Dict[str, Any]]) -> int:
        now = int(time.time())
        cur = self._exec(
            """INSERT INTO checks(doc_id,checked_at,engine,level,hits,findings)
               VALUES (?,?,?,?,?,?)""",
            (str(doc_id), now, engine, level, len(findings),
             json.dumps(findings, ensure_ascii=False)))
        self.set_document_status(doc_id, check_status=level,
                                 check_hits=len(findings))
        self.log("check", doc_id, f"{engine} → {level}（{len(findings)} 处）")
        return int(cur.lastrowid or 0)

    def list_checks(self, doc_id: str, limit: int = 20) -> List[Dict[str, Any]]:
        out = []
        for r in self._rows(
                """SELECT * FROM checks WHERE doc_id=?
                   ORDER BY checked_at DESC LIMIT ?""", (str(doc_id), int(limit))):
            d = dict(r)
            try:
                d["findings"] = json.loads(d.get("findings") or "[]")
            except Exception:  # noqa: BLE001
                d["findings"] = []
            out.append(d)
        return out

    def latest_check(self, doc_id: str) -> Optional[Dict[str, Any]]:
        r = self.list_checks(doc_id, 1)
        return r[0] if r else None

    # ---------------- 本地任务 ---------------- #

    def create_job(self, job_id: str, doc_ids: Sequence[str],
                   note: str = "") -> str:
        now = int(time.time())
        with self._lock:
            self._exec(
                "INSERT OR REPLACE INTO jobs(job_id,created_at,updated_at,mode,status,note) "
                "VALUES(?,?,?,'local','open',?)", (job_id, now, now, note))
            for d in doc_ids:
                self._exec(
                    "INSERT OR IGNORE INTO job_items(job_id,doc_id,status,updated_at) "
                    "VALUES(?,?,'pending',?)", (job_id, str(d), now))
            self.log("job.new", None, f"{job_id}：{len(doc_ids)} 篇")
        return job_id

    def get_job(self, job_id: str) -> Optional[Dict[str, Any]]:
        j = self._one("SELECT * FROM jobs WHERE job_id=?", (job_id,))
        if not j:
            return None
        d = dict(j)
        d["items"] = [dict(r) for r in self._rows(
            "SELECT * FROM job_items WHERE job_id=? ORDER BY rowid", (job_id,))]
        return d

    def list_jobs(self, limit: int = 20) -> List[Dict[str, Any]]:
        return [dict(r) for r in self._rows(
            "SELECT * FROM jobs ORDER BY created_at DESC LIMIT ?", (int(limit),))]

    def set_job_item(self, job_id: str, doc_id: str, status: str,
                     message: str = "", payload: Any = None,
                     record: Any = None) -> None:
        now = int(time.time())
        sets = ["status=?", "message=?", "updated_at=?"]
        args: List[Any] = [status, message[:800], now]
        if payload is not None:
            sets.append("payload=?")
            args.append(json.dumps(payload, ensure_ascii=False))
        if record is not None:
            sets.append("record=?")
            args.append(json.dumps(record, ensure_ascii=False))
        args.extend([job_id, str(doc_id)])
        with self._lock:
            self._exec(f"UPDATE job_items SET {','.join(sets)} "
                       f"WHERE job_id=? AND doc_id=?", tuple(args))
            self._exec("UPDATE jobs SET updated_at=?, status=? WHERE job_id=?",
                       (now, "running", job_id))

    def job_pending(self, job_id: str, limit: int = 0) -> List[Dict[str, Any]]:
        sql = ("SELECT * FROM job_items WHERE job_id=? AND status='pending' "
               "ORDER BY rowid")
        args: List[Any] = [job_id]
        if limit:
            sql += " LIMIT ?"
            args.append(int(limit))
        return [dict(r) for r in self._rows(sql, args)]

    def finish_job(self, job_id: str, status: str = "done",
                   summary: Any = None) -> None:
        with self._lock:
            self._exec("UPDATE jobs SET status=?, updated_at=?, summary=? WHERE job_id=?",
                       (status, int(time.time()),
                        json.dumps(summary or {}, ensure_ascii=False), job_id))
            self.log("job.finish", None, f"{job_id} → {status}")

    # ---------------- 一致性闸门 ---------------- #

    def consistency_report(self, doc_id: str) -> Dict[str, Any]:
        """上传前的一致性自检 —— 用户「文档是一致的，图片都在」的可执行版本。

        比对对象：最近一次 `pre_upload` 快照（或 `original`） vs 库中当前状态。
        """
        doc = self.get_document(doc_id)
        if not doc:
            return {"ok": False, "note": "库里没有这个文档"}
        base = (self.latest_snapshot(doc_id, "pre_upload")
                or self.latest_snapshot(doc_id, "original"))
        if not base:
            return {"ok": False, "note": "还没有基准快照，无法比对"}
        return {
            "ok": True,
            "doc_id": doc_id,
            "base_label": base["label"],
            "base_at": base["taken_at"],
            "title_now": doc["title_now"],
            "base_title": base["title"],
            "text_same": doc["text_hash"] == base["text_hash"],
            "image_same": doc["image_sig"] == base["image_sig"],
            "images_base": base["image_count"],
            "images_now": doc["image_count"],
            "revisions_total": len(self.list_revisions(doc_id)),
            "revisions_unverified": len(self.unverified_revisions(doc_id)),
        }

    def close(self) -> None:
        pass


# --------------------------------------------------------------------------- #
# 命令行自检（无需第三方依赖即可跑）
# --------------------------------------------------------------------------- #

def _selftest() -> int:
    import tempfile
    tmp = Path(tempfile.mkdtemp()) / "t.db"
    st = Store(tmp)
    html = ('<p>第一段。</p><img src="https://pic1.zhimg.com/v2-'
            'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaa_1440w.jpg" data-original-token="tok1">'
            '<p>第二段。</p>')
    st.upsert_document({"id": "1", "type": "article", "title": "标题",
                        "url": "https://zhuanlan.zhihu.com/p/1"}, html)
    st.add_snapshot("1", "标题", html, "original")
    rid = st.add_revision("1", "title", "title", "标题", "【清一新教育】标题",
                          rule_id="T01", rule_label="标题前置品牌词", applied=True)
    st.add_check("1", "rules", "pass", [])
    print("stats      :", json.dumps(st.stats(), ensure_ascii=False))
    print("manifest   :", json.dumps(image_manifest(html), ensure_ascii=False))
    print("img_diff   :", json.dumps(image_diff(html, html), ensure_ascii=False))
    print("consistency:", json.dumps(st.consistency_report("1"), ensure_ascii=False))
    print("timeline   :", json.dumps(st.timeline("1"), ensure_ascii=False)[:400])
    print("rev_id     :", rid)
    st.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(_selftest())
