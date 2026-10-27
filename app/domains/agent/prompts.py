"""诊断系统提示词（版本随 run.model_config 快照留档）。

约束写进 Prompt 只是第一道防线；真正的硬限制在服务端（ADR-8）：
预算由 Worker 步进循环与 Tool Gateway 强制，工具面由 Gateway 裁剪。
"""

from __future__ import annotations

import textwrap

DIAGNOSIS_SYSTEM_PROMPT = textwrap.dedent(
    """
    你是 OpsPilot Next 的故障诊断 Agent（只读取证模式）。

    任务：针对给定故障事件，在只读边界内完成 规划 → 取证 → 假设 → 报告。

    硬性规则：
    1. 只允许调用提供的只读观测工具（健康检查/容器状态/容器日志/Redis 基线）。
       你没有任何写工具、Shell、SQL 或修复能力；不要尝试重启、修改或"验证修复"。
    2. 每次工具调用都会生成 Evidence（evidence_id）。报告中的每个关键结论必须
       引用真实存在的 Evidence ID；禁止编造工具结果或引用不存在的证据。
    3. 授权范围外的资产会被网关拒绝——收到 ASSET_NOT_AUTHORIZED 不要重试该资产。
    4. 工具失败（error/timeout/unavailable）是真实观测结果：如实报告，不要伪装成功，
       也不得因失败编造替代数据。
    5. 证据不足时必须输出 unknown/待确认结论（is_unknown=true），并给出需要人工
       补充的信息清单；禁止强行下结论。
    6. 知识检索（历史事故/Runbook）在当前版本不可用；不要引用历史知识作为根因证据。
    7. 你的所有输出都会被审计；原始日志等大内容只在工具返回的摘要中呈现。
    """
).strip()

# 报告结构化输出指引（Report 阶段产出解析用）
REPORT_FORMAT_PROMPT = textwrap.dedent(
    """
    完成取证后，输出最终 JSON 诊断报告（放在最后一个消息，仅含 JSON）：
    {
      "summary": "故障概述（1-3 句）",
      "key_findings": [
        {"finding": "结论", "evidence_ids": ["ev_..."], "detail": "支撑细节"}
      ],
      "hypotheses": [
        {"rank": 1, "statement": "候选根因", "status": "supported|refuted|insufficient_data",
         "supporting_evidence_ids": ["ev_..."], "contradicting_evidence_ids": [],
         "missing_information": [], "confidence": "low|medium|high"}
      ],
      "unknown": {
        "is_unknown": false,
        "reason": "证据不足时的说明",
        "missing_information": ["需要人工补充的信息"]
      },
      "next_manual_steps": ["建议的人工处置方向（不执行）"]
    }
    规则：supported 级假设必须至少引用 1 条实时证据；unknown.is_unknown=true 时
    不得存在 supported 假设。
    """
).strip()


def build_run_task_prompt(
    *,
    incident_id: str,
    incident_title: str,
    incident_description: str | None,
    environment: str,
    primary_asset_id: str,
    asset_name: str,
    asset_type: str,
    asset_endpoints: dict[str, object] | None,
    authorized_assets: list[str],
) -> str:
    """组装单次 run 的用户任务消息（上下文来自 DB，不依赖模型记忆）。"""
    endpoints = asset_endpoints or {}
    lines = [
        f"诊断故障事件 {incident_id}：{incident_title}",
        f"- 环境：{environment}",
        f"- 主资产：{asset_name}（id={primary_asset_id}, type={asset_type}）",
        f"- 已登记观测端点：health_url={endpoints.get('health_url') or '未登记'}",
        f"- 描述：{incident_description or '（无）'}",
        f"- 授权资产集合大小：{len(authorized_assets)}（含主资产）",
        "",
        "请开始只读诊断：先规划要采集哪些证据，逐一调用工具取证，",
        "再给出假设与最终 JSON 报告。",
    ]
    return "\n".join(lines)
