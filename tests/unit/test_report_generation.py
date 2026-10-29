"""结构化报告生成的单元测试（report-schema-v2 / ReportGenerator）。

覆盖：
- 直读成功 / 代码围栏 / 前后文本；
- 截断、无 JSON、Schema 不合法、引用全部无效的失败分类；
- 独立格式化调用与**一次**受控修复；
- 格式化阶段没有工具可调用（结构上无法重新取证）；
- 编造 evidence_id 被剔除（部分/全部两种）。
"""

from __future__ import annotations

import json
from typing import Any

from langchain_core.callbacks import CallbackManagerForLLMRun
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from app.core.config import Settings
from app.domains.agent.report import (
    REPORT_STATUS_FAILED,
    REPORT_STATUS_FORMATTED,
    REPORT_STATUS_OK,
    REPORT_STATUS_REPAIRED,
    EvidenceBrief,
    ReportFailureKind,
    ReportGenerator,
    extract_json_object,
)

EVIDENCE = [
    EvidenceBrief(
        evidence_id="ev_redis_1",
        source_type="redis",
        status="unavailable",
        error_code="REDIS_UNAVAILABLE",
        summary="redis.health unavailable (REDIS_UNAVAILABLE): ConnectionError",
    ),
    EvidenceBrief(
        evidence_id="ev_docker_1",
        source_type="docker",
        status="ok",
        error_code=None,
        summary="docker.inspect OK: exit_code=137 oom_killed=True running=False",
    ),
]


def _payload(**overrides: Any) -> dict[str, Any]:
    base = {
        "summary": "缓存不可达。",
        "key_findings": [
            {"finding": "redis 连接被拒绝", "evidence_ids": ["ev_redis_1"], "detail": ""}
        ],
        "hypotheses": [
            {
                "rank": 1,
                "statement": "Redis 不可达（REDIS_UNAVAILABLE）",
                "status": "supported",
                "supporting_evidence_ids": ["ev_redis_1"],
                "contradicting_evidence_ids": [],
                "missing_information": [],
                "confidence": "medium",
            }
        ],
        "unknown": {"is_unknown": False, "reason": "", "missing_information": []},
        "next_manual_steps": ["人工复核"],
    }
    base.update(overrides)
    return base


class ScriptedChatModel(BaseChatModel):
    """按脚本返回固定文本的模型；未实现 structured output（走 JSON 提示词回退）。"""

    responses: list[str]
    calls: list[list[Any]] = []  # noqa: RUF012 — 测试内可变默认

    @property
    def _llm_type(self) -> str:
        return "scripted"

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        self.calls.append(list(messages))
        index = min(len(self.calls) - 1, len(self.responses) - 1)
        return ChatResult(
            generations=[ChatGeneration(message=AIMessage(content=self.responses[index]))]
        )


def _message(text: str) -> AIMessage:
    return AIMessage(content=text)


def _generator(texts: list[str]) -> tuple[ReportGenerator, ScriptedChatModel]:
    model = ScriptedChatModel(responses=texts, calls=[])
    settings = Settings(report_format_timeout_seconds=5.0)
    return ReportGenerator(model, settings), model


async def test_direct_valid_report_is_ok() -> None:
    generator, model = _generator(["unused"])
    result = await generator.generate(
        final_message=_message(json.dumps(_payload())), evidence=EVIDENCE
    )
    assert result.status == REPORT_STATUS_OK
    assert result.attempts == 0
    assert model.calls == []  # 直读成功：不发起格式化调用
    assert result.payload is not None
    assert result.payload["hypotheses"][0]["status"] == "supported"


async def test_fenced_json_with_prose_is_accepted() -> None:
    fenced = "以下是报告：\n```json\n" + json.dumps(_payload()) + "\n```\n完成。"
    generator, _model = _generator(["unused"])
    result = await generator.generate(final_message=_message(fenced), evidence=EVIDENCE)
    assert result.status == REPORT_STATUS_OK


async def test_truncated_output_is_formatted_with_explicit_initial_failure() -> None:
    generator, _model = _generator([json.dumps(_payload())])
    truncated = '{"summary": "缓存不可达", "hypotheses": [{"statement": "Redis 不可达"'
    result = await generator.generate(final_message=_message(truncated), evidence=EVIDENCE)
    assert result.status == REPORT_STATUS_FORMATTED
    assert result.attempts == 1
    assert result.detail["initial_failure"] == ReportFailureKind.TRUNCATED.value


