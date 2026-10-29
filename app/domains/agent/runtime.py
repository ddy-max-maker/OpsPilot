"""AgentRuntime：DeepAgents 单执行核心适配层（ADR-D2-EXECUTION-CORE）。

边界：
- 本模块是 LangGraph/DeepAgents 消息对象唯一允许出现的位置；
  领域层只见 RunOutcome / Evidence / 事件 payload；
- 预算（迭代/token/deadline）与取消在 astream 步进间由业务侧强制（服务端硬限制）；
- 工具面由 ToolGateway + HarnessProfile 裁剪（execute/task 移除，见 ADR-D2 决策 2）。
"""

from __future__ import annotations

import contextlib
import json
import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, cast

from deepagents import GeneralPurposeSubagentProfile, HarnessProfile, create_deep_agent
from deepagents.backends.state import StateBackend
from deepagents.profiles import register_harness_profile
from langchain_core.language_models import BaseChatModel
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from psycopg_pool import AsyncConnectionPool
from sqlalchemy import func, select
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.core.enums import (
    Confidence,
    HypothesisStatus,
    RunEventType,
    RunStatus,
    RunStopReason,
)
from app.core.ids import new_id
from app.domains.agent.prompts import (
    DIAGNOSIS_SYSTEM_PROMPT,
    REPORT_FORMAT_PROMPT,
    build_run_task_prompt,
)
from app.domains.agent.report import (
    REPORT_STATUS_FORMATTED,
    REPORT_STATUS_OK,
    REPORT_STATUS_REPAIRED,
    EvidenceBrief,
    ReportFailureKind,
    ReportGenerator,
    ReportResult,
)
from app.domains.agent.report_schema import REPORT_SCHEMA_VERSION
from app.domains.tools.context import (
    LEASE_GUARD_SQL,
    LeaseFence,
    LeaseLostError,
    RunBudget,
    RunCanceled,
    RunEventWriter,
    RunToolContext,
    ToolBudgetExhausted,
)
from app.domains.tools.evidence_quality import (
    EvidenceQuality,
    classify_evidence,
    root_cause_capable,
)
from app.domains.tools.gateway import ToolGateway
from app.domains.tools.registry import load_resolution_map
from app.models import (
    Asset,
    DiagnosticReport,
    Evidence,
    Hypothesis,
    Incident,
    InvestigationRun,
)

logger = logging.getLogger(__name__)


class DeadlineExceeded(ToolBudgetExhausted):
    """deadline_at 到期（服务端强制）。"""


class TokenBudgetExhausted(ToolBudgetExhausted):
    """max_tokens 耗尽（服务端强制）。"""


class IterationBudgetExhausted(ToolBudgetExhausted):
    """max_iterations 耗尽（服务端强制）。"""


# ---- HarnessProfile（ADR-D2 决策 2：工具面裁剪）----

HARNESS_PROFILE = HarnessProfile(
    excluded_tools=frozenset({"execute"}),
    general_purpose_subagent=GeneralPurposeSubagentProfile(enabled=False),
)

_PROFILE_KEYS_REGISTERED: set[str] = set()


def _register_harness_profile_for(model: BaseChatModel, provider: str, model_name: str) -> None:
    """按 DeepAgents 自身的 provider/identifier 推导规则注册裁剪 Profile。

    查找键：`{provider}:{identifier}` 精确 → `{provider}` 兜底（与
    deepagents._models 的解析一致），保证任意模型实例都能命中。
    """
    from deepagents._models import get_model_identifier, get_model_provider  # noqa: PLC0415

    candidates = {f"{provider}:{model_name}", provider}
    identifier = get_model_identifier(model)
    instance_provider = get_model_provider(model)
    if identifier and instance_provider:
        candidates.add(f"{instance_provider}:{identifier}")
        candidates.add(instance_provider)
    for key in candidates:
        if key in _PROFILE_KEYS_REGISTERED:
            continue
        register_harness_profile(key, HARNESS_PROFILE)
        _PROFILE_KEYS_REGISTERED.add(key)
    logger.info("harness profile registered for keys: %s", sorted(candidates))


# ---- 运行输入 / 输出 ----


@dataclass
class RunExecution:
    """单次 run 的执行输入（业务上下文，不含框架对象）。"""

    run: InvestigationRun
    incident: Incident
    asset: Asset
    cancel_checker: Any  # CancelChecker
    events: RunEventWriter
    # D2.1 fencing：绑定后所有业务写入（事件/证据/假设/报告）前校验租约
    lease_fence: LeaseFence | None = None


