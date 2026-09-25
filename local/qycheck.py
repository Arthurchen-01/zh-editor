#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""qycheck —— 敏感内容检查（规则引擎 + 给你自己 AI 的提示词）。

设计前提
--------
你（用户）决定：「AI 可以让用户用自己的 AI（给那个 AI 一个提示词啥的）」。

所以本模块分两半，各自独立可用：

  ① 规则引擎（`scan()`）—— 纯本地、零依赖、离线可跑。
     它只做一件事：把「可能成为把柄」的表述**定位出来并给出改法**。
     它不判断文章好坏，不删任何东西，只产出一张清单。

  ② 提示词生成（`build_ai_prompt()`）—— 把同一套原则写成一封给 AI 的信。
     用户把提示词 + 文章正文贴给自己惯用的 AI，拿回严格 JSON，
     再由 `parse_ai_reply()` 解析进同一套数据结构。

两半的结论最后由 `cross_check()` 交叉比对 —— 这正是你要求的
「大家自己检查完后可以请伙伴帮忙交叉检查一下，保证没有漏网之鱼」。

定级约定（为什么是三级而不是布尔）
----------------------------------
    block  高危。几乎一定会被拿来做把柄，必须处理。
    warn   需要调整措辞。内容本身可能没问题，但用词会被误读。
    info   提示。需人工判断，或需要补一句说明。
清单里**不出现**「安全」这个结论 —— 没查到不等于没问题，这是刻意的。

🔴 block 级绝不允许裸词（本文件最重要的一条设计约束）
----------------------------------------------------
在真实账号上实测过：裸词定 block 会立刻产生误报 ——
    「对方只能听，没有真正的交流」   → 被当成「要求服从」
    「它完全颠覆了我对【学校】的概念」→ 被当成「政治颠覆」
    「你要去打人的话，机器狗比你强」 → 被当成「体罚」
三条都是 `block` 级假警报。后果很实际：用户被误报三次之后就不看了，
真问题从此漏掉 —— **误报的代价不是噪音，是失去检测能力**。

所以本表的约束是：
    block 级 → 必须有 `require`（同段共现的加重词）或紧邻语义约束；
               拿不准的降到 warn。
    warn  级 → 可以是裸词。warn 的语义就是「你的用词可能被误读」，
               裸词恰好命中这个语义。
    guard_before / guard_after → 词边界护栏，防止命中长词内部
               （如「精神让我」里含「神让我」）。

规则号命名
----------
    C**  邪教 / 迷信 / 精神控制
    S**  灵性 / 疗愈 措辞
    R**  修行 / 宗教 / 闭关
    F**  集资 / 传销 / 境外资金
    P**  政治 / 分裂政党 / 涉外身份
    D**  极端饮食 / 苦行 / 体罚
    W**  脱水减重（不改，但要说明）
    G**  与神对话 / 神启表述
    M**  机构 / 竞赛 / 宣传合规
    V**  隐私 / 可识别信息
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field as dc_field
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

__version__ = "1.0.0"

LEVELS = ("pass", "info", "warn", "block")
_LEVEL_RANK = {"pass": 0, "info": 1, "warn": 2, "block": 3}

CAT_LABEL = {
    "C": "邪教 / 迷信 / 精神控制",
    "S": "灵性 / 疗愈 措辞",
    "R": "修行 / 宗教 / 闭关",
    "F": "集资 / 传销 / 境外资金",
    "P": "政治 / 分裂政党 / 涉外身份",
    "D": "极端饮食 / 苦行 / 体罚",
    "W": "脱水减重（不改，须说明）",
    "G": "与神对话 / 神启表述",
    "M": "机构 / 竞赛 / 宣传合规",
    "V": "隐私 / 可识别信息",
}

_TAG_RE = re.compile(r"<[^>]+>")


def to_text(html: str) -> str:
    """富文本 → 纯文本。检查必须在纯文本上做，否则标签会把词切断。"""
    import html as _h
    if not html:
        return ""
    s = re.sub(r"<br\s*/?>", "\n", html, flags=re.I)
    s = re.sub(r"</(p|h[1-6]|li|blockquote|div|figure|td|tr)>", "\n", s, flags=re.I)
    s = re.sub(r"<img\b[^>]*>", " [图片] ", s, flags=re.I)
    s = _TAG_RE.sub("", s)
    s = _h.unescape(s)
    return re.sub(r"\n{2,}", "\n", s).strip()


# --------------------------------------------------------------------------- #
# 规则
# --------------------------------------------------------------------------- #

@dataclass
class Rule:
    id: str
    cat: str
    level: str
    pattern: str
    label: str
    why: str
    fix: str
    # 上下文约束
    require: Optional[str] = None    # 附近还必须出现（否则不报）
    exclude: Optional[str] = None    # 附近若出现则不报（自我豁免）
    window: int = 120                # require/exclude 的字符窗口上限
    same_para: bool = True           # 上下文是否限定在**同一段落**内（默认是）
    guard_before: str = ""           # 命中前不得出现的字符（防长词误命中）
    guard_after: str = ""            # 命中后不得出现的字符
    scope: str = "both"              # title / body / both（刻意不叫 field，
    #                                  否则遮蔽 dataclasses.field）
    _re: Optional[re.Pattern] = dc_field(default=None, repr=False)
    _rq: Optional[re.Pattern] = dc_field(default=None, repr=False)
    _ex: Optional[re.Pattern] = dc_field(default=None, repr=False)
    _gb: frozenset = dc_field(default=frozenset(), repr=False)
    _ga: frozenset = dc_field(default=frozenset(), repr=False)

    def compile(self) -> "Rule":
        self._re = re.compile(self.pattern, re.I)
        self._rq = re.compile(self.require, re.I) if self.require else None
        self._ex = re.compile(self.exclude, re.I) if self.exclude else None
        self._gb = frozenset(self.guard_before)
        self._ga = frozenset(self.guard_after)
        return self


# --------------------------------------------------------------------------- #
# 规则表
#
# 每条 fix 都是「可直接落笔的改法」，不是「建议注意」——
# 因为含糊的建议等于没建议。
# --------------------------------------------------------------------------- #

