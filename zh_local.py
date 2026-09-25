#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
zh_local.py v2.0 —— 纯本地知乎文章与回答批量自定义修改工具
支持：
  * 可视化软件界面模式（python zh_local.py gui 或直接双击 一键启动-用户端-Windows.bat）
  * 同时支持修改【专栏文章 (article)】与【知乎回答 (answer)】
  * 内置【四书五经/国学经典】（《大学》《中庸》《论语》《孟子》《诗经》《尚书》《礼记》《周易》《春秋》）
  * 内置【国家现行法律条文】（《宪法》《民法典》《爱国主义教育法》《义务教育法》《未成年人保护法》等）
  * 支持直接填写自定义标题与自定义正文内容
  * 支持手动提供 Cookie（cookie.txt 或终端/网页直接粘贴，无需关闭浏览器）
  * 每一篇改动前自动做本地备份，可随时一键还原

常用命令
  python zh_local.py gui                              # 启动本地可视化修改软件界面（推荐！）
  python zh_local.py presets                          # 查看内置的四书五经与法律条文列表
  python zh_local.py list --kind all                  # 拉取文章 + 回答列表
  python zh_local.py backup --kind all                # 备份全部文章与回答原文
  python zh_local.py plan --kind all                  # 生成 plan.json
  python zh_local.py fill --preset daxue              # 一键填充《大学》（或 law_xianfa / classics / law）
  python zh_local.py fill --title "新标题" --content "自定义正文内容"
  python zh_local.py check                            # 校验 plan.json
  python zh_local.py apply --force                    # 试运行（只看不改）
  python zh_local.py apply --yes --force              # 正式写入知乎
  python zh_local.py restore 1234567890 --yes         # 用本地备份还原某一篇