@dataclass
class RunOutcome:
    """执行结果（业务语义，领域层消费）。"""

    status: RunStatus
    stop_reason: RunStopReason
    usage: dict[str, int]
    report: dict[str, Any] | None = None
    unknown: bool = False
    error: str | None = None
    # D6 收尾：报告生成状态（ok/formatted/repaired/failed）与失败类型——
    # 报告生成失败必须与「真实诊断 unknown」区分，评估与审计分别统计
    report_status: str = REPORT_STATUS_OK
    report_failure: str | None = None
    report_attempts: int = 0


@dataclass
class RuntimeDeps:
    """Worker 启动时构建的运行时依赖（连接器配置在每次 run 前刷新）。"""

    session_factory: async_sessionmaker[AsyncSession]
    settings: Settings
    gateway: ToolGateway
    checkpointer: AsyncPostgresSaver
    pool: AsyncConnectionPool
    model_handle: Any  # ModelHandle（fake 时为测试注入句柄）
    model: BaseChatModel


async def load_connector_configs(
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
) -> dict[str, dict[str, Any]]:
    """（D2 兼容保留）按类型聚合的连接器配置；D3 起优先使用 load_resolution_map。"""
    from app.domains.tools.registry import load_resolution_map as _load  # noqa: PLC0415

    m = await _load(session_factory)
    return {code: instances[0].config for code, instances in m.enabled_by_type.items() if instances}