RULES: List[Rule] = [r.compile() for r in [

    # ================= C：邪教 / 迷信 / 精神控制 =================
    Rule("C01", "C", "block",
         r"邪教|cult\b",
         "直接出现「邪教」字样",
         "无论语境是批判还是引用，关键词本身就会被搜索命中并截图。",
         "删除该词；若必须讨论，改为「某些组织」并去掉可指向具体对象的信息。"),

    Rule("C02", "C", "block",
         r"教主|上师|活佛|转世灵童|法师开示",
         "宗教权威头衔",
         "配合「弟子」「供养」类表述时，是判定邪教组织的典型组合。"
         "护栏：实测「助教主要解决了」里的「助教主」不是这个词，已用前字护栏排除。",
         "改为「老师」「教练」「导师」等教育场景可解释的称呼。",
         guard_before="助"),

    Rule("C03", "C", "block",
         r"皈依|受戒|灌顶|顶礼",
         "宗教仪轨用语",
         "「顶礼」「皈依」在组织化语境下极易被解读为精神控制。",
         "改为「行礼」「致敬」。"),

    Rule("C03b", "C", "warn",
         r"跪拜|叩拜|膜拜",
         "跪拜类用语",
         "可能是武术抱拳礼等正当礼仪，但用词本身在公开语境中容易被误读。",
         "若为武术礼仪，写明「抱拳礼」「跪坐礼」等具体名称；否则改为「行礼」。"),

    Rule("C04", "C", "block",
         r"神通|特异功能|宿命通|他心通|天眼通",
         "超自然能力宣称",
         "宣称超自然能力是迷信/邪教类举报的核心证据。"
         "护栏：「神通广大」是常用成语，不是这个词。",
         "整段删除，或改为「状态好」「反应快」等可验证的描述。",
         guard_after="广"),

    Rule("C04b", "C", "warn",
         r"超能力|天眼",
         "超自然能力用语",
         "讨论影视、动漫或玩笑语境下无问题；单独被截图则风险很高。",
         "若在讲作品，写明作品名；若在讲自己，删除。"),

    Rule("C05", "C", "block",
         r"前世|今生来世|轮回转世|因果报应|业障|消业",
         "轮回 / 业力表述",
         "「业障」「消业」与「疗愈」组合时，指向灵修组织的特征非常明显。",
         "删除；若在讲心理变化，改为「过去的习惯」「长期积累的问题」。"),

    Rule("C06", "C", "block",
         r"通灵|驱魔|招魂|降头|巫术",
         "巫术 / 通灵表述",
         "涉迷信，且在法律上无任何辩护空间。",
         "整段删除。"),

    Rule("C07", "C", "block",
         r"末日|末世|大灾变|审判日",
         "末世论表述",
         "末世论是邪教动员的标准话术。",
         "删除；如指现实风险，改为具体的、可核实的表述。"),

    Rule("C07b", "C", "warn",
         r"劫难",
         "「劫难」措辞",
         "可能只是比喻（如「一场劫难」），但用词偏宗教化。",
         "改为「难关」「挫折」。"),

    Rule("C08", "C", "block",
         r"洗脑|精神控制|思想控制",
         "自认「洗脑 / 精神控制」",
         "即使是在自我调侃或反思，这个词被单独截图就是致命把柄。",
         "删除该词；改为「高强度的习惯重塑」等中性说法。"),

    Rule("C09", "C", "block",
         r"绝对服从|不许质疑|不准质疑|无条件听从|只能听(我|老师|教练|指挥|命令)",
         "服从性要求",
         "「绝对服从」是判定精神控制的直接证据。",
         "删除；改为「按计划执行」「先执行再复盘」等有边界感的表述。"),

    Rule("C09b", "C", "warn",
         r"只能听",
         "「只能听」措辞",
         "描述单向沟通（「对方只能听」）时无问题；但被单独截图会读成要求服从。",
         "改为「插不上话」「只有一方在讲」。"),

    Rule("C10", "C", "block",
         r"不许联系(家人|父母|外界)|断绝(联系|关系)",
         "隔离外界",
         "切断社会联系是非法组织控制的标志性特征。",
         "删除；若描述闭关，改为「集中训练期间手机统一保管」。"),

    Rule("C10b", "C", "warn",
         r"与世隔绝",
         "「与世隔绝」措辞",
         "可以只是形容地方偏远，但被单独截图会读成「被隔离」。",
         "改为「比较封闭」「离城市远」。"),

    Rule("C11", "C", "warn",
         r"感恩教育|孝道教育",
         "「感恩教育」类提法",
         "该提法在公开语境中已被与争议机构绑定。",
         "改为「家庭关系」「亲子沟通」。"),

    Rule("C12", "C", "warn",
         r"密宗|法门|仁波切|堪布|法王",
         "宗教派别 / 法门用语",
         "属宗教内容，本身不违法，但在机构对外内容中会被归入「宣教」。"
         "「法门」也常用于比喻（「找到自己的法门」），故只提醒不拦截。",
         "改为「方法」「路径」；确需谈宗教，写明是文化介绍而非宣导。"),

    Rule("C13", "C", "warn",
         r"秘密结社|秘密组织|地下组织",
         "结社类敏感表述",
         "该词在历史/政治语境下属中性描述，但若指向自身组织即构成不利证据。"
         "护栏：必须同段出现「我们」「加入」「成立」等自指词才提醒，"
         "避免把「历史上禁止秘密结社」这类中性叙述误报。",
         "删除；若在讲历史事件，写明具体年代与公开史料来源。",
         require=r"我们|加入|成立|成员|发展|组织起来",
         window=80),

    # ================= S：灵性 / 疗愈 措辞 =================
    Rule("S01", "S", "warn",
         r"疗愈",
         "「疗愈」措辞",
         "「疗愈」是灵修/身心灵行业的标志性用词，会被自动归入该类目。",
         "改为「恢复」「调养」「情绪疏导」「身体修复」等具体说法。"),

    Rule("S02", "S", "warn",
         r"灵性|灵修|身心灵|灵性成长",
         "「灵性」措辞",
         "同上；且「灵性」与「修行」同现时风险叠加。",
         "改为「心态」「内在状态」「心理建设」。"),

    Rule("S02b", "S", "warn",
         r"得道|开悟|悟道|明心见性",
         "「得道 / 开悟」措辞",
         "宣称达到某种境界，属「迷信」类目下的常见证据；"
         "单独被截图时无法用「这是比喻」解释。",
         "改为「想通了」「豁然开朗」「状态变好了」。"),

    Rule("S03", "S", "warn",
         r"能量场|高频能量|能量频率|能量疗愈|气场",
         "能量话语",
         "伪科学色彩明显，易被判定为迷信。",
         "删除；若在讲状态，改为「精神状态」「专注度」。"),

    Rule("S04", "S", "warn",
         r"脉轮|灵气|气脉|中脉|拙火",
         "身体能量体系术语",
         "与迷信/伪科学强关联。",
         "删除，或改为具体的身体感受描述（如「呼吸变慢」）。"),

    Rule("S05", "S", "warn",
         r"觉醒|扬升|升维|频率共振|同频",
         "灵修圈层用语",
         "「觉醒」「扬升」在公开语境中已高度绑定灵修组织。",
         "改为「想通了」「看清楚了」「状态提升」。"),

    Rule("S06", "S", "warn",
         r"灵魂|高我|本我觉醒|内在小孩",
         "灵魂 / 高我表述",
         "同 S05。",
         "改为「自己」「内心」。"),

    Rule("S07", "S", "warn",
         r"宇宙能量|吸引力法则|显化|同频共振|宇宙法则|宇宙规律",
         "新纪元运动用语",
         "属「迷信」类目下的常见证据。",
         "删除。"),

    # ---- 小灵魂与太阳：用户明确「这个故事不能留」 ----
    Rule("S08", "S", "block",
         r"小灵魂与太阳",
         "「小灵魂与太阳」故事",
         "该故事出自《与神对话》系列，是团队风控点名的**必须整段删除**项。"
         "书名可以改成「一本书」，但故事本身不能被改写保留 —— "
         "故事的情节特征（光、太阳）足以被反向识别。",
         "整段删除该故事，不要改写、不要缩写、不要只删标题。"),

    Rule("S08b", "S", "warn",
         r"小灵魂|你是光|把光遮住|遮住自己的光",
         "该故事的情节特征词",
         "单独出现时可能只是普通鼓励语；但与该故事同现即可反向识别。"
         "护栏：「你是光」单独出现不判 block，只提醒核对。",
         "核对是否出自该故事；若是，整段删除；若不是，改为「加油」「相信自己」。"),

    # ================= R：修行 / 宗教 / 闭关 =================
    Rule("R01", "R", "warn",
         r"修行",
         "「修行」措辞",
         "该词本身不违法，但在对外内容中会指向宗教/灵修，需要调整。",
         "改为「训练」「练习」「自我管理」。"),

    Rule("R02", "R", "warn",
         r"闭关",
         "「闭关」措辞",
         "「闭关」与「黑关」相邻出现时，指向争议训练法。",
         "改为「集中训练」「封闭式学习」「密集学习期」。"),

    Rule("R02b", "R", "warn",
         r"出关",
         "「出关」措辞",
         "与「闭关」同族，指结束争议训练法；也可能只是货物通关。"
         "护栏：「海关」「报关」「出境」同小句时判为通关，不提醒。",
         "若指结束闭关，改为「集中训练结束」；若指通关，本提醒可忽略。",
         exclude=r"海关|报关|通关|出境|口岸"),

    Rule("R03", "R", "block",
         r"黑关",
         "「黑关」",
         "该词是争议训练法的专名，被检索到时解释成本极高。",
         "整段删除该词；如需描述，改为「全暗环境的静坐训练」并说明时长与安全保障。"),

    Rule("R04", "R", "warn",
         r"慧心课",
         "「慧心课」",
         "课程专名，与上述训练法关联，属于需要调整的措辞。",
         "改为「心理课」「心态课」，或删除课程名只保留收获。"),

    Rule("R05", "R", "warn",
         r"打坐|禅修|静坐|冥想",
         "静坐类表述",
         "单独出现风险不高，但与其他灵修词同现时会被整体归类。",
         "改为「静坐」「安静下来」；保留「冥想」时去掉任何神秘化描述。"),

    Rule("R06", "R", "block",
         r"持咒|念经|诵经|做法事|开光|祈福仪式",
         "宗教仪式",
         "属宗教活动，公开传播有合规风险。",
         "整段删除。"),

    Rule("R07", "R", "warn",
         r"道场|寺庙|寺院|出家",
         "宗教场所 / 身份",
         "同上。",
         "删除或改为中性的地点描述。"),

    Rule("R08", "R", "warn",
         r"辟谷",
         "「辟谷」措辞",
         "断食类修行专名，与「黑关」「闭关」同属争议训练法叙事。",
         "改为「清淡饮食调理」；若为医学方案，写明医疗机构。"),

    # ================= F：集资 / 传销 / 境外资金 =================
    Rule("F01", "F", "block",
         r"拉人头|发展下线|层级返利|三级分销|团队计酬",
         "传销特征",
         "直接对应《禁止传销条例》的构成要件。",
         "整段删除。"),

    Rule("F02", "F", "block",
         r"静态收益|动态收益|复利滚动|稳赚不赔|保本高息|躺赚",
         "非法集资特征",
         "承诺收益是非法吸收公众存款的核心特征。",
         "整段删除。"),

    Rule("F03", "F", "block",
         r"资金盘|互助盘|拆分盘|盘子|拆分模式",
         "资金盘用语",
         "同上。",
         "整段删除。"),

    Rule("F04", "F", "block",
         r"虚拟币|代币|token\s*发行|IEO|IDO|挖矿收益|量化机器人",
         "加密资产募资",
         "在国内属非法金融活动，涉刑事风险。"
         "「代币」在游戏/积分语境下属正常用词，故要求同段出现募资语义。",
         "整段删除。",
         require=r"投资|收益|募集|发行|认购|暴涨|赚|买入|出售|返利|佣金|钱包|交易所",
         window=90),

    Rule("F05", "F", "block",
         r"原始股|内部股|上市前认购|期权兑现|股权众筹",
         "变相公开发行证券",
         "未经许可向不特定对象发行证券违法。",
         "整段删除。"),

    Rule("F06", "F", "block",
         r"境外(账户|公司|平台|服务器).{0,20}(充值|转账|汇款|入金|出金)",
         "跨境资金流动",
         "「跨国非法集资」的典型结构：境外账户 + 境内拉人。",
         "整段删除；确需收款时只保留境内合规渠道。"),

    Rule("F07", "F", "warn",
         r"会员费|入会费|加盟费.{0,30}(返|分|提成|佣金)",
         "会员费 + 返佣结构",
         "单独出现不违法，但「入门费 + 拉人返利」两要件齐备即构成传销。",
         "删除返佣描述，只保留一次性服务费。"),

    Rule("F08", "F", "warn",
         r"外汇|购汇|换汇|跨境汇款",
         "外汇相关表述",
         "若与投资/收益同现，会被归入非法集资叙事。",
         "删除；如为合规业务，须写明持牌机构名称。"),

    Rule("F09", "F", "block",
         r"(?:跨国|跨境|境外|海外|国外)(?:的)?(?:非法)?(?:集资|募资|融资)",
         "跨国非法集资",
         "用户点名的红线之一。带「跨国/跨境/境外」限定语的集资表述，"
         "即指向「境外主体 + 境内吸金」这一典型结构，无辩解空间。",
         "整段删除；不得改写保留。"),

    Rule("F09b", "F", "warn",
         r"非法集资|非法募资",
         "「非法集资」泛提法",
         "可能是批判他人或普法叙述（「要警惕非法集资」），"
         "所以不裸词拦截；但被单独截图仍有风险。",
         "若在批判他人，写明主语（「某些机构」）；否则删除。"),

    # ================= P：政治 / 分裂政党 / 涉外身份 =================
    Rule("P01", "P", "block",
         r"境外政党|分裂势力|民族自决|公投独立|分裂政党|分裂党|独立政党",
         "分裂相关表述",
         "涉国家主权与领土完整，无任何回旋空间。"
         "用户点名的红线之一（「国外分裂政党」）。",
         "整段删除，不得改写保留。"),

    Rule("P01b", "P", "warn",
         r"独立运动",
         "「独立运动」措辞",
         "可能指历史或体育语境，但用词指向分裂主张，必须核对。",
         "改为「争取自治的过程」或删除。"),

    Rule("P02", "P", "block",
         r"政治庇护|难民身份|政治避难",
         "政治庇护相关",
         "同上；且会被解读为对国家的负面定性。",
         "整段删除。"),

    Rule("P03", "P", "block",
         r"颠覆(国家|政权|政府|党|体制|制度)|推翻政权|颜色革命|反华|反共",
         "颠覆类表述",
         "涉国家政权，无任何回旋空间。"
         "注意：「颠覆认知」「颠覆了我的想法」属比喻义，本规则刻意不命中。",
         "整段删除。"),

    Rule("P04", "P", "block",
         r"港独|台独|疆独|藏独|一中一台|两国论",
         "涉港台疆藏的错误表述",
         "涉及国家主权与领土完整。中国香港、中国台湾、中国澳门均是中国的一部分。",
         "整段删除，并核对全文所有涉及地区的表述是否规范。"),

    Rule("P05", "P", "warn",
         r"(国外|海外|境外).{0,12}(政党|党派|议会|议员).{0,20}(加入|参与|支持|站台)",
         "涉外政治参与",
         "参与境外政治活动会被放大解读。",
         "删除参与类描述。"),

    Rule("P06", "P", "warn",
         r"(香港|台湾|澳门)(?!.{0,6}(地区|省|特别行政区))",
         "地区表述待核对",
         "涉外内容中地区称谓必须规范。",
         "改为「中国香港」「中国台湾」「中国澳门」，或「香港地区」「台湾地区」。",
         guard_before="国"),

    # ================= D：极端饮食 / 苦行 / 体罚 =================
    Rule("D01", "D", "block",
         r"只能吃|不许吃|不准吃|禁止吃|只给吃|只准吃|统一只吃",
         "被强制限定的饮食",
         "「只能吃 X」是外界解读为虐待的直接依据。"
         "「我是素食者只能吃素」这类个人选择不该报 —— 故要求同段出现"
         "组织化/强制性的施动者。",
         "改为「简单饮食」；若确为自愿，必须写明「我自己选择」。",
         require=r"我们|大家|所有人|全部|每天|统一|规定|要求|老师|学校|教练",
         exclude=r"我自己选择|我主动选择|我选择|自愿|个人选择",
         window=80),

    Rule("D02", "D", "warn",
         r"黄豆酱(配|拌|和)?(米饭|白饭)|白饭(配|拌)?黄豆酱|馒头(配|就)?咸菜",
         "极端简朴饮食的具体描述",
         "具体到「只吃某两样」会被当作虐待证据，而非「朴素」证据。",
         "改为「简单饮食」，不写具体食物搭配。",
         exclude=r"我自己选择|我主动选择|我选择|自愿|个人选择",
         window=80),

    Rule("D03", "D", "block",
         r"断食\s*\d+\s*天|不吃不喝|连续\s*\d+\s*天不(吃|进食)",
         "断食 / 挨饿描述",
         "涉及未成年人时，会被认定为虐待。",
         "整段删除；如为医学监督下的方案，须写明医疗机构与医生。"),

    Rule("D03b", "D", "warn",
         r"(饿|空)肚子|忍饥挨饿|忍饿|挨饿",
         "「饿肚子」措辞",
         "可能只是口语（「不想饿肚子」），但被单独截图会读成「被饿着」。",
         "改为「饮食简单」「吃得少」。"),

    Rule("D04", "D", "block",
         r"体罚|鞭打|抽打|扇耳光|罚跪|关禁闭|小黑屋|打骂(学生|孩子)|殴打",
         "体罚 / 变相体罚",
         "涉《未成年人保护法》明令禁止的行为。"
         "注意：「打人」在讨论格斗/搏击语境下属正常用词，本规则刻意不命中。",
         "整段删除；改为「承担后果」「加练」「复盘」。"),

    Rule("D04b", "D", "warn",
         r"打人|罚站|罚跑",
         "惩罚 / 暴力相关用语",
         "讨论格斗、竞技、社会现象时属正常用词；但被单独截图有风险。",
         "若在讲现象，写明语境；若在讲自己的经历，改为「体能加练」。"),

    Rule("D05", "D", "warn",
         r"加练\s*\d+\s*(圈|公里|小时)|俯卧撑\s*\d+\s*个",
         "惩罚性训练",
         "量化的惩罚易被解读为体罚。",
         "改为「体能加练」并去掉数字。"),

    Rule("D06", "D", "warn",
         r"(不许|不准|不能)(吃|睡|休息|坐下)",
         "疑似剥夺基本需求",
         "可能只是医嘱或过敏（「不能吃花生」），但被单独截图会读成「被剥夺」。",
         "改为「按统一安排作息」；如为医嘱，写明「医生要求」。",
         require=r"老师|教练|学校|规定|要求|命令|统一|必须|所有人|全部人|大家",
         window=60),

    # ================= W：脱水减重（不改，须说明） =================
    # 刻意定为 warn 而非 info：用户的原则是「不用改，但必须说清楚是为打比赛做的」，
    # 也就是**有一个必须补的动作**。info 级在界面上容易被整批忽略，等于没提醒。
    Rule("W01", "W", "warn",
         r"脱水|控水|减重|称重前|过磅|降体重",
         "脱水减重",
         "**不用改**，但必须写明「这是为参加比赛（称重）做的」，"
         "否则会被读成长期节食或虐待。",
         "补一句：「以下脱水减重是为参加 XX 比赛称重做的，赛前短期、有教练监督、"
         "赛后立即恢复，不是日常做法。」"),

    # ================= G：与神对话 / 神启表述 =================
    Rule("G01", "G", "warn",
         r"与神对话|与神交流|与神沟通|与神对谈|与神的对话",
         "《与神对话》相关",
         "该书属新纪元运动代表作，与灵性话题强绑定，可能需要删减。",
         "删除书名；若要保留收获，改为「一本书」「一段对话」。"),

    Rule("G04", "G", "block",
         r"(?<![A-Za-z])(?:ysdh)(?![A-Za-z])",
         "拼音缩写避审（ysdh）",
         "用拼音缩写指代书名是**刻意规避检索**的行为，本身就是不利证据 —— "
         "比写出书名更严重。这条不是「措辞不当」，而是「有意隐藏」。",
         "删掉缩写；若确指某本书，改为「看了一本书」或「书中说」，"
         "不要用任何缩写形式。"),

    Rule("G02", "G", "block",
         r"神(说|告诉我|跟我说|的声音|让我)|上帝(说|告诉我)|上天(说|告诉我)",
         "神启式表述",
         "「神告诉我」会被直接归入迷信。"
         "护栏：「精神让我」「眼神让我」等长词内含不命中。",
         "改为「我意识到」「我想明白」。",
         guard_before="精出入眼心定愣走失分提聚"),

    Rule("G03", "G", "warn",
         r"(宇宙|上天|老天)(安排|注定|指引|给我|的旨意)",
         "宿命 / 天意表述",
         "同 G02。",
         "改为「恰好」「后来发现」。"),

    # ================= M：机构 / 竞赛 / 宣传合规 =================
    Rule("M01", "M", "warn",
         r"(清华|北大|哈佛|耶鲁|MIT|斯坦福|牛津|剑桥|东京大学|早稻田)"
         r".{0,30}(保(录|送|过)|一定(能|会)(上|进)|包(录取|进|上)|保证录取)",
         "名校录取承诺",
         "教育广告中的录取承诺违反《广告法》。",
         "删除承诺；改为「往届学员的录取结果（示例，非承诺）」。"),

    Rule("M02", "M", "warn",
         r"保过|包过|包就业|100%\s*(提分|通过|录取)|提分\s*\d+\s*分",
         "效果承诺",
         "同上。",
         "删除数字承诺；改为「示例数据，非承诺」。"),

    Rule("M03", "M", "warn",
         r"《[^》]{2,30}》第\s*[\d一二三四五六七八九十]+条",
         "引用具体法条",
         "法条引用一旦被改字或误引，会被当作「伪造法规」。",
         "核对原文后再保留；不确定就删掉条号，只讲结论。"),

    Rule("M04", "M", "warn",
         r"(法规|政策|法律).{0,10}(规定|要求|允许|禁止).{0,30}(可以|不必|无需|不受限制)",
         "对法规的转述",
         "转述法规属于高风险表述，容易被指为曲解。",
         "改为直接引用原文，或删除该句。"),

    Rule("M05", "M", "info",
         r"(大学|学院|高中|中学|小学)(录取|申请|招生).{0,20}(内部|关系|渠道|名额)",
         "升学渠道暗示",
         "「内部名额」类暗示涉违规。",
         "删除「内部」「关系」「渠道」等词。"),

    # ================= V：隐私 / 可识别信息 =================
    Rule("V01", "V", "block",
         r"1[3-9]\d{9}",
         "手机号",
         "个人手机号公开等于泄露，且会被用于骚扰举报。",
         "删除或打码为 138****0000。"),

    Rule("V02", "V", "block",
         r"\b\d{17}[\dXx]\b",
         "身份证号",
         "高危隐私泄露。",
         "整段删除。"),

    Rule("V03", "V", "warn",
         r"(微信|wechat|vx|v信|扣扣|qq)\s*[:：]?\s*[A-Za-z0-9_\-]{5,}",
         "联系方式",
         "公开联系方式会被用于举报与骚扰。",
         "改为「私信联系」。"),

    Rule("V04", "V", "warn",
         r"(住在|地址|家庭住址|宿舍)\s*[:：]?\s*[^\n，。]{4,40}",
         "住址信息",
         "隐私风险。",
         "删除具体地址，只保留城市。"),

    Rule("V05", "V", "warn",
         r"(未成年|学生).{0,10}(真名|姓名|全名)|[A-Z][a-z]{1,10}\s*(同学|学生)",
         "未成年人可识别信息",
         "公开未成年人姓名涉《未成年人保护法》与个人信息保护要求。",
         "改为「A 同学」「一位学生」。"),
]]


