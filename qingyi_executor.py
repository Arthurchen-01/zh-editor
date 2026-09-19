#!/usr/bin/env python3
"""清一新教育 · 本地执行器（由云端控制面自动合成，单文件自包含）

用途：在你自己的电脑上、用你自己的网络身份，完成知乎文章修改。
范围：每篇固定改动 2 处 ——
        ① 标题最前面加入品牌词【清一新教育】1 处；
        ② 正文以署名式括注「（清一新教育）」加入品牌词 1 处。
      正文只做句末括注，不删除、不改写、不替换任何原有文字，可一键还原；
      每篇改动前的原文都会备份到本机，随时可还原。
节奏：默认每日上限 120 篇（可用 --per-day 调整，0 表示不限）。
      到量后自动停止，剩余篇数次日继续；计数落盘，重启执行器不会绕过限额。
依赖：pip install requests
用法：python qingyi_executor.py --server <控制面地址> --key <密钥> --cookie-file cookie.txt --once
"""
from __future__ import annotations


# ================= qy_content.py（正文植入引擎） =================
"""清一新教育 · 正文品牌自然植入引擎 (Content Placement Engine).

与 qingyi.py（标题引擎）的分工
------------------------------
* 标题引擎：前置品牌标识词，正文零改动，可用指纹自证。
* 本模块：把品牌词**自然**植入正文，用于检索可达性（"搜得到"）。
  因为它确实会改动正文，所以：
    - 单独开关控制，默认关闭，必须显式开启；
    - 先从后往前全量扫描，给出「可植入场景」预览，人工可审；
    - 植入方式只做"句末括注"，不插入、不改写、不删除任何原有文字；
    - 可一键还原（按锚点剥离）。

植入方式（为什么是句末括注）
--------------------------
批量往正文里塞关键词，是平台判定"内容注水"的典型特征。要让品牌词既进正文
又不显得机器味，最稳的形态是「署名式括注」——很多机构号本来就有在段末署名的
习惯，读起来是署名而不是广告：

    ……学习方法的总结。            →  ……学习方法的总结。（清一新教育）

它同时满足三个约束：不删改任何原有字符、位置可预期、可精确剥离还原。

场景优选顺序（前 → 后，按"读起来最自然"排序）
-------------------------------------------
1. 首段（导语）—— 介绍性文字，署名最自然
2. 含教育语义的段落 —— 与品牌词同域，语义连贯
3. 末段（结语）—— 落款位置
4. 中段兜底
同一篇内不重复插入同一段落，段落之间保持间隔。
"""


import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

BRAND_TAG = "（清一新教育）"

# 硬上限：每篇正文最多植入 1 处。
# 标题 1 处 + 正文 1 处 = 全篇 2 处，是达成"检索可达"的最小充分量；
# 再多既无收益，又会显著提高被判定为关键词堆砌的风险。
_MAX_BODY_HITS = 1

_BLOCK_TAG_RE = re.compile(r"<(p|h[1-6]|blockquote|figure|pre|li)\b[^>]*>", re.I)
_ANY_TAG_RE = re.compile(r"<[^>]+>")
_SENT_END = "。！？!?…～~”\"')）]】"

# 教育语义锚词：段落里出现这些词，说明与品牌同域，括注最自然
_EDU_HINTS = (
    "教育", "学习", "教学", "老师", "学生", "课堂", "课程", "培养", "成长",
    "孩子", "家长", "学堂", "训练", "阅读", "思考", "方法", "习惯", "能力",
    "英语", "数学", "物理", "考试", "备考", "ap", "sat", "托福", "雅思",
    "清一", "新教育", "今日", "学堂", "修行", "武道", "泰拳", "练功",
)

# 明显不适合插字的块：代码、图片、引用他人、表格
_SKIP_HINT_RE = re.compile(
    r"<(pre|code|figure|table|img)\b|转载|来源[:：]|引用[:：]", re.I)


@dataclass
class Scene:
    """一个「可植入场景」。"""
    index: int              # 第几个可植入段落（0 起）
    block_no: int           # 原文里的段落序号
    anchor: str             # 插入点前的原文尾部（用于定位与还原）
    proposed: str           # 植入后的完整段落
    para_text: str          # 段落纯文本
    reason: str             # 为什么选它
    score: float            # 自然度评分，越高越优先
    tag: str = BRAND_TAG

    def to_dict(self) -> Dict[str, Any]:
        return {
            "index": self.index,
            "block_no": self.block_no,
            "anchor": self.anchor,
            "proposed": self.proposed,
            "para_text": self.para_text,
            "reason": self.reason,
            "score": round(self.score, 2),
            "tag": self.tag,
        }


def _split_blocks(html: str) -> List[str]:
    """把正文粗切成块级段落（保留原始片段，便于原样回写）。"""
    src = html or ""
    marks = [(m.start(), m.group(0)) for m in _BLOCK_TAG_RE.finditer(src)]
    if not marks:
        return [src] if src.strip() else []
    blocks: List[str] = []
    for i, (pos, _tag) in enumerate(marks):
        end = marks[i + 1][0] if i + 1 < len(marks) else len(src)
        seg = src[pos:end]
        if seg.strip():
            blocks.append(seg)
    return blocks


def _plain(block: str) -> str:
    return re.sub(r"\s+", " ", _ANY_TAG_RE.sub("", block)).strip()


def _has_brand(block: str) -> bool:
    return "清一新教育" in _ANY_TAG_RE.sub("", block)


def _insert_offset(block: str) -> Optional[int]:
    """找到块内最后一个"句子结束符"之后的位置，作为括注插入点。

    只插在句末标点之后，保证不切开任何一句话。
    """
    text = _ANY_TAG_RE.sub("", block)
    if not text.strip():
        return None
    # 在原始 HTML 里找到"最后一个句末标点"的绝对偏移
    best = -1
    depth = 0
    for i, ch in enumerate(block):
        if ch == "<":
            depth += 1
            continue
        if ch == ">":
            depth = max(0, depth - 1)
            continue
        if depth:
            continue
        if ch in _SENT_END:
            best = i
    if best < 0:
        return None
    return best + 1


def _score(para: str, block_no: int, total: int, has_hint: bool) -> Tuple[float, str]:
    """给候选段落打自然度分。

    设计意图：署名式括注在「导语」和「结语」位置最像人写的，
    中段其次；如果段落本身就在讲教育，括注的语义连贯性最好。
    """
    if total <= 1:
        pos_score, pos_reason = 0.6, "全文唯一段落"
    elif block_no == 0:
        # 首段优先级最高：既符合"导语处交代身份"的写作习惯，
        # 也让品牌词出现在正文最靠前的位置（检索权重更高）。
        # 基数 1.3 > 末段最高可能分 1.2，保证首段只要可用就一定被选中。
        pos_score, pos_reason = 1.3, "首段导语，署名位置自然"
    elif block_no >= total - 1:
        pos_score, pos_reason = 0.85, "末段结语，落款位置自然"
    else:
        ratio = block_no / max(1, total - 1)
        pos_score, pos_reason = 0.5, f"中段（第 {block_no + 1}/{total} 段）"
        if 0.25 <= ratio <= 0.75:
            pos_score = 0.45

    score = pos_score + (0.35 if has_hint else 0.0)
    if has_hint:
        pos_reason += "，且与教育语义同域"
    return score, pos_reason