class AgentRuntime:
    """单主 Agent 执行器（Worker 调用；API 进程不加载本模块）。"""

    def __init__(self, deps: RuntimeDeps) -> None:
        self._deps = deps
        # 报告生成器：只持有 ChatModel（无工具），格式化/修复阶段结构上无法重新取证
        self._report_generator = ReportGenerator(
            deps.model,
            deps.settings,
            allow_formatting=deps.model_handle.provider != "fake",
        )
        _register_harness_profile_for(
            deps.model,
            deps.model_handle.provider,
            deps.model_handle.model_name,
        )

    # ---- Agent 构建（DeepAgents 单核心 + PostgresSaver）----

    def build_agent(self, ctx: RunToolContext, events: RunEventWriter) -> Any:
        tools = self._deps.gateway.build_agent_tools(ctx, events)
        return create_deep_agent(
            model=self._deps.model,
            tools=tools,
            system_prompt=DIAGNOSIS_SYSTEM_PROMPT + "\n\n" + REPORT_FORMAT_PROMPT,
            backend=StateBackend(),
            checkpointer=self._deps.checkpointer,
        )

    # ---- 单次 run 执行 ----

    async def execute_run(self, execution: RunExecution) -> RunOutcome:
        run = execution.run
        settings = self._deps.settings
        budget = _budget_from_run(run, settings)
        ctx, configs = await self._build_context(execution, budget)
        agent = self.build_agent(ctx, execution.events)
        config = {"configurable": {"thread_id": run.id}}
        # D3：主资产绑定的指标模板白名单（query_metrics 可用键，进任务提示）
        metric_templates: list[str] | None = None
        prom_binding = (
            ctx.instance_map.bindings.get((execution.asset.id, "prom-ro"))
            if ctx.instance_map is not None
            else None
        )
        if prom_binding is not None:
            templates = prom_binding.config.get("allowed_templates")
            if isinstance(templates, dict) and templates:
                metric_templates = sorted(templates)
        task_prompt = build_run_task_prompt(
            incident_id=execution.incident.id,
            incident_title=execution.incident.title,
            incident_description=execution.incident.description,
            environment=str(execution.incident.environment),
            primary_asset_id=execution.asset.id,
            asset_name=execution.asset.name,
            asset_type=str(execution.asset.asset_type),
            asset_endpoints=execution.asset.endpoints,
            authorized_assets=sorted(ctx.authorized_asset_ids),
            metric_templates=metric_templates,
        )

        await execution.events.append(
            RunEventType.RUN_STARTED,
            {
                "provider": self._deps.model_handle.provider,
                "model": self._deps.model_handle.model_name,
                # D4：执行快照字段（与创建时 model_config 区分，可审计）
                "prompt_version": settings.agent_prompt_version,
                "rag_mode": settings.rag_mode,
                "connector_instances": sorted(configs),
            },
        )

        final_message: Any = None
        last_evidence_count = 0
        last_round_calls = 0
        stale_rounds = 0
        try:
            async for chunk in agent.astream(
                {"messages": [("user", task_prompt)]}, config, stream_mode="updates"
            ):
                budget.iterations_used += 1
                _accumulate_tokens(chunk, budget)
                last = _extract_last_ai_message(chunk)
                if last is not None:
                    final_message = last
                # 服务端硬限制（Prompt 无法绕过，ADR-8）
                now = datetime.now(UTC)
                if await execution.cancel_checker():
                    raise RunCanceled("cancel requested")
                if budget.deadline_exceeded(now):
                    raise DeadlineExceeded("deadline exceeded")
                if budget.iterations_used > budget.max_iterations:
                    raise IterationBudgetExhausted(
                        f"max_iterations={budget.max_iterations} exhausted"
                    )
                if (budget.tokens_in + budget.tokens_out) > budget.max_tokens:
                    raise TokenBudgetExhausted(f"max_tokens={budget.max_tokens} exhausted")
                # 停止条件：连续 N 个模型轮无新增网关取证调用 → no_new_evidence
                # D3 修正（实证）：DeepAgents 对一批并行工具调用会流式产出多个
                # "tools" chunk（每工具完成一个），且预算计数在调用开始即递增——
                # chunk 级计数会把同批 chunk 误判为空转。故以"模型轮边界"评估：
                # 两个模型轮之间没有发生任何新的网关取证调用 → 该轮无新证据。
                if "tools" not in chunk:
                    if budget.tool_calls_used > last_round_calls:
                        last_round_calls = budget.tool_calls_used
                        last_evidence_count = await self._count_evidence(run.id)
                        stale_rounds = 0
                    elif last_evidence_count > 0:
                        stale_rounds += 1
                        if stale_rounds >= settings.no_new_evidence_rounds:
                            raise _NoNewEvidence()
        except RunCanceled:
            return RunOutcome(
                status=RunStatus.CANCELED,
                stop_reason=RunStopReason.CANCELED,
                usage=budget.as_usage(),
            )
        except LeaseLostError as exc:
            # D2.1 fencing：租约被接管——立即静默退出；终态回写将由 fence 拒绝，
            # 不再产生任何业务写入（含 ERROR 事件）
            logger.warning("run %s aborted: %s", run.id, exc)
            return RunOutcome(
                status=RunStatus.FAILED,
                stop_reason=RunStopReason.ERROR,
                usage=budget.as_usage(),
                error=str(exc)[:500],
            )
        except ToolBudgetExhausted as exc:
            with contextlib.suppress(LeaseLostError):
                await execution.events.append(RunEventType.BUDGET_WARNING, {"reason": str(exc)})
            return RunOutcome(
                status=RunStatus.COMPLETED,
                stop_reason=RunStopReason.BUDGET_EXHAUSTED,
                usage=budget.as_usage(),
                unknown=True,
            )
        except _NoNewEvidence:
            return RunOutcome(
                status=RunStatus.COMPLETED,
                stop_reason=RunStopReason.NO_NEW_EVIDENCE,
                usage=budget.as_usage(),
                unknown=True,
            )
        except Exception as exc:  # noqa: BLE001 — 模型/框架错误统一 failed
            logger.exception("run %s failed", run.id)
            with contextlib.suppress(LeaseLostError):
                await execution.events.append(
                    RunEventType.ERROR, {"error": type(exc).__name__, "detail": str(exc)[:500]}
                )
            return RunOutcome(
                status=RunStatus.FAILED,
                stop_reason=RunStopReason.ERROR,
                usage=budget.as_usage(),
                error=f"{type(exc).__name__}: {exc}"[:500],
            )

        # 正常结束：结构化报告（直读 → 独立格式化 → 一次受控修复 → 明确失败）
        evidence_briefs = await self._evidence_briefs(run.id)
        report_result = await self._report_generator.generate(
            final_message=final_message,
            evidence=evidence_briefs,
            run_id=run.id,
        )
        if report_result.failed:
            logger.warning(
                "run %s report generation failed: kind=%s attempts=%s tail=%.300r",
                run.id,
                report_result.failure_kind,
                report_result.attempts,
                getattr(final_message, "content", None),
            )
            with contextlib.suppress(LeaseLostError):
                await execution.events.append(
                    RunEventType.ERROR,
                    {
                        "error": "ReportGenerationFailed",
                        "detail": str(report_result.failure_kind),
                        "attempts": report_result.attempts,
                    },
                )
        elif report_result.status in (REPORT_STATUS_FORMATTED, REPORT_STATUS_REPAIRED):
            logger.info(
                "run %s report recovered via %s (mode=%s attempts=%s)",
                run.id,
                report_result.status,
                report_result.mode,
                report_result.attempts,
            )
        return await self._finalize_report(execution, report_result, budget)

    # ---- 报告 / 假设 / 证据引用校验 ----

    async def _finalize_report(
        self,
        execution: RunExecution,
        report_result: ReportResult | dict[str, Any] | None,
        budget: RunBudget,
    ) -> RunOutcome:
        run = execution.run
        deps = self._deps
        if not isinstance(report_result, ReportResult):
            # 兼容内部调用/测试直接传入已解析字典；None = 未产出报告（按生成失败处理）
            report_result = ReportResult(
                payload=report_result if isinstance(report_result, dict) else None,
                status=REPORT_STATUS_OK if isinstance(report_result, dict) else "failed",
                failure_kind=None if isinstance(report_result, dict) else None,
            )
        report_data = report_result.payload
        report_failed = report_result.failed
        failure_kind = report_result.failure_kind
        if report_failed and failure_kind is None:
            failure_kind = ReportFailureKind.MODEL_ERROR
        failure_detail = str(report_result.detail.get("message") or "") if report_failed else ""
        if report_data is None:
            # 报告生成失败：**明确**标注为生成失败（不伪装成诊断结论 unknown）
            report_data = {
                "summary": (
                    "报告生成失败：模型未能产出符合 Schema 的结构化报告"
                    f"（{failure_kind}）。原始输出已留档于 run 事件与日志。"
                ),
                "key_findings": [],
                "hypotheses": [],
                "unknown": {
                    "is_unknown": True,
                    "reason": (
                        f"报告生成失败（{failure_kind}）：{failure_detail or '无可用结构化输出'}"
                    ),
                    "missing_information": [
                        "重新发起诊断（模型输出稳定性问题，非诊断结论）",
                        "必要时人工复核本次 run 的证据与模型原始输出",
                    ],
                    "failure_kind": "report_generation_failed",
                },
                "next_manual_steps": ["人工复核本次 run 的实时证据与模型原始输出"],
            }
        unknown_note: dict[str, Any] = dict(report_data.get("unknown") or {})
        is_unknown = bool(unknown_note.get("is_unknown")) or report_failed

        # 证据引用有效性：全部 evidence_ids 必须命中本 run 已落库 Evidence；
        # D3 证据质量四分类：healthy（正常取证）/ fault_observed（成功采集到目标
        # 错误响应，如 HTTP 504——有效故障观测）/ indeterminate（采集超时/连接错误，
        # 不单独支撑根因）/ collection_failed（采集通道问题，不等于业务根因）
        async with deps.session_factory() as session:
            rows = (
                await session.execute(
                    select(
                        Evidence.id,
                        Evidence.status,
                        Evidence.error_code,
                        Evidence.data_ref,
                        Evidence.source_type,
                    ).where(Evidence.run_id == run.id)
                )
            ).all()
        existing = {row.id for row in rows}
        quality_by_id = {
            row.id: classify_evidence(
                row.status, row.error_code, row.data_ref, source_type=row.source_type
            )
            for row in rows
        }
        key_findings = report_data.get("key_findings") or []
        for finding in key_findings:
            refs = finding.get("evidence_ids") or []
            finding["evidence_ids"] = [r for r in refs if r in existing]

        # 假设：supported 必须有 ≥1 条本 run 实时证据（CONTRACTS §2.7）；
        # D3：且至少 1 条 root_cause_capable 证据（healthy 或 fault_observed）——
        # 仅由采集失败/超时证据支撑的假设不构成业务根因，降级为 insufficient_data
        hypotheses: list[dict[str, Any]] = []
        for rank, item in enumerate(report_data.get("hypotheses") or [], start=1):
            supporting = [r for r in (item.get("supporting_evidence_ids") or []) if r in existing]
            contradicting = [
                r for r in (item.get("contradicting_evidence_ids") or []) if r in existing
            ]
            status = str(item.get("status") or HypothesisStatus.CANDIDATE.value)
            demote_reason: str | None = None
            if status == HypothesisStatus.SUPPORTED.value:
                if not supporting:
                    status = HypothesisStatus.INSUFFICIENT_DATA.value
                    demote_reason = "无有效实时证据"
                else:
                    supporting_qualities = [
                        quality_by_id[r] for r in supporting if r in quality_by_id
                    ]
                    if not any(root_cause_capable(q) for q in supporting_qualities):
                        status = HypothesisStatus.INSUFFICIENT_DATA.value
                        if all(q is EvidenceQuality.KNOWLEDGE for q in supporting_qualities):
                            # D4 红线：历史知识只引导排查，绝不单独证明当前根因
                            demote_reason = (
                                "仅有历史知识（source_type=knowledge）支撑——历史相似性"
                                "不构成当前实时根因证据，需本 Run 实时业务观测"
                            )
                        else:
                            demote_reason = "仅有采集失败或不可判读证据，不构成根因支持"
            if is_unknown and status == HypothesisStatus.SUPPORTED.value:
                # unknown 报告不得存在 supported 根因（CONTRACTS §2.16）
                status = HypothesisStatus.INSUFFICIENT_DATA.value
                demote_reason = demote_reason or "unknown 报告禁止 supported 根因"
            try:
                confidence = Confidence(str(item.get("confidence") or Confidence.LOW.value))
            except ValueError:
                confidence = Confidence.LOW
            hypotheses.append(
                {
                    "rank": item.get("rank") or rank,
                    "statement": str(item.get("statement") or "")[:1000],
                    "status": status,
                    "supporting_evidence_ids": supporting,
                    "contradicting_evidence_ids": contradicting,
                    "missing_information": [
                        *list(item.get("missing_information") or []),
                        *([demote_reason] if demote_reason else []),
                    ],
                    "confidence": confidence.value,
                }
            )

        # 落库：hypotheses + diagnostic_reports（版本号递增：接管重跑写 v2+，禁止覆盖）
        # D3 TOCTOU：同事务租约护栏（行锁持有至提交，接管方无法在期间完成 claim）
        # 注意：HYPOTHESIS_ADDED 事件必须在护栏事务之外追加——RunEventWriter 的
        # 护栏会等同一行锁，护栏事务内等待事件写入将自锁死锁（D3 修复）
        hypothesis_ids = [new_id("hyp_") for _ in hypotheses]
        for hyp_id, item in zip(hypothesis_ids, hypotheses, strict=True):
            await execution.events.append(
                RunEventType.HYPOTHESIS_ADDED,
                {
                    "hypothesis_id": hyp_id,
                    "rank": item["rank"],
                    "status": item["status"],
                    "statement": item["statement"][:200],
                },
            )
        async with deps.session_factory() as session:
            if execution.lease_fence is not None:
                guard = cast(
                    "CursorResult[Any]",
                    await session.execute(
                        LEASE_GUARD_SQL,
                        {
                            "run_id": run.id,
                            "fence_token": execution.lease_fence.fence_token,
                            "worker_id": execution.lease_fence.worker_id,
                        },
                    ),
                )
                if guard.rowcount == 0:
                    raise LeaseLostError(f"run {run.id} lease lost before report write")
            next_version = (
                int(
                    await session.scalar(
                        select(func.max(DiagnosticReport.version_no)).where(
                            DiagnosticReport.run_id == run.id
                        )
                    )
                    or 0
                )
                + 1
            )
            for hyp_id, item in zip(hypothesis_ids, hypotheses, strict=True):
                session.add(
                    Hypothesis(
                        id=hyp_id,
                        run_id=run.id,
                        rank=int(item["rank"]),
                        statement=item["statement"],
                        status=item["status"],
                        supporting_evidence_ids=item["supporting_evidence_ids"],
                        contradicting_evidence_ids=item["contradicting_evidence_ids"],
                        missing_information=item["missing_information"],
                        confidence=item["confidence"],
                    )
                )
            report = DiagnosticReport(
                id=new_id("rpt_"),
                run_id=run.id,
                version_no=next_version,
                status="final",
                conclusion={
                    "summary": str(report_data.get("summary") or "")[:2000],
                    "key_findings": key_findings,
                    "next_manual_steps": list(report_data.get("next_manual_steps") or []),
                    # 报告生成状态（评估与审计据此区分「结构化输出失败」与诊断结论）
                    "report_status": report_result.status,
                    "report_failure_kind": str(failure_kind) if report_failed else None,
                },
                hypothesis_ids=hypothesis_ids,
                evidence_refs=sorted(existing),
                unknown_note={
                    "is_unknown": is_unknown,
                    "reason": str(unknown_note.get("reason") or "") if is_unknown else None,
                    "missing_information": list(unknown_note.get("missing_information") or []),
                    "failure_kind": "report_generation_failed" if report_failed else None,
                },
                generator={
                    "provider": self._deps.model_handle.provider,
                    "model": self._deps.model_handle.model_name,
                    "prompt_version": deps.settings.agent_prompt_version,
                    "rag_mode": deps.settings.rag_mode,
                    "report_schema_version": REPORT_SCHEMA_VERSION,
                    "report_status": report_result.status,
                    "report_mode": report_result.mode,
                    "report_attempts": report_result.attempts,
                    "report_failure_kind": str(failure_kind) if report_failed else None,
                    "report_dropped_references": report_result.dropped_references,
                },
                generated_at=datetime.now(UTC),
            )
            session.add(report)
            await session.commit()

        final_status = RunStatus.NEEDS_HUMAN if is_unknown else RunStatus.COMPLETED
        stop_reason = RunStopReason.UNKNOWN_EXIT if is_unknown else RunStopReason.COMPLETED
        return RunOutcome(
            status=final_status,
            stop_reason=stop_reason,
            usage=budget.as_usage(),
            report=report_data,
            unknown=is_unknown,
            report_status=report_result.status,
            report_failure=str(failure_kind) if report_failed else None,
            report_attempts=report_result.attempts,
        )

    # ---- 内部 ----

    async def write_fallback_report(
        self, execution: RunExecution, *, reason: str, evidence_ids: list[str] | None = None
    ) -> None:
        """预算耗尽/无新证据终态时写入 unknown 快照（保证终态有报告，ADR-13）。

        D2.1：版本号递增（避免接管重跑覆盖）；写入前校验租约。
        """
        deps = self._deps
        run = execution.run
        async with deps.session_factory() as session:
            # D3 TOCTOU：同事务租约护栏（行锁持有至提交）
            if execution.lease_fence is not None:
                guard = cast(
                    "CursorResult[Any]",
                    await session.execute(
                        LEASE_GUARD_SQL,
                        {
                            "run_id": run.id,
                            "fence_token": execution.lease_fence.fence_token,
                            "worker_id": execution.lease_fence.worker_id,
                        },
                    ),
                )
                if guard.rowcount == 0:
                    raise LeaseLostError(f"run {run.id} lease lost before fallback report")
            existing = sorted(
                (
                    await session.execute(select(Evidence.id).where(Evidence.run_id == run.id))
                ).scalars()
            )
            next_version = (
                int(
                    await session.scalar(
                        select(func.max(DiagnosticReport.version_no)).where(
                            DiagnosticReport.run_id == run.id
                        )
                    )
                    or 0
                )
                + 1
            )
            report = DiagnosticReport(
                id=new_id("rpt_"),
                run_id=run.id,
                version_no=next_version,
                status="final",
                conclusion={
                    "summary": f"诊断在完成前终止：{reason}",
                    "key_findings": [],
                    "next_manual_steps": ["重新发起诊断或人工排查"],
                },
                hypothesis_ids=[],
                evidence_refs=existing if evidence_ids is None else evidence_ids,
                unknown_note={
                    "is_unknown": True,
                    "reason": reason,
                    "missing_information": ["诊断未完成，证据不足"],
                },
                generator={
                    "provider": self._deps.model_handle.provider,
                    "model": self._deps.model_handle.model_name,
                    "prompt_version": deps.settings.agent_prompt_version,
                    "rag_mode": deps.settings.rag_mode,
                },
                generated_at=datetime.now(UTC),
            )
            session.add(report)
            await session.commit()

    async def _build_context(
        self, execution: RunExecution, budget: RunBudget
    ) -> tuple[RunToolContext, dict[str, dict[str, Any]]]:
        deps = self._deps
        snapshot: dict[str, Any] = dict(execution.run.authorization_snapshot or {})
        instance_map = await load_resolution_map(deps.session_factory)
        ctx = RunToolContext(
            run_id=execution.run.id,
            environment=str(execution.incident.environment),
            primary_asset_id=execution.incident.primary_asset_id,
            is_admin=bool(snapshot.get("is_admin")),
            authorized_asset_ids=frozenset(snapshot.get("asset_ids") or []),
            authorized_environments=frozenset(snapshot.get("environments") or []),
            budget=budget,
            cancel_checker=execution.cancel_checker,
            connector_configs={},
            all_assets=bool(snapshot.get("all_assets")),
            instance_map=instance_map,
            rag_mode=deps.settings.rag_mode,
            lease_fence=execution.lease_fence,
        )
        configs = {
            code: items[0].config for code, items in instance_map.enabled_by_type.items() if items
        }
        return ctx, configs

    async def _count_evidence(self, run_id: str) -> int:

        async with self._deps.session_factory() as session:
            count = await session.scalar(
                select(func.count()).select_from(Evidence).where(Evidence.run_id == run_id)
            )
            return int(count or 0)

    async def _evidence_briefs(self, run_id: str) -> list[EvidenceBrief]:
        """报告格式化阶段的证据清单（只读摘要；原始日志不进提示词）。"""
        async with self._deps.session_factory() as session:
            rows = (
                await session.execute(
                    select(
                        Evidence.id,
                        Evidence.source_type,
                        Evidence.status,
                        Evidence.error_code,
                        Evidence.summary_redacted,
                    ).where(Evidence.run_id == run_id)
                )
            ).all()
        return [
            EvidenceBrief(
                evidence_id=row.id,
                source_type=str(row.source_type or ""),
                status=str(row.status or ""),
                error_code=str(row.error_code) if row.error_code is not None else None,
                summary=str(row.summary_redacted or "")[:400],
            )
            for row in rows
        ]