# --------------------------------------------------------------------------- #
# 扫描
# --------------------------------------------------------------------------- #

@dataclass
class Finding:
    rule_id: str
    cat: str
    cat_label: str
    level: str
    label: str
    why: str
    fix: str
    field: str
    text: str
    start: int
    end: int
    excerpt: str
    source: str = "rules"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "rule_id": self.rule_id, "cat": self.cat,
            "cat_label": self.cat_label, "level": self.level,
            "label": self.label, "why": self.why, "fix": self.fix,
            "field": self.field, "text": self.text,
            "start": self.start, "end": self.end,
            "excerpt": self.excerpt, "source": self.source,
        }


def _excerpt(text: str, start: int, end: int, pad: int = 30) -> str:
    lo = max(0, start - pad)
    hi = min(len(text), end + pad)
    return ("…" if lo > 0 else "") + text[lo:hi].replace("\n", " ") \
        + ("…" if hi < len(text) else "")


def _paragraph_spans(text: str) -> List[Tuple[int, int]]:
    """非空行的连续区间 —— 检查器的「段落」定义。"""
    return [(m.start(), m.end()) for m in re.finditer(r"[^\n]+", text or "")]


# 转折词：豁免声明被它推翻
_CONTRAST_RE = re.compile(r"但|可是|然而|不过|却|结果|后来|反而|竟|没想到|岂料|谁知道")