"""

import argparse
import html as _html
import json
import random
import re
import sys
import time
import uuid
from pathlib import Path
from urllib import error as urlerr
from urllib import request as urlreq

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import high_value_essays as hve  # noqa: E402

VERSION = "2.0"
BACKUP_DIR = HERE / "backup"
PLAN_FILE = HERE / "plan.json"
LIST_FILE = HERE / "articles.json"

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36")
ZH = "https://www.zhihu.com"
ZHUANLAN = "https://zhuanlan.zhihu.com"

MIN_GAP, MAX_GAP = 4.0, 9.0
MAX_CONSECUTIVE_FAIL = 3


def log(msg=""):
    print(msg, flush=True)


def html_to_text(s):
    s = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", s or "", flags=re.S | re.I)
    s = re.sub(r"<br\s*/?>|</p>|</div>", "\n", s, flags=re.I)
    s = re.sub(r"<[^>]+>", "", s)
    s = _html.unescape(s)
    s = re.sub(r"[ \t\u00a0]+", " ", s)
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()


def text_to_html(s):
    """纯文本 -> 简单 HTML 段落（若已含 HTML 标签则原样保留）。"""
    raw = (s or "").strip()
    if not raw:
        return ""
    if re.search(r"<(p|h[1-6]|blockquote|div|ul|ol|li|br)\b", raw, re.I):
        return raw
    parts = [p.strip() for p in re.split(r"\n\s*\n", raw) if p.strip()]
    out = []
    for p in parts:
        out.append("<p>" + _html.escape(p).replace("\n", "<br/>") + "</p>")
    return "".join(out)


def fmt_time(ts):
    try:
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(int(ts)))
    except Exception:
        return "-"


# ---------------------------------------------------------------- 会话

class Zhihu:
    """只和知乎通信的本地客户端（支持文章与回答）。"""

    def __init__(self, cookie, url_token=None):
        self.cookie = (cookie or "").strip()
        if not self.cookie:
            raise SystemExit("cookie.txt 是空的。请把浏览器里的知乎 Cookie 存进去，或运行 python zh_local.py gui 在界面中粘贴。")
        m = re.search(r"_xsrf=([^;]+)", self.cookie)
        self.xsrf = m.group(1).strip() if m else ""
        self.url_token = url_token
        self._me = None

    def _headers(self, referer=None, origin=None):
        h = {
            "User-Agent": UA,
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Cookie": self.cookie,
            "x-requested-with": "fetch",
        }
        if self.xsrf:
            h["x-xsrftoken"] = self.xsrf
        if referer:
            h["Referer"] = referer
        if origin:
            h["Origin"] = origin
        return h

    def _req(self, method, url, body=None, referer=None, origin=None, timeout=45):
        data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
        req = urlreq.Request(url, data=data, method=method,
                             headers=self._headers(referer, origin))
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urlreq.urlopen(req, timeout=timeout) as resp:
                return resp.status, resp.read().decode("utf-8", "replace")
        except urlerr.HTTPError as e:
            return e.code, e.read().decode("utf-8", "replace")
        except Exception as e:
            return 0, f"网络异常: {e}"

    def _get_json(self, url, **kw):
        code, raw = self._req("GET", url, **kw)
        if code != 200:
            return code, None, raw
        try:
            return 200, json.loads(raw), raw
        except Exception:
            return 200, None, raw

    def _get_json_retry(self, url, tries=4, **kw):
        last_code, last_raw = 0, ""
        for i in range(tries):
            code, j, raw = self._get_json(url, **kw)
            if code == 200 and j is not None:
                return 200, j, raw
            last_code, last_raw = code, raw
            if i < tries - 1:
                time.sleep(2.0 * (i + 1) + random.uniform(0, 1.5))
        return last_code, None, last_raw

    def me(self):
        if self._me is None:
            _, j, _ = self._get_json(ZH + "/api/v4/me")
            self._me = j or {}
        return self._me

    @property
    def token(self):
        if not self.url_token:
            self.url_token = str(self.me().get("url_token") or "")
        return self.url_token

    def list_articles(self, cap=0):
        token = self.token
        if not token:
            raise SystemExit("没能识别你的账号（登录态可能已失效），请更新 cookie.txt。")
        out, offset = [], 0
        while True:
            url = f"{ZH}/api/v4/members/{token}/articles?include=data[*].comment_count,voteup_count&limit=20&offset={offset}"
            code, j, raw = self._get_json_retry(url, referer=f"{ZH}/people/{token}/posts")
            if code != 200 or not isinstance(j, dict):
                if not out:
                    raise SystemExit(f"读取文章列表失败 HTTP {code}: {raw[:200]}")
                break
            data = j.get("data") or []
            if not data:
                break
            for a in data:
                if not a.get("id"):
                    continue
                out.append({
                    "id": str(a["id"]),
                    "type": "article",
                    "title": a.get("title") or "(无标题)",
                    "created": a.get("created"),
                    "updated": a.get("updated"),
                    "voteup_count": a.get("voteup_count") or 0,
                    "comment_count": a.get("comment_count") or 0,
                    "url": f"{ZHUANLAN}/p/{a['id']}",
                    "excerpt": html_to_text(a.get("excerpt") or "")[:120],
                })
            if cap and len(out) >= cap:
                return out[:cap]
            if (j.get("paging") or {}).get("is_end", True):
                break
            offset += len(data)
            if offset > 20000:
                break
            time.sleep(random.uniform(0.4, 1.0))
        return out

    def list_answers(self, cap=0):
        token = self.token
        if not token:
            raise SystemExit("没能识别你的账号（登录态可能已失效），请更新 cookie.txt。")
        out, offset = [], 0
        while True:
            url = f"{ZH}/api/v4/members/{token}/answers?include=data[*].comment_count,voteup_count&limit=20&offset={offset}"
            code, j, raw = self._get_json_retry(url, referer=f"{ZH}/people/{token}/answers")
            if code != 200 or not isinstance(j, dict):
                break
            data = j.get("data") or []
            if not data:
                break
            for a in data:
                if not a.get("id"):
                    continue
                q = a.get("question") or {}
                title = (q.get("title") if isinstance(q, dict) else "") or "(回答)"
                out.append({
                    "id": str(a["id"]),
                    "type": "answer",
                    "title": title,
                    "created": a.get("created_time"),
                    "updated": a.get("updated_time"),
                    "voteup_count": a.get("voteup_count") or 0,
                    "comment_count": a.get("comment_count") or 0,
                    "url": f"{ZH}/answer/{a['id']}",
                    "excerpt": html_to_text(a.get("excerpt") or "")[:120],
                })
            if cap and len(out) >= cap:
                return out[:cap]
            if (j.get("paging") or {}).get("is_end", True):
                break
            offset += len(data)
            if offset > 20000:
                break
            time.sleep(random.uniform(0.4, 1.0))
        return out

    def list_items(self, kind="article", cap=0):
        out = []
        if kind in ("article", "all"):
            out.extend(self.list_articles(cap=cap))
        if kind in ("answer", "all"):
            out.extend(self.list_answers(cap=cap))
        return out[:cap] if cap else out

    def get_draft(self, aid, kind="article"):
        if kind == "answer":
            code, j, raw = self._get_json(
                f"{ZH}/api/v4/answers/{aid}?include=content,editable_content,question",
                referer=f"{ZH}/answer/{aid}")
            if code != 200 or not isinstance(j, dict):
                raise RuntimeError(f"读取回答失败 HTTP {code}: {raw[:160]}")
            q = j.get("question") or {}
            return {
                "title": (q.get("title") if isinstance(q, dict) else "") or "(回答)",
                "content": j.get("content") or j.get("editable_content") or "",
            }
        code, j, raw = self._get_json(
            f"{ZHUANLAN}/api/articles/{aid}/draft",
            referer=f"{ZHUANLAN}/p/{aid}/edit")
        if code != 200 or not isinstance(j, dict):
            raise RuntimeError(f"读取草稿失败 HTTP {code}: {raw[:160]}")
        return j

    def patch_draft(self, aid, title, content_html, kind="article"):
        if kind == "answer":
            body = {
                "content": content_html,
                "reshipment_settings": "allowed",
                "comment_permission": "all",
                "reward_info": {"can_reward": False, "tagline": ""},
            }
            code, raw = self._req(
                "PUT", f"{ZH}/api/v4/answers/{aid}", body=body,
                referer=f"{ZH}/answer/{aid}", origin=ZH)
            if code == 200:
                return True, "回答已更新"
            return False, f"更新回答失败 HTTP {code} {raw[:160]}"
        body = {
            "title": title,
            "content": content_html,
            "delta_time": random.randint(3, 9),
            "can_reward": False,
        }
        code, raw = self._req(
            "PATCH", f"{ZHUANLAN}/api/articles/{aid}/draft", body=body,
            referer=f"{ZHUANLAN}/p/{aid}/edit",
            origin=ZHUANLAN)
        if code == 200:
            return True, "草稿已保存"
        return False, f"保存草稿失败 HTTP {code} {raw[:160]}"

    def publish(self, aid, title, content_html, kind="article"):
        if kind == "answer":
            return True, "回答发布成功"
        pc_business = json.dumps({
            "disclaimer_type": "none",
            "disclaimer_status": "close",
            "table_of_contents_enabled": False,
            "content": content_html,
            "title": title,
            "commercial_report_info": {"commercial_types": []},
            "commercial_zhitask_bind_info": None,
            "canReward": False,
        }, ensure_ascii=False)
        payload = {
            "action": "article",
            "data": {
                "publish": {"traceId": f"{int(time.time() * 1000)},{uuid.uuid4()}"},
                "extra_info": {"publisher": "pc", "pc_business_params": pc_business},
                "draft": {"disabled": 1, "id": aid, "isPublished": True},
                "commentsPermission": {},
                "creationStatement": {"disclaimer_type": "none",
                                      "disclaimer_status": "close"},
                "contentsTables": {"table_of_contents_enabled": False},
                "commercialReportInfo": {"isReport": 0},
                "appreciate": {"can_reward": False, "tagline": ""},
                "hybridInfo": {},
                "hybrid": {"html": content_html},
            },
        }
        code, raw = self._req(
            "POST", f"{ZH}/api/v4/content/publish", body=payload,
            referer=f"{ZHUANLAN}/p/{aid}/edit", origin=ZH, timeout=60)
        if code != 200:
            return False, f"发布失败 HTTP {code} {raw[:160]}"
        try:
            j = json.loads(raw)
        except Exception:
            return True, "已提交"
        if j.get("code") == 0:
            return True, "发布成功"
        return False, f"知乎返回: {json.dumps(j, ensure_ascii=False)[:200]}"


# ---------------------------------------------------------------- 凭证 / 备份

def _clean_cookie(raw):
    for line in (raw or "").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.lower().startswith("cookie:"):
            line = line[7:].strip()
        line = line.strip("\"' ")
        if "z_c0=" in line or "d_c0=" in line or len(line) > 60:
            return line
    return ""


def load_cookie(args):
    if getattr(args, "cookie", None):
        ck = _clean_cookie(args.cookie)
        if ck:
            return ck
    p = Path(args.cookie_file)
    if not p.is_absolute():
        p = HERE / p
    if p.exists():
        ck = _clean_cookie(p.read_text(encoding="utf-8", errors="replace"))
        if ck:
            return ck
    log(f"[!] {p} 中尚未配置知乎 Cookie。")
    try:
        pasted = input(">>> 请直接在此粘贴知乎 Cookie（或运行 python zh_local.py gui 打开网页界面）: ").strip()
    except EOFError:
        pasted = ""
    ck = _clean_cookie(pasted)
    if ck:
        p.write_text(ck + "\n", encoding="utf-8")
        return ck
    raise SystemExit("未提供有效的知乎 Cookie。")


def backup_path(aid):
    return BACKUP_DIR / str(aid)


def ensure_backup(zh, aid, kind="article", force=False):
    d = backup_path(aid)
    meta = d / "meta.json"
    if meta.exists() and not force:
        return False
    d.mkdir(parents=True, exist_ok=True)
    draft = zh.get_draft(aid, kind=kind)
    title = draft.get("title") or ""
    content = draft.get("content") or ""
    (d / "title.txt").write_text(title, encoding="utf-8")
    (d / "content.html").write_text(content, encoding="utf-8")
    (d / "content.txt").write_text(html_to_text(content), encoding="utf-8")
    meta.write_text(json.dumps({
        "id": str(aid),
        "type": kind,
        "title": title,
        "content_html": content,
        "saved_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "url": f"{ZH}/answer/{aid}" if kind == "answer" else f"{ZHUANLAN}/p/{aid}",
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    return True


# ---------------------------------------------------------------- 命令

def cmd_presets(_args):
    log("=" * 72)
    log("  内置【四书五经/国学经典】与【国家现行法律条文】预设一览")
    log("=" * 72)
    log("\n【一、按类别智能轮换预设】")
    for p in hve.get_catalog()["presets"]:
        log(f"  --preset {p['key']:<14}  {p['label']}")
    log("\n【二、四书五经与国学经典单篇指定】")
    for e in hve.CLASSIC_ESSAYS:
        log(f"  --preset {e['key']:<14}  {e.get('label') or e['title']}")
    log("\n【三、国家现行法律条文单篇指定】")
    for e in hve.LAW_ESSAYS:
        log(f"  --preset {e['key']:<14}  {e.get('label') or e['title']}")
    log("\n示例用法：")
    log("  python zh_local.py fill --preset daxue        # 将计划替换为《大学》")
    log("  python zh_local.py fill --preset law_xianfa   # 将计划替换为《中华人民共和国宪法》")
    log("  python zh_local.py fill --title '自定义标题' --content '自定义正文'")
    log("  python zh_local.py gui                        # 打开本地可视化界面自由选择")
    return 0


def cmd_gui(args):
    import qingyi_client
    argv = ["--cookie-file", args.cookie_file]
    if getattr(args, "port", None):
        argv += ["--port", str(args.port)]
    return qingyi_client.main(argv)


def cmd_list(args):
    zh = Zhihu(load_cookie(args))
    me = zh.me()
    kind = getattr(args, "kind", "article") or "article"
    log(f"zh_local v{VERSION} · 纯本地工具，请求只发往知乎")
    log(f"账号：{me.get('name') or '(未知)'}   主页：{ZH}/people/{zh.token}/posts")
    arts = zh.list_items(kind=kind, cap=args.limit or 0)
    LIST_FILE.write_text(json.dumps(arts, ensure_ascii=False, indent=2), encoding="utf-8")
    log(f"共 {len(arts)} 条内容（已存到 {LIST_FILE.name}）\n")
    log(f"{'序号':<4}{'类型':<6}{'点赞':>6}{'评论':>6}  {'发布时间':<17}标题")
    log("-" * 84)
    for i, a in enumerate(arts, 1):
        k_lbl = "回答" if a.get("type") == "answer" else "文章"
        log(f"{i:<4}{k_lbl:<6}{a['voteup_count'] or 0:>6}{a['comment_count'] or 0:>6}  "
            f"{fmt_time(a['created']):<17}{a['title'][:42]}")
    return 0


def cmd_backup(args):
    zh = Zhihu(load_cookie(args))
    kind = getattr(args, "kind", "article") or "article"
    arts = zh.list_items(kind=kind, cap=args.limit or 0)
    log(f"开始备份 {len(arts)} 条（标题 + 正文 HTML + 纯文本）...")
    ok = 0
    for i, a in enumerate(arts, 1):
        try:
            ensure_backup(zh, a["id"], kind=a.get("type", "article"), force=True)
            ok += 1
            log(f"  [{i}/{len(arts)}] ✓ {a['title'][:40]}")
        except Exception as e:
            log(f"  [{i}/{len(arts)}] ✗ {a['id']} {e}")
        time.sleep(random.uniform(0.6, 1.4))
    log(f"\n完成：{ok}/{len(arts)} 条已备份到 {BACKUP_DIR}")
    return 0


def cmd_plan(args):
    zh = Zhihu(load_cookie(args))
    kind = getattr(args, "kind", "article") or "article"
    arts = zh.list_items(kind=kind, cap=args.limit or 0)

    done = {}
    if PLAN_FILE.exists():
        try:
            for it in (json.loads(PLAN_FILE.read_text(encoding="utf-8")).get("articles") or []):
                if it.get("id") and (it.get("old_title") or it.get("old_content_html")):
                    done[str(it["id"])] = it
        except Exception:
            done = {}
    if done:
        log(f"已有 {len(done)} 条的旧内容，跳过不重复读取。")

    todo = [a for a in arts if str(a["id"]) not in done]
    log(f"共 {len(arts)} 条，本次需读取 {len(todo)} 条的现有标题与正文...")
    for i, a in enumerate(todo, 1):
        item_kind = a.get("type") or "article"
        try:
            d = zh.get_draft(a["id"], kind=item_kind)
        except Exception as e:
            log(f"  [{i}/{len(todo)}] ✗ {a['id']} {e}")
            time.sleep(2.0)
            continue
        content = d.get("content") or ""
        done[str(a["id"])] = {
            "id": a["id"],
            "type": item_kind,
            "url": a["url"],
            "created": fmt_time(a["created"]),
            "voteup_count": a["voteup_count"],
            "comment_count": a["comment_count"],
            "old_title": d.get("title") or "",
            "old_content_html": content,
            "old_content_text": html_to_text(content),
            "new_title": "",
            "new_content_text": "",
            "new_content_html": "",
            "enabled": False,
            "note": "",
        }
        log(f"  [{i}/{len(todo)}] ✓ {(d.get('title') or '')[:40]}")
        time.sleep(random.uniform(0.6, 1.4))

    items = [done[str(a["id"])] for a in arts if str(a["id"]) in done]
    PLAN_FILE.write_text(json.dumps({"articles": items}, ensure_ascii=False, indent=2),
                         encoding="utf-8")
    log(f"\n已生成 {PLAN_FILE.name}，含 {len(items)}/{len(arts)} 条。")
    log("接下来可运行 `python zh_local.py fill --preset daxue`（或 law_xianfa / classics / law）一键填入四书五经或法律条文！")
    return 0


def cmd_fill(args):
    """用内置四书五经、法律条文或自定义标题+正文填充 plan.json。"""
    plan = _load_plan()
    arts = plan.get("articles") or []
    if not arts:
        log("plan.json 为空，请先运行 python zh_local.py plan")
        return 1
    only_ids = {x.strip() for x in (args.only or "").split(",") if x.strip()}
    preset = (args.preset or "daxue").strip()
    c_title = (args.title or "").strip()
    c_content = (args.content or "").strip()
    if args.content_file:
        cf = Path(args.content_file)
        if cf.exists():
            c_content = cf.read_text(encoding="utf-8")

    cnt = 0
    for it in arts:
        aid = str(it.get("id"))
        if only_ids and aid not in only_ids:
            continue
        if args.limit and cnt >= args.limit:
            break
        if c_content:
            nt = c_title or it.get("old_title") or ""
            nh = text_to_html(c_content)
            label = "自定义内容"
        else:
            essay = hve.get_essay_by_preset(aid, preset)
            nt = it.get("old_title") if args.keep_title else essay["title"]
            nh = essay["content"]
            label = essay.get("label") or essay["title"]
        it["new_title"] = nt
        it["new_content_html"] = nh
        it["new_content_text"] = html_to_text(nh)
        it["enabled"] = True
        it["note"] = f"已填充：{label}"
        cnt += 1

    PLAN_FILE.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
    log(f"✓ 已为 {cnt} 条内容填充方案（preset={preset}），并勾选 enabled=true。")
    log("现在可运行：python zh_local.py check  或  python zh_local.py apply --yes --force")
    return 0


def _load_plan():
    if not PLAN_FILE.exists():
        raise SystemExit(f"找不到 {PLAN_FILE.name}，先运行：python zh_local.py plan")
    return json.loads(PLAN_FILE.read_text(encoding="utf-8"))


def _resolved(it):
    nt = (it.get("new_title") or "").strip()
    nh = (it.get("new_content_html") or "").strip()
    ntx = (it.get("new_content_text") or "").strip()
    if not nh and ntx:
        nh = text_to_html(ntx)
    return nt, nh


def _issues(it):
    problems, warns = [], []
    nt, nh = _resolved(it)
    old_t = (it.get("old_title") or "").strip()
    old_c = (it.get("old_content_text") or "").strip()
    new_txt = html_to_text(nh)

    if not nt and it.get("type", "article") == "article":
        problems.append("new_title 为空")
    if not nh:
        problems.append("new_content_html 和 new_content_text 都为空")
    if not problems and nt == old_t and new_txt == old_c:
        problems.append("内容与线上完全一致，无需重复修改")

    if old_c and len(new_txt) < max(20, len(old_c) * 0.1):
        warns.append(f"正文将从 {len(old_c)} 字缩到 {len(new_txt)} 字（减少 90% 以上）")
    if old_t and nt and len(nt) < max(3, len(old_t) * 0.2):
        warns.append(f"标题将从 {len(old_t)} 字缩到 {len(nt)} 字")
    return problems, warns


def cmd_check(_args):
    plan = _load_plan()
    arts = plan.get("articles") or []
    todo = [it for it in arts if it.get("enabled")]
    log(f"计划里共 {len(arts)} 条，已勾选（enabled=true）{len(todo)} 条。\n")
    bad = warn_n = 0
    for it in todo:
        problems, warns = _issues(it)
        tag = "✓" if not problems and not warns else ("✗" if problems else "!")
        if problems:
            bad += 1
        if warns:
            warn_n += 1
        log(f" {tag} {it['id']}  {(it.get('old_title') or '')[:36]} -> {(it.get('new_title') or '')[:36]}")
        for p in problems:
            log(f"      ✗ {p}")
        for w in warns:
            log(f"      ! {w}")
    log("")
    if bad:
        log(f"{bad} 条有问题，修好 plan.json 再执行。")
        return 1
    if warn_n:
        log(f"{warn_n} 条内容有长度变化提示，确认无误后执行时加 --force。")
    log("校验通过，可以执行：python zh_local.py apply --yes --force")
    return 0


def cmd_apply(args):
    plan = _load_plan()
    arts = [it for it in (plan.get("articles") or []) if it.get("enabled")]
    if args.only:
        arts = [it for it in arts if str(it.get("id")) == str(args.only)]
    if not arts:
        log("没有勾选任何条目（可先运行 python zh_local.py fill --preset daxue）。")
        return 0
    if args.limit:
        arts = arts[:args.limit]

    dry = not args.yes
    risky = []
    for it in arts:
        problems, warns = _issues(it)
        if problems:
            log(f"✗ {it['id']} 无法执行：{'; '.join(problems)}")
            return 1
        if warns:
            risky.append((it, warns))

    if risky and not args.force:
        log("以下内容长度变化较大：\n")
        for it, warns in risky:
            log(f"  {it['id']}  {(it.get('old_title') or '')[:36]}")
            for w in warns:
                log(f"      ! {w}")
        log("\n确认无误请加 --force 重新执行。")
        return 1

    log(f"{'【试运行】' if dry else '【正式执行】'} 共 {len(arts)} 条")
    log(f"写入前会先把每篇原文备份到 {BACKUP_DIR}\n")
    if dry:
        for it in arts:
            nt, nh = _resolved(it)
            log(f"  将修改 {it['id']} ({it.get('type', 'article')})  {it.get('old_title','')[:30]}")
            log(f"          新标题：{nt[:60]}")
            log(f"          新正文：{len(html_to_text(nh))} 字（原 {len(it.get('old_content_text') or '')} 字）")
        log("\n这是试运行，没有写入任何东西。确认后加 --yes 正式执行。")
        return 0

    zh = Zhihu(load_cookie(args))
    ok = fail = 0
    streak = 0
    for i, it in enumerate(arts, 1):
        aid = str(it["id"])
        kind = it.get("type") or "article"
        nt, nh = _resolved(it)
        head = f"[{i}/{len(arts)}] {aid} {nt[:34]}"
        try:
            created = ensure_backup(zh, aid, kind=kind)
            if created:
                log(f"{head}  · 已备份原文")
            good, msg = zh.patch_draft(aid, nt, nh, kind=kind)
            if not good:
                raise RuntimeError(msg)
            good, msg = zh.publish(aid, nt, nh, kind=kind)
            if not good:
                raise RuntimeError(msg)
            ok += 1
            streak = 0
            it["enabled"] = False
            it["note"] = "已改于 " + time.strftime("%Y-%m-%d %H:%M:%S")
            log(f"{head}  ✓ {msg}")
        except Exception as e:
            fail += 1
            streak += 1
            log(f"{head}  ✗ {e}")
            if streak >= MAX_CONSECUTIVE_FAIL:
                log(f"\n连续失败 {streak} 条，已自动中止。")
                break
        PLAN_FILE.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
        if i < len(arts):
            time.sleep(random.uniform(MIN_GAP, MAX_GAP))

    left = sum(1 for x in (plan.get("articles") or []) if x.get("enabled"))
    log(f"\n完成：成功 {ok} 条，失败 {fail} 条。计划里还剩 {left} 条未处理。")
    return 0 if fail == 0 else 1


def cmd_restore(args):
    aid = str(args.id)
    d = backup_path(aid)
    meta = d / "meta.json"
    if not meta.exists():
        raise SystemExit(f"没有 {aid} 的本地备份（{d}）")
    m = json.loads(meta.read_text(encoding="utf-8"))
    kind = m.get("type") or "article"
    zh = Zhihu(load_cookie(args))
    log(f"准备还原 {aid} ({kind}) → {m['title'][:40]}")
    if not args.yes:
        log("试运行。加 --yes 真正还原。")
        return 0
    good, msg = zh.patch_draft(aid, m["title"], m["content_html"], kind=kind)
    if not good:
        raise SystemExit(msg)
    good, msg = zh.publish(aid, m["title"], m["content_html"], kind=kind)
    log(("✓ " if good else "✗ ") + msg)
    return 0 if good else 1


def main():
    ap = argparse.ArgumentParser(
        description="纯本地知乎文章与回答批量修改工具 v2.0（支持四书五经/法律条文/自定义内容）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__)
    ap.add_argument("--cookie-file", default="cookie.txt", help="凭证文件（默认 cookie.txt）")
    ap.add_argument("--cookie", default="", help="直接传入 Cookie 字符串")
    sub = ap.add_subparsers(dest="cmd")

    p = sub.add_parser("gui", help="启动本地可视化修改软件界面（推荐）")
    p.add_argument("--port", type=int, default=8765)
    p.set_defaults(func=cmd_gui)

    p = sub.add_parser("presets", help="查看内置的四书五经与法律条文预设列表")
    p.set_defaults(func=cmd_presets)

    p = sub.add_parser("list", help="拉取文章/回答列表")
    p.add_argument("--kind", default="article", choices=["article", "answer", "all"])
    p.add_argument("--limit", type=int, default=0, help="最多取几篇（0=全部）")
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("backup", help="备份全部文章/回答")
    p.add_argument("--kind", default="article", choices=["article", "answer", "all"])
    p.add_argument("--limit", type=int, default=0)
    p.set_defaults(func=cmd_backup)

    p = sub.add_parser("plan", help="生成 plan.json")
    p.add_argument("--kind", default="article", choices=["article", "answer", "all"])
    p.add_argument("--limit", type=int, default=0)
    p.set_defaults(func=cmd_plan)

    p = sub.add_parser("fill", help="一键用四书五经（如大学）、法律条文或自定义内容填充 plan.json")
    p.add_argument("--preset", default="daxue", help="预设名称（如 daxue, zhongyong, lunyu, mengzi, classics, law_xianfa, law 等）")
    p.add_argument("--title", default="", help="自定义新标题")
    p.add_argument("--content", default="", help="自定义新正文")
    p.add_argument("--content-file", default="", help="从文件读取自定义新正文")
    p.add_argument("--keep-title", action="store_true", help="保留原标题不变")
    p.add_argument("--only", default="", help="仅填充指定 ID（逗号分隔）")
    p.add_argument("--limit", type=int, default=0, help="最多填充几篇")
    p.set_defaults(func=cmd_fill)

    p = sub.add_parser("check", help="校验 plan.json")
    p.set_defaults(func=cmd_check)

    p = sub.add_parser("apply", help="执行修改（默认试运行）")
    p.add_argument("--yes", action="store_true", help="真正写入知乎")
    p.add_argument("--force", action="store_true", help="确认内容长度变化的提示")
    p.add_argument("--only", default="", help="只处理指定 ID")
    p.add_argument("--limit", type=int, default=0)
    p.set_defaults(func=cmd_apply)

    p = sub.add_parser("restore", help="用备份还原某一篇")
    p.add_argument("id", help="文章或回答 ID")
    p.add_argument("--yes", action="store_true")
    p.set_defaults(func=cmd_restore)

    args = ap.parse_args()
    if not getattr(args, "func", None):
        ap.print_help()
        return 0
    return args.func(args) or 0


if __name__ == "__main__":
    sys.exit(main())
