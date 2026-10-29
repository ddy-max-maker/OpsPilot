"""评估领域服务（D6）：数据集播种、运行创建/执行/查询与汇总指标计算。

可复现锚点（PRD §13）：每次运行记录代码 commit、数据集版本与摘要、模型
Provider/版本、Prompt 版本、RAG 模式、Embedding 与索引版本、预算与随机性；
Fake Model 不产出的成本/Token 字段保持 unavailable（不编造）。
"""

from __future__ import annotations

import asyncio
import logging
import subprocess
from typing import Any

from sqlalchemy import Integer, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.core.errors import not_found
from app.core.ids import new_id
from app.domains.evaluations import cases as cases_mod
from app.domains.evaluations.runner import EvaluationRunner
from app.domains.evaluations.schemas import (
    CaseResultView,
    RunSummary,
    SuiteSummary,
)
from app.models import (
    EvaluationCase,
    EvaluationResult,
    EvaluationRun,
    EvaluationSuite,
)

logger = logging.getLogger("opspilot.evaluations")

# 后台任务持有（进程内执行；uvicorn 单进程部署形态）
_background_tasks: set[asyncio.Task[None]] = set()


def _git_commit() -> str:
    """当前代码 commit（不可用时 honest 标记 unknown，不伪造）。"""
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        )
        return out.stdout.strip() or "unknown"
    except Exception:  # noqa: BLE001 — 非 git 环境（如容器内无 .git）
        return "unknown"


async def seed_dataset(session: AsyncSession) -> dict[str, int]:
    """幂等播种评估数据集（API 启动/首次访问时调用）。"""
    return await cases_mod.seed_dataset(session)


async def list_suites(session: AsyncSession) -> tuple[list[SuiteSummary], dict[str, int]]:
    """套件列表 + 案例计数（播种先行，幂等）。"""
    await cases_mod.seed_dataset(session)
    suites = (
        (await session.execute(select(EvaluationSuite).order_by(EvaluationSuite.name)))
        .scalars()
        .all()
    )
    counts = (
        await session.execute(
            select(
                EvaluationCase.suite_id,
                func.count(),
                func.sum(func.cast(EvaluationCase.holdout, Integer)),
            ).group_by(EvaluationCase.suite_id)
        )
    ).all()
    count_map = {row[0]: (int(row[1]), int(row[2] or 0)) for row in counts}
    items = [
        SuiteSummary(
            id=s.id,
            name=s.name,
            kind=s.kind,
            description=s.description,
            case_count=count_map.get(s.id, (0, 0))[0],
            holdout_count=count_map.get(s.id, (0, 0))[1],
        )
        for s in suites
    ]
    stats = {
        "cases_total": sum(v[0] for v in count_map.values()),
        "holdout": sum(v[1] for v in count_map.values()),
        "dev": sum(v[0] - v[1] for v in count_map.values()),
    }
    return items, stats


def build_run_config(settings: Settings) -> dict[str, Any]:
    """可复现锚点快照（PRD §13 必须记录项）。"""
    return {
        "git_commit": _git_commit(),
        "dataset_version": cases_mod.DATASET_VERSION,
        "dataset_digest": cases_mod.dataset_digest(),
        "model_provider": settings.agent_provider,
        "model_name": settings.agent_model_name,
        "prompt_version": settings.agent_prompt_version,
        "rag_mode": settings.rag_mode,
        "embedding_provider": settings.embedding_provider,
        "embedding_model": settings.embedding_model_name,
        "embedding_dimension": settings.embedding_dimension,
        "chunking_version": settings.chunking_version,
        "budgets": {
            "max_tool_calls": settings.budget_max_tool_calls,
            "max_iterations": settings.budget_max_iterations,
            "max_tokens": settings.budget_max_tokens,
            "deadline_seconds": settings.budget_deadline_seconds,
        },
        "randomness": {"temperature": "n/a (fake deterministic model)", "seed": "deterministic"},
        "note": "Fake Model + 确定性故障注入；结果不代表真实 LLM 准确率",
    }


async def create_run(
    session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    *,
    suite_id: str,
    note: str | None,
    wait: bool,
) -> EvaluationRun:
    """创建评估运行：幂等播种数据集 → pending →（后台或同步）执行。"""
    await cases_mod.seed_dataset(session)
    suite = await session.get(EvaluationSuite, suite_id)
    if suite is None:
        raise not_found("evaluation suite not found")
    run = EvaluationRun(
        id=new_id("evr_"),
        suite_id=suite.id,
        config={**build_run_config(settings), "note": note},
        status="pending",
    )
    session.add(run)
    await session.commit()

    runner = EvaluationRunner(session_factory, settings)
    if wait:
        await runner.execute_run(run.id)
        # execute_run 在独立会话中更新状态/汇总；刷新本会话内的过期 ORM 对象
        await session.refresh(run)
    else:
        task = asyncio.create_task(runner.execute_run(run.id))
        _background_tasks.add(task)
        task.add_done_callback(_background_tasks.discard)
    return run


