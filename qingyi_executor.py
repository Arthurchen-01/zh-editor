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

# 正文植入处数的硬上限（调用方可在 1.._MAX_BODY_HITS 之间自选）。
# 默认 1 处：标题 1 + 正文 1 = 全篇 2 处，是达成"检索可达"的最小充分量。
# 放宽到 5，是因为用户明确要求「正文可以自己选加几处」；
# 但必须记住：同一篇里重复堆同一个词，是平台判定"内容注水 / 关键词堆砌"的
# 典型特征 —— 处数越多风险越高。**这是上限，不是推荐值。**
_MAX_BODY_HITS = 5

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

    N 由调用方给定（默认 1），两处受约束：
      * 正文只要已有品牌词 → 直接返回空（幂等，绝不重复植入）；
      * 任何 limit 都会被 _MAX_BODY_HITS 夹住。
    重复堆同一个词是平台判定"内容注水"的典型特征，处数越多风险越高，
    所以默认保持 1 处，只有用户显式要更多时才增加。
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

# ================= high_value_essays.py（高价值文库） =================
# -*- coding: utf-8 -*-
"""
高质量公版经典（四书五经/国学经典）与中国现行法律文库 (High-Value Classic & Chinese Law Library)
用于内容替换与合规更新：
- 结构严谨、字数扎实 (800 ~ 2,500 字符)
- 包含标准 HTML 标题 (<h2>)、引用块 (<blockquote>)、段落 (<p>)
- 涵盖「四书五经」（《大学》《中庸》《论语》《孟子》《诗经》《尚书》《礼记》《周易》《春秋》）与国家现行法律条文
- 支持按类别随机轮换（law / classics / random_all）或直接按具体篇目 key 精准指定
"""

import hashlib
from typing import List, Dict, Any

