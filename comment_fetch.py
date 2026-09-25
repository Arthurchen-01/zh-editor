#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""知乎评论抓取器 —— 文章与回答两套接口的统一封装。

为什么需要这个模块
------------------
知乎对「文章」和「回答」用的是**两套完全不同的评论接口**，字段名也不一样：

* 文章：``/api/v4/comment_v5/articles/{id}/root_comment``
  单条评论：作者名在 ``author.name``，正文字段 ``content``（纯文本），
  点赞数在 **``like_count``**（注意不是 ``vote_count``，后者恒为 None），
  时间为 ``created_time``（int 秒）。

* 回答：``/api/v4/answers/{id}/comments``
  单条评论：作者名在 ``author.member.name``，正文是 HTML，
  点赞数在 ``vote_count``。

写错任何一个字段名，结果都是**静默拿到一堆空值**——不报错，只是没数据。
这就是原来那份导出代码「说包含评论、实际一条没有」的成因，所以这里把差异
集中处理，并对取不满的情况**如实标注**，绝不假装取全了。

分页的现实
----------
文章的 ``paging.next`` 里 offset 形如 ``5_11528046914_0``（score_时间戳_评论id_0）。
跟随该游标能取回的数量**可能小于**接口自报的 ``counts.total_counts``
（实测 16 条只能取回 11 条）。这是服务端游标分页的固有限制，不是代码问题。
本模块的处理方式是：尽量取全 → 去重 → 返回时同时给出「实际取回」与「名义总数」，
由调用方如实写进归档，而不是把 11 当成 16。
"""

from __future__ import annotations

import html as _html
import random
import re
import time
from typing import Any, Dict, List, Optional

__all__ = ["fetch_comments", "CommentsResult"]

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")

_ARTICLE_URL = ("https://www.zhihu.com/api/v4/comment_v5/articles/{id}/root_comment"
                "?order_by=score&limit=10")
_ANSWER_URL = ("https://www.zhihu.com/api/v4/answers/{id}/comments"
               "?order=normal&limit=20&offset={offset}")


class CommentsResult(dict):
    """一个能同时当字典用的结果容器，字段见 ``to_dict``。"""

    @property
    def items(self) -> List[Dict[str, Any]]:
        return self.get("items") or []


def _to_int(v: Any, default: int = 0) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def _author_name(comment: Dict[str, Any]) -> str:
    """两套接口的作者名位置不同，这里一并兜住。"""
    au = comment.get("author")
    if isinstance(au, dict):
        for key in ("name", "member", "user"):
            v = au.get(key)
            if isinstance(v, str) and v.strip():
                return v.strip()
            if isinstance(v, dict):
                nm = v.get("name")
                if nm:
                    return str(nm).strip()
    for key in ("author_name", "name"):
        v = comment.get(key)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return "(匿名)"


def _like_count(comment: Dict[str, Any]) -> int:
    """文章接口用 like_count，回答接口用 vote_count —— 两个都试。"""
    for key in ("like_count", "vote_count", "liked_count"):
        if key in comment:
            n = _to_int(comment.get(key), -1)
            if n >= 0:
                return n
    return 0


def _plain(text: Any) -> str:
    if text is None:
        return ""
    s = str(text)
    if "<" in s and ">" in s:
        s = _TAG_RE.sub("", s)
        s = _html.unescape(s)
    return _WS_RE.sub(" ", s).strip()


def _normalise(comment: Dict[str, Any], kind: str) -> Dict[str, Any]:
    tags = comment.get("comment_tag") or []
    region = ""
    if isinstance(tags, list):
        for t in tags:
            if isinstance(t, dict) and t.get("type") == "ip_info":
                region = str(t.get("text") or "")
                break
    return {
        "id": str(comment.get("id") or ""),
        "author": _author_name(comment),
        "content": _plain(comment.get("content")),
        "created": _to_int(comment.get("created_time")) or None,
        "likes": _like_count(comment),
        "is_author": bool(comment.get("is_author")),
        "region": region,
        "replies": _to_int(comment.get("child_comment_count")),
        "kind": kind,
    }


def _get(session, url: str, retries: int = 2) -> Optional[Dict[str, Any]]:
    delay = 0.8
    last_err = ""
    for attempt in range(retries + 1):
        try:
            r = session.get(url, timeout=25)
            if r.status_code == 200:
                return r.json()
            last_err = "HTTP %d %s" % (r.status_code, (r.text or "")[:110])
        except Exception as exc:  # noqa: BLE001
            last_err = "%s: %s" % (type(exc).__name__, exc)
        if attempt < retries:
            time.sleep(delay)
            delay *= 2
    return {"__error__": last_err, "data": []}


def _fetch_article(session, aid: str, max_pages: int,
                   max_items: int) -> Dict[str, Any]:
    url = _ARTICLE_URL.format(id=aid)
    items: List[Dict[str, Any]] = []
    seen = set()
    nominal: Optional[int] = None
    err = ""
    pages = 0
    while url and pages < max_pages and len(items) < max_items:
        j = _get(session, url)
        if j is None:
            err = "请求无响应"
            break
        if j.get("__error__"):
            err = j["__error__"]
            break
        pages += 1
        counts = j.get("counts") or {}
        if nominal is None and isinstance(counts.get("total_counts"), int):
            nominal = counts["total_counts"]
        page = j.get("data") or []
        fresh = 0
        for c in page:
            if not isinstance(c, dict):
                continue
            cid = str(c.get("id") or "")
            if not cid or cid in seen:
                continue
            seen.add(cid)
            items.append(_normalise(c, "article"))
            fresh += 1
        pg = j.get("paging") or {}
        if pg.get("is_end") or not page or fresh == 0:
            break
        nxt = pg.get("next")
        if not nxt or nxt == url:
            break
        url = nxt
        time.sleep(random.uniform(0.5, 1.1))
    return {"items": items, "nominal": nominal, "pages": pages, "error": err}


def _fetch_answer(session, aid: str, max_pages: int,
                  max_items: int) -> Dict[str, Any]:
    items: List[Dict[str, Any]] = []
    seen = set()
    nominal: Optional[int] = None
    err = ""
    pages = 0
    offset = 0
    while pages < max_pages and len(items) < max_items:
        url = _ANSWER_URL.format(id=aid, offset=offset)
        j = _get(session, url)
        if j is None:
            err = "请求无响应"
            break
        if j.get("__error__"):
            err = j["__error__"]
            break
        pages += 1
        pg = j.get("paging") or {}
        if nominal is None and isinstance(pg.get("totals"), int):
            nominal = pg["totals"]
        page = j.get("data") or []
        fresh = 0
        for c in page:
            if not isinstance(c, dict):
                continue
            cid = str(c.get("id") or "")
            if not cid or cid in seen:
                continue
            seen.add(cid)
            items.append(_normalise(c, "answer"))
            fresh += 1
        if pg.get("is_end") or not page or fresh == 0:
            break
        offset += len(page)
        time.sleep(random.uniform(0.5, 1.1))
    return {"items": items, "nominal": nominal, "pages": pages, "error": err}


def fetch_comments(session, kind: str, item_id: str, nominal_count: int = 0,
                   max_pages: int = 12, max_items: int = 400) -> CommentsResult:
    """抓取一条内容下的评论。

    :param session: 已带知乎登录态的 ``requests.Session``
    :param kind: ``"article"`` 或 ``"answer"``
    :param item_id: 内容 id
    :param nominal_count: 列表接口自报的评论数，用于对照
    :returns: ``{items, fetched, nominal, complete, pages, error}``

    ``complete`` 为 False 时表示**没取全**——调用方必须把这个事实写进产物，
    不能把「取了 11 条」说成「评论已全部归档」。
    """
    kind = (kind or "article").strip().lower()
    if kind not in ("article", "answer"):
        kind = "article"
    try:
        if kind == "article":
            res = _fetch_article(session, str(item_id), max_pages, max_items)
        else:
            res = _fetch_answer(session, str(item_id), max_pages, max_items)
    except Exception as exc:  # noqa: BLE001
        res = {"items": [], "nominal": nominal_count or None,
               "pages": 0, "error": "%s: %s" % (type(exc).__name__, exc)}

    items = res.get("items") or []
    nominal = res.get("nominal")
    if nominal is None:
        nominal = nominal_count or None
    complete = bool(nominal is not None and len(items) >= nominal) or \
        (nominal is None and not res.get("error"))

    return CommentsResult({
        "items": items,
        "fetched": len(items),
        "nominal": nominal,
        "complete": complete,
        "pages": res.get("pages") or 0,
        "error": res.get("error") or "",
        "kind": kind,
    })