def _exempt(text: str, rule: "Rule", s: int, e: int, lo: int, hi: int) -> bool:
    """判断该命中是否被 ``exclude`` 豁免。

    为什么不是「按小句判定」
    ------------------------
    最初想用「豁免必须与命中同小句」来修 bug，但实测立刻出现反例：

        「这批货下周三出关，走的是海关报关流程。」
        命中「出关」在第一小句，豁免词「海关」在第二小句
        → 判成不豁免 → 误报。

    所以小句边界不是正确的判据。真正的信号是**转折**：

    * 「我自己选择吃米糊，**但**后来变成了只能吃米糊」
      —— 豁免被「但」推翻了，必须报。
    * 「我自己选择只能吃简单饮食」
      —— 豁免与命中之间没有任何转折，豁免成立。
    * 「这批货下周三出关，走的是海关报关流程」
      —— 豁免在命中之后，中间无转折，豁免成立。

    规则：豁免按**段落**取（同段的说明都算数），但只要豁免与命中之间
    夹了转折词，豁免作废 —— 因为那说明「自愿」已被后面的事推翻。
    """
    m = rule._ex.search(text[lo:hi])
    if not m:
        return False
    ex_s, ex_e = lo + m.start(), lo + m.end()
    if ex_e <= s:                       # 豁免在命中之前
        return not _CONTRAST_RE.search(text[ex_e:s])
    if ex_s >= e:                       # 豁免在命中之后
        return not _CONTRAST_RE.search(text[e:ex_s])
    return True                         # 豁免与命中重叠 → 视为豁免


