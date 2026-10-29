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
    # D3：database 资产走 MySQL 只读诊断（跨语言/多能力确定性回归）
    ("read_mysql_diagnostics", ("database",)),
)

# D6 评估：full 剖面在健康检查后加入可观测性取证（指标/链路；跨服务根因场景）
_OBSERVABILITY_STEPS = (
    ("query_metrics", None),
    ("query_traces", None),
)

# D4：RAG 开启时前置知识检索步骤（确定性验证知识工具链路 + 证据语义）
_KNOWLEDGE_STEPS = (
    ("retrieve_incidents", None),
    ("search_runbooks", None),
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
    # D4：RAG 开启时（Worker 按设置注入）前置知识检索步骤；默认关闭保持 D2/D3 计划
    include_knowledge_steps: bool = False
    # D6 评估：full 剖面加入指标/链路取证步骤（默认 default 保持既有确定性计划）
    plan_profile: str = "default"

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

        plan: list[tuple[str, tuple[str, ...] | None]] = []
        if self.include_knowledge_steps:
            plan.extend(_KNOWLEDGE_STEPS)
        plan.extend(_TOOL_PLAN[:1])
        if self.plan_profile == "full":
            plan.extend(_OBSERVABILITY_STEPS)
        plan.extend(_TOOL_PLAN[1:])
        steps = [
            name for name, applicable in plan if applicable is None or asset_type in applicable
        ]
        # 知识检索查询词：取事件标题（任务提示首行"诊断故障事件 inc_x：标题"）
        title_match = re.search(r"诊断故障事件\s+\S+：(.+)", human_text)
        knowledge_query = (title_match.group(1).strip() if title_match else "")[:200] or "故障排查"

        # 下一轮工具调用（或计划完成 → 生成报告）
        if ai_calls < len(steps):
            tool_name = steps[ai_calls]
            args: dict[str, Any] = {"asset_id": asset_id}
            if tool_name == "retrieve_incidents" or tool_name == "search_runbooks":
                args = {"query": knowledge_query, "limit": 3}
            elif tool_name == "query_metrics":
                # 模板名固定 "up"（评估场景的 prom 实例白名单含该键）
                args["template"] = "up"
            message = AIMessage(
                content=f"（fake 计划第 {ai_calls + 1}/{len(steps)} 步）",
                tool_calls=[
                    {
                        "name": tool_name,
                        "args": args,
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
    # 主假设来源：真实失败的探测（status != ok）
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
    # 次级假设来源：探测成功但载荷携带 anomaly 异常标记（链路/指标级故障，
    # 如下游依赖错误 span——由真实证据文本驱动，不凭空推断）
    for obs in observations:
        summary = str(obs.get("summary_redacted", ""))
        if obs.get("status") == "ok" and "anomaly=" in summary:
            rank += 1
            hypotheses.append(
                {
                    "rank": rank,
                    "statement": f"观测异常（依赖/指标标记）：{summary[:160]}",
                    "status": "supported",
                    "supporting_evidence_ids": [obs["evidence_id"]],
                    "contradicting_evidence_ids": [],
                    "missing_information": [],
                    "confidence": "low",
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