LAW_ESSAYS: List[Dict[str, Any]] = [
    {
        "key": "law_xianfa",
        "category": "law",
        "label": "《中华人民共和国宪法》公民基本权利与义务",
        "title": "《中华人民共和国宪法》公民基本权利与义务研读",
        "content": (
            "<h2>一、宪法的根本地位与法治原则</h2>"
            "<p>《中华人民共和国宪法》是国家的根本法，具有最高的法律效力。全国各族人民、一切国家机关和武装力量、各政党和各社会团体、各企业事业组织，都必须以宪法为根本的活动准则，并且负有维护宪法尊严、保证宪法实施的职责。国家维护社会主义法制的统一和尊严，一切法律、行政法规和地方性法规都不得同宪法相抵触。</p>"
            "<h2>二、公民的基本权利</h2>"
            "<p>中华人民共和国公民在法律面前一律平等。国家尊重和保障人权。任何公民享有宪法和法律规定的权利，同时必须履行宪法和法律规定的义务。</p>"
            "<blockquote>中华人民共和国公民有受教育的权利和义务。国家培养青年、少年、儿童在品德、智力、体质等方面全面发展。中华人民共和国公民有进行科学研究、文学艺术创作和其他文化活动的自由。</blockquote>"
            "<p>宪法规定，国家对于从事教育、科学、技术、文学、艺术和其他文化事业的公民的有益于人民的创造性工作，给以鼓励和帮助。公民的人格尊严不受侵犯。禁止用任何方法对公民进行侮辱、诽谤和诬告陷害。</p>"
            "<h2>三、公民的基本义务</h2>"
            "<p>中华人民共和国公民必须遵守宪法和法律，保守国家秘密，爱护公共财产，遵守劳动纪律，遵守公共秩序，尊重社会公德。公民在行使自由和权利的时候，不得损害国家的、社会的、集体的利益和其他公民的合法的自由和权利。保卫祖国、抵抗侵略是每一个公民的神圣职责。依照法律服兵役和参加民兵组织是公民的光荣义务。</p>"
        ),
    },
    {
        "key": "law_minfa",
        "category": "law",
        "label": "《中华人民共和国民法典》民事主体与诚信原则",
        "title": "《中华人民共和国民法典》总则编：民事主体与诚信原则解析",
        "content": (
            "<h2>一、民法典的立法宗旨与基本原则</h2>"
            "<p>《中华人民共和国民法典》被称为“社会生活的百科全书”。民法调整平等主体的自然人、法人和非法人组织之间的人身关系和财产关系。民事主体在民事活动中的法律地位一律平等。民事主体从事民事活动，应当遵循自愿原则，按照自己的意思设立、变更、终止民事法律关系。</p>"
            "<h2>二、公平、诚信与公序良俗原则</h2>"
            "<p>民事主体从事民事活动，应当遵循公平原则，合理确定各方的权利和义务。民事主体从事民事活动，应当遵循诚信原则，秉持诚实，恪守承诺。</p>"
            "<blockquote>民事主体从事民事活动，不得违反法律，不得违背公序良俗。民事主体从事民事活动，应当有利于节约资源、保护生态环境。</blockquote>"
            "<p>诚信原则是民法的核心原则，要求民事主体在行使权利、履行义务时诚实不欺、信守诺言。在现代社会中，弘扬社会主义核心价值观，恪守公序良俗，维护良好的社会经济秩序，是每一位公民和企业应尽的法律责任。</p>"
            "<h2>三、民事权利的行使与保护</h2>"
            "<p>自然人的身心健康、人身自由、姓名权、肖像权、名誉权、荣誉权、隐私权等人格权受法律保护。民事权利可以依据法律规定或者当事人约定行使，但不得滥用民事权利损害国家利益、社会公共利益或者他人合法权益。</p>"
        ),
    },
    {
        "key": "law_aiguo",
        "category": "law",
        "label": "《中华人民共和国爱国主义教育法》核心条文",
        "title": "《中华人民共和国爱国主义教育法》核心条文与实践导引",
        "content": (
            "<h2>一、弘扬爱国主义精神与立德树人</h2>"
            "<p>国家在全体人民中开展爱国主义教育，培育和践行社会主义核心价值观，弘扬以爱国主义为核心的民族精神和以改革创新为核心的时代精神，为全面建成社会主义现代化强国、实现中华民族伟大复兴汇聚磅礴力量。</p>"
            "<h2>二、爱国主义教育的主要内容</h2>"
            "<p>爱国主义教育涵盖中华优秀传统文化、革命文化、社会主义先进文化；国旗、国歌、国徽等国家象征和标志；祖国的壮美山河和历史文化遗产；宪法和法律；国家统一和民族团结、国家安全和国防等方面的意识和观念；英雄烈士和先进模范人物的事迹及体现的民族精神、时代精神。</p>"
            "<blockquote>国家将爱国主义教育贯穿国民教育和精神文明建设全过程。学校应当将爱国主义教育贯穿学校教育全过程，办好思想政治理论课，并将爱国主义教育内容融入各类学科和教材中。</blockquote>"
            "<h2>三、全社会协同推进机制</h2>"
            "<p>家庭、学校、社会各界应当密切配合，共同营造浓厚的爱国主义教育氛围。充分利用博物馆、纪念馆、文化馆、图书馆和爱国主义教育基地，引导广大青少年树立正确的世界观、人生观、价值观，厚植家国情怀，勤奋学习，立志成才。</p>"
        ),
    },
    {
        "key": "law_jiaoyu",
        "category": "law",
        "label": "《中华人民共和国义务教育法》育人方针",
        "title": "《中华人民共和国义务教育法》教育教学与育人方针学习札记",
        "content": (
            "<h2>一、义务教育的方针与目标</h2>"
            "<p>义务教育是国家统一实施的所有适龄儿童、少年必须接受的教育，是国家必须予以保障的公益性事业。实施义务教育，应当贯彻国家的教育方针，坚持立德树人，遵循教育规律，提高教育质量。</p>"
            "<h2>二、培养全面发展的社会主义建设者</h2>"
            "<p>教育教学工作应当符合教育规律和学生身心发展特点，面向全体学生，教书育人，将德育、智育、体育、美育等有机统一在教育教学活动中，注重培养学生独立思考能力、创新能力和实践能力，促进学生全面发展。</p>"
            "<blockquote>学校和教师按照确定的教育教学内容和课程设置开展教育教学活动，保证达到国家规定的基本质量要求。国家鼓励学校和教师采用启发式教育等教育教学方法，提高教育教学质量。</blockquote>"
            "<h2>三、尊重关爱学生与教师崇高职责</h2>"
            "<p>教师应当尊重学生的人格，不得歧视学生，不得对学生实施体罚、变相体罚或者其他侮辱人格尊严的行为，不得侵犯学生合法权益。全社会应当尊师重教，保障教师合法权益，提高教师社会地位。</p>"
        ),
    },
    {
        "key": "law_weichengnian",
        "category": "law",
        "label": "《中华人民共和国未成年人保护法》六大保护体系",
        "title": "《中华人民共和国未成年人保护法》家庭、学校与社会保护体系解析",
        "content": (
            "<h2>一、最有利于未成年人的立法原则</h2>"
            "<p>《中华人民共和国未成年人保护法》旨在保护未成年人身心健康，保障未成年人合法权益，促进未成年人德智体美劳全面发展。保护未成年人，应当坚持最有利于未成年人的原则，处理涉及未成年人事项，应当给予未成年人特殊、优先保护，尊重未成年人人格尊严，适应未成年人身心健康发展的规律和特点。</p>"
            "<h2>二、家庭保护与学校保护职责</h2>"
            "<p>未成年人的父母或者其他监护人应当学习家庭教育知识，接受家庭教育指导，创造良好、和睦、文明的家庭环境。学校应当全面贯彻国家教育方针，坚持立德树人，实施素质教育，提高教育质量，注重培养未成年学生认知能力、合作能力、创新能力和实践能力。</p>"
            "<blockquote>学校应当关心、爱护未成年学生，不得因家庭、身体、心理、学习能力等情况歧视学生。对家庭困难、身心有障碍的学生，应当提供关爱；对行为异常、学习有困难的学生，应当耐心帮助。</blockquote>"
            "<h2>三、网络保护与社会协同</h2>"
            "<p>国家、社会、学校和家庭应当加强未成年人网络素养宣传教育，培养和提高未成年人的网络素养，增强未成年人科学、文明、安全、合理使用网络的意识和能力，保障未成年人在网络空间的合法权益。</p>"
        ),
    },
    {
        "key": "law_kexue",
        "category": "law",
        "label": "《中华人民共和国科学技术进步法》科技创新与基础研究",
        "title": "《中华人民共和国科学技术进步法》基础研究与科技创新精神研读",
        "content": (
            "<h2>一、科技自立自强与创新驱动发展</h2>"
            "<p>《中华人民共和国科学技术进步法》明确规定，国家坚持新发展理念，坚持科技创新在国家现代化建设全局中的核心地位，把科技自立自强作为国家发展的战略支撑，实施科教兴国战略、人才强国战略和创新驱动发展战略，走中国特色自主创新道路。</p>"
            "<h2>二、基础研究与原始创新能力建设</h2>"
            "<p>国家加强基础研究能力建设，尊重科学发展规律和人才成长规律，强化项目、人才、基地系统布局，为基础研究发展提供良好的物质条件和有力的制度保障。国家鼓励科学技术研究开发与高等教育、产业发展相结合，鼓励学科交叉融合和相互促进。</p>"
            "<blockquote>国家培育崇尚科学、追求真理、勇于探究、宽容失败的创新文化，弘扬科学家精神，激励科学技术人员自由探索、勇攀高峰，营造良好学术生态。</blockquote>"
            "<h2>三、科研诚信与科技伦理规范</h2>"
            "<p>科学技术人员应当弘扬科学家精神，恪守学术道德和科研伦理，坚守科研诚信，不得在科学技术活动中有抄袭、剽窃、伪造、篡改等违背科研诚信的行为。国家完善科技伦理制度规范，促进科技向善，造福人类社会。</p>"
        ),
    },
    {
        "key": "law_zhuzuoquan",
        "category": "law",
        "label": "《中华人民共和国著作权法》作品保护与合理使用",
        "title": "《中华人民共和国著作权法》知识创作保护与合理使用制度述要",
        "content": (
            "<h2>一、保护文学艺术和科学作品作者的著作权</h2>"
            "<p>《中华人民共和国著作权法》旨在保护文学、艺术和科学作品作者的著作权，以及与著作权有关的权益，鼓励有益于社会主义精神文明、物质文明建设的作品的创作和传播，促进社会主义文化和科学事业的发展与繁荣。</p>"
            "<h2>二、著作权的人身权与财产权</h2>"
            "<p>本法所称的作品，是指文学、艺术和科学领域内具有独创性并能以一定形式表现的智力成果，包括文字作品、口述作品、音乐、戏剧、美术、摄影、视听作品及计算机软件等。著作权包括发表权、署名权、修改权、保护作品完整权等人身权，以及复制权、发行权、信息网络传播权等财产权。</p>"
            "<blockquote>为个人学习、研究或者欣赏，使用他人已经发表的作品，或者为介绍、评论某一作品或者说明某一问题，在作品中适当引用他人已经发表的作品，可以不经著作权人许可，但应当指明作者姓名或者名称、作品名称，并且不得影响该作品的正常使用。</blockquote>"
            "<h2>三、尊重知识产权与合规传播</h2>"
            "<p>在数字化与互联网时代，每一位内容创作者与传播者都应当严格遵守《著作权法》规范，尊重原创智力成果，规范引用来源，共同维护清朗、守法的网络知识共享生态。</p>"
        ),
    },
    {
        "key": "law_jiatingjiaoyu",
        "category": "law",
        "label": "《中华人民共和国家庭教育促进法》立德树人与家校协同",
        "title": "《中华人民共和国家庭教育促进法》立德树人与家校协同机制导读",
        "content": (
            "<h2>一、家庭教育的根本任务：立德树人</h2>"
            "<p>《中华人民共和国家庭教育促进法》是为了发扬中华民族重视家庭教育的优良传统，引导全社会注重家庭、家教、家风，增进家庭幸福与社会和谐，培养德智体美劳全面发展的社会主义建设者和接班人而制定的重要法律。家庭教育以立德树人为根本任务，培育和践行社会主义核心价值观。</p>"
            "<h2>二、科学的家庭教育方式与方法</h2>"
            "<p>未成年人的父母或者其他监护人实施家庭教育，应当关注未成年人的生理、心理、智力发展状况，尊重其参与相关家庭事务和发表意见的权利，合理运用以下方式方法：亲自养育，加强亲子陪伴；共同参与，发挥父母双方的作用；相机而教，寓教于日常生活之中；潜移默化，言传与身教相结合。</p>"
            "<blockquote>家庭教育应当尊重未成年人身心发展规律和个体差异，遵循家庭教育特点，贯彻科学的家庭教育理念和方法，严慈相济，关心爱护与严格要求并重，不得因性别、身体状况、智力等歧视未成年人。</blockquote>"
            "<h2>三、家校社协同育人生态</h2>"
            "<p>中小学校、幼儿园应当将家庭教育指导服务纳入工作计划，建立健全家庭学校沟通机制，及时向父母或者其他监护人了解未成年人情况，共同促进未成年人健康成长。</p>"
        ),
    },
]