def _context_span(spans: List[Tuple[int, int]], pos: int, end: int,
                  rule: "Rule", text: str) -> Tuple[int, int]:
    """算出 require/exclude 的作用区间。

    为什么必须按段落而不是按字符窗口
    --------------------------------
    实测反例：正文第一段是「我们每天只能吃黄豆酱配米饭」，
    第四段是「我自己选择吃米糊」。按 80 字符窗口取上下文时，
    第四段的「我自己选择」落进了窗口里，于是把第一段的强制饮食
    **静默豁免**掉了 —— 一条真问题凭空消失。

    语义上「我自己选择」只能豁免它**自己所在的那句话/那一段**，
    不能豁免几十字外的另一段。所以默认按段落取上下文，
    `window` 退化为该段落内的一个上限。
    """
    lo, hi = 0, len(text)
    if rule.same_para:
        for a, b in spans:
            if a <= pos < b:
                lo, hi = a, b
                break
        else:
            # 命中的是空行里的字符（罕见）→ 退化为整篇
            lo, hi = 0, len(text)
    if rule.window and rule.window > 0:
        lo = max(lo, pos - rule.window)
        hi = min(hi, end + rule.window)
    return lo, hi


def scan(title: str, body_html: str, only_level: Optional[str] = None,
         rules: Optional[Sequence[Rule]] = None) -> List[Finding]:
    """扫一遍标题 + 正文，返回按位置排序的问题清单。

    这是**只读**操作：不改任何字，不返回改写结果。改法在 finding["fix"] 里。
    """
    body = to_text(body_html)
    found: List[Finding] = []
    for r in (rules if rules is not None else RULES):
        for fname, text in (("title", title or ""), ("body", body)):
            if r.scope != "both" and r.scope != fname:
                continue
            if not text:
                continue
            spans = _paragraph_spans(text) if (r._rq or r._ex) else []
            for m in r._re.finditer(text):
                s, e = m.start(), m.end()
                # 词边界护栏：防「精神让我」命中「神让我」这类长词内含
                if r._gb and s > 0 and text[s - 1] in r._gb:
                    continue
                if r._ga and e < len(text) and text[e] in r._ga:
                    continue
                if r._rq or r._ex:
                    lo, hi = _context_span(spans, s, e, r, text)
                    if r._rq and not r._rq.search(text[lo:hi]):
                        continue
                    # exclude 走 _exempt：段落取豁免，但被转折词推翻就作废。
                    if r._ex and _exempt(text, r, s, e, lo, hi):
                        continue
                found.append(Finding(
                    rule_id=r.id, cat=r.cat, cat_label=CAT_LABEL.get(r.cat, r.cat),
                    level=r.level, label=r.label, why=r.why, fix=r.fix,
                    field=fname, text=m.group(0), start=s, end=e,
                    excerpt=_excerpt(text, s, e)))
                break  # 同一规则在同一字段只报第一处，避免刷屏
    found.sort(key=lambda f: (-_LEVEL_RANK.get(f.level, 0),
                              f.field != "title", f.start))
    if only_level:
        keep = _LEVEL_RANK.get(only_level, 0)
        found = [f for f in found if _LEVEL_RANK.get(f.level, 0) >= keep]
    return found


