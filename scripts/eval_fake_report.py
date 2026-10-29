"""CI 评估报告：运行全部 4 个确定性套件并保存 JSON 报告（产物上传）。

用法（目标库经 DATABASE_URL 控制，CI 用一次性测试库）：
    uv run python scripts/eval_fake_report.py --out docs/eval/fake_ci_report.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

os.environ.setdefault(
    "DATABASE_URL", "postgresql+asyncpg://opspilot:opspilot@127.0.0.1:5432/opspilot_test"
)
os.environ.setdefault("NO_PROXY", "127.0.0.1,localhost")
os.environ.setdefault("EMBEDDING_PROVIDER", "fake")

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import text  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402

from app.domains.evaluations import cases as cases_mod  # noqa: E402
from app.domains.evaluations import judge as judge_mod  # noqa: E402
from app.domains.evaluations.runner import EvaluationRunner, _fast_settings  # noqa: E402
from app.models import EvaluationRun  # noqa: E402

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

SUITES = ["fault-core", "security-gates", "rag-ablation", "reliability-recovery"]


async def main(out_path: str) -> int:
    settings = _fast_settings()
    # 连接器超时保持默认 5s：1s 的压缩值在本机/CI 负载下会把 stub 的
    # 正常响应误判为 CONNECTOR_TIMEOUT（pytest 路径即 5s）
    settings.connector_timeout_seconds = 5.0
    engine = create_async_engine(settings.database_url, pool_pre_ping=True)
    sf = async_sessionmaker(engine, expire_on_commit=False)

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

    async with sf() as session:
        seed_stats = await cases_mod.seed_dataset(session)
    print(f"seeded: {json.dumps(seed_stats)}")

    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver  # noqa: PLC0415
    from psycopg_pool import AsyncConnectionPool  # noqa: PLC0415

    pool = AsyncConnectionPool(
        conninfo=settings.psycopg_database_url,
        max_size=4,
        open=False,
        kwargs={"autocommit": True},
    )
    await pool.open()
    checkpointer = AsyncPostgresSaver(pool)  # type: ignore[arg-type]
    await checkpointer.setup()

    runner = EvaluationRunner(sf, settings)
    runner._pool = (pool, checkpointer)
    await runner._stubs.start()

    suites_report: list[dict[str, Any]] = []
    try:
        for suite_name in SUITES:
            async with sf() as session:
                run = EvaluationRun(
                    id=f"erun_ci_{suite_name.replace('-', '_')}",
                    suite_id=f"evs_{suite_name.replace('-', '_')}",
                    status="pending",
                    config={"model_provider": "fake", "ci": True},
                )
                session.add(run)
                await session.commit()
            await runner.execute_run(run.id)
            async with sf() as session:
                stored = await session.get(EvaluationRun, run.id)
                assert stored is not None
                suites_report.append(
                    {
                        "suite": suite_name,
                        "status": stored.status,
                        "summary": (stored.config or {}).get("summary", {}),
                    }
                )
                summary = (stored.config or {}).get("summary", {})
                print(f"{suite_name}: {stored.status} {json.dumps(summary)}")
    finally:
        await runner._stubs.stop()
        with __import__("contextlib").suppress(Exception):
            await pool.close()
        await engine.dispose()

    all_passed = all(
        s["summary"].get("failed", 1) == 0 and s["status"] == "completed" for s in suites_report
    )
    report = {
        "status": "completed" if all_passed else "failed",
        "eval_type": "fake_deterministic",
        "judge_version": judge_mod.JUDGE_VERSION,
        "dataset_version": cases_mod.DATASET_VERSION,
        "dataset_digest": cases_mod.dataset_digest(),
        "suites": suites_report,
        "generated_at": datetime.now(UTC).isoformat(),
        "git_commit": os.popen("git rev-parse --short HEAD").read().strip(),
    }
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    Path(out_path).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"REPORT -> {out_path}")
    return 0 if all_passed else 1


if __name__ == "__main__":
    if sys.platform == "win32":
        # psycopg 异步模式不支持 ProactorEventLoop（Windows 默认）；
        # 策略必须在 asyncio.run 创建循环之前设置
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default="docs/eval/fake_ci_report.json")
    raise SystemExit(asyncio.run(main(parser.parse_args().out)))