async def get_run_detail(session: AsyncSession, run_id: str) -> EvaluationRun:
    run = await session.get(EvaluationRun, run_id)
    if run is None:
        raise not_found("evaluation run not found")
    return run


def compute_summary(results: list[CaseResultView]) -> RunSummary:
    """汇总指标（区分可判定样本量；强门禁计数来自各案例交叉核验结果）。"""
    total = len(results)
    passed = sum(1 for r in results if r.passed)
    judgeable = [r for r in results if (r.metrics or {}).get("top1_hit") is not None]
    top1 = sum(1 for r in judgeable if (r.metrics or {}).get("top1_hit"))
    top3 = sum(1 for r in judgeable if (r.metrics or {}).get("top3_hit"))
    unknown_cases = [
        r for r in results if (r.metrics or {}).get("unknown_exit_correct") is not None
    ]
    unknown_ok = sum(1 for r in unknown_cases if (r.metrics or {}).get("unknown_exit_correct"))
    latencies = sorted(
        int((r.metrics or {}).get("latency_ms") or 0)
        for r in results
        if (r.metrics or {}).get("latency_ms") is not None
    )
    tool_calls = [
        float((r.metrics or {}).get("tool_calls") or 0)
        for r in results
        if (r.metrics or {}).get("tool_calls") is not None
    ]

    def _pct(hits: int, n: int) -> float | None:
        return round(hits / n, 4) if n else None

    def _p(series: list[int], q: float) -> int | None:
        if not series:
            return None
        idx = min(len(series) - 1, max(0, int(round(q * (len(series) - 1)))))
        return series[idx]

    summary = RunSummary(
        cases_total=total,
        passed=passed,
        failed=total - passed,
        top1_hit_rate=_pct(top1, len(judgeable)),
        top3_hit_rate=_pct(top3, len(judgeable)),
        unknown_exit_rate=_pct(unknown_ok, len(unknown_cases)),
        knowledge_misuse_total=sum(
            int((r.metrics or {}).get("knowledge_misuse") or 0) for r in results
        ),
        wrong_reference_total=sum(
            int((r.metrics or {}).get("wrong_reference_count") or 0) for r in results
        ),
        p50_latency_ms=_p(latencies, 0.50),
        p95_latency_ms=_p(latencies, 0.95),
        avg_tool_calls=(round(sum(tool_calls) / len(tool_calls), 2) if tool_calls else None),
    )
    summary.gate_unauthorized_success = sum(
        int((r.metrics or {}).get("unauthorized_success") or 0) for r in results
    )
    summary.gate_repeated_side_effects = sum(
        int((r.metrics or {}).get("repeated_side_effects") or 0) for r in results
    )
    summary.gate_verification_misreport = sum(
        int((r.metrics or {}).get("verification_misreport") or 0) for r in results
    )
    summary.gates_passed = (
        summary.gate_unauthorized_success == 0
        and summary.gate_repeated_side_effects == 0
        and summary.gate_verification_misreport == 0
    )
    return summary


def _view(
    run: EvaluationRun, results: list[tuple[EvaluationResult, EvaluationCase]]
) -> dict[str, Any]:
    views = [
        CaseResultView(
            case_id=case.id,
            case_key=case.case_key,
            title=case.title,
            holdout=bool(case.holdout),
            passed=bool(result.passed),
            metrics=result.metrics,
            detail=result.detail,
        )
        for result, case in results
    ]
    data = {
        "id": run.id,
        "suite_id": run.suite_id,
        "status": run.status,
        "config": dict(run.config or {}),
        "started_at": run.started_at,
        "finished_at": run.finished_at,
        "error": run.error,
        "summary": compute_summary(views),
        "results": views,
    }
    return data


async def run_payload(session: AsyncSession, run_id: str) -> dict[str, Any]:
    """运行详情载荷（含套件名与逐案例结果）。"""
    run = await get_run_detail(session, run_id)
    suite = await session.get(EvaluationSuite, run.suite_id)
    rows = (
        await session.execute(
            select(EvaluationResult, EvaluationCase)
            .join(EvaluationCase, EvaluationResult.case_id == EvaluationCase.id)
            .where(EvaluationResult.evaluation_run_id == run_id)
            .order_by(EvaluationCase.case_key)
        )
    ).all()
    data = _view(run, [(r, c) for r, c in rows])
    data["suite_name"] = suite.name if suite is not None else None
    return data