async def test_no_json_falls_back_to_formatting() -> None:
    generator, _model = _generator([json.dumps(_payload())])
    result = await generator.generate(
        final_message=_message("我需要更多证据才能下结论，暂时无法给出报告。"),
        evidence=EVIDENCE,
    )
    assert result.status == REPORT_STATUS_FORMATTED
    assert result.detail["initial_failure"] == ReportFailureKind.NO_JSON.value


async def test_formatting_calls_have_no_tools_bound() -> None:
    generator, model = _generator([json.dumps(_payload())])
    await generator.generate(final_message=_message("无 JSON"), evidence=EVIDENCE)
    assert model.calls, "应发生格式化调用"
    for call in model.calls:
        roles = [getattr(m, "type", "") for m in call]
        assert roles == ["system", "human"], roles
        assert not any(getattr(m, "tool_calls", None) for m in call)


async def test_schema_error_repaired_once_then_succeeds() -> None:
    bad = json.dumps({"summary": "x", "hypotheses": "not-a-list"})
    generator, model = _generator([bad, json.dumps(_payload())])
    result = await generator.generate(final_message=_message("没有 JSON"), evidence=EVIDENCE)
    assert result.status == REPORT_STATUS_REPAIRED
    assert result.attempts == 2
    assert len(model.calls) == 2


async def test_schema_error_after_one_repair_fails_explicitly() -> None:
    bad = json.dumps({"summary": "x", "hypotheses": "not-a-list"})
    generator, model = _generator([bad, bad, bad])
    result = await generator.generate(final_message=_message("没有 JSON"), evidence=EVIDENCE)
    assert result.status == REPORT_STATUS_FAILED
    assert result.failure_kind == ReportFailureKind.SCHEMA_ERROR
    assert result.attempts == 2  # 只允许一次受控修复
    assert len(model.calls) == 2


async def test_extra_field_is_schema_error() -> None:
    payload = _payload()
    payload["root_cause"] = "自己加的字段"
    text = json.dumps(payload)
    generator, _model = _generator([text, text])
    result = await generator.generate(final_message=_message(text), evidence=EVIDENCE)
    # 直读失败 → 格式化返回同样带多余字段的 JSON → 两次后明确失败
    assert result.status == REPORT_STATUS_FAILED
    assert result.failure_kind == ReportFailureKind.SCHEMA_ERROR


async def test_partial_fabricated_evidence_is_dropped() -> None:
    payload = _payload()
    payload["key_findings"][0]["evidence_ids"] = ["ev_redis_1", "ev_fabricated"]
    result = await _generator(["unused"])[0].generate(
        final_message=_message(json.dumps(payload)), evidence=EVIDENCE
    )
    assert result.status == REPORT_STATUS_OK
    assert result.dropped_references == 1
    assert result.payload is not None
    assert result.payload["key_findings"][0]["evidence_ids"] == ["ev_redis_1"]


async def test_all_fabricated_evidence_is_reference_error() -> None:
    payload = _payload()
    payload["key_findings"][0]["evidence_ids"] = ["ev_fabricated"]
    payload["hypotheses"][0]["supporting_evidence_ids"] = ["ev_fabricated_2"]
    result = await _generator([json.dumps(payload), json.dumps(payload)])[0].generate(
        final_message=_message(json.dumps(payload)), evidence=EVIDENCE
    )
    assert result.status == REPORT_STATUS_FAILED
    assert result.failure_kind == ReportFailureKind.REFERENCE_ERROR


async def test_empty_final_message_is_model_error() -> None:
    generator, model = _generator(["unused"])
    result = await generator.generate(final_message=_message("   "), evidence=EVIDENCE)
    assert result.status == REPORT_STATUS_FAILED
    assert result.failure_kind == ReportFailureKind.MODEL_ERROR
    assert model.calls == []


def test_extract_json_object_classifies_truncation() -> None:
    assert extract_json_object("没有 JSON") == (None, ReportFailureKind.NO_JSON)
    assert extract_json_object('{"a": 1') == (None, ReportFailureKind.TRUNCATED)
    body, kind = extract_json_object('前缀 {"a": 1} 后缀')
    assert kind is None and body == '{"a": 1}'