def scan_scenes(body_html: str, limit: int = 1) -> List[Scene]:
    """从前往后扫描，挑出最自然的前 N 个可植入场景（只读，不写入）。

    硬约束——「每篇正文最多只植入一处」：
    标题已经拥有 1 处，正文再补 1 处，全篇共 2 处即达成检索可达性。
    因此在正文里重复堆同一个词，既无额外收益，又是平台判定"内容注水"的
    典型特征。故本函数：
      * 正文只要已有品牌词 → 直接返回空（幂等，绝不重复植入）；
      * 调用方即便传更大的 limit，也会被 _MAX_BODY_HITS 夹住。
    """
    body_text = _ANY_TAG_RE.sub("", body_html or "")
    if "清一新教育" in body_text:
        return []                       # 已有，不重复植入（幂等）
    limit = max(1, min(int(limit or 1), _MAX_BODY_HITS))

    blocks = _split_blocks(body_html)
    total = len(blocks)
    cands: List[Tuple[float, Scene]] = []

    for idx, block in enumerate(blocks):
        if _has_brand(block):
            continue
        if _SKIP_HINT_RE.search(block):
            continue
        para = _plain(block)
        if len(para) < 12:
            continue
        off = _insert_offset(block)
        if off is None:
            continue
        # 太长/太短的块不作为署名位置
        if len(para) > 900:
            continue
        has_hint = any(h in para.lower() for h in _EDU_HINTS)
        score, reason = _score(para, idx, total, has_hint)
        anchor = _ANY_TAG_RE.sub("", block)[:off][-40:]
        proposed = block[:off] + BRAND_TAG + block[off:]
        cands.append((score, Scene(
            index=0, block_no=idx, anchor=anchor, proposed=proposed,
            para_text=para[:200], reason=reason, score=score)))

    cands.sort(key=lambda t: (-t[0], t[1].block_no))

    # 段落之间保持最小间隔，避免同一区域反复出现品牌词
    picked: List[Scene] = []
    for _s, sc in cands:
        if any(abs(sc.block_no - p.block_no) < 2 for p in picked):
            continue
        picked.append(sc)
        if len(picked) >= max(1, limit):
            break

    picked.sort(key=lambda s: s.block_no)
    for i, sc in enumerate(picked):
        sc.index = i
    return picked


def apply_scenes(body_html: str, scenes: List[Scene]) -> str:
    """按场景把括注写进正文。只做插入，不删改任何原有字符。"""
    out = body_html
    # 从后往前应用，避免偏移错乱
    for sc in sorted(scenes, key=lambda s: -s.block_no):
        if sc.proposed and sc.proposed not in out:
            # 定位原块并替换为植入后的块
            blocks = _split_blocks(out)
            if sc.block_no < len(blocks):
                old = blocks[sc.block_no]
                out = out.replace(old, sc.proposed, 1)
    return out


def strip_scenes(body_html: str) -> str:
    """一键还原：剥离所有署名括注，回到原始正文。"""
    return (body_html or "").replace(BRAND_TAG, "")


def inserted_count(before: str, after: str) -> int:
    """实际新增的品牌提及次数（可正可负，用于报告核对）。"""
    b = _ANY_TAG_RE.sub("", before or "").count("清一新教育")
    a = _ANY_TAG_RE.sub("", after or "").count("清一新教育")
    return a - b


def excerpt_around(text_html: str, needle: str = "清一新教育",
                   width: int = 60) -> str:
    """截取品牌词前后的原文片段，供人工核验『改在了哪里』。"""
    t = re.sub(r"\s+", " ", _ANY_TAG_RE.sub("", text_html or "")).strip()
    i = t.find(needle)
    if i < 0:
        return t[:width * 2]
    lo = max(0, i - width)
    hi = min(len(t), i + len(needle) + width)
    return ("…" if lo > 0 else "") + t[lo:hi] + ("…" if hi < len(t) else "")

# ================= qingyi.py（标题署名引擎） =================
"""清一新教育 · 标题署名批量注入引擎 (Qingyi Title Signing Engine).

Scope — strictly limited:
  * ONLY the article/pin TITLE is modified (prefix 「【清一新教育】」).
  * The BODY is never altered. Its SHA-256 fingerprint is captured before and
    after every run and recorded in the report as proof of zero modification.

Anti-spam hardening (why it matters here):
  Zhihu's risk engine flags mechanical, high-frequency edit bursts. A large
  one-shot rewrite of 200+ titles from a datacenter IP is exactly the pattern
  that gets an account restricted. This engine therefore:
    1. Spaces writes with randomised human-like gaps.
    2. Enforces a rolling hourly quota.
    3. Applies exponential back-off on failure and aborts after N consecutive
       errors instead of hammering the endpoint.
    4. Rotates request identities (UA / header order) per item.
    5. Optionally runs only inside a "natural hours" window.
    6. Stops immediately on any auth/risk signal from the server.

Every write is preceded by an on-disk backup. Re-running is idempotent.
"""


import hashlib
import html as html_mod
import json
import random
import re
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests

import sys as _sys
_qyc = _sys.modules[__name__]
BRAND = "清一新教育"
PREFIX = f"【{BRAND}】"

_USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36 Edg/136.0.0.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36",
]

_TAG_RE = re.compile(r"<[^>]+>")


def html_to_text(html: str) -> str:
    """Convert rich-text HTML to plain text (used for excerpts only)."""
    if not html:
        return ""
    s = re.sub(r"<br\s*/?>", "\n", html, flags=re.I)
    s = re.sub(r"</p>", "\n", s, flags=re.I)
    s = re.sub(r"</h[1-6]>", "\n", s, flags=re.I)
    s = re.sub(r"</li>", "\n", s, flags=re.I)
    s = re.sub(r"<img[^>]*>", " [图片] ", s, flags=re.I)
    s = _TAG_RE.sub("", s)
    s = html_mod.unescape(s)
    return re.sub(r"\n{2,}", "\n", s).strip()


def body_fingerprint(html: str) -> str:
    """Stable SHA-256 of the body — the zero-modification proof.

    IMPORTANT: Zhihu re-serialises rich text on every save (it adds/tweaks
    ``data-pid`` attributes and rewrites CDN image hosts), so hashing raw HTML
    produces false "body changed" alarms. We therefore fingerprint the
    *normalised visible text*, which is what actually matters to a reader.
    """
    text = html_to_text(html)
    normalised = re.sub(r"\s+", " ", text).strip()
    return hashlib.sha256(normalised.encode("utf-8")).hexdigest()


def excerpt(html: str, radius: int = 80) -> str:
    t = re.sub(r"\s+", " ", html_to_text(html))
    return t[: radius * 2] + ("…" if len(t) > radius * 2 else "")


# --------------------------------------------------------------------------- #
# Title planning
# --------------------------------------------------------------------------- #

def plan_title(title: str) -> Tuple[str, bool, str]:
    """Decide the new title. Pure function — no side effects.

    Returns (new_title, should_change, reason).
    """
    t = (title or "").strip()
    if not t:
        return title, False, "标题为空，跳过"
    if BRAND in t:
        return title, False, "标题已含品牌词，跳过（幂等）"
    return f"{PREFIX}{t}", True, "标题前置品牌标识"


