"""确定性 Fake 诊断模型（provider=fake，用于 CI 与无凭据演示）。

诚实性设计（禁止编造）：
- 工具调用序列固定（健康 → Redis → 容器状态 → 容器日志），目标资产取自任务提示；
- 最终报告**由真实 ToolMessage 内容合成**：key_findings = 各工具返回的
  summary_redacted + evidence_id；假设 status 由证据 status 推导；
  无任何工具结果时输出 is_unknown=true。
- 该模型不联网、无凭据；真实模型验收见 tests integration 标记（无凭据时跳过）。
"""

from __future__ import annotations

import json
import re
from typing import Any

from langchain_core.callbacks import CallbackManagerForLLMRun
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult

# 依序探测（失败也是真实观测结果）；applicable_assets 限定资产类型（与 manifest 一致）
_TOOL_PLAN = (
    ("get_service_health", None),
    ("check_redis_health", ("cache",)),
    ("get_container_status", None),
    ("read_container_logs", None),
)

_TYPE_RE = re.compile(r"type=([a-z]+)")

_ASSET_RE = re.compile(r"as_[0-9a-z_]+")
_INCIDENT_RE = re.compile(r"inc_[0-9a-z_]+")


class FakeDiagnoserChatModel(BaseChatModel):
    """按固定计划取证、从工具结果合成报告的确定性模型。

    无实例状态：轮次从消息历史推导（AI 消息中已出现的工具调用次数），
    因此跨 run 共享实例安全，且与 LangGraph checkpoint 恢复兼容。
    """

    model_name: str = "fake-diagnoser"

    @property
    def _llm_type(self) -> str:
        return "fake-diagnoser"

    def bind_tools(self, tools: Any, **kwargs: Any) -> FakeDiagnoserChatModel:  # noqa: ANN401
        return self

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,  # noqa: ANN003
    ) -> ChatResult:
        tool_messages = [m for m in messages if isinstance(m, ToolMessage)]
        ai_calls = sum(1 for m in messages if getattr(m, "tool_calls", None))
        human = next((m for m in messages if getattr(m, "type", "") == "human"), None)
        human_text = str(human.content) if human else ""
        asset_match = _ASSET_RE.search(human_text)
        incident_match = _INCIDENT_RE.search(human_text)
        type_match = _TYPE_RE.search(human_text)
        asset_id = asset_match.group(0) if asset_match else ""
        incident_id = incident_match.group(0) if incident_match else ""
        asset_type = type_match.group(1) if type_match else ""

        plan = [
            name
            for name, applicable in _TOOL_PLAN
            if applicable is None or asset_type in applicable
        ]

        # 下一轮工具调用（或计划完成 → 生成报告）
        if ai_calls < len(plan):
            tool_name = plan[ai_calls]
            message = AIMessage(
                content=f"（fake 计划第 {ai_calls + 1}/{len(plan)} 步）",
                tool_calls=[
                    {
                        "name": tool_name,
                        "args": {"asset_id": asset_id},
                        "id": f"call_{ai_calls + 1}",
                    }
                ],
            )
            return ChatResult(generations=[ChatGeneration(message=message)])

        # 计划完成：从真实工具结果合成报告
        report = _synthesize_report(incident_id=incident_id, tool_messages=tool_messages)
        content = json.dumps(report, ensure_ascii=False)
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content=content))])


def _synthesize_report(*, incident_id: str, tool_messages: list[ToolMessage]) -> dict[str, Any]:
    findings: list[dict[str, Any]] = []
    observations: list[dict[str, Any]] = []
    for tm in tool_messages:
        if isinstance(tm.content, str):
            try:
                data = json.loads(tm.content)
            except json.JSONDecodeError:
                continue
        elif isinstance(tm.content, dict):
            data = tm.content
        else:
            continue
        if not isinstance(data, dict) or "evidence_id" not in data:
            continue
        observations.append(data)
        findings.append(
            {
                "finding": f"{data.get('correlation', {}).get('service', '')} "
                f"{tm.name}: {data.get('summary_redacted', '')}",
                "evidence_ids": [data["evidence_id"]],
                "detail": f"status={data.get('status')}",
            }
        )

    hypotheses: list[dict[str, Any]] = []
    rank = 0
    for obs in observations:
        if obs.get("status") != "ok":
            rank += 1
            hypotheses.append(
                {
                    "rank": rank,
                    "statement": f"观测异常：{obs.get('summary_redacted', '')[:160]}",
                    "status": "supported",  # 有真实失败证据支撑
                    "supporting_evidence_ids": [obs["evidence_id"]],
                    "contradicting_evidence_ids": [],
                    "missing_information": [],
                    "confidence": "medium",
                }
            )
    is_unknown = not observations
    if is_unknown:
        hypotheses = []

    return {
        "summary": (
            f"事件 {incident_id}：确定性诊断完成，共采集 {len(observations)} 条证据。"
            if observations
            else f"事件 {incident_id}：未采集到任何证据。"
        ),
        "key_findings": findings,
        "hypotheses": hypotheses,
        "unknown": {
            "is_unknown": is_unknown,
            "reason": "所有工具均未返回证据" if is_unknown else "",
            "missing_information": [] if observations else ["检查连接器配置与资产登记"],
        },
        "next_manual_steps": [
            "根据关键证据人工复核",
            "确认无误后按处置手册操作（本系统不自动修复）",
        ],
    }
