#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""生成样例交付物：真实文章 + 真实图床图片 → Word / Markdown / 检查报告。"""
import json
import pathlib
import sys
import tempfile
import time

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "local"))

import qynet  # noqa: E402
qynet.install()
import qystore, qycheck, qyplane, qydocx  # noqa: E402

OUT = pathlib.Path("/Users/rancho/WorkBuddy/2026-09-23-21-36-51/样例输出")
OUT.mkdir(parents=True, exist_ok=True)

t0 = time.time()
cookie = (ROOT / "cookie.txt").read_text(encoding="utf-8").strip()
tmp = pathlib.Path(tempfile.mkdtemp())
st = qystore.Store(tmp / "s.db")
plane = qyplane.LocalPlane(cookie, store=st, backup_dir=tmp / "bk")


def say(msg):
    print(f"[{time.time()-t0:6.1f}s] {msg}", flush=True)


say("列文章…")
plane.inspect(("article",), cap=126)
docs = st.list_documents(kind="article")
say(f"入库 {len(docs)} 篇")

POOL = 24
ids = [d["doc_id"] for d in docs][:POOL]
say(f"拉取 {POOL} 篇正文（含图片清单）…")


def prog(title, i, n):
    if i % 6 == 0 or i == n:
        say(f"  正文 {i}/{n}  {(title or '')[:34]}")


plane.sync_body(ids, progress=prog)

# 现在才有真实的图片数，按它挑
have = []
for d in docs:
    s = st.latest_snapshot(d["doc_id"])
    if s and s["image_count"]:
        have.append((s["image_count"], d))
have.sort(key=lambda t: -t[0])
say(f"有图文章 {len(have)} 篇；按图片数取前 5")
picks = [d for _, d in have[:5]] or docs[:5]
for d in picks:
    s = st.latest_snapshot(d["doc_id"])
    say(f"  图 {s['image_count']:2} 张 | {(d['title_now'] or '')[:44]}")

items = []
for d in picks:
    s = st.latest_snapshot(d["doc_id"])
    if not s:
        continue
    items.append({"id": d["doc_id"], "title": d["title_now"] or "(无标题)",
                  "html": s["content"] or "", "url": d["url"] or "",
                  "note": f"原文图片 {s['image_count']} 张"})

total_imgs = sum(1 for it in items
                 for _ in qystore.image_manifest(it["html"]))
say(f"下载真实图床图片（约 {total_imgs} 张）…")
fetcher = qydocx.ImageFetcher(cache_dir=tmp / "img")

say("生成整本 Word…")
r = qydocx.write_bundle_docx(OUT / "文章备份样例.docx", items, fetcher=fetcher,
                             title="清一新教育 · 文章备份（样例）")
say(f"  整本: {r['bytes']:,} 字节 | {r['documents']} 篇 | "
    f"内嵌图 {r['images_embedded']} 张 | 失败 {r['images_failed']}")
if r["failed_urls"]:
    say(f"  失败 URL: {r['failed_urls'][:3]}")
say(f"  图片缓存: {json.dumps(fetcher.stats, ensure_ascii=False)}")
for p in r["per_document"]:
    say(f"    {(p['title'] or '')[:38]:40} 图 {p['images_embedded']}/{p['images_seen']}"
        f" | 块 {p['blocks']}")

say("生成单篇 Word…")
first = items[0]
r3 = qydocx.write_article_docx(
    OUT / "单篇样例.docx", first["title"], first["html"],
    meta={"url": first["url"], "author": "冠军班 胡启岩",
          "exported_at": time.strftime("%Y-%m-%d %H:%M"),
          "note": "本地 Word 导出器生成 · 零第三方依赖 · 图片真实内嵌"},
    fetcher=fetcher)
say(f"  {r3['bytes']:,} 字节 | 内嵌图 {r3['images_embedded']} 张")

say("生成 Markdown 备份…")
r2 = qydocx.write_markdown(OUT / "文章备份样例.md", items)
say(f"  {r2['bytes']:,} 字节")

say("生成敏感检查报告…")
lines = ["# 敏感内容检查报告（样例）", "",
         f"> 规则 {len(qycheck.RULES)} 条 · 样本 {len(items)} 篇 · "
         f"生成 {time.strftime('%Y-%m-%d %H:%M')}", "",
         "定级含义：`block` 高危必须处理 · `warn` 措辞需调整 · `info` 需人工判断。",
         "**清单里不出现「安全」结论 —— 没查到不等于没问题。**", ""]
for d in picks:
    s = st.latest_snapshot(d["doc_id"])
    if not s:
        continue
    rep = qycheck.report(qycheck.scan(d["title_now"] or "", s["content"] or ""))
    lines += [f"## {(d['title_now'] or '')[:60]}", "",
              f"- 原文：{d['url']}",
              f"- 正文 {len(s['content'] or ''):,} 字符 · 图片 {s['image_count']} 张",
              f"- 定级 **{rep['level']}** · 命中 {rep['hits']} 处"
              f"（block {rep.get('block')} / warn {rep.get('warn')} / "
              f"info {rep.get('info')}）", ""]
    if rep["findings"]:
        lines += ["| 级别 | 规则 | 原文片段 | 为什么 | 改法 |", "|---|---|---|---|---|"]
        for f in rep["findings"]:
            q = (f["text"] or "").replace("|", "\\|")
            lines.append(f"| `{f['level']}` | {f['rule_id']} | `{q}` | "
                         f"{f['label']} | {f['fix']} |")
    else:
        lines.append("_规则引擎未发现已知风险表述。这不等于没有问题。_")
    lines.append("")
(OUT / "敏感检查报告样例.md").write_text("\n".join(lines), encoding="utf-8")

# 一致性审计报告
say("生成一致性审计报告…")
plane.inspect(("article",), cap=126)
aud = plane.audit()
al = ["# 一致性审计报告（样例）", "",
      f"> 扫描 {aud['documents_scanned']} 篇 · 发现 {aud['issues']} 处", "",
      "分类说明：",
      "- `half_applied` 正文有品牌词、标题没有（历史批次只跑完一半）",
      "- `title_only` 只有标题有品牌词",
      "- `unverified` 有已应用但未复核的替换记录",
      "- `blocked_pending` 检查定级 block 但仍待上传",
      "- `image_drift` 图片数与最初快照不一致", "",
      f"分类统计：`{json.dumps(aud['by_kind'], ensure_ascii=False)}`", ""]
if aud["items"]:
    al += ["| 级别 | 类型 | 文章 ID | 标题 | 说明 | 建议动作 |", "|---|---|---|---|---|---|"]
    for i in aud["items"]:
        al.append(f"| `{i['level']}` | {i['kind']} | `{i['doc_id']}` | "
                  f"{(i['title'] or '')[:34]} | {i['note']} | {i['fix']} |")
else:
    al.append("_没有发现不一致。_")
(OUT / "一致性审计报告样例.md").write_text("\n".join(al), encoding="utf-8")
say(f"  审计: {aud['issues']} 处 {json.dumps(aud['by_kind'], ensure_ascii=False)}")

say("")
say("== 产物 ==")
for p in sorted(OUT.iterdir()):
    if p.name.startswith("."):
        continue
    say(f"  {p.name:32} {p.stat().st_size:>12,} 字节")
say(f"总耗时 {time.time()-t0:.1f}s")