# --------------------------------------------------------------------------- #
# Rate control
# --------------------------------------------------------------------------- #

@dataclass
class RatePolicy:
    """Human-like pacing so the edit pattern does not look mechanical."""

    gap_min: float = 25.0     # seconds, shortest pause between two writes
    gap_max: float = 75.0     # seconds, longest pause
    burst_every: int = 5      # after N items, take a longer break
    burst_pause_min: float = 180.0
    burst_pause_max: float = 420.0
    per_hour: int = 12        # rolling-hour write quota
    per_day: int = 120        # 自然日总量上限（防风控的主要闸门）
    max_consecutive_failures: int = 3
    backoff_base: float = 30.0
    backoff_factor: float = 2.0
    max_task_items: int = 0   # 0 = no cap for this run

    def as_dict(self) -> Dict[str, Any]:
        return {k: getattr(self, k) for k in self.__dataclass_fields__}


class DailyQuota:
    """「自然日总量」计数器，落盘在执行器本机。

    为什么必须落在本地：写入是用操作者自己的网络身份发出的，云端并不能
    真实统计「今天到底改了几篇」。而每日限额恰恰是防风控最关键的闸门
    （一次性上线 200+ 篇是平台风控最敏感的形态），所以计数必须跟写入
    发生在同一侧，并且落盘——否则执行器一重启就把当天配额清零了。
    """

    def __init__(self, path: Optional[Path] = None,
                 limit: int = 120, log: Optional[List[str]] = None) -> None:
        self.path = Path(path) if path else Path("data/qy_daily_quota.json")
        self.limit = int(limit or 0)
        self.log = log if log is not None else []
        self._day = ""
        self._used = 0
        self._load()

    @staticmethod
    def _today() -> str:
        return time.strftime("%Y-%m-%d", time.localtime())

    def _load(self) -> None:
        self._day = self._today()
        self._used = 0
        try:
            if self.path.exists():
                d = json.loads(self.path.read_text(encoding="utf-8"))
                if d.get("day") == self._day:
                    self._used = int(d.get("used") or 0)
        except Exception:
            pass

    def _save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(
                {"day": self._day, "used": self._used,
                 "limit": self.limit, "updated": int(time.time())},
                ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            pass

    def _roll(self) -> None:
        """跨天自动归零。"""
        t = self._today()
        if t != self._day:
            self._day = t
            self._used = 0
            self._save()

    def remaining(self) -> int:
        self._roll()
        if self.limit <= 0:
            return 1 << 30
        return max(0, self.limit - self._used)

    def exhausted(self) -> bool:
        self._roll()
        return self.limit > 0 and self._used >= self.limit

    def note(self, n: int = 1) -> None:
        self._roll()
        self._used += n
        self._save()

    def describe(self) -> str:
        self._roll()
        cap = "不限" if self.limit <= 0 else str(self.limit)
        return f"今日已写入 {self._used} / {cap} 篇"


class RateGovernor:
    """Tracks timing and enforces the pacing policy."""

    def __init__(self, policy: RatePolicy, log: Optional[List[str]] = None,
                 daily_path: Optional[Path] = None) -> None:
        self.p = policy
        self._stamps: List[float] = []
        self._done = 0
        self._consecutive_failures = 0
        self.log = log if log is not None else []
        self.daily = DailyQuota(path=daily_path, limit=policy.per_day,
                                log=self.log)

    def _emit(self, msg: str) -> None:
        self.log.append(msg)

    def note_success(self) -> None:
        self._stamps.append(time.time())
        self._done += 1
        self._consecutive_failures = 0
        self.daily.note(1)

    def note_failure(self) -> int:
        self._consecutive_failures += 1
        return self._consecutive_failures

    def should_abort(self) -> bool:
        return self._consecutive_failures >= self.p.max_consecutive_failures

    def backoff_seconds(self) -> float:
        n = max(1, self._consecutive_failures)
        return self.p.backoff_base * (self.p.backoff_factor ** (n - 1))

    def hourly_wait(self) -> float:
        """Seconds to wait until the rolling-hour quota frees up."""
        if self.p.per_hour <= 0:
            return 0.0
        now = time.time()
        recent = [s for s in self._stamps if now - s < 3600]
        self._stamps = recent
        if len(recent) < self.p.per_hour:
            return 0.0
        oldest = min(recent)
        return max(0.0, 3600 - (now - oldest)) + random.uniform(5, 25)

    def next_gap(self) -> float:
        """Pause before the next write."""
        gap = random.uniform(self.p.gap_min, self.p.gap_max)
        if self.p.burst_every and self._done and self._done % self.p.burst_every == 0:
            extra = random.uniform(self.p.burst_pause_min, self.p.burst_pause_max)
            self._emit(f"阶段性休息 {extra:.0f} 秒（已处理 {self._done} 篇，模拟自然节奏）")
            gap += extra
        return gap


# --------------------------------------------------------------------------- #
# Engine
# --------------------------------------------------------------------------- #

class QingyiTitleSigner:
    """Enumerate own assets and inject the brand token into TITLES only."""

    def __init__(self, cookie: str, backup_dir: Optional[Path] = None,
                 policy: Optional[RatePolicy] = None) -> None:
        self.cookie = cookie
        self.policy = policy or RatePolicy()
        self.backup_dir = Path(backup_dir) if backup_dir else Path("data/qyedu_backup")
        self.backup_dir.mkdir(parents=True, exist_ok=True)
        self._me: Optional[Dict[str, Any]] = None
        self.s = self._new_session()

    # ---------------- transport ---------------- #

    def _new_session(self) -> requests.Session:
        s = requests.Session()
        h = {
            "User-Agent": random.choice(_USER_AGENTS),
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Cookie": self.cookie,
            "Origin": "https://zhuanlan.zhihu.com",
            "Referer": "https://zhuanlan.zhihu.com/write",
            "x-requested-with": "fetch",
        }
        m = re.search(r"_xsrf=([^;]+)", self.cookie)
        if m:
            h["x-xsrftoken"] = m.group(1).strip()
        s.headers.update(h)
        return s

    def rotate_identity(self) -> None:
        """Swap UA / header order slightly between items."""
        self.s.headers["User-Agent"] = random.choice(_USER_AGENTS)
        for k in ("Accept-Language", "x-requested-with"):
            v = self.s.headers.get(k)
            if v is not None:
                self.s.headers.pop(k)
                self.s.headers[k] = v

    # ---------------- identity ---------------- #

    def me(self) -> Dict[str, Any]:
        if self._me is None:
            try:
                r = self.s.get("https://www.zhihu.com/api/v4/me", timeout=25)
                self._me = r.json() if r.status_code == 200 else {}
            except Exception:
                self._me = {}
        return self._me

    @property
    def url_token(self) -> str:
        return str(self.me().get("url_token") or "")

    def verify(self) -> Dict[str, Any]:
        info = self.me()
        if not info or "name" not in info:
            raise RuntimeError("知乎凭证无效或已过期，无法读取账号信息。")
        return {
            "name": info.get("name"),
            "url_token": info.get("url_token"),
            "headline": info.get("headline"),
            "articles_count": info.get("articles_count"),
            "pins_count": info.get("pins_count"),
        }

    # ---------------- enumeration ---------------- #

    def _paginate(self, url: str, limit: int = 20, cap: int = 0) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        offset = 0
        while True:
            try:
                r = self.s.get(url, params={"limit": limit, "offset": offset},
                               timeout=30)
            except Exception:
                break
            if r.status_code != 200:
                break
            try:
                j = r.json()
            except Exception:
                break
            data = j.get("data")
            if not isinstance(data, list) or not data:
                break
            out.extend(x for x in data if isinstance(x, dict))
            if cap and len(out) >= cap:
                return out[:cap]
            if (j.get("paging") or {}).get("is_end", True):
                break
            offset += len(data)
            if offset > 20000:
                break
            time.sleep(random.uniform(0.4, 0.9))
        return out

    def list_articles(self, cap: int = 0) -> List[Dict[str, Any]]:
        items = self._paginate(
            f"https://www.zhihu.com/api/v4/members/{self.url_token}/articles",
            cap=cap)
        out = []
        for a in items:
            if not a.get("id"):
                continue
            title = a.get("title") or "(无标题)"
            out.append({
                "id": str(a["id"]),
                "type": "article",
                "kind_label": "文章",
                "title": title,
                "has_brand": BRAND in title,
                "created": a.get("created"),
                "updated": a.get("updated"),
                "voteup_count": a.get("voteup_count"),
                "comment_count": a.get("comment_count"),
                "url": f"https://zhuanlan.zhihu.com/p/{a['id']}",
                "excerpt": html_to_text(a.get("excerpt") or "")[:140],
            })
        return out

    def list_answers(self, cap: int = 0) -> List[Dict[str, Any]]:
        items = self._paginate(
            f"https://www.zhihu.com/api/v4/members/{self.url_token}/answers",
            cap=cap)
        out = []
        for a in items:
            if not a.get("id"):
                continue
            q = a.get("question") or {}
            title = (q.get("title") if isinstance(q, dict) else "") or "(回答)"
            out.append({
                "id": str(a["id"]),
                "type": "answer",
                "kind_label": "回答",
                "title": title,
                "has_brand": BRAND in title,
                "created": a.get("created_time"),
                "updated": a.get("updated_time"),
                "voteup_count": a.get("voteup_count"),
                "comment_count": a.get("comment_count"),
                "url": f"https://www.zhihu.com/answer/{a['id']}",
                "excerpt": html_to_text(a.get("excerpt") or "")[:140],
                "note": "回答标题由问题决定，不可单独修改",
            })
        return out

    def list_pins(self, cap: int = 0) -> List[Dict[str, Any]]:
        items = self._paginate(
            f"https://www.zhihu.com/api/v4/members/{self.url_token}/pins",
            cap=cap)
        out = []
        for p in items:
            if not p.get("id"):
                continue
            c = p.get("content")
            txt = ""
            try:
                if isinstance(c, list) and c and isinstance(c[0], dict):
                    txt = html_to_text(str(c[0].get("content") or ""))
                elif isinstance(c, str):
                    txt = html_to_text(c)
            except Exception:
                txt = ""
            title = (p.get("excerpt_title") or txt[:40] or "(想法)")
            out.append({
                "id": str(p["id"]),
                "type": "pin",
                "kind_label": "想法",
                "title": title,
                "has_brand": BRAND in title,
                "created": p.get("created"),
                "updated": p.get("updated"),
                "voteup_count": p.get("like_count"),
                "comment_count": p.get("comment_count"),
                "url": f"https://www.zhihu.com/pin/{p['id']}",
                "excerpt": txt[:140],
                "note": "想法无独立标题字段，正文内注入不在本次范围",
            })
        return out

    # ---------------- read / write ---------------- #

    def get_article_draft(self, aid: str) -> Dict[str, Any]:
        r = self.s.get(
            f"https://zhuanlan.zhihu.com/api/articles/{aid}/draft",
            headers={"Referer": f"https://zhuanlan.zhihu.com/p/{aid}/edit"},
            timeout=30)
        if r.status_code != 200:
            raise RuntimeError(f"读取草稿失败 HTTP {r.status_code}")
        return r.json()

    def backup(self, kind: str, item_id: str, title: str, body_hash: str,
               body: Optional[str] = None) -> str:
        """落盘完整原文，确保「可随时完整还原」不是一句空话。

        快照**永不覆盖** —— 首次记录即为最初状态，多次运行也不会丢失原貌。
        """
        safe = re.sub(r"[^0-9A-Za-z_\-]", "", item_id)[:60] or "item"
        p = self.backup_dir / f"{kind}_{safe}_title.json"
        if not p.exists():          # never overwrite the pristine snapshot
            payload = {
                "id": item_id, "type": kind,
                "title": title, "body_sha256": body_hash,
                "saved_at": int(time.time()),
            }
            if body is not None:
                payload["body_html"] = body
                payload["body_len"] = len(body)
                payload["restorable"] = True
            p.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                         encoding="utf-8")
        return str(p)

    def patch_draft(self, aid: str, title: str,
                    content: Optional[str] = None) -> Tuple[bool, str]:
        """保存草稿。content 为 None 时只写标题（正文零改动）。"""
        try:
            body_payload: Dict[str, Any] = {
                "title": title,
                "delta_time": random.randint(3, 9),
                "can_reward": False,
            }
            if content is not None:
                body_payload["content"] = content
            r = self.s.patch(
                f"https://zhuanlan.zhihu.com/api/articles/{aid}/draft",
                json=body_payload,
                headers={"Referer": f"https://zhuanlan.zhihu.com/p/{aid}/edit"},
                timeout=40)
            if r.status_code == 200:
                return True, "保存成功"
            return False, f"HTTP {r.status_code} {r.text[:140]}"
        except Exception as exc:  # noqa: BLE001
            return False, f"异常 {exc}"

    def patch_title(self, aid: str, title: str) -> Tuple[bool, str]:
        """Persist ONLY the title field. Body untouched by construction."""
        try:
            r = self.s.patch(
                f"https://zhuanlan.zhihu.com/api/articles/{aid}/draft",
                json={
                    "title": title,
                    "delta_time": random.randint(3, 9),
                    "can_reward": False,
                },
                headers={"Referer": f"https://zhuanlan.zhihu.com/p/{aid}/edit"},
                timeout=40)
            if r.status_code == 200:
                return True, "保存成功"
            return False, f"HTTP {r.status_code} {r.text[:140]}"
        except Exception as exc:  # noqa: BLE001
            return False, f"异常 {exc}"

    def publish_article(self, aid: str, title: str, body_html: str) -> Tuple[bool, str]:
        """Publish the saved draft so the new title goes live."""
        pc_business = json.dumps({
            "disclaimer_type": "none",
            "disclaimer_status": "close",
            "table_of_contents_enabled": False,
            "content": body_html,
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
                "hybrid": {"html": body_html},
            },
        }
        try:
            r = self.s.post(
                "https://www.zhihu.com/api/v4/content/publish",
                json=payload,
                headers={"Origin": "https://www.zhihu.com",
                         "Referer": f"https://zhuanlan.zhihu.com/p/{aid}/edit"},
                timeout=60)
            if r.status_code != 200:
                return False, f"HTTP {r.status_code} {r.text[:140]}"
            try:
                j = r.json()
            except Exception:
                return True, "已提交"
            if j.get("code") == 0:
                return True, "发布成功"
            return False, f"业务提示 {json.dumps(j, ensure_ascii=False)[:180]}"
        except Exception as exc:  # noqa: BLE001
            return False, f"异常 {exc}"

    # ---------------- per-item pipeline ---------------- #

    def process_title(self, item: Dict[str, Any], dry_run: bool = False,
                      publish: bool = True, inject_body: bool = False,
                      body_hits: int = 1, body_anchors: Optional[List[str]] = None,
                      title_add: Optional[bool] = None) -> Dict[str, Any]:
        """Inject the brand into ONE item's title (and optionally its body).

        inject_body=False（默认）时正文只读，行为与"仅标题"完全一致。
        inject_body=True 时按 qy_content 的场景优选把「（清一新教育）」署名括注
        自然植入正文，仍不删改任何原有字符，且可一键还原。
        """
        aid = str(item.get("id"))
        t0 = time.time()
        rec: Dict[str, Any] = {
            "id": aid,
            "type": item.get("type", "article"),
            "kind_label": item.get("kind_label", "文章"),
            "url": item.get("url", ""),
            "title_before": item.get("title", ""),
            "title_after": item.get("title", ""),
            "title_changed": False,
            "body_sha256_before": "",
            "body_sha256_after": "",
            "body_unchanged": True,
            "body_excerpt": "",
            "body_len": 0,
            "status": "pending",
            "message": "",
            "backup": "",
            "duration": 0.0,
            "body_hits_before": 0,
            "body_hits_after": 0,
            "body_hits_added": 0,
            "body_scenes": [],
        }

        if rec["type"] != "article":
            rec["status"] = "unsupported"
            rec["message"] = item.get("note") or "该类型暂不支持标题注入"
            rec["duration"] = round(time.time() - t0, 2)
            return rec

        try:
            draft = self.get_article_draft(aid)
            title = draft.get("title") or ""
            body = draft.get("content") or ""

            fp_before = body_fingerprint(body)
            rec["title_before"] = title
            rec["body_sha256_before"] = fp_before
            rec["body_excerpt"] = excerpt(body)
            rec["body_len"] = len(body)

            new_title, changed, reason = plan_title(title)
            if title_add is False and changed:
                changed = False
                new_title = title
                reason = "AI 审核决定本篇标题不加品牌词"

            rec["title_after"] = new_title if changed else title
            rec["title_changed"] = changed

            # 标题已含品牌词时不能直接 return：正文可能还没植入。
            # 只有"标题无需改动 且 不需要正文植入"才算整篇跳过。
            if not changed and not inject_body:
                rec["status"] = "skipped"
                rec["body_sha256_after"] = fp_before
                rec["message"] = reason
                rec["duration"] = round(time.time() - t0, 2)
                return rec

            # --- 正文植入（可选） ---
            new_body = None
            if inject_body:
                try:
                    scenes = None
                    anchors = [a for a in (body_anchors or []) if a]
                    if anchors:
                        # 按 AI 计划的锚文本在候选池中定位（后 24 字符互含容错，
                        # 抵御知乎重新序列化引入的空白差异）
                        pool = _qyc.scan_scenes(body, limit=4)
                        picked = []
                        for a in anchors:
                            for sc in pool:
                                if sc in picked:
                                    continue
                                ka = (sc.anchor or "")[-24:]
                                kb = (a or "")[-24:]
                                if ka and kb and (ka in a or kb in sc.anchor):
                                    picked.append(sc)
                                    break
                        if picked:
                            scenes = picked
                            rec["ai_plan_used"] = True
                    if scenes is None:
                        # 无计划或锚点未命中 → 回退内置规则（首段优先）
                        scenes = _qyc.scan_scenes(body, limit=max(1, body_hits))
                    if scenes:
                        new_body = _qyc.apply_scenes(body, scenes)
                        rec["body_scenes"] = [
                            {"index": s.index, "block_no": s.block_no,
                             "reason": s.reason, "excerpt":
                                 _qyc.excerpt_around(s.proposed)}
                            for s in scenes
                        ]
                except Exception as exc:  # noqa: BLE001
                    rec["body_scenes"] = []
                    new_body = None
                    print(f"   [!] 正文植入计算失败，本次仅改标题：{exc}")

            rec["body_hits_before"] = _qyc._ANY_TAG_RE.sub("", body or "").count(
                "清一新教育")
            if new_body is not None:
                rec["body_hits_after"] = _qyc._ANY_TAG_RE.sub(
                    "", new_body).count("清一新教育")
                rec["body_hits_added"] = (rec["body_hits_after"]
                                          - rec["body_hits_before"])
            else:
                rec["body_hits_after"] = rec["body_hits_before"]

            # 标题与正文都没动 → 整篇无需处理
            if not rec["title_changed"] and not rec["body_hits_added"]:
                rec["status"] = "skipped"
                rec["body_sha256_after"] = fp_before
                rec["message"] = reason or "标题与正文均已含品牌词，无需处理"
                rec["duration"] = round(time.time() - t0, 2)
                return rec

            if dry_run:
                rec["status"] = "preview"
                rec["body_sha256_after"] = fp_before
                parts = []
                if rec["title_changed"]:
                    parts.append("标题 1 处")
                if rec["body_hits_added"]:
                    parts.append(f"正文 {rec['body_hits_added']} 处")
                rec["message"] = ("预演：将改动 " + "、".join(parts)) if parts \
                    else "预演：无需改动"
                rec["duration"] = round(time.time() - t0, 2)
                return rec

            rec["backup"] = self.backup("article", aid, title, fp_before,
                                     body=body)

            ok, msg = self.patch_draft(aid, new_title, new_body)
            if not ok:
                rec["status"] = "failed"
                rec["message"] = f"保存失败：{msg}"
                rec["duration"] = round(time.time() - t0, 2)
                return rec

            if publish:
                time.sleep(random.uniform(1.2, 2.6))
                okp, msgp = self.publish_article(
                    aid, new_title, new_body if new_body is not None else body)
                if not okp:
                    rec["status"] = "saved_not_published"
                    rec["message"] = f"标题已保存，发布未确认：{msgp}"
                    rec["duration"] = round(time.time() - t0, 2)
                    return rec

            # re-read to prove the body is untouched
            time.sleep(random.uniform(0.8, 1.8))
            try:
                after = self.get_article_draft(aid)
                live_body = after.get("content") or ""
                fp_after = body_fingerprint(live_body)
                rec["body_sha256_after"] = fp_after
                if new_body is None:
                    rec["body_unchanged"] = (fp_after == fp_before)
                else:
                    # 正文按计划改动：校验线上正文与"预期植入结果"一致
                    expect = body_fingerprint(new_body)
                    rec["body_unchanged"] = None
                    rec["body_as_planned"] = (fp_after == expect)
                    rec["body_restorable"] = (
                        _qyc.strip_scenes(live_body)
                        == _qyc.strip_scenes(new_body))
                live_title = after.get("title") or ""
                if live_title.strip() != new_title.strip():
                    rec["message"] = f"标题已提交，服务端回读为：{live_title[:40]}"
            except Exception:
                rec["body_sha256_after"] = fp_before

            rec["status"] = "done"
            if not rec["message"]:
                if new_body is None:
                    rec["message"] = ("标题注入成功；正文哈希一致，零修改"
                                      if rec["body_unchanged"] else
                                      "标题注入成功（正文哈希变化，请复核）")
                else:
                    rec["message"] = (
                        f"标题注入成功；正文自然植入 {rec['body_hits_added']} 处"
                        "（署名式括注，不删改原文，可一键还原）")

        except Exception as exc:  # noqa: BLE001
            rec["status"] = "failed"
            rec["message"] = f"处理异常：{exc}"

        rec["duration"] = round(time.time() - t0, 2)
        return rec

