#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""qyapp 端到端冒烟测试 —— 对着已启动的服务器跑一遍全流程。

用法：
    python3 local/qyapp.py --port 8791 --no-browser &
    python3 local/test_qyapp.py --port 8791

只做**只读**操作 + 导出到本地。不会写回知乎。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.error
import urllib.request

# 绕开系统代理，直连本机
_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

BASE = "http://127.0.0.1:8791"
TOKEN = ""
PASS = 0
FAIL = 0


def call(path: str, body=None, method: str = "") -> dict:
    url = BASE + path
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data,
                                 method=method or ("POST" if data else "GET"))
    if TOKEN:
        req.add_header("X-QY-Token", TOKEN)
    if data:
        req.add_header("Content-Type", "application/json")
    try:
        with _opener.open(req, timeout=120) as r:
            raw = r.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8")
    try:
        return json.loads(raw)
    except Exception:  # noqa: BLE001
        return {"_raw": raw[:400]}


def ok(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✓ {name}" + (f"  {detail}" if detail else ""))
    else:
        FAIL += 1
        print(f"  ✗ {name}  {detail}")


def wait(task_id: str, timeout: float = 900.0) -> dict:
    t0 = time.time()
    last = ""
    while time.time() - t0 < timeout:
        d = call(f"/api/task?id={task_id}")
        t = d.get("task") or {}
        cur = f"{t.get('state')} {t.get('done')}/{t.get('total')}"
        if cur != last:
            print(f"     … {cur} {str(t.get('current'))[:40]}")
            last = cur
        if t.get("state") != "running":
            return t
        time.sleep(1.2)
    return {"state": "timeout"}


def main() -> int:
    global BASE, TOKEN
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8791)
    ap.add_argument("--export", action="store_true", help="也测导出（会下载图片，慢）")
    a = ap.parse_args()
    BASE = f"http://127.0.0.1:{a.port}"

    print("=" * 64)
    print("  qyapp 端到端冒烟测试")
    print("=" * 64)

    # ---- 0. 页面 & 令牌 ----
    print("\n[0] 页面与令牌")
    with _opener.open(BASE + "/", timeout=20) as r:
        page = r.read().decode("utf-8")
    m = re.search(r'name="qy-token" content="([^"]+)"', page)
    ok("index.html 可访问且注入了令牌", bool(m))
    if not m:
        return 1
    TOKEN = m.group(1)
    for f in ("/style.css", "/app.js"):
        with _opener.open(BASE + f, timeout=20) as r:
            ok(f"{f} 可访问", r.status == 200, f"{len(r.read())} 字节")

    # ---- 1. 状态 ----
    print("\n[1] 账号状态")
    st = call("/api/status")
    acct = st.get("account") or {}
    ok("已登录并读到账号", bool(acct.get("name")), str(acct.get("name")))
    ok("规则条数 > 0", st.get("rules", 0) > 0, f"{st.get('rules')} 条")
    ok("默认只读", st.get("allow_write") is False)

    # ---- 2. 同步列表 ----
    print("\n[2] 同步列表")
    r = call("/api/inspect", {"kinds": ["article"], "with_body": False})
    t = wait(r["task_id"])
    ok("列表任务完成", t.get("state") == "done",
       str(t.get("error") or "")[:120])
    docs = (t.get("result") or {}).get("documents", 0)
    ok("入库条数 > 0", docs > 0, f"{docs} 条")

    arts = call("/api/articles").get("items") or []
    ok("列表接口返回文章", len(arts) > 0, f"{len(arts)} 条")
    ok("字段齐全", all(k in arts[0] for k in
                   ("doc_id", "title", "check_status", "has_body")))

    # ---- 3. 同步正文 ----
    print("\n[3] 同步正文（前 6 篇）")
    r = call("/api/inspect", {"kinds": ["article"], "with_body": True,
                              "body_limit": 6})
    t = wait(r["task_id"])
    ok("正文同步完成", t.get("state") == "done",
       str(t.get("error") or "")[:120])
    b = (t.get("result") or {}).get("body") or {}
    ok("正文入库 > 0", (b.get("ok") or 0) > 0, f"成功 {b.get('ok')} 失败 {b.get('fail')}")

    arts = call("/api/articles").get("items") or []
    have = [x for x in arts if x.get("has_body")]
    ok("有正文的文章可列出", len(have) > 0, f"{len(have)} 篇")

    # ---- 4. 敏感检查 ----
    print("\n[4] 敏感检查")
    ids = [x["doc_id"] for x in have[:6]]
    r = call("/api/check", {"doc_ids": ids})
    t = wait(r["task_id"])
    res = t.get("result") or {}
    ok("检查任务完成", t.get("state") == "done", str(t.get("error") or "")[:120])
    ok("有分级统计", all(k in res for k in ("block", "warn", "pass")),
       f"阻断 {res.get('block')} 提醒 {res.get('warn')} 通过 {res.get('pass')}")

    # ---- 5. 单篇详情 ----
    print("\n[5] 单篇详情")
    did = ids[0]
    d = call(f"/api/article/{did}")
    ok("详情返回标题", bool(d.get("title")), str(d.get("title"))[:40])
    ok("详情返回正文", bool(d.get("body_html")), f"{len(d.get('body_html') or '')} 字节")
    ok("详情返回图片清单", isinstance(d.get("images"), list),
       f"{d.get('image_count')} 张")
    ok("详情含检查结果", bool(d.get("check")))
    ok("详情含一致性报告", isinstance(d.get("consistency"), dict))

    # ---- 6. AI 提示词 ----
    print("\n[6] AI 提示词")
    p = call(f"/api/prompt/{did}")
    pr = p.get("prompt") or ""
    ok("提示词非空", len(pr) > 500, f"{p.get('chars')} 字")
    ok("提示词含正文", (d.get("title") or "")[:10] in pr or "文章" in pr)

    # ---- 7. AI 回复解析 + 交叉核对 ----
    print("\n[7] AI 回复解析与交叉核对")
    fake = ('```json\n{"findings":[{"category":"饮食","level":"warn",'
            '"quote":"我们只能吃黄豆酱配米饭","why":"可能被误读",'
            '"fix":"改成简单饮食","confidence":0.9},'
            '{"category":"编造","level":"block","quote":"这句话原文里根本没有",'
            '"why":"幻觉","fix":"无","confidence":0.1}]}\n```')
    r = call("/api/ai_ingest", {"doc_id": did, "reply": fake})
    ok("AI 回复可解析", r.get("ok") is True, str(r.get("error") or "")[:120])
    ok("编造的引文被丢弃（quote 不在原文）",
       (r.get("ai_count") or 0) <= 1,
       f"AI 命中 {r.get('ai_count')} 处（原文没有的那条应被丢弃）")
    ok("返回交叉核对结果", "cross" in r)

    # ---- 8. 审计 ----
    print("\n[8] 一致性审计")
    au = call("/api/audit")
    ok("审计接口可用", au.get("ok") is True, str(au.get("error") or "")[:120])
    ok("含本地统计", isinstance(au.get("local"), dict),
       json.dumps(au.get("local"), ensure_ascii=False)[:110])

    # ---- 9. 写保护 ----
    print("\n[9] 写保护（只读模式必须拒绝写回）")
    r = call("/api/save", {"doc_id": did, "title": "x", "body_html": "<p>x</p>"})
    ok("只读模式下拒绝写回", r.get("ok") is False and
       "只读" in str(r.get("error", "")), str(r.get("error"))[:80])

    # ---- 10. 导出（可选）----
    if a.export:
        print("\n[10] 导出 Word（单篇，高清图）")
        light = sorted(have, key=lambda x: x.get("image_count") or 0)[:2]
        eids = [x["doc_id"] for x in light]
        r = call("/api/export", {"doc_ids": eids})
        t = wait(r["task_id"])
        er = t.get("result") or {}
        ok("导出任务完成", t.get("state") == "done", str(t.get("error") or "")[:120])
        ok("生成了文件", (er.get("count") or 0) > 0,
           f"{er.get('count')} 个 · {round((er.get('total_bytes') or 0)/1048576,1)} MB")
        ok("无失败", not er.get("failed"), str(er.get("failed"))[:120])
        for f in (er.get("files") or []):
            print(f"     {f['file']}  {round(f['bytes']/1048576,2)} MB  "
                  f"图 {f['images_embedded']} 张 失败 {f['images_failed']}")

    print("\n" + "=" * 64)
    print(f"  通过 {PASS} · 失败 {FAIL}")
    print("=" * 64)
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