def report(findings: List[Finding]) -> Dict[str, Any]:
    """把清单汇总成页面/报告能直接用的结构。"""
    if not findings:
        return {"level": "pass", "hits": 0, "by_level": {}, "by_cat": {},
                "block": 0, "warn": 0, "info": 0, "findings": [],
                "note": "规则引擎未发现已知风险表述。这不等于没有问题。"}
    lv = max(_LEVEL_RANK.get(f.level, 0) for f in findings)
    by_level: Dict[str, int] = {}
    by_cat: Dict[str, int] = {}
    for f in findings:
        by_level[f.level] = by_level.get(f.level, 0) + 1
        by_cat[f.cat] = by_cat.get(f.cat, 0) + 1
    name = {v: k for k, v in _LEVEL_RANK.items()}[lv]
    return {
        "level": name,
        "hits": len(findings),
        "by_level": by_level,
        "by_cat": {CAT_LABEL.get(k, k): v for k, v in by_cat.items()},
        "block": sum(1 for f in findings if f.level == "block"),
        "warn": sum(1 for f in findings if f.level == "warn"),
        "info": sum(1 for f in findings if f.level == "info"),
        "findings": [f.to_dict() for f in findings],
    }


# --------------------------------------------------------------------------- #
# 给你自己 AI 的提示词
# --------------------------------------------------------------------------- #

AI_PROMPT = """\
你是内容合规审校员。你的任务是**只找出问题，不重写全文**。

【背景】
我在为一家教育机构做公开内容。内容会发布在国内平台，可能被竞争对手、
媒体或监管部门逐字检索。我需要你帮我找出「可能成为把柄」的表述。

【检查范围 —— 逐项过，不要跳过】
1. 邪教 / 迷信 / 精神控制：教主、皈依、顶礼、神通、前世轮回、业障、
   通灵、附体、末世论、洗脑、绝对服从、不许联系家人。
2. 灵性 / 疗愈措辞：疗愈、灵性、能量场、脉轮、觉醒、扬升、灵魂、
   高我、宇宙能量、吸引力法则。
3. 修行 / 宗教 / 闭关：修行、闭关、黑关、慧心课、打坐、持咒、念经、道场。
4. 集资 / 传销 / 境外资金：拉人头、发展下线、层级返利、静态/动态收益、
   稳赚不赔、资金盘、虚拟币募资、原始股、境外账户收款。
5. 政治 / 分裂政党 / 涉外身份：境外政党、分裂势力、独立运动、政治庇护、
   颠覆类表述。注意：中国香港、中国台湾、中国澳门都是中国的一部分，
   任何把它们当作独立国家的表述都必须指出。
6. 极端饮食 / 苦行 / 体罚：只能吃 X、不许吃 X、断食 N 天、打骂、体罚、
   罚跪、关禁闭。
7. 脱水减重：**这类不用改**，但如果出现，必须在文中有说明「这是为参加
   比赛称重做的」。若没有说明，把它列为需要补说明的问题。
8. 与神对话 / 神启：与神对话、神说、神告诉我、宇宙安排。
9. 宣传合规：名校录取承诺、保过包就业、100% 提分、引用具体法条、
   对法规的转述、「内部名额」暗示。
10. 隐私：手机号、身份证号、微信号、住址、未成年人真实姓名。

【判断原则】
- 「只能吃黄豆酱配米饭」这类**被限定**的饮食 → 必须改，改成「简单饮食」。
- 但若原文明确写的是「我自己选择吃米糊 / 简单饮食」→ 没问题，不要报。
- 脱水减重本身不改，但缺说明要报。
- 宁多勿漏：拿不准的，报出来并标注 confidence 为 low。

【输出格式 —— 必须是严格的 JSON，不要任何解释文字、不要 Markdown 代码块】
{
  "findings": [
    {
      "category": "上面 1-10 的编号与名称",
      "level": "block | warn | info",
      "quote": "原文中的**逐字**片段，必须能在原文里搜到，不要改写",
      "why": "为什么它可能成为把柄（一句话）",
      "fix": "可以直接落笔的改法（一句话，给出具体替换后的文字）",
      "confidence": "high | medium | low"
    }
  ],
  "summary": "整体判断，一句话"
}

【硬性要求】
- `quote` 必须是原文的连续片段，不得拼接、不得改写。我会用程序在原文里检索它，
  检索不到的那一条会被丢弃。
- 如果确实没有任何问题，返回 {"findings": [], "summary": "未发现问题"}。
- 不要输出「整体写得很好」之类的评价。我只要素材里的事实。

【待检查内容】
标题：{title}

正文：
{body}
"""


def build_ai_prompt(title: str, body_html: str,
                    max_chars: int = 60000) -> str:
    """生成给「你自己的 AI」的提示词。长文会被截断并注明。"""
    body = to_text(body_html)
    if len(body) > max_chars:
        body = body[:max_chars] + f"\n\n[正文过长，已截断，以上为前 {max_chars} 字]"
    return AI_PROMPT.replace("{title}", (title or "").strip() or "(无标题)") \
                    .replace("{body}", body)