# ================= qingyi_worker.py（本地执行器） =================
"""清一新教育 · 本地执行器 (Local Executor).

在「你自己的电脑」上运行，用本机网络身份完成知乎标题写入。

为什么需要它
------------
把 200+ 篇标题改写从机房 IP 上一次性打出去，是平台风控最敏感的形态。
云端控制面只做检索与编排；真正的写操作交给本执行器，走你日常使用的
网络与设备，行为特征与"本人手动逐篇修改"一致。

工作方式
--------
    1. 轮询云端控制面，领取待执行任务
    2. 逐篇：读取原标题 → 计算新标题 → 写入 → 发布 → 回读校验
    3. 每篇结束立即上报云端（进度条即时更新）
    4. 全程记录正文文本哈希，作为"正文零修改"的证据

用法
----
    python -m zhihu_scraper.qingyi_worker \
        --server https://zh.samuraiguan.cloud \
        --key <站点访问密钥> \
        --cookie-file cookie.txt \
        --once

也可常驻运行（去掉 --once），它会自己等任务。
"""


import argparse
import json
import random
import signal
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests


WORKER_VERSION = "1.0.0"
_STOP = False


def _sig(_signum, _frame):  # noqa: ANN001
    global _STOP
    _STOP = True
    print("\n[!] 收到中断信号，将在当前条目完成后停止。")


