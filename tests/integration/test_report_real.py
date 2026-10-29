"""真实模型的结构化报告集成测试（D6 收尾）。

与 Fake 单测互补：真实调用 DeepSeek，验证两条路径都能产出**符合 Schema** 的报告：
1. 模型直接给出 JSON（直读）；
2. 模型给的是散文（触发独立格式化调用）——这是旧版 1/7 的主要失败点。

默认跳过（需要凭据与费用）：设置 `RUN_REAL_MODEL=1` 且提供 DEEPSEEK_API_KEY。
断言只检查「结构 + 引用可追溯」，不检查具体诊断内容（那是评估脚本的事）。
"""

from __future__ import annotations

import json
import os

import pytest

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get("RUN_REAL_MODEL") != "1" or not os.environ.get("DEEPSEEK_API_KEY"),
        reason="真实模型报告测试需 RUN_REAL_MODEL=1 且 DEEPSEEK_API_KEY",
    ),
]

EVIDENCE = [
    {
        "evidence_id": "ev_http_1",
        "source_type": "http",
        "status": "error",
        "error_code": "HTTP_500",
        "summary": "http.health_check error (HTTP_500) for /health/orders",
    },
    {
        "evidence_id": "ev_redis_1",
        "source_type": "redis",
        "status": "unavailable",
        "error_code": "REDIS_UNAVAILABLE",
        "summary": "redis.health unavailable (REDIS_UNAVAILABLE): ConnectionError",
    },
]


def _generator():
    from app.core.config import Settings
    from app.domains.agent.provider import build_chat_model
    from app.domains.agent.report import ReportGenerator

    settings = Settings()
    if settings.agent_provider == "fake":
        settings.agent_provider = "deepseek"
        settings.agent_model_name = "deepseek-chat"
        settings.agent_api_key_env = "DEEPSEEK_API_KEY"
    handle = build_chat_model(settings)
    return ReportGenerator(handle.model, settings)


async def _run(final_text: str):
    from langchain_core.messages import AIMessage

    from app.domains.agent.report import EvidenceBrief

    generator = _generator()
    result = await generator.generate(
        final_message=AIMessage(content=final_text),
        evidence=[EvidenceBrief(**item) for item in EVIDENCE],
        run_id="run_real_report_test",
    )
    return result


async def test_real_model_direct_json_report_is_schema_valid() -> None:
    """模型直接输出的 JSON 报告必须通过严格 Schema 校验（或经一次格式化补救）。"""
    prompt = (
        "你是只读诊断助手。下面是本次 run 的真实证据：\n"
        + "\n".join(f"- {e['evidence_id']}: {e['summary']}" for e in EVIDENCE)
        + "\n请只输出 JSON 报告（summary/key_findings/hypotheses/unknown/next_manual_steps）。"
    )
    result = await _run(prompt)
    assert result.payload is not None, f"报告生成失败: {result.failure_kind} {result.detail}"
    payload = result.payload
    assert set(payload) >= {
        "summary",
        "key_findings",
        "hypotheses",
        "unknown",
        "next_manual_steps",
    }
    json.dumps(payload)  # 可序列化
    cited = {
        ref for finding in payload["key_findings"] for ref in finding.get("evidence_ids") or []
    } | {
        ref
        for hypothesis in payload["hypotheses"]
        for ref in hypothesis.get("supporting_evidence_ids") or []
    }
    assert cited <= {e["evidence_id"] for e in EVIDENCE}, "不得引用不存在的证据"


async def test_real_model_prose_output_recovered_by_formatter() -> None:
    """散文输出（无 JSON）→ 独立格式化调用必须能救回成 Schema 合法报告。"""
    prose = (
        "经过排查：orders 的健康端点返回 500，同时 Redis 探测出现连接错误。"
        "直接原因是应用自身错误 + 缓存不可达；更深层原因需要人工确认。"
        "建议先复核应用日志，再确认 Redis 实例状态。"
    )
    result = await _run(prose)
    assert result.payload is not None, f"格式化未能救回: {result.failure_kind} {result.detail}"
    assert result.status in ("formatted", "repaired")
    assert result.payload["summary"], "格式化结果必须有 summary"
    assert isinstance(result.payload["unknown"], dict)
