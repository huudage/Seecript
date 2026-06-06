"""stage-23 公共 helper：migration_preference → 注入下游 agent 的 prompt 片段。

plan_agent / copy_outline_agent / aigc_prompt_agent 三个 agent 都拿这一份文案，
保证用户切换"情绪增强 / 节奏紧凑 / 平淡复刻"三档时，全链路调性同步。
"""
from __future__ import annotations

from typing import Optional

from ...schemas import MigrationPreference, SampleAnalysis


_PREFERENCE_HINT: dict[MigrationPreference, str] = {
    "mirror": (
        "【迁移倾向：平淡复刻】保持原片结构与调性，仅替换素材主题；"
        "不要主动加强情绪 / 不要加快节奏 / 不要改动 CTA 力度。"
    ),
    "amp_emotion": (
        "【迁移倾向：情绪增强】钩子更猛、收尾更有共鸣、CTA 更燃；"
        "在原片基础上把情绪曲线整体抬高 20-30%。文案多用动词、感叹与对比。"
    ),
    "amp_pace": (
        "【迁移倾向：节奏紧凑】每段比原片缩短 10-25%；去掉缓冲与过渡，让信息更密集。"
        "总时长按 settings.target_duration_seconds 严格执行；口播追求短句、爽利。"
    ),
}


def preference_hint(pref: Optional[MigrationPreference]) -> str:
    """返回该 preference 对应的 prompt 片段；None / 未知值默认 amp_emotion。"""
    if not pref:
        pref = "amp_emotion"
    return _PREFERENCE_HINT.get(pref, _PREFERENCE_HINT["amp_emotion"])


def analysis_hint(analysis: Optional[SampleAnalysis]) -> str:
    """把 SampleAnalysis 的亮点 + 改进项渲染成 prompt 片段。无 analysis 时返空字符串。"""
    if not analysis:
        return ""
    parts: list[str] = []
    if analysis.highlights:
        parts.append("【原片亮点（迁移时必须保留这些表达）】")
        for h in analysis.highlights:
            parts.append(f"- [{h.aspect}] {h.text}")
    if analysis.improvements:
        parts.append("【原片不足（迁移时主动规避 / 改进）】")
        for im in analysis.improvements:
            parts.append(f"- [{im.aspect}] {im.text} → 改进：{im.suggestion}")
    if analysis.one_line_verdict:
        parts.append(f"【原片总评】{analysis.one_line_verdict}")
    return "\n".join(parts)