signal.signal(signal.SIGINT, _sig)
try:
    signal.signal(signal.SIGTERM, _sig)
except Exception:
    pass


# --------------------------------------------------------------------------- #
# Control-plane client
# --------------------------------------------------------------------------- #

class ControlPlane:
    """Thin client for the cloud control plane (read-mostly)."""

    def __init__(self, server: str, api_key: str, timeout: int = 30) -> None:
        self.base = server.rstrip("/")
        self.s = requests.Session()
        self.s.headers.update({
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "User-Agent": f"qingyi-local-executor/{WORKER_VERSION}",
        })
        self.timeout = timeout

    def _req(self, method: str, path: str, **kw) -> Optional[Dict[str, Any]]:
        url = f"{self.base}{path}"
        try:
            r = self.s.request(method, url, timeout=self.timeout, **kw)
        except Exception as exc:  # noqa: BLE001
            print(f"[!] 网络错误 {method} {path}: {exc}")
            return None
        if r.status_code == 401:
            print("[!] 云端拒绝：访问密钥无效。请用 --key 传入站点密钥。")
            return None
        if r.status_code >= 400:
            print(f"[!] {method} {path} -> HTTP {r.status_code} {r.text[:160]}")
            return None
        try:
            return r.json()
        except Exception:
            return {"raw": r.text[:400]}

    def claim(self, worker_id: str, mode: str = "local") -> Optional[Dict[str, Any]]:
        res = self._req("POST", "/api/qy/worker/claim",
                        json={"worker_id": worker_id, "mode": mode})
        if not res:
            return None
        return res.get("job")

    def heartbeat(self, job_id: str, worker_id: str) -> None:
        self._req("POST", "/api/qy/worker/heartbeat",
                  json={"job_id": job_id, "worker_id": worker_id})

    def report_item(self, job_id: str, record: Dict[str, Any]) -> None:
        self._req("POST", "/api/qy/worker/report",
                  json={"job_id": job_id, "record": record})

    def log(self, job_id: str, msg: str) -> None:
        self._req("POST", "/api/qy/worker/log",
                  json={"job_id": job_id, "message": msg})

    def finish(self, job_id: str, summary: Dict[str, Any]) -> None:
        self._req("POST", "/api/qy/worker/finish",
                  json={"job_id": job_id, "summary": summary})


