"""Clarify Agent —— 视频工坊 step 1 意图澄清的多轮追问引擎（v2 · 五件套结构化）。

为什么这次重写：v1 用 `===DRAFT===` / `===QUESTION===` 文本标记切流，draft 是自由
文本，用户没法局部改、finalize 还得再问一次 LLM。v2 改成 JSON 五件套：
`topic / content / audience / goal / tone`，每轮把每个字段单独 emit，前端可以
独立编辑、用户点 OK 时由前端把五件套拼成 brief，后端 finalize 不再 LLM。

工作方式：无状态多轮——前端把 INITIAL_BRIEF + 历史 Q/A transcript 一起送进来，
本 agent 让 LLM 输出一段 JSON：
```json
{
  "outline": {
    "topic": "...", "content": "...", "audience": "...",
    "goal": "...", "tone": "..."
  },
  "question": "本轮唯一追问，已经够清楚就给 null",
  "thinking": "（可选）思考流，前端展示给用户看推理过程"
}
```
路由层根据 round_no/3 + force_finalize 决定 is_final，最终轮强制把 question 置空。

兼容性：保留 `===DRAFT===` 字面值在系统提示里——MockLLMClient 路由用「短视频脚本意图
澄清助手」做指纹，并按这串字符识别 mock 分支；这次改 prompt 必须保留指纹。
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Optional

from pydantic import BaseModel

from ...schemas import ClarifyOutline
from ..llm_client import LLMError, get_llm_client

log = logging.getLogger("seecript.agent.clarify")


# System prompt 同时是 MockLLMClient 路由指纹：必须含「短视频脚本意图澄清助手」。
# 换 prompt 时务必保留这串中文，否则 mock 分支识别不到，本地 dev 全链路崩。
_CLARIFY_SYSTEM = (
    "你是短视频脚本意图澄清助手。任务是把用户给的 INITIAL_BRIEF + 历史 TRANSCRIPT，"
    "整合成一份五件套结构化 brief：主题 / 内容卖点 / 受众 / 目的 / 语气。\n\n"
    "**核心规则：每一轮都必须把 outline 五个字段全部填满**——\n"
    "- 用户没明说的字段，**根据 INITIAL_BRIEF 做最合理的推测**填入，并在 question 里说明你假设了什么；\n"
    "- 仅当字段在已有信息里完全无依据、且推测会误导时才允许 null；topic 必须永远非空。\n"
    "- 用户在后续轮次给出补充/纠正后，要把对应字段更新为新值。\n"
    "- 用户意图（INITIAL_BRIEF + TRANSCRIPT）权重最大；DETECTED_SUBJECTS 只是辅助参考，"
    "  与意图冲突的 detected 一律丢弃，不要硬塞进 outline。\n\n"
    "DETECTED_SUBJECTS 处理规则（重要）：\n"
    "- DETECTED_SUBJECTS 是用户已上传素材里 VLM 自动识别到的物体/场景，**可能掺杂大量与脚本无关的陪衬**——"
    "  典型脏数据：模特佩戴的耳钉/项链/手表、美甲、发型、妆容、衣着、构图词（近景特写/中景）、"
    "  光线词（暖调光线/冷色调）、品类元词（美食展示/产品展示/美食种草）、活动名（探店/试吃）等。\n"
    "- 你必须在输出里给 `relevant_detected_subjects` 字段——是 DETECTED_SUBJECTS 的**子集**——"
    "  **只保留**与本次脚本主题/产品/核心场景紧密相关的对象：\n"
    "    ✅ 保留：产品本体（干脆面饼/红色包装袋）、产品使用场景（居家室内/试吃工位）、人物的核心扮演角色（试吃女生）；\n"
    "    ❌ 丢弃：与脚本无关的随身饰品/妆容/穿搭（耳钉/项链/美甲/卷发/发型）、构图/光线/营销 meta 词、"
    "       与产品无关的环境陪衬（背景的盆栽除非 brief 强调植物）。\n"
    "- 仅 `relevant_detected_subjects` 中的对象会被强制写进 outline.content（系统兜底机制）；"
    "  未入选的不会出现在最终脚本里。\n"
    "- 若 DETECTED_SUBJECTS 全是陪衬/无关物，relevant_detected_subjects 给空数组 []；"
    "  宁可空，也不要把无关物塞进 content 害下游 plan 给它单独排镜头。\n\n"
    "outline.content 撰写规则：\n"
    "- 必须把 relevant_detected_subjects 里的对象都点名出现（用顿号串联），缺的就拼上；\n"
    "- 不要点名 dropped 的陪衬物——这是用户绝对不想强调的。\n\n"
    "question 的语义（v3 调整）：\n"
    "- 不再是「让用户作答」的硬追问，而是「让用户检查」的提示——告诉用户你做了哪些假设，"
    "或哪个字段你还没把握，引导用户决定要不要补充。\n"
    "- 若五件套已经非常贴合用户表达、无需任何假设，question 给 null。\n"
    "- IS_FINAL=true 时 question 必须是 null。\n\n"
    "输出严格 JSON 对象，不要 Markdown 围栏，不要任何额外文字：\n"
    "{\n"
    '  "thinking": "（可选）30 字内的思考流，告诉用户你怎么综合的",\n'
    '  "outline": {\n'
    '    "topic":    "<必填，一句话主题，最多 50 字>",\n'
    '    "content":  "<核心卖点/亮点；多条用顿号或换行；最多 200 字>",\n'
    '    "audience": "<目标受众画像；最多 80 字>",\n'
    '    "goal":     "<目的：卖货/种草/教程/娱乐/品牌 等>",\n'
    '    "tone":     "<语气风格：温柔/高能/沙雕/严肃 等>"\n'
    "  },\n"
    '  "brief_subjects": ["<从 INITIAL_BRIEF + outline.content 抽出的具象名词，≤6 个>"],\n'
    '  "relevant_detected_subjects": ["<DETECTED_SUBJECTS 的子集；只保留与脚本主题强相关的对象；可为 []>"],\n'
    '  "question": "<向用户求证你做的假设；够清楚或最终轮请给 null；≤40 字>"\n'
    "}\n\n"
    "brief_subjects 抽取规则（核心）：\n"
    "- ❌ 不要抽 brief 字面词（尤其是上位词、品类词、活动名）。\n"
    "- ✅ 要**反推**用户最可能拿镜头拍到的**具体实物**——可摆桌上、可拿手里、镜头能拍出形状的物件。\n"
    "- 通过类别去想代表性单品：\n"
    "  例 1：「家清好物 / 居家日用」→ [\"纸巾\", \"抹布\", \"清洁喷雾\", \"拖把\"]，不要给 [\"家清\", \"好物\", \"日用品\"]。\n"
    "  例 2：「国家文物展 / 博物馆」→ [\"青铜鼎\", \"玉器\", \"陶俑\", \"古画\", \"瓷器\"]，不要给 [\"文物\", \"文物展\", \"展览\"]。\n"
    "  例 3：「健身教程」→ [\"哑铃\", \"瑜伽垫\", \"弹力带\"]，不要给 [\"健身\", \"教程\", \"运动\"]。\n"
    "  例 4：「咖啡探店」→ [\"拿铁\", \"咖啡豆\", \"手冲壶\", \"吧台\"]，不要给 [\"咖啡\", \"探店\", \"饮品\"]。\n"
    "- 严禁抽：感受/情绪/形容词/动作/上位词/品类名/活动名/营销词（「氛围」「干净」「使用」「日用品」「好物」「品质」「文物」「展览」「探店」均不要）。\n"
    "- 优先 2–6 个字的实词；超过 12 字的短语丢弃。\n"
    "- 与 relevant_detected_subjects 去重——已在那边出现的不要重复。\n"
    "- 若 brief 太抽象、反推不到具体物体，给空数组 []，不要硬凑、不要回退到上位词。\n\n"
    "重要约束：\n"
    "- 输出**纯 JSON**，禁止三重反引号或任何前后缀。\n"
    "- 历史轮 TRANSCRIPT 已经包含用户补充信息，不要重复假设、不要忽略用户已澄清的字段。\n"
    "- 即使是历史 marker `===DRAFT===` 也别出现在你的输出里——纯 JSON 即可。"
)


class ClarifyTurn(BaseModel):
    """一轮 Q/A 历史。前端把 transcript 完整回传，本 agent 无状态。"""

    question: str
    answer: str


@dataclass
class ThinkingDelta:
    """『思考流』流式片段——LLM 在出 JSON 前的中间叙述（mock 模式没有）。"""

    text: str


@dataclass
class OutlineReady:
    """LLM 完整输出已解析成五件套结构。

    brief_subjects 是 LLM 从 INITIAL_BRIEF + outline.content 自抽的具象名词（≤6 个），
    与 detected_subjects（VLM 素材路径）平行，前端分两组显示让用户检查。

    relevant_detected_subjects 是 LLM 用「意图最大权重」从 DETECTED_SUBJECTS 里挑出的
    与脚本主题强相关的子集；dropped_detected_subjects 是被丢弃的陪衬物（耳钉/美甲/构图词等）——
    前端可以分两组渲染让用户检查/翻案，下游 _enforce_subjects_in_content 只强制 relevant 部分。
    """

    outline: ClarifyOutline
    thinking: str
    brief_subjects: list[str]
    relevant_detected_subjects: list[str] = field(default_factory=list)
    dropped_detected_subjects: list[str] = field(default_factory=list)


@dataclass
class RoundDone:
    """整轮结束。is_final=True 时 question 永远 None。"""

    outline: ClarifyOutline
    question: Optional[str]
    is_final: bool


ClarifyEvent = ThinkingDelta | OutlineReady | RoundDone


def _build_user_payload(
    *,
    initial_brief: str,
    transcript: list[ClarifyTurn],
    round_no: int,
    is_final: bool,
    detected_subjects: list[str] | None = None,
) -> str:
    lines: list[str] = []
    lines.append(f"INITIAL_BRIEF:\n{initial_brief.strip() or '(empty)'}\n")
    if detected_subjects:
        # 用户已上传的素材里 VLM 已识别到的对象/主体清单。LLM 必须在 outline.content
        # 里点名出现这些对象（用顿号串联），保证「带货纸巾」上传纸巾照片就一定能在
        # 内容卖点里看到「纸巾」二字。
        subjects_str = "、".join(s.strip() for s in detected_subjects if s.strip())[:300]
        if subjects_str:
            lines.append("DETECTED_SUBJECTS（用户已上传素材里 VLM 识别出的物体/场景；")
            lines.append("  必须在 outline.content 里点名出现，缺的就拼上）:")
            lines.append(subjects_str)
            lines.append("")
    if transcript:
        lines.append("TRANSCRIPT:")
        for i, t in enumerate(transcript, 1):
            lines.append(f"Q{i}: {t.question.strip()}")
            lines.append(f"A{i}: {t.answer.strip()}")
        lines.append("")
    else:
        lines.append("TRANSCRIPT: (empty — this is round 1)")
        lines.append("")
    lines.append(f"ROUND: {round_no}/3")
    lines.append(f"IS_FINAL: {'true' if is_final else 'false'}")
    return "\n".join(lines)


_JSON_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.MULTILINE)


def _extract_json_object(text: str) -> Optional[dict[str, Any]]:
    """从 LLM 输出里抠出第一个 `{...}` JSON 对象。

    LLM 偶尔会带 Markdown 围栏或前后说明文字。先剥围栏，再用括号配对从第一个 `{`
    扫到对应的 `}`——比直接 json.loads 整段文本鲁棒。
    """
    if not text:
        return None
    cleaned = _JSON_FENCE_RE.sub("", text).strip()
    # 找第一个 `{`
    start = cleaned.find("{")
    if start < 0:
        return None
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(cleaned)):
        ch = cleaned[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                snippet = cleaned[start: i + 1]
                try:
                    return json.loads(snippet)
                except json.JSONDecodeError:
                    return None
    return None


def _coerce_outline(raw: Any) -> ClarifyOutline:
    """把 LLM 出的 outline dict 强转成 ClarifyOutline；非 dict / 字段缺失都填 None。"""
    if not isinstance(raw, dict):
        return ClarifyOutline()

    def _str_or_none(v: Any, max_len: int) -> Optional[str]:
        if v is None:
            return None
        if isinstance(v, (list, tuple)):
            v = "、".join(str(x) for x in v if x)
        s = str(v).strip()
        if not s or s.lower() in {"null", "none", "n/a", "不知道", "未知"}:
            return None
        return s[:max_len]

    return ClarifyOutline(
        topic=_str_or_none(raw.get("topic"), 200),
        content=_str_or_none(raw.get("content"), 400),
        audience=_str_or_none(raw.get("audience"), 200),
        goal=_str_or_none(raw.get("goal"), 200),
        tone=_str_or_none(raw.get("tone"), 200),
    )


def _coerce_question(v: Any) -> Optional[str]:
    if v is None:
        return None
    s = str(v).strip()
    if not s or s.lower() in {"null", "none", "n/a"}:
        return None
    # 单行；去掉可能的 markdown 前缀和句末空白
    first_line = next((ln.strip() for ln in s.splitlines() if ln.strip()), None)
    if not first_line:
        return None
    return first_line[:200]


# LLM 经常会把这些抽象词当 subject 输出——前端拿来当具象锚点会误导（Seedream 没法
# 画「氛围」「品质」也没法画「文物」「展览」这种上位词）。后端做最后一道过滤。
# 分三类：抽象感受 / 品类活动上位词 / 角色泛指。
_SUBJECT_BLACKLIST: frozenset[str] = frozenset({
    # 抽象感受 / 形容
    "氛围", "品质", "感受", "感觉", "体验", "效果", "细节", "情绪",
    "状态", "干净", "整洁", "舒适", "简洁", "好看", "美观", "质感",
    # 画面构图泛指
    "场景", "画面", "镜头", "风格", "构图", "色调",
    # 内容/主题 meta
    "故事", "内容", "主题", "想法", "概念", "理念", "灵感", "创意",
    # 角色 / 受众泛指
    "用户", "目标", "受众", "人群", "客户", "观众", "粉丝",
    # 动作 / 用法
    "使用", "用法", "用途", "操作", "演示",
    # 营销品类上位词（不可拍）
    "好物", "好处", "亮点", "卖点", "特点", "优势", "价值", "意义",
    "生活", "日常", "日用品", "好物", "产品", "商品", "物品", "用品",
    "礼物", "礼品", "周边",
    # 活动名 / 展览 / 节目类
    "文物", "文物展", "展览", "展品", "展示", "活动", "节目", "演出",
    "探店", "测评", "教程", "教学", "课程", "讲座", "分享",
    # 行业大类
    "健身", "运动", "美妆", "护肤", "时尚", "穿搭", "数码", "家居",
    "餐饮", "美食", "饮品", "旅行", "出行",
})


def _coerce_brief_subjects(raw: Any, detected: list[str] | None) -> list[str]:
    """规整 LLM 自抽的 brief_subjects：去空 / 去黑名单 / 去重（含与 detected_subjects 互斥）/ 限 6。

    长度卡 2–12 字，过短/过长一律丢——保护前端 chip 排版。
    """
    if not raw:
        return []
    if isinstance(raw, str):
        # 容错：LLM 偶尔会输出顿号串
        items = [x.strip() for x in re.split(r"[、,，\n;；]", raw)]
    elif isinstance(raw, (list, tuple)):
        items = [str(x).strip() for x in raw]
    else:
        return []
    detected_set = {s.strip() for s in (detected or []) if s and s.strip()}
    seen: set[str] = set()
    out: list[str] = []
    for s in items:
        if not s:
            continue
        if len(s) < 2 or len(s) > 12:
            continue
        if s in _SUBJECT_BLACKLIST:
            continue
        if s in detected_set:
            continue
        if s in seen:
            continue
        seen.add(s)
        out.append(s)
        if len(out) >= 6:
            break
    return out


# 素材识别清洗：构图/光线/营销 meta 词从来不该当 subject，无论 LLM 是否漏过滤都直接 ban。
# 与 _SUBJECT_BLACKLIST 互补——那个偏抽象上位词，这个偏视觉/营销 meta。
_DETECTED_META_BLACKLIST: frozenset[str] = frozenset({
    # 构图
    "近景特写", "中景特写", "远景特写", "近景", "中景", "远景", "特写", "全景",
    "俯拍", "仰拍", "平视", "侧拍", "正拍",
    # 光线
    "暖调光线", "冷色调光线", "暖调柔光", "冷色光", "顺光", "逆光", "自然光", "侧光",
    "暖色调", "冷色调", "高对比", "低对比",
    # 营销 meta
    "美食展示", "产品展示", "美食种草", "好物种草", "高颜值", "高级感",
    # 活动名
    "生活化试吃", "试吃", "探店", "测评", "教程", "教学",
    # 类别
    "美食", "产品", "饮品", "穿搭", "护肤", "数码",
})


def _coerce_relevant_detected_subjects(
    raw: Any, detected: list[str] | None
) -> tuple[list[str], list[str]]:
    """切分 detected_subjects 为 (relevant, dropped)。

    LLM 的 relevant_detected_subjects 必须是 detected_subjects 的子集——任何 LLM 编造出
    detected 里没有的项都丢弃（防止 LLM 从 brief 反向脑补污染素材标签）。

    清洗规则：
    - relevant 必须是 detected 子集（精确匹配；strip 后比较）
    - 自动剔除 _DETECTED_META_BLACKLIST 里的视觉/营销 meta 词（即便 LLM 失误判为相关）
    - 自动剔除明显的穿搭/饰品/妆容关键词——任何含「耳钉/项链/手表/美甲/发型」的项都丢弃
    - dropped = detected - relevant
    返回 (relevant_list, dropped_list)，dropped 用于前端展示。
    """
    detected_clean = [s.strip() for s in (detected or []) if s and s.strip()]
    if not detected_clean:
        return [], []
    detected_set = set(detected_clean)

    if isinstance(raw, str):
        candidates = [x.strip() for x in re.split(r"[、,，\n;；]", raw) if x.strip()]
    elif isinstance(raw, (list, tuple)):
        candidates = [str(x).strip() for x in raw if str(x).strip()]
    else:
        candidates = []

    # 强制黑名单：饰品 / 妆容 / 穿搭 / 视觉 meta —— 即便 LLM 留了也强行剔除
    _ACCESSORY_HINTS = ("耳钉", "耳环", "项链", "手镯", "手表", "戒指", "美甲", "发型",
                        "卷发", "直发", "短发", "长发", "假发", "口红", "眼妆", "妆容",
                        "穿搭", "服装", "T恤", "卫衣", "外套", "毛衣")

    def _is_blacklisted(item: str) -> bool:
        if item in _DETECTED_META_BLACKLIST:
            return True
        for hint in _ACCESSORY_HINTS:
            if hint in item:
                return True
        return False

    relevant: list[str] = []
    seen: set[str] = set()
    for c in candidates:
        if c not in detected_set:
            continue  # LLM 编造的不收
        if _is_blacklisted(c):
            continue
        if c in seen:
            continue
        seen.add(c)
        relevant.append(c)

    relevant_set = set(relevant)
    dropped = [s for s in detected_clean if s not in relevant_set]
    return relevant, dropped


async def run_clarify_round(
    *,
    initial_brief: str,
    transcript: list[ClarifyTurn],
    round_no: int,
    is_final: bool,
    detected_subjects: list[str] | None = None,
) -> AsyncIterator[ClarifyEvent]:
    """跑一轮意图澄清。

    yield 顺序：
    1. 任意条 ThinkingDelta（思考流；mock 里没有）
    2. OutlineReady(outline, thinking) —— 解析 LLM JSON 完成
    3. RoundDone(outline, question, is_final) —— 最后一条

    is_final=True 时 RoundDone.question 强制 None。

    detected_subjects 是用户已上传素材里 VLM 已识别出的物体/主体清单（如 ["纸巾", "客厅"]），
    本 agent 把它喂进 LLM prompt，要求 outline.content 里必须点名出现这些对象，
    保证素材上传与意图 outline 双向同步（#420）。
    """
    user_payload = _build_user_payload(
        initial_brief=initial_brief,
        transcript=transcript,
        round_no=round_no,
        is_final=is_final,
        detected_subjects=detected_subjects,
    )
    client = get_llm_client()

    # 完整 token 累积；JSON 完整性只能整段解析（与 v1 不同，v1 是文本 marker）
    buf: list[str] = []
    # 简易思考流：在第一个 `{` 之前的 token 实时透出，让用户感觉有响应
    json_started = False
    pre_json: list[str] = []

    try:
        async for delta in client.stream_complete(
            _CLARIFY_SYSTEM,
            user_payload,
            temperature=0.6,
            max_tokens=900,
        ):
            buf.append(delta)
            if not json_started:
                pre_json.append(delta)
                joined = "".join(pre_json)
                idx = joined.find("{")
                if idx >= 0:
                    head = joined[:idx]
                    if head.strip():
                        yield ThinkingDelta(text=head)
                    json_started = True
                    pre_json = []
                else:
                    # 没看到 `{` 之前的纯文本就是思考流
                    if delta:
                        yield ThinkingDelta(text=delta)
    except LLMError:
        log.exception("[clarify] LLM stream failed round=%d is_final=%s", round_no, is_final)
        raise

    full = "".join(buf)
    parsed = _extract_json_object(full)
    if parsed is None:
        log.warning("[clarify] failed to parse JSON, raw=%r", full[:500])
        # 最低兜底：把整段当 topic 塞进去，让用户能看见原文
        outline = ClarifyOutline(topic=full.strip()[:200] or None)
        thinking = ""
        question_raw: Any = None
        brief_subjects_raw: Any = None
        relevant_raw: Any = None
    else:
        outline = _coerce_outline(parsed.get("outline") or {})
        thinking = str(parsed.get("thinking") or "").strip()
        question_raw = parsed.get("question")
        brief_subjects_raw = parsed.get("brief_subjects")
        relevant_raw = parsed.get("relevant_detected_subjects")

    # 用户意图（INITIAL_BRIEF + TRANSCRIPT）权重最大：从 detected_subjects 里挑出
    # 真正与脚本主题相关的子集，丢掉陪衬物（耳钉/美甲/构图词等）。即便 LLM 漏过滤，
    # _coerce_relevant_detected_subjects 里的强制黑名单也会兜底。
    relevant_detected, dropped_detected = _coerce_relevant_detected_subjects(
        relevant_raw, detected_subjects
    )

    # detected_subjects 兜底：LLM 经常会忘把这些对象点名进 content。在 yield 前
    # 机械补回去——把缺的对象用顿号串拼到 content 末尾，括号注「（涉及 X、Y、Z）」。
    # 关键：只强制 relevant 子集进 content，dropped 的陪衬物绝不能进——否则下游
    # plan_agent.extract_subject_anchors 又会把它们拉成独立分镜（耳钉单镜头 bug）。
    if relevant_detected:
        outline = _enforce_subjects_in_content(outline, relevant_detected)

    brief_subjects = _coerce_brief_subjects(brief_subjects_raw, detected_subjects)
    yield OutlineReady(
        outline=outline,
        thinking=thinking,
        brief_subjects=brief_subjects,
        relevant_detected_subjects=relevant_detected,
        dropped_detected_subjects=dropped_detected,
    )

    question_out: Optional[str] = None if is_final else _coerce_question(question_raw)
    yield RoundDone(outline=outline, question=question_out, is_final=is_final)


def _enforce_subjects_in_content(
    outline: ClarifyOutline, subjects: list[str]
) -> ClarifyOutline:
    """LLM 忘把 detected_subjects 写进 outline.content 时，机械补回去。

    判定：subject 文本未出现在 content 里（精确子串匹配）。
    补法：把所有缺的 subject 顿号串拼，括号注「（涉及 ...）」追加到 content 末尾。
    总长度仍受 200 字限制；超过就截。
    """
    cleaned = [s.strip() for s in subjects if s and s.strip()]
    if not cleaned:
        return outline
    content = (outline.content or "").strip()
    missing = [s for s in cleaned if s not in content]
    if not missing:
        return outline
    suffix = "（涉及" + "、".join(missing) + "）"
    if content:
        new_content = content + suffix
    else:
        new_content = "核心卖点：" + "、".join(cleaned)
    new_content = new_content[:200]
    return outline.model_copy(update={"content": new_content})


def stitch_outline_to_brief(outline: ClarifyOutline) -> str:
    """把五件套拼成可直接灌进 BriefInput 的中文段。

    顺序固定：主题 → 内容 → 受众 → 目的 → 语气；缺的字段直接跳过，不留空头。
    用户点「采纳」时前端调用，后端 finalize 也复用——保证两边一致。
    """
    parts: list[tuple[str, Optional[str]]] = [
        ("主题", outline.topic),
        ("内容", outline.content),
        ("受众", outline.audience),
        ("目的", outline.goal),
        ("语气", outline.tone),
    ]
    chunks: list[str] = []
    for label, value in parts:
        if value and value.strip():
            chunks.append(f"【{label}】{value.strip()}")
    return "\n".join(chunks)
