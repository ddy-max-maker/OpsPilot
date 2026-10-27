"""真实模型端到端测试（@pytest.mark.integration：本地可跑、CI 跳过）。

要求环境变量提供凭据：
- ANTHROPIC_API_KEY（AGENT_PROVIDER=anthropic）或
- GOOGLE_API_KEY / GEMINI_API_KEY（AGENT_PROVIDER=google）

无凭据时自动跳过——不伪造真实模型验收结果。
"""

from __future__ import annotations

import os

import pytest

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not (
            os.environ.get("ANTHROPIC_API_KEY")
            or os.environ.get("GOOGLE_API_KEY")
            or os.environ.get("GEMINI_API_KEY")
        ),
        reason="真实模型验收未完成：缺少 ANTHROPIC_API_KEY / GOOGLE_API_KEY（CI 用 fake 模型）",
    ),
]


async def test_real_model_tool_call_loop(db_engine, db_session_factory) -> None:
    """真实模型完成至少一次工具调用闭环（诊断 run 全链路）。"""
    from app.core.config import Settings
    from app.domains.agent.provider import build_chat_model

    settings = Settings()
    if settings.agent_provider == "fake":
        pytest.skip("AGENT_PROVIDER=fake：本测试需要真实 Provider 配置")

    handle = build_chat_model(settings)
    assert handle.provider == settings.agent_provider

    from app.worker import claim
    from tests.integration.test_worker import _execute, _make_runtime, _seed_run

    runtime, pool = await _make_runtime(db_engine, db_session_factory, settings)
    try:
        run = await _seed_run(db_session_factory)
        claimed = await claim.claim_next_run(
            db_session_factory, worker_id="real-model-test", lease_ttl_seconds=120
        )
        assert claimed is not None
        execution = await _execute(runtime, db_session_factory, run.id)
        outcome = await runtime.execute_run(execution)
        assert outcome.status.value in ("completed", "needs_human")
        assert outcome.usage["tool_calls_used"] >= 1, "真实模型必须完成至少一次工具调用"
        assert outcome.report is not None
    finally:
        await pool.close()