class _NoNewEvidence(Exception):
    """连续 N 轮无新证据（停止条件）。"""


# ---- 纯函数辅助 ----


def _as_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _budget_from_run(run: InvestigationRun, settings: Settings) -> RunBudget:
    budgets: dict[str, Any] = dict(run.budgets or {})
    deadline_raw = budgets.get("deadline_at")
    deadline: datetime | None = None
    if isinstance(deadline_raw, datetime):
        deadline = deadline_raw
    elif isinstance(deadline_raw, str):
        try:
            deadline = datetime.fromisoformat(deadline_raw.replace("Z", "+00:00"))
        except ValueError:
            deadline = None
    if deadline is None:
        from datetime import timedelta  # noqa: PLC0415

        deadline = datetime.now(UTC) + timedelta(seconds=settings.budget_deadline_seconds)
    return RunBudget(
        max_tool_calls=_as_int(budgets.get("max_tool_calls"), settings.budget_max_tool_calls),
        max_iterations=_as_int(budgets.get("max_iterations"), settings.budget_max_iterations),
        max_tokens=_as_int(budgets.get("max_tokens"), settings.budget_max_tokens),
        deadline_at=deadline,
    )


def _accumulate_tokens(chunk: dict[str, Any], budget: RunBudget) -> None:
    """从 stream updates 的 AI 消息 usage_metadata 累计 token 用量。"""
    for update in chunk.values():
        if not isinstance(update, dict):
            continue
        for message in update.get("messages") or []:
            usage = getattr(message, "usage_metadata", None)
            if not usage:
                continue
            budget.tokens_in += _as_int(usage.get("input_tokens"), 0)
            budget.tokens_out += _as_int(usage.get("output_tokens"), 0)


def _extract_last_ai_message(chunk: dict[str, Any]) -> Any:
    for update in chunk.values():
        if not isinstance(update, dict):
            continue
        messages = update.get("messages") or []
        for message in reversed(messages):
            if getattr(message, "type", "") == "ai":
                return message
    return None


_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$")


def _parse_report(message: Any) -> dict[str, Any] | None:
    """从最终 AI 消息解析报告 JSON（容忍代码围栏与前后文本）。"""
    if message is None:
        return None
    content = getattr(message, "content", None)
    if not isinstance(content, str) or not content.strip():
        return None
    text = content.strip()
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if match is None:
        return None
    candidate = _FENCE_RE.sub("", match.group(0)).strip()
    try:
        data = json.loads(candidate)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    return data
