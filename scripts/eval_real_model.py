"""真实 DeepSeek 模型评估（D6 收尾 v2；与 Fake 确定性评估严格分离）。

改动要点（相对 D6 首版脚本）：

1. **测试库护栏**：只允许在显式白名单的测试库上清库（默认 `opspilot_test`），
   且必须显式确认（`--confirm`）；其余库一律拒绝 TRUNCATE。
2. **盲测集**：固定 7 例之外，追加 `blind_cases.py` 的 3 例（此前从未参与调试）。
3. **完整轨迹留档**：每例保存证据、假设、报告生成状态、run 事件与失败分类。
4. **新旧判官并排**：同一轨迹同时给出 `judge-v1` 与 `judge-v2` 判定，便于说明
   评估规则变化（旧成绩文件不覆盖）。
5. **指标口径**：报告生成成功率、诊断正确率、合理弃答率、安全违规数、token
   （来自 provider usage 元数据；不可用时如实标注）。

用法（密钥只来自 .env 的 DEEPSEEK_API_KEY，不经过 AI）：
    .venv/Scripts/python.exe scripts/eval_real_model.py --confirm
可选：
    --cases fault_redis_down_refuse,blind_gateway_504   # 只跑子集
    --out docs/eval                                     # 输出目录
    --no-truncate                                       # 复跑时保留现场
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
from urllib.parse import urlparse

# 测试库隔离（在导入 app 之前设置，避免污染开发库）
DEFAULT_TEST_DB = "opspilot_test"
os.environ.setdefault(
    "DATABASE_URL", f"postgresql+asyncpg://opspilot:opspilot@127.0.0.1:5432/{DEFAULT_TEST_DB}"
)
os.environ.setdefault("NO_PROXY", "127.0.0.1,localhost")

from dotenv import load_dotenv  # noqa: E402

# 显式路径：find_dotenv 基于调用文件位置，脚本从任意 cwd 启动都能取到项目 .env
load_dotenv(Path(__file__).resolve().parents[1] / ".env")
os.environ.setdefault("AGENT_API_KEY_ENV", "DEEPSEEK_API_KEY")  # 间接引用变量名
# 检索向量用确定性 fake embedding（评估焦点是真实 LLM 诊断行为；与 fake 套件同口径）
os.environ.setdefault("EMBEDDING_PROVIDER", "fake")

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.domains.agent import provider as provider_mod  # noqa: E402
from app.domains.agent.runtime import AgentRuntime, RuntimeDeps  # noqa: E402
from app.domains.evaluations import blind_cases as blind_mod  # noqa: E402
from app.domains.evaluations import cases as cases_mod  # noqa: E402
from app.domains.evaluations import judge as judge_mod  # noqa: E402
from app.domains.evaluations.runner import EvaluationRunner, _fast_settings  # noqa: E402
from app.domains.tools.gateway import ToolGateway  # noqa: E402
from app.models import EvaluationCase  # noqa: E402

# 允许清库的数据库名白名单（全部必须是测试库；任何其他库直接拒绝）
ALLOWED_TEST_DATABASES = frozenset({"opspilot_test", "opspilot_eval_test", "opspilot_e2e_test"})

# 固定代表性场景（六类故障；与 D6 首版一致，未做任何 expected 改写）
REAL_EVAL_CASES = [
    "fault_redis_down_refuse",  # Redis 不可达
    "fault_container_oom_exit",  # Docker OOM
    "fault_java_stack_503",  # Java 503
    "fault_mysql_connect_refuse",  # MySQL 连接故障
    "fault_redis_down_timeout",  # 观测超时
    "fault_unknown_exit_budget0",  # 未知故障（合理弃答）
    "rag_hybrid_misleading_resist",  # 历史经验误导
]

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


def _database_name(url: str) -> str:
    parsed = urlparse(url)
    return (parsed.path or "").lstrip("/") or "<unknown>"


def _guard_database(url: str, *, confirmed: bool) -> tuple[bool, str]:
    """严格测试库白名单 + 显式确认（非测试库一律拒绝清库）。"""
    name = _database_name(url)
    if name not in ALLOWED_TEST_DATABASES:
        return False, (
            f"REFUSED: database '{name}' is not in the test-database allowlist "
            f"{sorted(ALLOWED_TEST_DATABASES)} — TRUNCATE is forbidden on non-test databases"
        )
    if not confirmed:
        return False, (
            f"REFUSED: destructive reset of '{name}' requires explicit --confirm "
            "(this script TRUNCATEs all business tables before the run)"
        )
    return True, name


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


async def _case_trace(sf: Any, incident_id: str | None) -> dict[str, Any]:
    """从 DB 取回本案例的取证轨迹、报告与失败原因（审核用，不含密钥）。"""
    if not incident_id:
        return {}
    from sqlalchemy import select  # noqa: PLC0415

    from app.models import (  # noqa: PLC0415
        DiagnosticReport,
        Evidence,
        Hypothesis,
        InvestigationRun,
        RunEvent,
    )

    async with sf() as session:
        run = (
            await session.execute(
                select(InvestigationRun)
                .where(InvestigationRun.incident_id == incident_id)
                .order_by(InvestigationRun.created_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if run is None:
            return {}
        evidence_rows = (
            (
                await session.execute(
                    select(Evidence).where(Evidence.run_id == run.id).order_by(Evidence.created_at)
                )
            )
            .scalars()
            .all()
        )
        hypothesis_rows = (
            (
                await session.execute(
                    select(Hypothesis).where(Hypothesis.run_id == run.id).order_by(Hypothesis.rank)
                )
            )
            .scalars()
            .all()
        )
        report = (
            (
                await session.execute(
                    select(DiagnosticReport)
                    .where(DiagnosticReport.run_id == run.id)
                    .order_by(DiagnosticReport.version_no.desc())
                    .limit(1)
                )
            )
            .scalars()
            .first()
        )
        events = (
            (await session.execute(select(RunEvent).where(RunEvent.run_id == run.id)))
            .scalars()
            .all()
        )
    return {
        "run_id": run.id,
        "run_status": run.status,
        "stop_reason": run.stop_reason,
        "usage": dict(run.usage or {}),
        "events": [
            {
                "seq": event.seq,
                "type": event.type,
                "payload": event.payload,
                "at": event.created_at.isoformat() if event.created_at else None,
            }
            for event in sorted(events, key=lambda e: e.seq)
        ],
        "evidence": [
            {
                "evidence_id": e.id,
                "source_type": e.source_type,
                "status": e.status,
                "error_code": e.error_code,
                "summary": str(e.summary_redacted or "")[:400],
            }
            for e in evidence_rows
        ],
        "hypotheses": [
            {
                "rank": h.rank,
                "status": h.status,
                "statement": h.statement,
                "supporting_evidence_ids": h.supporting_evidence_ids,
                "contradicting_evidence_ids": h.contradicting_evidence_ids,
                "missing_information": h.missing_information,
                "confidence": h.confidence,
            }
            for h in hypothesis_rows
        ],
        "report": (
            {
                "conclusion": report.conclusion,
                "unknown_note": report.unknown_note,
                "generator": report.generator,
                "evidence_refs": report.evidence_refs,
            }
            if report is not None
            else None
        ),
    }


def _aggregate(results: list[dict[str, Any]]) -> dict[str, Any]:
    total = len(results)
    passed = sum(1 for r in results if r["passed"])
    report_failed = sum(
        1 for r in results if str((r.get("metrics") or {}).get("report_status")) == "failed"
    )
    report_ok = total - report_failed
    abstain_cases = [r for r in results if (r.get("expected") or {}).get("unknown_exit")]
    abstain_ok = sum(
        1 for r in abstain_cases if (r.get("metrics") or {}).get("unknown_exit_correct")
    )
    side_effects = sum(
        int((r.get("metrics") or {}).get("repeated_side_effects") or 0) for r in results
    )
    misled_cases = sum(
        1 for r in results if int((r.get("metrics") or {}).get("knowledge_misuse") or 0) > 0
    )
    tokens_in = 0
    tokens_out = 0
    tokens_known = True
    for r in results:
        tok = (r.get("metrics") or {}).get("tokens")
        if isinstance(tok, dict):
            tokens_in += int(tok.get("in") or 0)
            tokens_out += int(tok.get("out") or 0)
        else:
            tokens_known = False
    v1_passed = sum(1 for r in results if (r.get("judge_v1") or {}).get("passed"))
    judge_false_negatives = [
        r["case_key"]
        for r in results
        if not (r.get("judge_v1") or {}).get("passed") and r["passed"]
    ]
    failure_classes: dict[str, int] = {}
    for r in results:
        cls = str((r.get("metrics") or {}).get("failure_class") or "unknown")
        failure_classes[cls] = failure_classes.get(cls, 0) + 1
    return {
        "cases_total": total,
        "passed": passed,
        "diagnosis_accuracy": round(passed / total, 4) if total else 0.0,
        "report_success_cases": report_ok,
        "report_failure_cases": report_failed,
        "report_success_rate": round(report_ok / total, 4) if total else 0.0,
        "safe_abstention_cases": len(abstain_cases),
        "safe_abstention_correct": abstain_ok,
        "safe_abstention_rate": (
            round(abstain_ok / len(abstain_cases), 4) if abstain_cases else "n/a"
        ),
        "safety_violations": {
            "side_effects": side_effects,
            "adopted_misleading_history": misled_cases,
            "total": side_effects + misled_cases,
        },
        "tokens": (
            {"in": tokens_in, "out": tokens_out, "total": tokens_in + tokens_out}
            if tokens_known
            else "unavailable"
        ),
        "tokens_note": (
            "provider usage 元数据（DeepSeek 返回 usage_metadata）"
            if tokens_known
            else "provider 未返回 usage 元数据，token 无法统计"
        ),
        "judge_v1_passed": v1_passed,
        "judge_v2_passed": passed,
        "judge_v1_false_negatives": judge_false_negatives,
        "failure_classes": failure_classes,
    }


async def main() -> int:
    parser = argparse.ArgumentParser(description="真实 DeepSeek 评估（报告生成 v2 + judge-v2）")
    parser.add_argument("--out", default="docs/eval", help="输出目录")
    parser.add_argument("--cases", default="", help="逗号分隔的 case_key 子集（默认全量 10 例）")
    parser.add_argument("--confirm", action="store_true", help="确认清空**测试库**业务表")
    parser.add_argument("--no-truncate", action="store_true", help="跳过清库（复跑保留现场）")
    parser.add_argument("--label", default="", help="结果文件标签（默认空）")
    args = parser.parse_args()

    settings = _fast_settings()
    # 连接器超时恢复默认 5s（1s 压缩值会把 stub 响应误判为 CONNECTOR_TIMEOUT）
    settings.connector_timeout_seconds = 5.0
    settings.agent_provider = "deepseek"  # 显式真实 provider（与 Fake 严格分离）
    # .env 里的 AGENT_MODEL_NAME 是 fake 模型名；真实评估必须显式使用真实模型名
    settings.agent_model_name = os.environ.get("REAL_EVAL_MODEL") or "deepseek-chat"
    pv = os.environ.get("AGENT_PROMPT_VERSION")
    settings.agent_prompt_version = pv or settings.agent_prompt_version

    db_ok, db_msg = _guard_database(settings.database_url, confirmed=args.confirm)
    print(f"[db] target={_database_name(settings.database_url)} guard={db_msg}", flush=True)
    if not db_ok:
        print(json.dumps({"status": "refused", "reason": db_msg}, ensure_ascii=False))
        return 3

    # 前置校验：Key 缺失则明确失败退出（禁止填充假数据）
    try:
        handle = provider_mod.build_chat_model(settings)
    except provider_mod.ProviderNotConfigured as exc:
        print(json.dumps({"status": "not_completed", "reason": str(exc)}, ensure_ascii=False))
        return 2

    from sqlalchemy import text  # noqa: PLC0415
    from sqlalchemy.ext.asyncio import (  # noqa: PLC0415
        async_sessionmaker,
        create_async_engine,
    )

    engine = create_async_engine(settings.database_url, pool_pre_ping=True)
    sf = async_sessionmaker(engine, expire_on_commit=False)

    if not args.no_truncate:
        async with engine.begin() as conn:
            await conn.execute(text(f"TRUNCATE TABLE {ALL_TABLES} RESTART IDENTITY CASCADE"))
        async with engine.begin() as conn:
            exists = await conn.scalar(
                text(
                    "SELECT EXISTS (SELECT 1 FROM pg_class c JOIN pg_namespace n "
                    "ON n.oid=c.relnamespace WHERE n.nspname='langgraph' "
                    "AND c.relname='checkpoints')"
                )
            )
            if exists:
                for t in ("checkpoint_writes", "checkpoint_blobs", "checkpoints"):
                    await conn.execute(text(f"DELETE FROM langgraph.{t}"))

    runner = RealModelRunner(sf, settings)

    fixed = {c["case_key"]: c for c in cases_mod._build_cases()}
    blind = {c["case_key"]: c for c in blind_mod.build_blind_cases()}
    all_defs = {**fixed, **blind}
    wanted = [k.strip() for k in args.cases.split(",") if k.strip()] or [
        *REAL_EVAL_CASES,
        *blind_mod.BLIND_CASE_KEYS,
    ]

    selected: list[EvaluationCase] = []
    expected_by_key: dict[str, dict[str, Any]] = {}
    for key in wanted:
        c = all_defs.get(key)
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
                # 盲测案例不属于冻结 holdout 集，标记为 false 以免污染 holdout 统计
                holdout=key in cases_mod._HOLDOUT_KEYS,
            )
        )
        expected_by_key[key] = dict(c["expected"])

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
    runner._pool = (pool, checkpointer)
    await runner._stubs.start()

    results: list[dict[str, Any]] = []
    try:
        for case in selected:
            print(f"=== REAL EVAL {case.case_key} ===", flush=True)
            trace: dict[str, Any] = {}
            try:
                passed, metrics, detail, incident_id = await runner._exec_case(case)
                trace = await _case_trace(sf, incident_id)
            except Exception as exc:  # noqa: BLE001 — 单案例崩溃不拖垮整体
                passed = False
                metrics = {"error": f"{type(exc).__name__}: {str(exc)[:300]}"}
                detail = {"error": str(exc)[:500]}
                incident_id = None
            v1 = (detail or {}).get("legacy_judge_v1") or {}
            results.append(
                {
                    "case_key": case.case_key,
                    "suite": case.suite_id,
                    "title": case.title,
                    "blind": case.case_key in blind_mod.BLIND_CASE_KEYS,
                    "passed": passed,
                    "judge_v1": {"passed": bool(v1.get("passed")), "metrics": v1.get("metrics")},
                    "metrics": metrics,
                    "detail": detail,
                    "expected": expected_by_key.get(case.case_key, {}),
                    "trace": trace,
                }
            )
            preview = json.dumps(
                {
                    "passed": passed,
                    "v1": bool(v1.get("passed")),
                    "report": metrics.get("report_status"),
                    "failure_class": metrics.get("failure_class"),
                    "tokens": metrics.get("tokens"),
                },
                ensure_ascii=False,
            )
            print(f"  -> {preview}", flush=True)
    finally:
        await runner._stubs.stop()
        with __import__("contextlib").suppress(Exception):
            await pool.close()
        await engine.dispose()

    summary = _aggregate(results)
    report = {
        "status": "completed",
        "eval_type": "real_model",
        "eval_round": "d6-final-v2",
        "provider": handle.provider,
        "model_name": handle.model_name,
        "model_config": handle.as_model_config(settings),
        "judge_version": judge_mod.JUDGE_VERSION,
        "legacy_judge_version": judge_mod.JUDGE_VERSION_V1,
        "dataset_version": cases_mod.DATASET_VERSION,
        "dataset_digest": cases_mod.dataset_digest(),
        "blind_dataset_version": blind_mod.BLIND_DATASET_VERSION,
        "blind_case_keys": blind_mod.BLIND_CASE_KEYS,
        "database": _database_name(settings.database_url),
        "summary": summary,
        "note": (
            "真实模型推理 + 确定性 stub 基础设施输入：真实的是模型推理/报告生成，"
            "不是真实容器故障注入；评估输入（scenario/expected）未做任何改写"
        ),
        "results": results,
        "generated_at": datetime.now(UTC).isoformat(),
        "git_commit": os.popen("git rev-parse --short HEAD").read().strip(),
    }
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    label = f"_{args.label}" if args.label else ""
    out = out_dir / (
        f"real_deepseek_{judge_mod.JUDGE_VERSION.replace('-', '')}{label}_"
        f"{datetime.now(UTC).strftime('%Y%m%d_%H%M%S')}.json"
    )
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"REPORT -> {out}")
    print(json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    if sys.platform == "win32":
        # psycopg 异步模式不支持 ProactorEventLoop（Windows 默认）
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    raise SystemExit(asyncio.run(main()))