_JSON_BLOCK_RE = re.compile(r"```(?:json)?\s*([\s\S]*?)```", re.I)


def parse_ai_reply(reply: str, title: str = "",
                   body_html: str = "") -> Dict[str, Any]:
    """解析 AI 回话。宽松：允许被代码块包裹、允许前后有废话。

    关键一步：拿 `quote` 回原文里**逐字检索**。检索不到的直接丢弃并计数 ——
    因为 AI 会编造引文，不校验就等于把幻觉写进审计记录。
    """
    raw = (reply or "").strip()
    if not raw:
        return {"ok": False, "note": "AI 回话为空", "findings": [],
                "dropped": 0}
    cand = raw
    m = _JSON_BLOCK_RE.search(raw)
    if m:
        cand = m.group(1)
    else:
        i, j = raw.find("{"), raw.rfind("}")
        if i >= 0 and j > i:
            cand = raw[i:j + 1]
    try:
        data = json.loads(cand)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "note": f"AI 回话不是合法 JSON：{exc}",
                "findings": [], "dropped": 0, "raw": raw[:1000]}

    items = data.get("findings") if isinstance(data, dict) else data
    if not isinstance(items, list):
        return {"ok": False, "note": "AI 回话里没有 findings 数组",
                "findings": [], "dropped": 0, "raw": raw[:1000]}

    body = to_text(body_html)
    haystacks = [("title", title or ""), ("body", body)]
    out: List[Finding] = []
    dropped = 0
    for it in items:
        if not isinstance(it, dict):
            dropped += 1
            continue
        quote = str(it.get("quote") or "").strip()
        level = str(it.get("level") or "warn").lower().strip()
        if level not in ("block", "warn", "info"):
            level = "warn"
        cat = str(it.get("category") or "AI").strip()
        cat_code = cat[:1].upper() if cat[:1].isalpha() else "A"
        hit_field, pos = "", -1
        for fname, text in haystacks:
            p = text.find(quote) if quote else -1
            if p >= 0:
                hit_field, pos = fname, p
                break
        if pos < 0:
            dropped += 1          # 引文对不上 → 丢弃（AI 幻觉）
            continue
        src = dict(haystacks)[hit_field]
        out.append(Finding(
            rule_id=f"AI-{len(out) + 1:02d}", cat=cat_code,
            cat_label=cat or CAT_LABEL.get(cat_code, "AI 判定"),
            level=level, label=str(it.get("why") or "AI 判定")[:200],
            why=str(it.get("why") or ""), fix=str(it.get("fix") or ""),
            field=hit_field, text=quote, start=pos, end=pos + len(quote),
            excerpt=_excerpt(src, pos, pos + len(quote)), source="ai"))
    out.sort(key=lambda f: (-_LEVEL_RANK.get(f.level, 0),
                            f.field != "title", f.start))
    return {"ok": True, "findings": [f.to_dict() for f in out],
            "dropped": dropped,
            "summary": (data.get("summary") if isinstance(data, dict) else "") or "",
            "note": (f"AI 报了 {len(items)} 条，"
                     f"其中 {dropped} 条引文在原文中检索不到，已丢弃"
                     if dropped else f"AI 报了 {len(items)} 条，全部核对通过")}


# --------------------------------------------------------------------------- #
# 交叉检查
# --------------------------------------------------------------------------- #

def cross_check(rule_findings: List[Dict[str, Any]],
                ai_findings: List[Dict[str, Any]],
                overlap: int = 12) -> Dict[str, Any]:
    """规则引擎 vs AI 的交叉比对 —— 「保证没有漏网之鱼」。

    判定两条命中「同一处」的条件：字段相同 + 位置相差 <= overlap 字符。
    因为两者的切词边界不可能完全一致，不能用位置相等来判重。
    """
    def _match(a: Dict[str, Any], pool: Iterable[Dict[str, Any]]) -> bool:
        for b in pool:
            if a.get("field") != b.get("field"):
                continue
            if abs(int(a.get("start") or 0) - int(b.get("start") or 0)) <= overlap:
                return True
        return False

    only_rules = [f for f in rule_findings if not _match(f, ai_findings)]
    only_ai = [f for f in ai_findings if not _match(f, rule_findings)]
    both = [f for f in rule_findings if _match(f, ai_findings)]

    lv = "pass"
    for f in list(rule_findings) + list(ai_findings):
        if _LEVEL_RANK.get(f.get("level", ""), 0) > _LEVEL_RANK.get(lv, 0):
            lv = f["level"]
    return {
        "level": lv,
        "both": len(both),
        "only_rules": only_rules,
        "only_ai": only_ai,
        "rule_hits": len(rule_findings),
        "ai_hits": len(ai_findings),
        "note": (
            f"双方一致 {len(both)} 处；仅规则发现 {len(only_rules)} 处"
            f"（多为措辞类，AI 容易忽略）；仅 AI 发现 {len(only_ai)} 处"
            f"（多为语义类，规则抓不到）。**只有 AI 发现的必须人工过一遍**。"
        ),
    }


# --------------------------------------------------------------------------- #
# 自检
# --------------------------------------------------------------------------- #

_SAMPLE = """<p>我们每天只能吃黄豆酱配米饭，但是我很开心。</p>
<p>这几个月我一直在做修行，也上了慧心课，感觉像是被疗愈了。</p>
<p>脱水减重是为了称重前过磅。</p>
<p>我自己选择吃米糊，觉得挺好的。</p>
<p>我自己选择只能吃简单饮食，这是我的个人选择，没人逼我。</p>
<p>《与神对话》这本书里，神告诉我应该怎么做。</p>
<p>后来我进了黑关，闭关七天。</p>
<p>联系方式：微信 abc12345，手机 13800001111。</p>
<p>我们承诺保过，100% 提分。</p>"""

# 段落作用域回归用例：(说明, 原文, 期望命中的规则号)
_PARA_CASES = [
    ("强制饮食 + 别段的自我豁免 → 必须报",
     "我们每天只能吃黄豆酱配米饭。\n我自己选择吃米糊。", {"D01", "D02"}),
    ("自我豁免与命中同段 → 不得报",
     "我自己选择只能吃简单饮食，这是我的个人选择。", set()),
    ("无豁免的同段强制饮食 → 必须报",
     "我们每天只能吃馒头。", {"D01"}),
    # 这条是本轮修 exclude 的直接原因：豁免在前一小句、被「但」推翻的强制在后一小句。
    # 注意必须同时带上强制施动者（「老师规定」），否则漏检的原因是 require 而非 exclude。
    ("豁免在前、被「但」推翻 → 必须报",
     "虽然我自己选择吃米糊，但后来老师规定只能吃米糊。", {"D01"}),
    ("豁免与命中同小句 → 不得报",
     "我自己选择只能吃简单饮食。", set()),
]

