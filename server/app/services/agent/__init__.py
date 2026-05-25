"""Agent 层 —— 业务编排，不直接接 HTTP。

- decompose_agent.py  样例拆解：scene_detect → audio → asr → vlm → llm sections
- gap_agent.py        缺口识别与补全：slot 匹配 + rerank/copy/aigc 动作分发
"""