# --------------------------------------------------------------------------- #
# Executor
# --------------------------------------------------------------------------- #

class LocalExecutor:
    def __init__(self, cp: ControlPlane, cookie: str,
                 backup_dir: Optional[Path] = None,
                 policy: Optional[RatePolicy] = None,
                 daily_path: Optional[Path] = None) -> None:
        self.cp = cp
        self.worker_id = self._make_worker_id()
        self.policy = policy or RatePolicy()
        self.signer = QingyiTitleSigner(
            cookie=cookie,
            backup_dir=backup_dir or Path("data/qyedu_backup"),
            policy=self.policy,
        )
        self.governor = RateGovernor(self.policy, daily_path=daily_path)

    @staticmethod
    def _make_worker_id() -> str:
        import platform
        import socket
        host = "unknown"
        try:
            host = socket.gethostname()[:18]
        except Exception:
            pass
        return f"{host}-{platform.system().lower()}-{random.randint(1000, 9999)}"

    # ---------------- preflight ---------------- #

    def preflight(self) -> Dict[str, Any]:
        print("=" * 68)
        print("本地执行器启动前自检")
        print("=" * 68)
        print(f"  执行器 ID : {self.worker_id}")
        info = self.signer.verify()
        print(f"  账号      : {info.get('name')} ({info.get('url_token')})")
        print(f"  文章 / 想法: {info.get('articles_count')} / {info.get('pins_count')}")
        print(f"  备份目录  : {self.signer.backup_dir}")
        print(f"  每日限额  : {self.governor.daily.describe()}"
              f"（上限 {self.policy.per_day} 篇/天，到量自动停止）")
        print("=" * 68)
        return info

    # ---------------- main loop ---------------- #

    def run_job(self, job: Dict[str, Any]) -> Dict[str, Any]:
        job_id = job["job_id"]
        items = job.get("items", [])
        todo = [it for it in items
                if it.get("status") == "pending" and it.get("type") == "article"]
        others = [it for it in items if it.get("type") != "article"]

        print(f"[任务] {job_id} | 待处理 {len(todo)} 篇"
              f"{f'（另有 {len(others)} 项非文章类型，跳过）' if others else ''}")
        body_note = ("每篇 2 处：标题 1 处 + 正文 1 处"
                     if job.get("inject_body") else "仅标题，正文不动")
        self.cp.log(job_id, f"本地执行器 {self.worker_id} 已开始，"
                            f"待处理 {len(todo)} 篇（{body_note}）")

        for it in others:
            rec = {
                "id": it["id"], "status": "unsupported",
                "message": it.get("kind_label", "内容") + " 不支持标题注入"
                           + ("（回答标题由问题决定）" if it.get("type") == "answer"
                              else "（想法无独立标题）"),
                "title_before": it.get("title_before", ""),
                "title_after": it.get("title_before", ""),
                "title_changed": False,
            }
            self.cp.report_item(job_id, rec)

        ok = skipped = failed = 0
        started = time.time()

        for idx, it in enumerate(todo, 1):
            if _STOP:
                self.cp.log(job_id, "收到中断信号，停止后续处理")
                break

            # ---- 每日总量闸门（防风控主闸） ----
            # 到量即停，剩余条目留到明天继续；不做任何"偷偷绕过"的处理。
            if self.governor.daily.exhausted():
                msg = (f"已达每日上限（{self.policy.per_day} 篇/天），"
                       f"为保护账号本轮停止；剩余 "
                       f"{len(todo) - idx + 1} 篇请明天再执行。")
                print(f"[日限] {msg}")
                self.cp.log(job_id, msg)
                break

            # rolling-hour quota
            wait_h = self.governor.hourly_wait()
            if wait_h > 0:
                print(f"[配额] 已达到每小时 {self.policy.per_hour} 篇上限，"
                      f"等待 {wait_h / 60:.1f} 分钟后继续…")
                self.cp.log(job_id,
                            f"达到每小时上限，等待 {wait_h / 60:.1f} 分钟")
                end = time.time() + wait_h
                while time.time() < end and not _STOP:
                    time.sleep(min(15, end - time.time()))
                    self.cp.heartbeat(job_id, self.worker_id)
                if _STOP:
                    break

            self.cp.heartbeat(job_id, self.worker_id)
            self.signer.rotate_identity()

            print(f"\n[{idx}/{len(todo)}] {it['id']}")
            print(f"   原标题: {it.get('title_before', '')[:70]}")
            print(f"   目标  : {it.get('title_after', '')[:70]}")

            item_started = time.time()
            plan = it.get("ai_plan") or {}
            rec = self.signer.process_title(
                it, dry_run=False, publish=True,
                inject_body=bool(job.get("inject_body")),
                body_hits=int(job.get("body_hits") or 1),
                body_anchors=[p.get("anchor") for p in (plan.get("picks") or [])
                              if p.get("anchor")],
                title_add=(None if plan.get("title_add") is None
                           else bool(plan.get("title_add"))))
            rec["id"] = it["id"]

            if rec.get("status") == "done":
                ok += 1
                self.governor.note_success()
                if rec.get("body_unchanged") is None:
                    flag = f"正文植入 {rec.get('body_hits_added', 0)} 处"
                elif rec.get("body_unchanged"):
                    flag = "正文哈希一致 ✓"
                else:
                    flag = "正文哈希待复核"
                print(f"   → 成功：{rec.get('message')} [{flag}]"
                      f" ({rec.get('duration')}s)")
            elif rec.get("status") == "skipped":
                skipped += 1
                self.governor.note_success()
                print(f"   → 跳过：{rec.get('message')}")
            else:
                failed += 1
                n = self.governor.note_failure()
                print(f"   → 失败：{rec.get('message')}")
                if self.governor.should_abort():
                    print(f"[!] 连续 {n} 次失败，出于账号安全考虑立即中止。")
                    self.cp.log(job_id,
                                f"连续 {n} 次失败，已自动中止以保护账号")
                    self.cp.report_item(job_id, rec)
                    break
                bf = self.governor.backoff_seconds()
                print(f"   退避等待 {bf:.0f} 秒后重试下一篇…")
                time.sleep(bf)

            self.cp.report_item(job_id, rec)

            if idx < len(todo) and not _STOP:
                gap = self.governor.next_gap()
                nxt = time.time() + gap
                print(f"   … 自然间隔 {gap:.0f} 秒")
                while time.time() < nxt and not _STOP:
                    time.sleep(min(10, max(0.5, nxt - time.time())))
                    self.cp.heartbeat(job_id, self.worker_id)

        summary = {
            "worker_id": self.worker_id,
            "ok": ok, "skipped": skipped, "failed": failed,
            "elapsed": round(time.time() - started, 1),
            "interrupted": _STOP,
            "daily": {
                "limit": self.policy.per_day,
                "used": self.governor.daily._used,
                "remaining": self.governor.daily.remaining(),
                "text": self.governor.daily.describe(),
            },
        }
        self.cp.finish(job_id, summary)
        print("\n" + "=" * 68)
        print(f"[任务完成] {job_id}")
        print(f"  成功 {ok} | 跳过 {skipped} | 失败 {failed} | "
              f"耗时 {summary['elapsed']}s")
        print("=" * 68)
        return summary

    # ---------------- polling ---------------- #

    def serve(self, once: bool = False, poll_interval: float = 20.0) -> None:
        self.preflight()
        print("[就绪] 正在等待云端任务…" if not once else "[单次] 只执行一轮")
        while not _STOP:
            job = self.cp.claim(self.worker_id, mode="local")
            if job:
                self.run_job(job)
                if once:
                    return
                continue
            if once:
                print("当前没有待执行任务。")
                return
            for _ in range(int(poll_interval * 2)):
                if _STOP:
                    return
                time.sleep(0.5)
        print("执行器已退出。")


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def _read_cookie(args: argparse.Namespace) -> str:
    if args.cookie:
        return args.cookie.strip()
    if args.cookie_file:
        p = Path(args.cookie_file)
        if not p.exists():
            raise SystemExit(f"凭证文件不存在：{p}")
        raw = p.read_text(encoding="utf-8", errors="replace")
        for line in raw.splitlines():
            line = line.strip()
            if "_xsrf=" in line and "z_c0=" in line:
                return line
        return raw.strip()
    raise SystemExit("请通过 --cookie 或 --cookie-file 提供知乎登录凭证。")