# v3.1 词表覆盖回归：(说明, 原文, 必须命中的规则号)
# 这些词来自 v3.1「李想合规专版」硬编码规则表 + 用户口述的原则。
# 加这一组是因为实测发现 15 个词本地引擎漏检 —— 没有回归用例，
# 下一次改规则还会悄悄漏掉。
_COV_CASES = [
    ("拼音缩写避审 ysdh", "我看的是 ysdh 这本书。", {"G04"}),
    ("「与神交流」变体", "我在与神交流的过程中想明白了一些事。", {"G01"}),
    ("「小灵魂与太阳」故事", "有个故事叫小灵魂与太阳。", {"S08"}),
    ("故事特征词", "书上说你是光，只是把光遮住了。", {"S08b"}),
    ("「得道 / 开悟」", "那段时间我好像开悟了。", {"S02b"}),
    ("「辟谷」", "我试过辟谷三天。", {"R08"}),
    ("「出关」", "结束闭关，出关那天阳光很好。", {"R02b"}),
    ("「跨国非法集资」", "那家公司做的是跨国非法集资。", {"F09"}),
    ("「分裂政党」", "他加入了境外分裂政党。", {"P01"}),
    ("「密宗 / 法门」", "他传了我一个法门。", {"C12"}),
    ("「忍饥挨饿」", "那阵子常常忍饥挨饿。", {"D03b"}),
    ("「宇宙法则」", "他总说宇宙法则会安排一切。", {"S07"}),
    ("脱水减重必须说明", "赛前我脱水减重了五公斤。", {"W01"}),
]

# 真实误报回归用例：(说明, 原文, 绝不允许命中的规则号)
# 这些都是从**真实账号**或真实语料里实测抓出来的假警报。
_FP_CASES = [
    ("批评单向沟通，不是要求服从",
     "自己一直输出，对方只能听，没有真正的交流。", {"C09"}),
    ("比喻义「颠覆」，与政治无关",
     "进入今日之后，它完全颠覆了我对【学校】这两个字的概念。", {"P03"}),
    ("讨论格斗作为观赏运动，不是体罚",
     "你要去打人的话，现在大概率机器狗或者AI机器人比你强很多。", {"D04"}),
    ("长词内含：「精神让我」不是「神让我」",
     "那段时间精神让我很难受，几乎撑不下去。", {"G02"}),
    ("正常地区表述：中国香港 / 台湾地区",
     "我们去了中国香港，也去了台湾地区。", {"P06"}),
    ("「不能吃」指过敏，不是剥夺饮食",
     "我对花生过敏，所以不能吃花生。", {"D01", "D06"}),
    ("「助教主」是助教+主要，不是教主",
     "最后总结助教主要解决了我们的两大问题。", {"C02"}),
    ("「神通广大」是成语，不是超自然宣称",
     "他真是神通广大，什么都懂。", {"C04"}),
    ("「只能吃素」是个人选择，不是被强制",
     "我是素食者，只能吃素。", {"D01"}),
    ("游戏代币不是加密募资",
     "这个游戏里的代币可以换皮肤。", {"F04"}),
    ("「出关」指货物通关，不是结束闭关",
     "这批货下周三出关，走的是海关报关流程。", {"R02b"}),
    ("「法门」是比喻义（找到方法）",
     "他找到了自己的法门。", set()),
    ("批判非法集资的普法语境，不裸词拦截",
     "要警惕非法集资，不要相信保本高息。", {"F09"}),
    ("普通鼓励语「你是光」不判故事",
     "加油，你是光，别怕。", {"S08"}),
]


def _selftest() -> int:
    bad = 0

    print("== 段落作用域回归 ==")
    for note, text, want in _PARA_CASES:
        got = {f.rule_id for f in scan("", "<p>" + text.replace("\n", "</p><p>") + "</p>")}
        got_d = got & {"D01", "D02"}
        ok = (want <= got_d) if want else (not got_d)
        print(f"  [{'OK ' if ok else 'FAIL'}] {note}")
        print(f"        期望 {sorted(want) or '无'} / 实得 {sorted(got_d) or '无'}")
        if not ok:
            bad += 1

    print()
    print("== 覆盖回归（v3.1 词表 + 用户原则） ==")
    for note, text, must in _COV_CASES:
        got = {f.rule_id for f in scan("", "<p>" + text + "</p>")}
        miss = must - got
        ok = not miss
        print(f"  [{'OK ' if ok else 'FAIL'}] {note}")
        print(f"        必须命中 {sorted(must)} / 漏检 {sorted(miss) or '无'}")
        if not ok:
            bad += 1

    print()
    print("== 误报回归（不得裸词命中） ==")
    for note, text, forbid in _FP_CASES:
        got = {f.rule_id for f in scan("", "<p>" + text + "</p>")}
        hit = got & forbid
        ok = not hit
        print(f"  [{'OK ' if ok else 'FAIL'}] {note}")
        print(f"        不该命中 {sorted(forbid) or '无'} / 实得误报 {sorted(hit) or '无'}")
        if not ok:
            bad += 1

    total = len(_PARA_CASES) + len(_COV_CASES) + len(_FP_CASES)
    print()
    print(f"回归总计：{total - bad}/{total} 通过")
    print()

    f = scan("我的成长经历", _SAMPLE)
    rep = report(f)
    print(f"命中 {rep['hits']} 条 → 定级 {rep['level']}"
          f"（block {rep.get('block')} / warn {rep.get('warn')} / info {rep.get('info')}）")
    print("-" * 66)
    for x in rep["findings"]:
        print(f"[{x['level']:5}] {x['rule_id']:4} {x['field']:5} {x['label']}")
        print(f"        原文：{x['text']!r}")
        print(f"        改法：{x['fix']}")
    print("-" * 66)
    print("分类统计：", json.dumps(rep["by_cat"], ensure_ascii=False))
    print()
    print("交叉检查演示（模拟 AI 只报了其中两条）：")
    ai = [{"field": "body", "start": 0, "level": "block"},
          {"field": "body", "start": 200, "level": "warn"}]
    cc = cross_check(rep["findings"], ai)
    print(" ", cc["note"])
    print()
    print("提示词长度：", len(build_ai_prompt("我的成长经历", _SAMPLE)), "字符")
    print("AI 回话解析（含一条编造引文，应被丢弃）：")
    fake = json.dumps({"findings": [
        {"category": "3 修行/宗教/闭关", "level": "block",
         "quote": "后来我进了黑关", "why": "专名", "fix": "删除", "confidence": "high"},
        {"category": "1 邪教", "level": "block",
         "quote": "这句话原文里根本没有", "why": "编的", "fix": "无", "confidence": "low"},
    ], "summary": "测试"}, ensure_ascii=False)
    print(" ", json.dumps(parse_ai_reply(fake, "我的成长经历", _SAMPLE),
                          ensure_ascii=False)[:300])
    return 0


if __name__ == "__main__":
    raise SystemExit(_selftest())
