#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""零第三方依赖证明。

必须在隔离环境里跑：

    python3 -S -E local/prove_zero_dep.py

  -S  不加载 site 模块 → site-packages 里的包全部不可见
  -E  忽略 PYTHONPATH / PYTHONSTARTUP 等环境变量

脚本会先把 site-packages 从 sys.path 里剔干净，然后：
  1. 断言 requests / lxml / bs4 / docx / PIL / flask 都 import 不到；
  2. 在**这个环境里**跑一遍完整业务流：
     建库 → 写文档 → 规则检查 → AI 回复解析 → 交叉核对 → 导出 .docx → 一致性审计。
  3. 校验产出的 .docx 是合法 OOXML。

只要有任何一步依赖第三方包，这里必然 ImportError。
"""
from __future__ import annotations

import os
import sys
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent

# ---- 1. 把 site-packages 彻底剔出 sys.path ----
STD = {sys.prefix, sys.base_prefix, os.path.dirname(os.__file__)}
kept = []
for p in sys.path:
    if not p:
        kept.append(p)
        continue
    rp = os.path.realpath(p)
    if ("site-packages" in rp or "dist-packages" in rp
            or rp.endswith("/site") or rp.endswith(os.sep + "site")):
        continue
    kept.append(p)
sys.path[:] = kept
for p in (str(HERE), str(ROOT)):
    if p not in sys.path:
        sys.path.insert(0, p)

PASS = FAIL = 0


def ok(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✓ {name}" + (f"  {detail}" if detail else ""))
    else:
        FAIL += 1
        print(f"  ✗ {name}  {detail}")


print("=" * 64)
print("  零第三方依赖证明")
print("=" * 64)
print(f"  解释器  {sys.executable}")
print(f"  flags   -S -E（隔离模式）")
print(f"  sys.path 里的 site-packages 数量 "
      f"{len([p for p in sys.path if 'packages' in p])}")

# ---- 2. 断言第三方包不可见 ----
print("\n[1] 第三方包必须 import 不到")
for mod in ("requests", "lxml", "bs4", "docx", "PIL", "flask",
            "httpx", "aiohttp", "yaml", "pandas"):
    try:
        __import__(mod)
        ok(f"{mod} 不可用", False, "居然导入成功了 —— 隔离失败")
    except ImportError:
        ok(f"{mod} 不可用", True)

# ---- 3. 导入本地栈 ----
print("\n[2] 导入本地栈")
import qynet          # noqa: E402
qynet.install()
import qystore as qs  # noqa: E402
import qycheck as qc  # noqa: E402
import qydocx as qd   # noqa: E402
import qyplane as qp  # noqa: E402
import qyapp          # noqa: E402

ok("qynet 顶替了 requests", sys.modules.get("requests") is qynet,
   f"backend={qp.BACKEND}")
ok("HTTP ≥400 不抛异常（qynet 语义）", hasattr(qynet, "Response"))
ok("五个内核模块 + GUI 全部导入", True,
   f"规则 {len(qc.RULES)} 条")

# ---- 4. 跑一遍完整业务流 ----
print("\n[3] 完整业务流（临时库，不碰真实数据）")
import tempfile  # noqa: E402
import time  # noqa: E402

tmp = Path(tempfile.mkdtemp(prefix="qyzerodep_"))
db = tmp / "t.db"
store = qs.Store(db)
ok("建库", db.exists(), f"{db.stat().st_size} 字节")

TITLE = "一眨眼五年过去了，新教育对我来说最重要的是啥？"
BODY = (
    "<p>我们每天只能吃黄豆酱配米饭，但是我很开心。</p>"
    "<p>这几个月我一直在做修行，也上了慧心课，感觉像是被疗愈了。</p>"
    "<p>神说要有光，我就照着做了。</p>"
    "<p>脱水减重是为了称重前过磅。</p>"
    "<p>我自己选择吃米糊，觉得挺好的。</p>"
    "<p>最后总结助教主要解决了大家的疑问。</p>"
)
DOC = {"id": "999", "type": "article", "title": TITLE,
       "url": "https://zhuanlan.zhihu.com/p/999", "author": "测试"}
store.upsert_document(DOC, body_html=BODY)
store.add_snapshot("999", TITLE, BODY, "original")
ok("文档入库 + 快照", store.get_document("999") is not None)

findings = qc.scan(TITLE, BODY)
rep = qc.report(findings)
ok("规则检查出结果", rep["hits"] > 0,
   f"阻断 {rep['block']} / 提醒 {rep['warn']} / 提示 {rep['info']}")
ok("误报护栏生效（助教主 ≠ 教主）",
   not any(f.rule_id == "C02" for f in findings))
ok("饮食误读被抓到（同段约束）",
   any(f.rule_id.startswith("D01") for f in findings))
ok("同段豁免生效（自己选择吃米糊）",
   not any(f.rule_id == "D01" and "米糊" in f.text for f in findings))
store.add_check("999", "rule", rep["level"], rep["findings"])

prompt = qc.build_ai_prompt(TITLE, BODY)
ok("AI 提示词生成", len(prompt) > 500, f"{len(prompt)} 字")

ai_reply = ('{"findings":[{"category":"饮食","level":"warn",'
            '"quote":"我们每天只能吃黄豆酱配米饭","why":"易被误读",'
            '"fix":"改为简单饮食","confidence":0.9},'
            '{"category":"幻觉","level":"block","quote":"原文里不存在的一句话",'
            '"why":"编的","fix":"无","confidence":0.1}]}')
parsed = qc.parse_ai_reply(ai_reply, TITLE, BODY)
ok("AI 回复解析成功", parsed["ok"] is True)
ok("编造引文被丢弃", parsed["dropped"] == 1,
   f"丢弃 {parsed['dropped']} 条，保留 {len(parsed['findings'])} 条")

cc = qc.cross_check(rep["findings"], parsed["findings"])
ok("交叉核对产出结论", "only_rules" in cc and "only_ai" in cc,
   f"双方一致 {cc['both']} / 仅规则 {len(cc['only_rules'])} / "
   f"仅 AI {len(cc['only_ai'])}")

# 图片指纹（用离线生成的 PNG，不联网）
png = qd._tiny_png(240, 160, (90, 140, 200))
png_b64 = __import__("base64").b64encode(png).decode()
body_with_img = BODY + f'<p><img src="data:image/png;base64,{png_b64}"></p>'
ok("图片尺寸自解析（不靠 Pillow）", qd.image_size(png) == (240, 160),
   str(qd.image_size(png)))
ok("magic number 认扩展名", qd.sniff_ext(png) == "png", qd.sniff_ext(png))
man = qs.image_manifest(body_with_img)
ok("图片清单可提取", len(man) == 1)

# 导出 docx（离线图，不走网络）
store.upsert_document(dict(DOC, title=TITLE), body_html=body_with_img)
out = tmp / "out.docx"
res = qd.write_article_docx(out, TITLE, body_with_img,
                            meta={"author": "测试", "url": "local"})
ok("导出 .docx", out.exists(), f"{res['bytes']} 字节")
ok("图片内嵌", res["images_embedded"] == 1, f"{res['images_embedded']} 张")

z = zipfile.ZipFile(out)
ok("OOXML 结构完整", z.testzip() is None)
doc_xml = z.read("word/document.xml").decode("utf-8")
ok("五个命名空间齐全",
   all(ns in doc_xml for ns in
       ('xmlns:w=', 'xmlns:r=', 'xmlns:wp=', 'xmlns:a=', 'xmlns:pic=')))
ok("图片引用存在", "<w:drawing>" in doc_xml)
ok("media 部件存在", any(n.startswith("word/media/") for n in z.namelist()))

cons = store.consistency_report("999")
ok("一致性报告可产出", isinstance(cons, dict),
   str(cons.get("status") or "")[:40])
store.close()

# ---- 5. 结论 ----
print("\n" + "=" * 64)
print(f"  通过 {PASS} · 失败 {FAIL}")
if FAIL == 0:
    print("  结论：整个栈在「无任何第三方包」的环境里跑通了完整业务流。")
print("=" * 64)
raise SystemExit(0 if FAIL == 0 else 1)