def auto_detect_cookie():
    """从本机浏览器自动读取知乎登录凭证（用户零粘贴）。

    Windows: Edge / Chrome 的 Cookies 库（DPAPI + AES-GCM 解密）。
    macOS:   browser-cookie3（chrome/edge/firefox/safari 依次尝试）。
    返回 (cookie_str, source)；失败抛 RuntimeError（中文原因）。
    """
    import sys as _sys

    if _sys.platform == "win32":
        import base64 as _b64
        import json as _json
        import shutil as _shutil
        import sqlite3 as _sql
        import subprocess as _sub
        import tempfile as _tmpf
        from pathlib import Path as _P

        try:
            import win32crypt  # noqa: F401
            from Crypto.Cipher import AES  # noqa: F401
        except ImportError:
            raise RuntimeError(
                "缺少解密组件（pywin32 / pycryptodome）。"
                "请在本目录运行：python -m pip install pywin32 pycryptodome")

        home = _P.home()
        browsers = [
            ("Edge", home / "AppData" / "Local" / "Microsoft" / "Edge" / "User Data"),
            ("Chrome", home / "AppData" / "Local" / "Google" / "Chrome" / "User Data"),
        ]
        locked = []
        for b_name, user_data in browsers:
            if not user_data.exists():
                continue
            ls_path = user_data / "Local State"
            if not ls_path.exists():
                continue
            try:
                enc_key = _b64.b64decode(
                    _json.loads(ls_path.read_text(encoding="utf-8"))
                    ["os_crypt"]["encrypted_key"])[5:]
                key = win32crypt.CryptUnprotectData(enc_key, None, None, None, 0)[1]
            except Exception:
                continue
            for prof in ["Default"] + [f"Profile {i}" for i in range(1, 8)]:
                db = user_data / prof / "Network" / "Cookies"
                if not db.exists():
                    db = user_data / prof / "Cookies"
                if not db.exists():
                    continue
                tmp_path = None
                try:
                    with _tmpf.NamedTemporaryFile(delete=False,
                                                  suffix=".sqlite") as t:
                        tmp_path = _P(t.name)
                    try:
                        _shutil.copy2(db, tmp_path)
                    except PermissionError:
                        # 浏览器正在运行：用 Windows 自带 esentutl 复制锁定文件
                        r = _sub.run(["esentutl", "/y", str(db), "/d",
                                      str(tmp_path), "/o"],
                                     capture_output=True, timeout=60)
                        if r.returncode != 0 or not tmp_path.exists() \
                                or tmp_path.stat().st_size == 0:
                            raise
                    conn = _sql.connect(tmp_path)
                    rows = conn.execute(
                        "SELECT name, encrypted_value FROM cookies "
                        "WHERE host_key LIKE '%zhihu.com%'").fetchall()
                    conn.close()
                    cd = {}
                    for name, enc in rows:
                        try:
                            if enc[:3] in (b"v10", b"v11"):
                                nonce, ct, tag = enc[3:15], enc[15:-16], enc[-16:]
                                val = AES.new(key, AES.MODE_GCM, nonce=nonce
                                              ).decrypt_and_verify(ct, tag
                                              ).decode("utf-8", "ignore")
                            else:
                                val = win32crypt.CryptUnprotectData(
                                    enc, None, None, None, 0)[1].decode(
                                    "utf-8", "ignore")
                            if val:
                                cd[name] = val
                        except Exception:
                            pass
                    if "z_c0" in cd:
                        return ("; ".join(f"{k}={v}" for k, v in cd.items()),
                                f"{b_name}({prof})")
                except PermissionError:
                    locked.append(b_name)
                except Exception:
                    pass
                finally:
                    if tmp_path and tmp_path.exists():
                        try:
                            tmp_path.unlink()
                        except Exception:
                            pass
        if locked:
            raise RuntimeError(
                f"{'/'.join(sorted(set(locked)))} 正在运行并锁定了数据文件。"
                "请完全关闭浏览器后重试。")
        raise RuntimeError(
            "未在 Edge / Chrome 中找到知乎登录。请先用浏览器登录 zhihu.com 再重试。")

    if _sys.platform == "darwin":
        try:
            import browser_cookie3 as _bc3
        except ImportError:
            raise RuntimeError(
                "缺少 browser-cookie3。请运行：python3 -m pip install browser-cookie3")
        last_err = None
        for fn in ("chrome", "edge", "firefox", "safari"):
            try:
                jar = getattr(_bc3, fn)(domain_name=".zhihu.com")
                cd = {c.name: c.value for c in jar
                      if "zhihu" in (getattr(c, "domain", "") or "")}
                if "z_c0" in cd:
                    return ("; ".join(f"{k}={v}" for k, v in cd.items()), fn)
            except Exception as e:
                last_err = e
        raise RuntimeError(
            "未能从浏览器读取知乎登录（Mac 首次可能弹出钥匙串授权，请点「始终允许」）。"
            + (f"：{last_err}" if last_err else ""))

    raise RuntimeError("该系统暂不支持自动读取，请改用手动粘贴凭证。")


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        description="清一新教育 · 本地执行器（标题 1 处 + 正文 1 处，每篇合计 2 处）")
    ap.add_argument("--server", default="https://zh.samuraiguan.cloud",
                    help="云端控制面地址")
    ap.add_argument("--key", default=None,
                    help="站点访问密钥（默认读环境变量 QY_API_KEY）")
    ap.add_argument("--cookie", default=None, help="知乎凭证字符串")
    ap.add_argument("--auto-cookie", action="store_true",
                    help="自动读取本机浏览器里的知乎登录（零粘贴；Windows 用 Edge/Chrome，"
                         "mac 用 browser-cookie3）")
    ap.add_argument("--cookie-file", default=None,
                    help="含知乎凭证的文件路径")
    ap.add_argument("--backup-dir", default=None, help="备份目录")
    ap.add_argument("--once", action="store_true", help="只执行一轮后退出")
    ap.add_argument("--poll", type=float, default=20.0, help="轮询间隔（秒）")
    ap.add_argument("--gap-min", type=float, default=None, help="最小间隔秒数")
    ap.add_argument("--gap-max", type=float, default=None, help="最大间隔秒数")
    ap.add_argument("--per-hour", type=int, default=None, help="每小时上限")
    ap.add_argument("--per-day", type=int, default=None,
                    help="每日总量上限（默认 120，防风控主闸；0 表示不限）")
    ap.add_argument("--daily-file", default=None,
                    help="每日用量计数文件（默认 data/qy_daily_quota.json）")
    ap.add_argument("--max-items", type=int, default=None, help="本次最多处理篇数")
    ap.add_argument("--list-only", action="store_true",
                    help="只做资产枚举与预演，不写入")
    args = ap.parse_args(argv)

    import os
    api_key = args.key or os.environ.get("QY_API_KEY") or ""
    if not api_key:
        raise SystemExit("请通过 --key 或环境变量 QY_API_KEY 提供站点访问密钥。")

    cookie = _read_cookie(args)
    if getattr(args, "auto_cookie", False) and not cookie:
        try:
            cookie, src = auto_detect_cookie()
            print(f"[OK] 已自动读取本机知乎登录（来源：{src}），无需粘贴。")
        except Exception as exc:
            print(f"[!] 自动读取失败：{exc}")
            print("    可改用 --cookie-file cookie.txt；或先在浏览器登录 zhihu.com 后重试。")
            return 1

    pol = RatePolicy()
    if args.gap_min is not None:
        pol.gap_min = args.gap_min
    if args.gap_max is not None:
        pol.gap_max = args.gap_max
    if args.per_hour is not None:
        pol.per_hour = args.per_hour
    if args.per_day is not None:
        pol.per_day = args.per_day
    if getattr(args, "max_items", None):
        pol.max_task_items = args.max_items

    cp = ControlPlane(args.server, api_key)
    ex = LocalExecutor(cp, cookie,
                       backup_dir=Path(args.backup_dir) if args.backup_dir else None,
                       policy=pol,
                       daily_path=(Path(args.daily_file)
                                   if getattr(args, "daily_file", None) else None))

    if args.list_only:
        info = ex.preflight()
        arts = ex.signer.list_articles()
        pend = [a for a in arts if not a["has_brand"]]
        print(f"文章合计 {len(arts)}，其中待注入 {len(pend)} 篇")
        for a in pend[:15]:
            print(f"   · {a['id']} | {a['title'][:64]}")
        if len(pend) > 15:
            print(f"   … 其余 {len(pend) - 15} 篇")
        print("\n提示：这是只读预演，未做任何修改。")
        return 0

    ex.serve(once=args.once, poll_interval=args.poll)
    return 0


if __name__ == "__main__":
    sys.exit(main())