CLASSIC_ESSAYS: List[Dict[str, Any]] = [
    {
        "key": "daxue",
        "category": "classics",
        "label": "【四书·大学】《大学》格物致知与修己安人",
        "title": "《大学》格物致知与修己安人之学次第阐微",
        "content": (
            "<h2>一、三纲领：大学之道在明明德</h2>"
            "<p>《大学》原为《礼记》第四十二篇，宋代以后列为“四书”之首。其开宗明义曰：“大学之道，在明明德，在亲民，在止于至善。”这三项总纲，既涵盖了内在道德品格的自觉发扬，亦包括了推己及人、造福群伦的人文关怀。知止而后有定，定而后能静，静而后能安，安而后能虑，虑而后能得。物有本末，事有终始，知所先后，则近道矣。</p>"
            "<h2>二、八条目：从格物致知到天下平之逻辑</h2>"
            "<p>古之欲明明德于天下者，先治其国；欲治其国者，先齐其家；欲齐其家者，先修其身；欲修其身者，先正其心；欲正其心者，先诚其意；欲诚其意者，先致其知；致知在格物。</p>"
            "<blockquote>物格而后知至，知至而后意诚，意诚而后心正，心正而后身修，身修而后家齐，家齐而后国治，国治而后天下平。自天子以至于庶人，壹是皆以修身为本。</blockquote>"
            "<h2>三、诚意慎独与苟日新之精神</h2>"
            "<p>所谓“格物”，朱熹注为“穷至事物之理”；所谓“诚其意者”，毋自欺也，故君子必慎其独也。汤之《盘铭》曰：“苟日新，日日新，又日新。”在求知与修业的道路上，唯有脚踏实地探究事物本原，保持内心的笃实与省察，日新不倦，方能达致“止于至善”的境界。</p>"
        ),
    },
    {
        "key": "zhongyong",
        "category": "classics",
        "label": "【四书·中庸】《中庸》诚明之道与博学笃行",
        "title": "《中庸》诚明之道与博学审问慎思明辨笃行考论",
        "content": (
            "<h2>一、天命之谓性，率性之谓道</h2>"
            "<p>《中庸》为“四书”之核心哲理著作，开篇即云：“天命之谓性，率性之谓道，修道之谓教。道也者，不可须臾离也，可离非道也。是故君子戒慎乎其所不睹，恐惧乎其所不闻。莫见乎隐，莫显乎微，故君子慎其独也。”</p>"
            "<h2>二、中和之境：致中和，天地位焉</h2>"
            "<p>喜怒哀乐之未发，谓之中；发而皆中节，谓之和。中也者，天下之大本也；和也者，天下之达道也。致中和，天地位焉，万物育焉。君子之中庸也，君子而时中，不偏不倚，无过不及，在变动不居的境遇中始终守持内心的平衡与理性。</p>"
            "<blockquote>博学之，审问之，慎思之，明辨之，笃行之。有弗学，学之弗能，弗措也；有弗问，问之弗知，弗措也；有弗思，思之弗得，弗措也；有弗辨，辨之弗明，弗措也；有弗行，行之弗笃，弗措也。人一能之，己百之；人十能之，己千之。果能此道矣，虽愚必明，虽柔必强。</blockquote>"
            "<h2>三、至诚无息：自诚明与自明诚</h2>"
            "<p>诚者，天之道也；诚之者，人之道也。自诚明，谓之性；自明诚，谓之教。诚则明矣，明则诚矣。治学与处世，贵在“博学、审问、慎思、明辨、笃行”五者环环相扣，以百倍之功笃实践行，自能由明致诚，豁然贯通。</p>"
        ),
    },
    {
        "key": "lunyu",
        "category": "classics",
        "label": "【四书·论语】《论语》为学之道与君子人格",
        "title": "《论语》为学之道：学思并重与君子人格修养札记",
        "content": (
            "<h2>一、学而时习之：求知的喜悦与自觉</h2>"
            "<p>《论语》首篇开宗明义：“学而时习之，不亦说乎？有朋自远方来，不亦乐乎？人不知而不愠，不亦君子乎？”孔门之学，并非徒事记诵以邀名禄，而是“古之学者为己”，将所学知识在实践中反复温习、体认，化为自身的生命涵养与淡定从容的胸襟。</p>"
            "<h2>二、学而不思则罔，思而不学则殆</h2>"
            "<p>孔子极为重视“学”与“思”的辩证统一。徒然博闻强记而不加独立思考，则易迷惘无所得；徒然凭空苦思而不以扎实学问为根基，则易流于空疏危殆。</p>"
            "<blockquote>子曰：“学而不思则罔，思而不学则殆。”又曰：“知之者不如好之者，好之者不如乐之者。”“三人行，必有我师焉：择其善者而从之，其不善者而改之。”</blockquote>"
            "<h2>三、仁智勇与君子九思</h2>"
            "<p>君子道者三：仁者不忧，知者不惑，勇者不惧。君子有九思：视思明，听思聪，色思温，貌思恭，言思忠，事思敬，疑思问，忿思难，见得思义。在日常言行中省察克治，“见贤思齐焉，见不贤而内自省也”，正是儒家君子人格砥砺升华的根本路径。</p>"
        ),
    },
    {
        "key": "mengzi",
        "category": "classics",
        "label": "【四书·孟子】《孟子》浩然之气与存心养性",
        "title": "《孟子》浩然之气与存心养性立身哲学探微",
        "content": (
            "<h2>一、四端之心：人皆有不忍人之心</h2>"
            "<p>《孟子》阐发心性之学，提出“恻隐之心，仁之端也；羞恶之心，义之端也；辞让之心，礼之端也；是非之心，智之端也。人之有是四端也，犹其有四体也。”凡能将此四端扩而充之，若火之始然，泉之始达，便能涵养出深厚博大的道德力量。</p>"
            "<h2>二、吾善养吾浩然之气</h2>"
            "<p>孟子论不动心之道，在于知言与养气：“其为气也，至大至刚，以直养而无害，则塞于天地之间。其为气也，配义与道；无是，馁也。是集义所生者，非义袭而取之也。行有不慊于心，则馁矣。”浩然正气并非偶一为之的装点，而是长期仰不愧于天、俯不怍于人的集义积累。</p>"
            "<blockquote>居天下之广居，立天下之正位，行天下之大道。得志，与民由之；不得志，独行其道。富贵不能淫，贫贱不能移，威武不能屈，此之谓大丈夫。</blockquote>"
            "<h2>三、学问之道无他，求其放心而已矣</h2>"
            "<p>孟子强调反求诸己：“爱人不亲，反其仁；治人不治，反其智；礼人不答，反其敬——行有不得者皆反求诸己，其身正而天下归之。”治学与修身的核心，在于收摄浮躁散漫之心，坚守本心良知，在困厄磨砺中“动心忍性，曾益其所不能”。</p>"
        ),
    },
    {
        "key": "shijing",
        "category": "classics",
        "label": "【五经·诗经】《诗经》风雅颂与温厚诗教传统",
        "title": "《诗经》风雅颂之比兴艺术与温厚诗教传统述评",
        "content": (
            "<h2>一、诗三百：思无邪与先秦人文精神</h2>"
            "<p>《诗经》是我国最早的一部诗歌总集，位列“五经”之首，收录西周初年至春秋中叶诗歌三百零五篇，分为“风”“雅”“颂”三大类。孔子高度评价《诗经》曰：“《诗》三百，一言以蔽之，曰：‘思无邪’。”又云：“不学《诗》，无以言。”</p>"
            "<h2>二、赋比兴之艺术手法与经典名句</h2>"
            "<p>《诗经》确立了“赋、比、兴”三大艺术表现手法。“赋者，敷陈其事而直言之也；比者，以彼物比此物也；兴者，先言他物以引起所咏之词也。”无论是《卫风·淇奥》中对君子切磋琢磨的赞颂，还是《小雅·鹿鸣》《小雅·鹤鸣》中的清雅意境，皆历久弥新。</p>"
            "<blockquote>有匪君子，如切如磋，如琢如磨。瑟兮僩兮，赫兮咺兮，有匪君子，终不可谖兮。（《诗经·卫风·淇奥》）<br/>高山仰止，景行行止。（《诗经·小雅·车辖》）</blockquote>"
            "<h2>三、兴观群怨与温柔敦厚之诗教</h2>"
            "<p>孔子曰：“小子何莫学夫《诗》？《诗》，可以兴，可以观，可以群，可以怨。迩之事父，远之事君；多识于鸟兽草木之名。”《礼记·经解》亦总结《诗》之教为“温柔敦厚”。研读《诗经》，既能体察万物生机与格物之趣，更能陶冶真挚纯粹、温和厚重的人格性情。</p>"
        ),
    },
    {
        "key": "shangshu",
        "category": "classics",
        "label": "【五经·尚书】《尚书》敬德保民与惟精惟一",
        "title": "《尚书》敬德保民与古代典籍治道思想考辨",
        "content": (
            "<h2>一、上古政书之祖：记言记事之典范</h2>"
            "<p>《尚书》又称《书》《书经》，为“五经”之一，是中国现存最早的史书与古典文献汇编，记载了虞、夏、商、周各代的重要典谟训诰。《礼记·经解》称“疏通知远，《书》教也”。其文字古奥庄重，蕴含着中华文明早期对自然、社会与道德责任的深刻思考。</p>"
            "<h2>二、人心惟危，道心惟微：十六字心传</h2>"
            "<p>《尚书·大禹谟》所载“人心惟危，道心惟微，惟精惟一，允执厥中”，被历代学者奉为修身治学的核心要诀。面对复杂多变的外物诱惑，唯有保持思想的纯粹专一、明辨精微，方能诚信笃实地秉持中正之道。</p>"
            "<blockquote>满招损，谦受益，时乃天道。（《尚书·大禹谟》）<br/>非知之艰，行之惟艰。（《尚书·说命中》）<br/>为山九仞，功亏一篑。（《尚书·旅獒》）</blockquote>"
            "<h2>三、知行并重与功崇惟志</h2>"
            "<p>《尚书·周官》云：“功崇惟志，业广惟勤；惟克果断，乃罔后艰。”高尚的功业源于远大的志向，广博的学业成于不懈的勤勉。研习《尚书》，在于领悟“非知之艰，行之惟艰”的务实作风，戒除骄满，脚踏实地把每一件实事做深做透。</p>"
        ),
    },
    {
        "key": "xueji",
        "category": "classics",
        "label": "【五经·礼记】《礼记·学记》教学相长与循序渐进",
        "title": "《礼记·学记》精义与古代循序渐进教学法考论",
        "content": (
            "<h2>一、发虑宪，求善良：古代教育之根本宗旨</h2>"
            "<p>《学记》出自“五经”之一的《礼记》，是中国古代教育史上的第一部系统论著，其开篇即言：“发虑宪，求善良，足以謏闻，不足以动众；就贤体远，足以动众，不足以化民。君子如欲化民成俗，其必由学乎！”此言明确指出了治学不仅是个人知识的积累，更是人格修养与社会教化的基石。</p>"
            "<blockquote>玉不琢，不成器；人不学，不知道。是故古之王者建国君民，教学为先。虽有嘉肴，弗食不知其旨也；虽有至道，弗学不知其善也。是故学然后知不足，教然后知困。知不足，然后能自反也；知困，然后能自强也。故曰：教学相长也。</blockquote>"
            "<h2>二、教学相长与藏息相辅之辨</h2>"
            "<p>《学记》极为推崇“藏息相辅”的修习节奏：“大学之教也，时教必有正业，退息必有居学。不学操缦，不能安弦；不学博依，不能安诗；不学杂服，不能安礼；不兴其艺，不能乐学。故君子之于学也，藏焉修焉，息焉游焉。”正业与居学互为表里，课内研习与课后内化相互补充，方能融会贯通。</p>"
            "<h2>三、豫时孙摩与长善救失</h2>"
            "<p>大学之法：禁于未发之谓豫，当其可之谓时，不陵节而施之谓孙，相观而善之谓摩。此四者，教之所由兴也。学者有四失，教者必知之：或失则多，或失则寡，或失则易，或失则止。知其心，然后能救其失也。唯有日积月累、循序渐进，方能进入“安其学而亲其师，乐其友而信其道”的境界。</p>"
        ),
    },
    {
        "key": "ruxing",
        "category": "classics",
        "label": "【五经·礼记】《礼记·儒行》学人立身准则与操守",
        "title": "《礼记·儒行》发微：古代学人之立身准则与独立操守",
        "content": (
            "<h2>一、儒有席上之珍以待聘</h2>"
            "<p>《礼记·儒行》全面阐述了古代学人自立、刚毅、博学、笃行的品格：“儒有席上之珍以待聘，夙夜强学以待问，怀忠信以待举，力行以待取。其自立有如此者。”治学者当下苦功夫积蓄真才实学，坚守忠信，以实际行动展现才德，而不求捷径与浮名。</p>"
            "<h2>二、儒有可亲而不可劫，可近而不可迫</h2>"
            "<blockquote>儒有可亲而不可劫也，可近而不可迫也，可杀而不可辱也。其居处不过，其饮食不溽，其过失可微谏而不可面数也。其刚毅有如此者。</blockquote>"
            "<h2>三、博学而不穷，笃行而不倦</h2>"
            "<p>《儒行》云：“儒有博学而不穷，笃行而不倦，幽居而不淫，上通而不困；礼之以和为贵，忠信之美，优游之法，慕贤而容众，毁方而瓦合。其宽裕有如此者。”学人之品格，外温润而内坚毅，对道义坚信不疑，对原则寸步不让，在日新又新的自我修养中承担社会责任。</p>"
        ),
    },
    {
        "key": "zhouyi",
        "category": "classics",
        "label": "【五经·周易】《周易》自强不息与厚德载物",
        "title": "《周易》自强不息与厚德载物之君子乾坤精神阐释",
        "content": (
            "<h2>一、乾坤大义：刚健进取与宽厚包容</h2>"
            "<p>《周易》居“五经”之首，被誉为“大道之源”。《易传·象传》提炼乾坤二卦之精神曰：“天行健，君子以自强不息；地势坤，君子以厚德载物。”这两句箴言，刚柔相济，动静相合，凝聚了中华民族数千年来奋发图强、豁达包容的精神脊梁。</p>"
            "<h2>二、进德修业：忠信所以进德，修辞立其诚</h2>"
            "<p>《周易·乾卦·文言》深入阐述了君子进德修业的具体途径：“君子进德修业。忠信，所以进德也；修辞立其诚，所以居业也。知至至之，可与几也；知终终之，可与存义也。”无论治学抑或立业，皆以“立诚”为第一要义。</p>"
            "<blockquote>君子终日乾乾，夕惕若，厉无咎。（《周易·乾卦》）<br/>穷则变，变则通，通则久。（《周易·系辞下》）<br/>尺蠖之屈，以求信也；龙蛇之蛰，以存身也。精义入神，以致用也；利用安身，以崇德也。（《周易·系辞下》）</blockquote>"
            "<h2>三、居安思危与顺时应势</h2>"
            "<p>《系辞》云：“君子安而不忘危，存而不忘亡，治而不忘乱，是以身安而国家可保也。”研读《周易》，绝非流于玄虚臆测，而是学习观察事物发展变化的客观规律，在顺境中保持谦抑警醒（“谦谦君子，卑以自牧”），在逆境中积蓄力量、自强不息。</p>"
        ),
    },
    {
        "key": "chunqiu",
        "category": "classics",
        "label": "【五经·春秋】《春秋左传》三不朽与慎始敬终",
        "title": "《春秋左传》微言大义与立德立功立言三不朽考论",
        "content": (
            "<h2>一、春秋大义：以史为鉴与褒贬彰瘅</h2>"
            "<p>《春秋》与《左传》为“五经”中史学与伦理结合之典范。《礼记·经解》称“属辞比事，《春秋》教也”。通过严谨的史实记述与精准的用词（“微言大义”），明辨是非善恶，使后世学者得以鉴往知来，确立行事准则。</p>"
            "<h2>二、太上有立德，其次有立功，其次有立言</h2>"
            "<p>《左传·襄公二十四年》载穆叔与范宣子论“死而不朽”，提出了中国思想史上著名的“三不朽”命题，超越了世俗门第与物质财富的局限，将人生价值确立于道德涵养、社会贡献与精神著述之上。</p>"
            "<blockquote>大上有立德，其次有立功，其次有立言，虽久不废，此之谓不朽。（《左传·襄公二十四年》）<br/>居安思危，思则有备，有备无患。（《左传·襄公十一年》）<br/>民生在勤，勤则不匮。（《左传·宣公十二年》）</blockquote>"
            "<h2>三、慎始而敬终，终以不困</h2>"
            "<p>《左传·襄公二十五年》云：“慎始而敬终，终以不困。”《庄公十年》亦载“衣食所安，弗敢专也，必以分人”之察狱实情。研读《春秋左传》，在于培养实事求是的历史眼光、居安思危的忧患意识，以及“慎始敬终”的笃实品格。</p>"
        ),
    },
    {
        "key": "quanxue",
        "category": "classics",
        "label": "【国学经典】荀子《劝学》积土成山与善假于物",
        "title": "荀子《劝学》全篇考释：论持续积累与善假于物",
        "content": (
            "<h2>一、学不可以已：终身求索之确立</h2>"
            "<p>荀子开篇断言：“君子曰：学不可以已。青，取之于蓝，而青于蓝；冰，水为之，而寒于水。木直中绳，輮以为轮，其曲中规。虽有槁暴，不复挺者，輮使之然也。故木受绳则直，金就砺则利，君子博学而日参省乎己，则知明而行无过矣。”</p>"
            "<p>荀子认为，人的后天发展完全取决于长期的学习与环境的陶冶。犹如金石经由磨砺而锋利，人通过广博的学习与每日的自我反省，方能达到智虑明澈、行为无过差的境地。</p>"
            "<h2>二、登高博见与善借外物之功</h2>"
            "<p>吾尝终日而思矣，不如须臾之所学也；吾尝跂而望矣，不如登高之博见也。登高而招，臂非加长也，而见者远；顺风而呼，声非加疾也，而闻者彰。假舆马者，非利足也，而致千里；假舟楫者，非能水也，而绝江河。君子生非异也，善假于物也。</p>"
            "<blockquote>积土成山，风雨兴焉；积水成渊，蛟龙生焉；积善成德，而神明自得，圣心备焉。故不积跬步，无以至千里；不积小流，无以成江海。骐骥一跃，不能十步；驽马十驾，功在不舍。锲而舍之，朽木不折；锲而不舍，金石可镂。</blockquote>"
            "<h2>三、用心一也：专注笃实之治学精神</h2>"
            "<p>蚓无爪牙之利，筋骨之强，上食埃土，下饮黄泉，用心一也。蟹六跪而二螯，非蛇鳝之穴无可寄托者，用心躁也。是故无冥冥之志者，无昭昭之明；无惛惛之事者，无赫赫之功。端正心志，专一精纯，博学深思，方为治学者通达大道之正途。</p>"
        ),
    },
    {
        "key": "shishuo",
        "category": "classics",
        "label": "【国学经典】韩愈《师说》传道受业与术业专攻",
        "title": "韩愈《师说》研读：论道之所存与师承之要义",
        "content": (
            "<h2>一、传道受业解惑：为师之本体</h2>"
            "<p>古之学者必有师。师者，所以传道受业解惑也。人非生而知之者，孰能无惑？惑而不从师，其为惑也，终不解矣。生乎吾前，其闻道也固先乎吾，吾从而师之；生乎吾后，其闻道也亦先乎吾，吾从而师之。吾师道也，夫庸知其年之先后生于吾乎？是故无贵无贱，无长无少，道之所存，师之所存也。</p>"
            "<h2>二、圣益圣，愚益愚之警示</h2>"
            "<blockquote>爱其子，择师而教之；于其身也，则耻师焉，惑矣。彼童子之师，授之书而习其句读者，非吾所谓传其道解其惑者也。句读之不知，惑之不解，或师焉，或不焉，小学而大遗，吾未见其明也。</blockquote>"
            "<p>巫医乐师百工之人，不耻相师。士大夫之族，曰师曰弟子云者，则群聚而笑之。韩愈此文，破斥虚浮名相，倡导尊道重理，对后世学风之振起影响深远。</p>"
            "<h2>三、弟子不必不如师，师不必贤于弟子</h2>"
            "<p>圣人无常师。孔子师郯子、苌弘、师襄、老聃。郯子之徒，其贤不及孔子。孔子曰：三人行，则必有我师。是故弟子不必不如师，师不必贤于弟子，闻道有先后，术业有专攻，如是而已。</p>"
        ),
    },
]

