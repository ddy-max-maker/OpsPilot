"""真实 DeepSeek 模型评估（D6 任务三；与 Fake 确定性评估严格分离）。

- 复用评估 runner 的世界播种/执行/判定管线，仅把 Fake 模型换成
  provider.build_chat_model 构造的真实 DeepSeek（版本随 run 快照留档）。
- 用例为固定、有代表性的 7 例（覆盖用户要求的六类故障场景）；
  不为指标改写任何 expected（与 d6-eval-v2 数据集同源）。
- 结果写入 docs/eval/real_deepseek_*.json，绝不混入 Fake 评估表口径。

用法：
    DATABASE_URL=postgresql+asyncpg://opspilot:opspilot@127.0.0.1:5432/opspilot_test \
        .venv/Scripts/python.exe scripts/eval_real_model.py
前置：.env 中配置 AGENT_PROVIDER=deepseek 与对应 API Key 环境变量。
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# 测试库隔离（在导入 app 之前设置，避免污染开发库）
os.environ.setdefault(
    "DATABASE_URL", "postgresql+asyncpg://opspilot:opspilot@127.0.0.1:5432/opspilot_test"
)
os.environ.setdefault("NO_PROXY", "127.0.0.1,localhost")

# 载入 .env（API Key 仅进入进程环境，不打印/不落盘）
from dotenv import load_dotenv  # noqa: E402

load_dotenv()
os.environ.setdefault("AGENT_API_KEY_ENV", "DEEPSEEK_API_KEY")  # 间接引用变量名
# 检索向量用确定性 fake embedding（评估焦点是真实 LLM 诊断行为；
# fake 套件同口径，保证 RAG 管线确定性）
os.environ.setdefault("EMBEDDING_PROVIDER", "fake")

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.domains.agent import provider as provider_mod  # noqa: E402
from app.domains.agent.runtime import AgentRuntime, RuntimeDeps  # noqa: E402
from app.domains.evaluations import cases as cases_mod  # noqa: E402
from app.domains.evaluations.runner import EvaluationRunner, _fast_settings  # noqa: E402
from app.domains.tools.gateway import ToolGateway  # noqa: E402
from app.models import EvaluationCase  # noqa: E402

# 六类代表性故障（fault/rag 各套件中未被逐案例调试修改的案例优先）
REAL_EVAL_CASES = [
    "fault_redis_down_refuse",       # Redis 故障（连接拒绝）
    "fault_container_oom_exit",      # Docker OOM
    "fault_java_stack_503",          # Java 异常与依赖故障
    "fault_mysql_connect_refuse",    # MySQL 连接异常
    "fault_redis_down_timeout",      # 网关/工具超时（CONNECTOR_TIMEOUT）
    "fault_unknown_exit_budget0",    # 未知故障：预算耗尽适当弃答
    "rag_hybrid_misleading_resist",  # 历史知识误导抵抗（hybrid 检索）
]


class RealModelRunner(EvaluationRunner):
    """把 Fake 模型替换为真实 DeepSeek 的评估执行器（其余管线不变）。"""

    def _runtime_for(self, rag_mode: str, plan_profile: str) -> AgentRuntime:
        key = (rag_mode, plan_profile)
        if key in self._runtimes:
            return self._runtimes[key]
        assert self._pool is not None
        pool, checkpointer = self._pool
        settings = self._settings.model_copy(update={"rag_mode": rag_mode})
        handle = provider_mod.build_chat_model(settings)  # 真实模型（失败即抛错，不静默降级）
        runtime = AgentRuntime(
            RuntimeDeps(
                session_factory=self._sf,
                settings=settings,
                gateway=ToolGateway(self._sf, settings),
                checkpointer=checkpointer,
                pool=pool,
                model_handle=handle,
                model=handle.model,
            )
        )
        self._runtimes[key] = runtime
        return runtime


async def main() -> int:
    settings = _fast_settings()
    # 连接器超时保持默认 5s（1s 压缩值会误判 stub 响应为 CONNECTOR_TIMEOUT）
    settings.connector_timeout_seconds = 5.0
    settings.agent_provider = "deepseek"  # 显式真实 provider（与 Fake 严格分离）
    settings.agent_model_name = os.environ.get("REAL_EVAL_MODEL", "deepseek-chat")
    pv = os.environ.get("AGENT_PROMPT_VERSION")
    settings.agent_prompt_version = pv or settings.agent_prompt_version

    # 前置校验：Key 缺失则明确失败退出（禁止填充假数据）
    try:
        handle = provider_mod.build_chat_model(settings)
    except provider_mod.ProviderNotConfigured as exc:
        print(json.dumps({"status": "not_completed", "reason": str(exc)}, ensure_ascii=False))
        return 2

    from sqlalchemy.ext.asyncio import (  # noqa: PLC0415
        async_sessionmaker,
        create_async_engine,  # noqa: PLC0415
    )

    engine = create_async_engine(settings.database_url, pool_pre_ping=True)
    sf = async_sessionmaker(engine, expire_on_commit=False)

    # 清空测试库业务表与 checkpoint（与 tests/conftest.truncate_all 同口径；
    # 不导入 conftest —— 其模块级 asyncio.run 与本脚本事件循环冲突）
    from sqlalchemy import text  # noqa: PLC0415

    ALL_TABLES = (
        "users, roles, user_roles, asset_access_policies, assets, asset_edges, "
        "incidents, alerts, audit_events, api_idempotency_keys, outbox_events, "
        "investigation_runs, run_events, evidence, hypotheses, diagnostic_reports, "
        "connector_types, connector_instances, secret_refs, "
        "capability_manifests, capability_test_runs, asset_observability_bindings, "
        "knowledge_documents, knowledge_versions, knowledge_chunks, knowledge_embeddings, "
        "retrieval_events, runbooks, runbook_versions, "
        "proposals, approvals, action_executions, action_idempotency, verification_results, "
        "evaluation_suites, evaluation_cases, evaluation_runs, evaluation_results"
    )
    async with engine.begin() as conn:
        await conn.execute(text(f"TRUNCATE TABLE {ALL_TABLES} RESTART IDENTITY CASCADE"))
    async with engine.begin() as conn:
        exists = await conn.scalar(
            text(
                "SELECT EXISTS (SELECT 1 FROM pg_class c JOIN pg_namespace n "
                "ON n.oid=c.relnamespace WHERE n.nspname='langgraph' AND c.relname='checkpoints')"
            )
        )
        if exists:
            for t in ("checkpoint_writes", "checkpoint_blobs", "checkpoints"):
                await conn.execute(text(f"DELETE FROM langgraph.{t}"))

    runner = RealModelRunner(sf, settings)

    all_cases = {c["case_key"]: c for c in cases_mod._build_cases()}
    selected = []
    for key in REAL_EVAL_CASES:
        c = all_cases.get(key)
        if c is None:
            print(f"SKIP unknown case_key {key}")
            continue
        selected.append(
            EvaluationCase(
                id=f"evc_{key}",
                suite_id=c["suite"],
                case_key=key,
                title=c["title"],
                scenario=c["scenario"],
                expected=c["expected"],
                holdout=key in cases_mod._HOLDOUT_KEYS,
            )
        )

    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver  # noqa: PLC0415
    from psycopg_pool import AsyncConnectionPool  # noqa: PLC0415

    pool = AsyncConnectionPool(
        conninfo=settings.psycopg_database_url, max_size=4, open=False,
        kwargs={"autocommit": True},
    )
    await pool.open()
    checkpointer = AsyncPostgresSaver(pool)  # type: ignore[arg-type]
    await checkpointer.setup()
    runner._pool = (pool, checkpointer)
    await runner._stubs.start()

    results: list[dict[str, Any]] = []
    try:
        for case in selected:
            print(f"=== REAL EVAL {case.case_key} ===", flush=True)
            try:
                passed, metrics, detail, _ = await runner._exec_case(case)
            except Exception as exc:  # noqa: BLE001
                passed, metrics, detail = False, {"error": str(exc)[:300]}, {}
            results.append(
                {
                    "case_key": case.case_key,
                    "category": case.title,
                    "passed": passed,
                    "metrics": metrics,
                    "detail": detail,
                }
            )
            metrics_preview = json.dumps(metrics, ensure_ascii=False)[:400]
            print(f"  -> passed={passed} metrics={metrics_preview}", flush=True)
    finally:
        await runner._stubs.stop()
        with __import__("contextlib").suppress(Exception):
            await pool.close()

    passed_n = sum(1 for r in results if r["passed"])
    report = {
        "status": "completed",
        "eval_type": "real_model",
        "provider": handle.provider,
        "model_name": handle.model_name,
        "model_config": handle.as_model_config(settings),
        "dataset_version": cases_mod.DATASET_VERSION,
        "dataset_digest": cases_mod.dataset_digest(),
        "cases_total": len(results),
        "passed": passed_n,
        "failed": len(results) - passed_n,
        "note": "真实模型评估与 Fake 确定性评估严格分离；预期取自 d6-eval-v2 数据集未做任何改写",
        "results": results,
        "generated_at": datetime.now(UTC).isoformat(),
        "git_commit": os.popen("git rev-parse --short HEAD").read().strip(),
    }
    out_dir = Path("docs/eval")
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"real_deepseek_{datetime.now(UTC).strftime('%Y%m%d_%H%M%S')}.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"REPORT -> {out}")
    print(json.dumps({"passed": passed_n, "total": len(results)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    if sys.platform == "win32":
        # psycopg 异步模式不支持 ProactorEventLoop（Windows 默认）
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    raise SystemExit(asyncio.run(main()))