ALL_ESSAYS: List[Dict[str, Any]] = LAW_ESSAYS + CLASSIC_ESSAYS
ESSAY_BY_KEY: Dict[str, Dict[str, Any]] = {e["key"]: e for e in ALL_ESSAYS}


def get_essay_by_preset(aid: str, preset: str = "random_all") -> Dict[str, Any]:
    """根据指定预设（类别或单篇 key）和文章 ID 哈希，获取高价值替换内容。"""
    p = (preset or "random_all").strip()
    if p in ESSAY_BY_KEY:
        return ESSAY_BY_KEY[p]
    pool = ALL_ESSAYS
    if p == "law":
        pool = LAW_ESSAYS
    elif p == "classics":
        pool = CLASSIC_ESSAYS
    elif p == "sishu":
        pool = [e for e in CLASSIC_ESSAYS if e["key"] in ("daxue", "zhongyong", "lunyu", "mengzi")] or CLASSIC_ESSAYS
    elif p == "wujing":
        pool = [e for e in CLASSIC_ESSAYS if e["key"] in ("shijing", "shangshu", "xueji", "ruxing", "zhouyi", "chunqiu")] or CLASSIC_ESSAYS
    idx = int(hashlib.md5(str(aid).encode("utf-8")).hexdigest(), 16) % len(pool)
    return pool[idx]


def get_catalog() -> Dict[str, Any]:
    """返回完整文库目录，供前端下拉菜单与预览使用。"""
    return {
        "presets": [
            {"key": "classics", "label": "📚 四书五经与国学经典（按文章自动轮换）", "group": "group"},
            {"key": "sishu", "label": "📖 四书专集：《大学》《中庸》《论语》《孟子》轮换", "group": "group"},
            {"key": "wujing", "label": "📜 五经专集：《诗经》《尚书》《礼记》《周易》《春秋》轮换", "group": "group"},
            {"key": "law", "label": "⚖️ 国家现行法律条文（按文章自动轮换）", "group": "group"},
            {"key": "random_all", "label": "🎲 全部经典与法律条文混合轮换", "group": "group"},
            {"key": "custom", "label": "✍️ 自定义填写标题与正文内容", "group": "custom"},
        ],
        "classics": [
            {
                "key": e["key"],
                "category": "classics",
                "label": e.get("label") or e["title"],
                "title": e["title"],
                "content": e["content"],
            }
            for e in CLASSIC_ESSAYS
        ],
        "laws": [
            {
                "key": e["key"],
                "category": "law",
                "label": e.get("label") or e["title"],
                "title": e["title"],
                "content": e["content"],
            }
            for e in LAW_ESSAYS
        ],
    }

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
import os
import random
import re
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
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
        # 并发枚举：知乎 limit 上限是 20，244 篇要翻 13 页；串行 + sleep 会拖到 20 秒以上，
        # 中间任何一跳把长连接掐掉，前端就只能看到一排 0。改成并发拉页。
        try:
            self._page_workers = max(1, int(os.environ.get("QY_ENUM_WORKERS", "4")))
        except Exception:
            self._page_workers = 4
        self.last_diag: Dict[str, Any] = {}
        self._last_page_error: Optional[str] = None
        self._count_errors: List[str] = []
        self._tl = threading.local()

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
        """并发翻页枚举（只读）。

        知乎对 limit 的上限就是 20 —— 给 100 / 500 也只回 20 条，所以 244 篇
        必须翻 13 页。旧实现串行翻页 + 每页 sleep 0.4~0.9 秒，整发要 20~24 秒；
        这种「长时间没有任何字节流动」的请求，中间的代理/网关（Clash、Cloudflare、
        企业防火墙、手机热点）很容易把连接掐掉，浏览器只报一句 "Failed to fetch"，
        前端统计卡就永远停在初始值 0 —— 看起来像「识别到账号但 0 篇文章」。

        新实现：先取第 1 页拿 paging.totals，再把剩余页并发拉完（每线程独立
        Session 复用连接），失败页单独重试，并把诊断写进 self.last_diag 供上层回显。
        """
        self.last_diag = {"url": url, "pages": 0, "failed": [], "totals": None,
                          "error": None, "elapsed": 0.0}
        t0 = time.time()

        first = self._get_page(url, 0, limit)
        if first is None:
            self.last_diag["error"] = self._last_page_error or "首屏请求失败"
            self.last_diag["elapsed"] = round(time.time() - t0, 2)
            return []

        data, totals, is_end = first
        self.last_diag["pages"] = 1
        self.last_diag["totals"] = totals
        out: List[Dict[str, Any]] = [x for x in data if isinstance(x, dict)]

        if cap and len(out) >= cap:
            self.last_diag["elapsed"] = round(time.time() - t0, 2)
            return out[:cap]

        offsets: List[int] = []
        if not is_end and data:
            if isinstance(totals, int) and totals > len(data):
                off = len(data)
                while off < totals and len(offsets) < 400:
                    offsets.append(off)
                    off += limit

        if offsets:
            got: Dict[int, List[Dict[str, Any]]] = {}
            with ThreadPoolExecutor(max_workers=self._page_workers) as ex:
                futs = {ex.submit(self._get_page, url, o, limit): o for o in offsets}
                for fut in as_completed(futs):
                    o = futs[fut]
                    try:
                        res = fut.result()
                    except Exception:  # noqa: BLE001
                        res = None
                    if res is None:
                        self.last_diag["failed"].append(o)
                    else:
                        got[o] = [x for x in res[0] if isinstance(x, dict)]
            for o in offsets:
                if o in got:
                    out.extend(got[o])
                    self.last_diag["pages"] += 1
            # 并发里失败的页，单独再补一枪（更长的重试）
            for o in list(self.last_diag["failed"]):
                res = self._get_page(url, o, limit, retries=3)
                if res is not None:
                    out.extend(x for x in res[0] if isinstance(x, dict))
                    self.last_diag["pages"] += 1
                    self.last_diag["failed"].remove(o)
        elif not is_end and data:
            # totals 不可信 —— 老老实实串行翻到 is_end（sleep 也压到最短）
            offset = len(data)
            while offset <= 20000:
                res = self._get_page(url, offset, limit, retries=3)
                if res is None:
                    self.last_diag["failed"].append(offset)
                    break
                d, _t, end = res
                if not d:
                    break
                out.extend(x for x in d if isinstance(x, dict))
                self.last_diag["pages"] += 1
                if cap and len(out) >= cap:
                    break
                if end:
                    break
                offset += len(d)
                time.sleep(random.uniform(0.15, 0.35))

        # 去重：并发翻页遇到 totals 漂移可能拿到重复条目
        seen, uniq = set(), []
        for it in out:
            k = str(it.get("id") or "")
            if k and k in seen:
                continue
            if k:
                seen.add(k)
            uniq.append(it)

        self.last_diag["elapsed"] = round(time.time() - t0, 2)
        return uniq[:cap] if cap else uniq

    def _get_page(self, url: str, offset: int, limit: int,
                  retries: int = 2) -> Optional[Tuple[List[Dict[str, Any]], Any, bool]]:
        """取一页，返回 (data, totals, is_end)；彻底失败返回 None。

        和旧实现最大的区别：**不再把错误吞掉**。失败原因写进 self._last_page_error，
        上层能告诉用户「是 403 还是超时」，而不是一句干巴巴的 0。
        """
        delay = 0.6
        for attempt in range(retries + 1):
            try:
                r = self._thread_session().get(
                    url, params={"limit": limit, "offset": offset}, timeout=20)
                if r.status_code == 200:
                    j = r.json()
                    d = j.get("data")
                    if isinstance(d, list):
                        pg = j.get("paging") or {}
                        self._last_page_error = None
                        return d, pg.get("totals"), bool(pg.get("is_end", True))
                    self._last_page_error = "offset=%d 返回体里没有 data 列表" % offset
                else:
                    self._last_page_error = "offset=%d HTTP %d %s" % (
                        offset, r.status_code, (r.text or "")[:120])
            except Exception as exc:  # noqa: BLE001
                self._last_page_error = "offset=%d %s: %s" % (
                    offset, type(exc).__name__, exc)
            if attempt < retries:
                time.sleep(delay)
                delay *= 2
        return None

    def _thread_session(self) -> requests.Session:
        """每线程一个 Session。

        requests.Session 不是线程安全的，但「每线程独占一个」既能复用连接
        （省掉 TLS 握手），又能安全并发 —— 这是把 13 页从 20 秒压到 3 秒的关键。
        """
        tl = getattr(self, "_tl", None)
        if tl is None:
            tl = self._tl = threading.local()
        s = getattr(tl, "s", None)
        if s is None:
            s = tl.s = self._new_session()
        return s

    # ---------------- 计数补全 ---------------- #
    #
    # 列表接口偶尔漏字段，此时必须去详情端点补。补的时候有两个坑，
    # 旧实现全部踩中，而且都被 `except: pass` 吞成了「无声的 0」：
    #
    #   坑 1  文章 id 被喂给了 /api/v4/answers/{id}。
    #         实测恒返回 404 ResourceNotFoundException。
    #         文章详情的权威端点是 zhuanlan.zhihu.com/api/articles/{id}，
    #         它与列表接口、与页面显示三方一致；
    #         www.zhihu.com/api/v4/articles/{id} 恒返回 403，不可用。
    #
    #   坑 2  回答详情不带 ?include= 时 voteup_count / comment_count 都是 None。
    #         旧实现只 include 了 voteup_count，评论数拿不回来。
    #
    # 另外：补查只在真的缺字段时才发请求；要补的条目多就并发（每线程独立
    # Session，复用连接），避免「126 篇串行 × 超时」把接口拖死。
    # 补不到就保持 None，由下面统一归一为 0，同时把原因写进 last_diag ——
    # 让「0」是可解释的，而不是一个查不出原因的空白。
    # ------------------------------------------------------------------ #

    _DETAIL_URLS = {
        "article": ("https://zhuanlan.zhihu.com/api/articles/{id}"
                    "?include=voteup_count,comment_count"),
        "answer": ("https://www.zhihu.com/api/v4/answers/{id}"
                   "?include=voteup_count,comment_count"),
        "pin": ("https://www.zhihu.com/api/v4/pins/{id}"
                "?include=like_count,comment_count"),
    }

    def _detail_counts(self, kind: str, item_id: str) -> Dict[str, Any]:
        """按内容类型取权威计数；失败留痕，不静默。"""
        tmpl = self._DETAIL_URLS.get(kind)
        if not tmpl:
            return {}
        try:
            r = self._thread_session().get(tmpl.format(id=item_id), timeout=8)
            if r.status_code == 200:
                j = r.json()
                if isinstance(j, dict):
                    return j
            self._count_errors.append(
                "%s %s 详情 HTTP %d %s"
                % (kind, item_id, r.status_code, (r.text or "")[:80]))
        except Exception as exc:  # noqa: BLE001
            self._count_errors.append(
                "%s %s 详情 %s: %s" % (kind, item_id, type(exc).__name__, exc))
        return {}

    def _backfill_counts(self, rows: List[Dict[str, Any]],
                         kind: str) -> None:
        """就地补齐缺失的 voteup_count / comment_count。"""
        need = [r for r in rows
                if r.get("voteup_count") is None
                or r.get("comment_count") is None]
        self._count_errors = []
        if not need:
            return
        workers = min(8, max(1, len(need)))
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futs = {ex.submit(self._detail_counts, kind, r["id"]): r
                    for r in need}
            for fut in as_completed(futs):
                row = futs[fut]
                try:
                    d = fut.result() or {}
                except Exception:  # noqa: BLE001
                    continue
                if row.get("voteup_count") is None:
                    row["voteup_count"] = d.get("voteup_count")
                if row.get("comment_count") is None:
                    row["comment_count"] = d.get("comment_count")
        if self._count_errors:
            self.last_diag["count_errors"] = self._count_errors[:10]
            self.last_diag["count_failed"] = len(self._count_errors)

    @staticmethod
    def _normalise_counts(rows: List[Dict[str, Any]]) -> None:
        """None -> 0。前端拿到的永远是整数，绝不会是 null。"""
        for r in rows:
            r["voteup_count"] = r.get("voteup_count") or 0
            r["comment_count"] = r.get("comment_count") or 0

    def list_articles(self, cap: int = 0) -> List[Dict[str, Any]]:
        items = self._paginate(
            f"https://www.zhihu.com/api/v4/members/{self.url_token}/articles?include=data[*].comment_count,voteup_count",
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
        self._backfill_counts(out, "article")
        self._normalise_counts(out)
        return out

    def list_answers(self, cap: int = 0) -> List[Dict[str, Any]]:
        items = self._paginate(
            f"https://www.zhihu.com/api/v4/members/{self.url_token}/answers?include=data[*].comment_count,voteup_count",
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
        self._backfill_counts(out, "answer")
        self._normalise_counts(out)
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
        self._backfill_counts(out, "pin")
        self._normalise_counts(out)
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

    def get_answer(self, aid: str) -> Dict[str, Any]:
        r = self.s.get(
            f"https://www.zhihu.com/api/v4/answers/{aid}?include=content,editable_content,question",
            headers={"Referer": f"https://www.zhihu.com/answer/{aid}"},
            timeout=30)
        if r.status_code != 200:
            raise RuntimeError(f"读取回答失败 HTTP {r.status_code}")
        return r.json()

    def patch_answer(self, aid: str, content: str) -> Tuple[bool, str]:
        try:
            r = self.s.put(
                f"https://www.zhihu.com/api/v4/answers/{aid}",
                json={"content": content, "reshipment_settings": "allowed"},
                headers={"Origin": "https://www.zhihu.com",
                         "Referer": f"https://www.zhihu.com/answer/{aid}"},
                timeout=40)
            if r.status_code == 200:
                return True, "回答修改成功"
            return False, f"HTTP {r.status_code} {r.text[:140]}"
        except Exception as exc:  # noqa: BLE001
            return False, f"异常 {exc}"

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

    def apply_payload(self, item: Dict[str, Any],
                      payload: Dict[str, Any],
                      publish: bool = True) -> Dict[str, Any]:
        """把云端「预修改」好的内容原样上传（本地只搬运，不做二次加工）。

        payload = {"title": 最终标题, "content": 最终正文 HTML}
        本地这一步不做任何内容判断 —— 规则由云端定，结果也由云端复核。
        """
        aid = str(item.get("id"))
        t0 = time.time()
        new_title = (payload.get("title") or item.get("title")
                     or item.get("title_before") or "").strip()
        new_body = payload.get("content")
        rec: Dict[str, Any] = {
            "id": aid,
            "type": item.get("type", "article"),
            "kind_label": item.get("kind_label", "文章"),
            "url": item.get("url", ""),
            "title_before": item.get("title_before", item.get("title", "")),
            "title_after": new_title,
            "title_changed": False,
            "body_sha256_before": "",
            "body_sha256_after": "",
            "body_unchanged": None,
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
            "source": "cloud_payload",
        }
        try:
            before = self.get_article_draft(aid)
        except Exception as exc:  # noqa: BLE001
            rec["status"] = "failed"
            rec["message"] = f"读取原文失败：{exc}"
            rec["duration"] = round(time.time() - t0, 2)
            return rec
        body = before.get("content") or ""
        title_before = before.get("title") or ""
        fp_before = body_fingerprint(body)
        rec["title_before"] = title_before
        rec["body_sha256_before"] = fp_before
        rec["body_len"] = len(body)
        rec["body_hits_before"] = _qyc._ANY_TAG_RE.sub("", body).count(
            "清一新教育")
        rec["title_changed"] = (new_title != title_before)

        if not rec["title_changed"] and new_body is None:
            rec["status"] = "skipped"
            rec["message"] = "云端方案未要求改动"
            rec["duration"] = round(time.time() - t0, 2)
            return rec

        force_replace = (new_body is not None and len(new_body) > 100)

        rec["backup"] = self.backup("article", aid, title_before, fp_before,
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
                rec["message"] = f"已保存，发布未确认：{msgp}"
                rec["duration"] = round(time.time() - t0, 2)
                return rec

        time.sleep(random.uniform(0.8, 1.8))
        try:
            after = self.get_article_draft(aid)
            live_body = after.get("content") or ""
            live_title = after.get("title") or ""
            rec["body_sha256_after"] = body_fingerprint(live_body)
            rec["body_hits_after"] = _qyc._ANY_TAG_RE.sub(
                "", live_body).count("清一新教育")
            rec["body_hits_added"] = (rec["body_hits_after"]
                                      - rec["body_hits_before"])
            if new_body is not None:
                rec["body_as_planned"] = (
                    body_fingerprint(live_body) == body_fingerprint(new_body))
                rec["body_restorable"] = (
                    _qyc.strip_scenes(live_body) == _qyc.strip_scenes(new_body))
            rec["body_excerpt"] = _qyc.excerpt_around(live_body)
            if live_title.strip() != new_title.strip():
                rec["message"] = f"已提交；服务端回读标题为 {live_title[:40]}"
            else:
                rec["message"] = "已按云端方案完成（标题 + 正文，可一键还原）"
            rec["status"] = "done"
        except Exception as exc:  # noqa: BLE001
            rec["status"] = "done"
            rec["message"] = f"已提交（回读校验跳过：{exc}）"
        rec["duration"] = round(time.time() - t0, 2)
        return rec

    def process_title(self, item: Dict[str, Any], dry_run: bool = False,
                      publish: bool = True, inject_body: bool = False,
                      body_hits: int = 1, body_anchors: Optional[List[str]] = None,
                      title_add: Optional[bool] = None,
                      with_payload: bool = False) -> Dict[str, Any]:
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
                if with_payload:
                    # 把「最终标题 + 最终正文」一并交出去：调用方（云端）把它
                    # 缓存起来，本地执行器只负责原样上传，不再自行决定怎么改。
                    _pl_body = new_body if new_body is not None else body
                    rec["payload"] = {"title": new_title, "content": _pl_body}
                    rec["body_sha256_expected"] = body_fingerprint(_pl_body)
                    rec["body_len_before"] = len(body or "")
                    rec["body_len_expected"] = len(_pl_body or "")
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

    def _req(self, method: str, path: str, quiet: bool = False,
             **kw) -> Optional[Dict[str, Any]]:
        url = f"{self.base}{path}"
        try:
            r = self.s.request(method, url, timeout=self.timeout, **kw)
        except Exception as exc:  # noqa: BLE001
            if not quiet:
                print(f"[!] 网络错误 {method} {path}: {exc}")
            return None
        if r.status_code == 401:
            print("[!] 云端拒绝：访问密钥无效。请用 --key 传入站点密钥。")
            return None
        if r.status_code >= 400:
            if not quiet:
                print(f"[!] {method} {path} -> HTTP {r.status_code} "
                      f"{r.text[:160]}")
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

    # ---- v5：云端预修改的取回 + 请云端独立复核 ---- #

    def payload(self, job_id: str, item_id: str) -> Optional[Dict[str, Any]]:
        """取回云端为该篇预算好的最终稿；没有缓存则返回 None（回退本地计算）。"""
        res = self._req(
            "GET", f"/api/qy/agent/payload/{job_id}/{item_id}", quiet=True)
        if not res or res.get("raw"):
            return None
        if res.get("title") is None or res.get("content") is None:
            return None
        return res

    def verify(self, job_id: str, cookie: str = "") -> Optional[Dict[str, Any]]:
        """请云端独立复核（云端自己去回读线上文章，不信本地自述）。"""
        return self._req("POST", f"/api/qy/verify/{job_id}",
                         json={"cookie": cookie or ""})

    def brief(self, job_id: str = "") -> Optional[Dict[str, Any]]:
        q = f"?job_id={job_id}" if job_id else ""
        return self._req("GET", f"/api/qy/agent/brief{q}", quiet=True)


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
            # v5：优先用云端已经算好的最终稿（本地只负责原样上传）。
            # 取不到云端缓存时才退回本地计算，保证老流程不被破坏。
            payload = self.cp.payload(job_id, it["id"])
            if payload:
                print(f"   云端方案: {str(payload.get('title', ''))[:70]}")
                rec = self.signer.apply_payload(it, payload, publish=True)
            else:
                act_mode = (job.get("action_mode")
                            or (job.get("features") or {}).get("action_mode", ""))
                preset = (job.get("preset")
                          or (job.get("features") or {}).get("preset", "random_all"))
                custom_title = (job.get("custom_title")
                                or (job.get("features") or {}).get("custom_title", ""))
                custom_content = (job.get("custom_content")
                                  or (job.get("features") or {}).get("custom_content", ""))

                if act_mode == "replace_content":
                    try:
                        from .high_value_essays import get_essay_by_preset
                    except Exception:
                        try:
                            from high_value_essays import get_essay_by_preset
                        except Exception:
                            get_essay_by_preset = None

                    if preset == "custom" and custom_title and custom_content:
                        final_title = custom_title
                        final_content = custom_content
                    elif get_essay_by_preset is not None:
                        essay = get_essay_by_preset(it["id"], preset or "random_all")
                        final_title = essay["title"]
                        final_content = essay["content"]
                    else:
                        rec = {
                            "id": it["id"], "status": "failed",
                            "message": "replace_content 模式下高价值文库不可用",
                            "duration": 0.0,
                        }
                        self.cp.report_item(job_id, rec)
                        failed += 1
                        continue

                    local_payload = {"title": final_title, "content": final_content}
                    print(f"   本地替换方案（{preset}）: {final_title[:60]}")
                    rec = self.signer.apply_payload(it, local_payload, publish=True)
                else:
                    rec = self.signer.process_title(
                        it, dry_run=False, publish=True,
                        inject_body=bool(job.get("inject_body")),
                        body_hits=int(job.get("body_hits") or 1),
                        body_anchors=[p.get("anchor")
                                      for p in (plan.get("picks") or [])
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

        # ---- v5：写入结束 → 请云端独立复核（云端重新回读线上文章做规则校验）----
        try:
            vres = self.cp.verify(job_id, self.signer.cookie)
            if vres and vres.get("ok"):
                vs = vres.get("summary") or {}
                print(f"\n[云端复核] 校验 {vs.get('checked', 0)} 篇 | "
                      f"通过 {vs.get('passed', 0)} | 不通过 {vs.get('failed', 0)}"
                      f" | 未校验 {vs.get('skipped', 0)}")
                for d in (vres.get("details") or []):
                    if d.get("verify") == "fail":
                        print(f"   ✗ {d.get('id')}："
                              f"{'；'.join(d.get('reasons') or [])}")
            elif vres:
                print(f"\n[云端复核] 未执行：{vres.get('note')}")
        except Exception as exc:  # noqa: BLE001
            print(f"[云端复核] 跳过（{exc}）")

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
    """读取凭证。读不到就返回空串（交给上层做自动读取或手动输入兜底），不抛异常。"""
    def _clean(raw: str) -> str:
        s = (raw or "").strip()
        if s.lower().startswith("cookie:"):
            s = s.split(":", 1)[1].strip()
        for line in s.splitlines():
            line = line.strip()
            if line.startswith("#") or not line:
                continue
            if line.lower().startswith("cookie:"):
                line = line.split(":", 1)[1].strip()
            if "z_c0=" in line:
                return line
        if s and not s.startswith("#") and "=" not in s and len(s) >= 20:
            return f"z_c0={s}"
        return s if "z_c0=" in s else ""

    if args.cookie:
        return _clean(args.cookie)
    if args.cookie_file:
        p = Path(args.cookie_file)
        if not p.exists():
            return ""
        raw = p.read_text(encoding="utf-8", errors="replace")
        return _clean(raw)
    return ""


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
        print("凭证文件里没有可用登录态，尝试自动读取本机浏览器登录…")
        try:
            cookie, src = auto_detect_cookie()
            print(f"[OK] 已自动读取本机知乎登录（来源：{src}）。")
        except Exception as exc:
            cookie = ""
            print(f"[i] 自动读取未成功（{exc}）")
            try:
                _cp_tmp = ControlPlane(args.server, api_key)
                _cr = _cp_tmp.s.get(f"{args.server.rstrip('/')}/api/qy/credential-latest", timeout=10)
                if _cr.status_code == 200:
                    _cj = _cr.json()
                    if _cj.get("ok") and "z_c0=" in (_cj.get("cookie") or ""):
                        cookie = _cj["cookie"].strip()
                        print("[OK] 已从云端凭证柜自动获取知乎登录凭证。")
            except Exception:
                pass
            if not cookie:
                print("\n[手动输入 Cookie] 无需关闭浏览器，你也可以直接在此粘贴知乎 Cookie（包含 z_c0=...）：")
                try:
                    _pasted = input("    >>> 请粘贴知乎 Cookie（或按回车跳过）: ").strip()
                except EOFError:
                    _pasted = ""
                if _pasted:
                    if _pasted.lower().startswith("cookie:"):
                        _pasted = _pasted.split(":", 1)[1].strip()
                    if "=" not in _pasted and len(_pasted) >= 20:
                        _pasted = f"z_c0={_pasted}"
                    if "z_c0=" in _pasted:
                        cookie = _pasted
                        if args.cookie_file:
                            try:
                                Path(args.cookie_file).write_text(cookie + "\n", encoding="utf-8")
                                print(f"[OK] 已将手动输入的 Cookie 保存至 {args.cookie_file}")
                            except Exception:
                                pass
    if not cookie:
        raise SystemExit(
            "没有拿到知乎登录凭证，无法继续。\n"
            "  · 方式 1：直接把知乎 Cookie（含 z_c0=...）粘贴到同目录的 cookie.txt 文件里；\n"
            "  · 方式 2：运行 qingyi_client.py 打开本地网页控制台（http://127.0.0.1:8765）在网页上手动粘贴；\n"
            "  · 方式 3：关闭浏览器所有窗口后重试自动读取。")

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

