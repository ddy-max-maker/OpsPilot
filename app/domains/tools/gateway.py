"""Tool Gateway（Worker 进程内强制调用层，ARCHITECTURE §3.5 / CONTRACTS §6）。

红线实现：
- Agent 可触达的工具句柄只能由本网关生成（build_agent_tools）；
- 每次调用：预算计数 → 取消检查 → manifest 启用态 → 参数 Pydantic 校验 →
  授权快照白名单 → 连接器限流 → 超时受控调用 → Evidence 归一化落库 →
  run_events（tool_called/tool_returned/evidence_added）+ audit_events；
- 返回给模型的是 {evidence_id, status, summary_redacted, correlation, truncated}，
  原始载荷不进 Prompt（ADR-7）。
"""

from __future__ import annotations

import os
import time
from datetime import UTC, datetime
from typing import Any, TypeVar, cast

from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.core.enums import (
    ActorType,
    EvidenceStatus,
    RunEventType,
)
from app.core.errors import validation_error
from app.core.ids import new_id
from app.domains.audit import service as audit_service
from app.domains.tools import connectors
from app.domains.tools.connectors.base import ConnectorResult
from app.domains.tools.context import (
    LEASE_GUARD_SQL,
    LeaseLostError,
    RunEventWriter,
    RunToolContext,
    ToolBudgetExhausted,
)
from app.domains.tools.manifests import (
    ALL_CAPABILITY_KEYS,
    BUILTIN_MANIFESTS,
    CapabilitySpec,
)
from app.domains.tools.redaction import redact_payload, truncate_text
from app.domains.tools.registry import ConnectorResolutionError, ResolvedInstance
from app.models import Asset, Evidence

# ---- 工具输入 Schema（Pydantic 校验；additionalProperties=false 语义）----


class AssetScopedInput(BaseModel):
    """全部 capability 的公共入参：asset_id 必填（授权校验主体）。"""

    model_config = {"extra": "forbid"}

    asset_id: str = Field(min_length=1, max_length=64)


class ServiceHealthInput(AssetScopedInput):
    pass


class RedisHealthInput(AssetScopedInput):
    pass


class ContainerStatusInput(AssetScopedInput):
    pass


class ContainerLogsInput(AssetScopedInput):
    tail_limit: int = Field(default=100, ge=1, le=500)


class MetricsQueryInput(AssetScopedInput):
    """受控指标查询：template 必须是实例配置 allowed_templates 的键（非原始 PromQL）。"""

    template: str = Field(min_length=1, max_length=100)
    time_range: dict[str, str] | None = None


class TracesQueryInput(AssetScopedInput):
    trace_id: str | None = Field(default=None, pattern=r"^[0-9a-f]{8,64}$")
    time_range: dict[str, str] | None = None


class MySqlDiagnosticsInput(AssetScopedInput):
    pass


INPUT_MODELS: dict[str, type[BaseModel]] = {
    "get_service_health": ServiceHealthInput,
    "check_redis_health": RedisHealthInput,
    "get_container_status": ContainerStatusInput,
    "read_container_logs": ContainerLogsInput,
    "query_metrics": MetricsQueryInput,
    "query_traces": TracesQueryInput,
    "read_mysql_diagnostics": MySqlDiagnosticsInput,
}


class ToolGateway:
    """按 manifest 生成受控只读工具；每个 run 绑定独立上下文。"""

    # D3 TOCTOU 修复：同事务租约护栏（行锁持有至提交；失效 → rowcount=0）
    _LEASE_GUARD_SQL = LEASE_GUARD_SQL

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        settings: Settings,
    ) -> None:
        self._session_factory = session_factory
        self._settings = settings

    # ---- 对 Agent 暴露的工具工厂 ----

    def build_agent_tools(
        self, ctx: RunToolContext, events: RunEventWriter
    ) -> list[StructuredTool]:
        tools: list[StructuredTool] = []
        for key, spec in ALL_CAPABILITY_KEYS.items():
            tools.append(self._build_tool(key, spec, ctx, events))
        return tools

    def _build_tool(
        self,
        key: str,
        spec: CapabilitySpec,
        ctx: RunToolContext,
        events: RunEventWriter,
    ) -> StructuredTool:
        input_model = INPUT_MODELS[key]

        async def _invoke(**kwargs: Any) -> dict[str, Any]:
            return await self.execute_capability(ctx, events, key, spec, kwargs)

        return StructuredTool.from_function(
            coroutine=_invoke,
            name=key,
            description=spec.description,
            args_schema=input_model,
        )

    # ---- 单次能力执行（CONTRACTS §6 通用行为 1–5）----

    async def execute_capability(
        self,
        ctx: RunToolContext,
        events: RunEventWriter,
        key: str,
        spec: CapabilitySpec,
        raw_params: dict[str, Any],
    ) -> dict[str, Any]:
        manifest = BUILTIN_MANIFESTS[spec.connector_code]
        started = time.monotonic()

        # 1) 预算 + 取消（服务端硬限制；Prompt 无法绕过）
        try:
            await ctx.check_tool_budget()
        except ToolBudgetExhausted as exc:
            await events.append(
                RunEventType.BUDGET_WARNING, {"capability": key, "reason": str(exc)}
            )
            raise

        # 2) 参数 Pydantic 校验（extra=forbid）
        try:
            # 全部 INPUT_MODELS 均继承 AssetScopedInput（asset_id 为授权校验主体）
            params = cast(AssetScopedInput, input_validate(INPUT_MODELS[key], raw_params))
        except Exception as exc:  # noqa: BLE001
            await events.append(
                RunEventType.ERROR,
                {"capability": key, "error": "VALIDATION_ERROR", "detail": str(exc)[:300]},
            )
            return {"error": "VALIDATION_ERROR", "detail": str(exc)[:300]}

        # 3) 资产存在性 + 授权快照（默认拒绝）
        requested_asset_id = str(params.asset_id)
        async with self._session_factory() as session:
            asset = await session.get(Asset, requested_asset_id)
        if asset is None:
            await events.append(RunEventType.ERROR, {"capability": key, "error": "ASSET_NOT_FOUND"})
            return {"error": "ASSET_NOT_FOUND", "asset_id": requested_asset_id}
        if not ctx.asset_authorized(asset.id, str(asset.environment)):
            await self._audit(ctx, key, asset, "denied", started)
            await events.append(
                RunEventType.ERROR,
                {
                    "capability": key,
                    "error": "ASSET_NOT_AUTHORIZED",
                    "asset_id": asset.id,
                },
            )
            return {"error": "ASSET_NOT_AUTHORIZED", "asset_id": asset.id}

        # 4) manifest 范围（环境/资产类型）
        if str(asset.environment) not in manifest.environments:
            await events.append(
                RunEventType.ERROR,
                {"capability": key, "error": "ENVIRONMENT_OUT_OF_MANIFEST_SCOPE"},
            )
            return {"error": "ENVIRONMENT_OUT_OF_MANIFEST_SCOPE", "asset_id": asset.id}
        if manifest.asset_types and str(asset.asset_type) not in manifest.asset_types:
            await events.append(
                RunEventType.ERROR,
                {"capability": key, "error": "ASSET_TYPE_OUT_OF_MANIFEST_SCOPE"},
            )
            return {"error": "ASSET_TYPE_OUT_OF_MANIFEST_SCOPE", "asset_id": asset.id}

        # 5) 连接器限流（max_calls_per_run）
        try:
            ctx.consume_connector_rate(spec.connector_code, manifest.rate_limit_per_run)
        except ToolBudgetExhausted as exc:
            await events.append(
                RunEventType.BUDGET_WARNING, {"capability": key, "reason": str(exc)}
            )
            return {"error": "RATE_LIMITED", "detail": str(exc)}

        # 6) tool_called 事件 → 连接器调用 → Evidence 落库 → tool_returned/evidence_added
        await events.append(
            RunEventType.TOOL_CALLED,
            {
                "capability": key,
                "collector": spec.collector,
                "asset_id": asset.id,
                "params": params.model_dump(),
            },
        )
        resolved: ResolvedInstance | None
        try:
            resolved = self._resolve_instance(ctx, spec.connector_code, asset)
            result = await self._invoke_connector(ctx, spec, asset, params, resolved)
        except _InstanceResolutionFailure as exc:
            # 多实例未绑定/未配置 → 明确失败的 unavailable 证据（不猜测凭据）
            resolved = None
            result = ConnectorResult(
                status=EvidenceStatus.UNAVAILABLE,
                payload={"reason": exc.message},
                error_code=exc.code,
            )
        evidence_id, evidence_summary = await self._persist_evidence(
            ctx, spec, asset, result, manifest.result_max_bytes, resolved
        )
        latency_ms = int((time.monotonic() - started) * 1000)
        await events.append(
            RunEventType.TOOL_RETURNED,
            {
                "capability": key,
                "status": result.status.value,
                "evidence_id": evidence_id,
                "latency_ms": latency_ms,
            },
        )
        await events.append(
            RunEventType.EVIDENCE_ADDED,
            {
                "evidence_id": evidence_id,
                "source_type": spec.source_type.value,
                "collector": spec.collector,
                "asset_id": asset.id,
            },
        )
        await self._audit(ctx, key, asset, result.status.value, started, evidence_id)
        return {
            "evidence_id": evidence_id,
            "status": result.status.value,
            "summary_redacted": evidence_summary,
            "correlation": {"service": asset.name, "asset_id": asset.id},
        }

    # ---- 连接器分发（仅白名单 capability）----

    def _resolve_instance(
        self, ctx: RunToolContext, connector_code: str, asset: Asset
    ) -> ResolvedInstance | None:
        """按（授权资产, 连接器类型）解析实例（D3：修复同类多实例覆盖串用）。

        instance_map（Registry 解析表）优先；未提供时回退 D2 的按类型配置
        （兼容旧调用方/测试）。解析失败映射为 unavailable Evidence。
        """
        if ctx.instance_map is not None:
            try:
                return cast(ResolvedInstance, ctx.instance_map.resolve(connector_code, asset.id))
            except ConnectorResolutionError as exc:
                raise _InstanceResolutionFailure(exc.code, str(exc)) from exc
        config = ctx.connector_configs.get(connector_code)
        if config is None:
            return None
        return ResolvedInstance(
            instance_id=f"type:{connector_code}", connector_code=connector_code, config=config
        )

    async def _invoke_connector(
        self,
        ctx: RunToolContext,
        spec: CapabilitySpec,
        asset: Asset,
        params: BaseModel,
        resolved: ResolvedInstance | None,
    ) -> ConnectorResult:
        if resolved is None:
            return ConnectorResult(
                status=EvidenceStatus.UNAVAILABLE,
                payload={"reason": "connector instance not configured"},
                error_code="CONNECTOR_NOT_CONFIGURED",
            )
        config = resolved.config
        timeout = float(config.get("timeout_seconds", self._settings.connector_timeout_seconds))
        if spec.key == "get_service_health":
            health_url = (asset.endpoints or {}).get("health_url")
            if not health_url:
                return ConnectorResult(
                    status=EvidenceStatus.UNAVAILABLE,
                    payload={"reason": "asset has no registered health_url"},
                    error_code="HEALTH_URL_NOT_REGISTERED",
                )
            return await connectors.http_health.get_service_health(
                health_url=str(health_url), timeout_seconds=timeout
            )
        if spec.key == "check_redis_health":
            return await connectors.redis_health.check_redis_health(
                dsn=str(config.get("dsn", "")), timeout_seconds=timeout
            )
        if spec.key in ("get_container_status", "read_container_logs"):
            base_url = str(config.get("base_url", ""))
            container_name = str(config.get("container_prefix", "")) + asset.name
            container_id = await connectors.docker_proxy.find_container_id(
                base_url=base_url, container_name=container_name, timeout_seconds=timeout
            )
            if container_id is None:
                return ConnectorResult(
                    status=EvidenceStatus.UNAVAILABLE,
                    payload={"reason": f"container '{container_name}' not found"},
                    error_code="CONTAINER_NOT_FOUND",
                )
            if spec.key == "get_container_status":
                return await connectors.docker_proxy.get_container_status(
                    base_url=base_url, container_id=container_id, timeout_seconds=timeout
                )
            logs_params: ContainerLogsInput = params  # type: ignore[assignment]
            return await connectors.docker_proxy.read_container_logs(
                base_url=base_url,
                container_id=container_id,
                tail_limit=logs_params.tail_limit,
                timeout_seconds=timeout,
            )
        if spec.key == "query_metrics":
            mq: MetricsQueryInput = params  # type: ignore[assignment]
            return await self._query_metrics(asset, resolved, mq, timeout)
        if spec.key == "query_traces":
            tq: TracesQueryInput = params  # type: ignore[assignment]
            return await self._query_traces(asset, resolved, tq, timeout)
        if spec.key == "read_mysql_diagnostics":
            return await self._mysql_diagnostics(asset, resolved, timeout)
        return ConnectorResult(
            status=EvidenceStatus.UNAVAILABLE,
            payload={"reason": "capability not implemented"},
            error_code="NOT_IMPLEMENTED",
        )

    # ---- D3 可观测性 / MySQL 受控查询 ----

    async def _query_metrics(
        self, asset: Asset, resolved: ResolvedInstance, mq: MetricsQueryInput, timeout: float
    ) -> ConnectorResult:
        """受控 PromQL：模板白名单 + 服务标签强制注入（不可查询其他服务）。"""
        base_url = str(resolved.config.get("base_url", "")).strip()
        if not base_url:
            return ConnectorResult(
                status=EvidenceStatus.UNAVAILABLE,
                payload={"reason": "prometheus base_url not configured"},
                error_code="PROM_NOT_CONFIGURED",
            )
        templates = dict(resolved.config.get("allowed_templates") or {})
        template = templates.get(mq.template)
        if template is None:
            return ConnectorResult(
                status=EvidenceStatus.UNAVAILABLE,
                payload={
                    "reason": f"metric template '{mq.template}' not in instance allowlist",
                    "allowed": sorted(templates),
                },
                error_code="METRIC_TEMPLATE_NOT_ALLOWED",
            )
        service_label = str(resolved.config.get("service") or asset.name)
        query = template.replace("{service}", service_label)
        return await connectors.prometheus.query_metrics(
            base_url=base_url,
            query=query,
            time_range=mq.time_range,
            timeout_seconds=timeout,
        )

    async def _query_traces(
        self, asset: Asset, resolved: ResolvedInstance, tq: TracesQueryInput, timeout: float
    ) -> ConnectorResult:
        base_url = str(resolved.config.get("base_url", "")).strip()
        if not base_url:
            return ConnectorResult(
                status=EvidenceStatus.UNAVAILABLE,
                payload={"reason": "tracing backend base_url not configured"},
                error_code="OTEL_NOT_CONFIGURED",
            )
        service_label = str(resolved.config.get("service") or asset.name)
        return await connectors.jaeger.query_traces(
            base_url=base_url,
            service=service_label,
            trace_id=tq.trace_id,
            time_range=tq.time_range,
            timeout_seconds=timeout,
        )

    async def _mysql_diagnostics(
        self, asset: Asset, resolved: ResolvedInstance, timeout: float
    ) -> ConnectorResult:
        """MySQL 只读诊断：预定义查询 + 最小权限账号；密码经环境变量解析。"""
        host = str(resolved.config.get("host", "")).strip()
        if not host:
            return ConnectorResult(
                status=EvidenceStatus.UNAVAILABLE,
                payload={"reason": "mysql host not configured"},
                error_code="MYSQL_NOT_CONFIGURED",
            )
        password_env = str(resolved.config.get("password_env", "")).strip()
        password = os.environ.get(password_env, "") if password_env else ""
        if not password:
            return ConnectorResult(
                status=EvidenceStatus.UNAVAILABLE,
                payload={"reason": "mysql password secret not configured"},
                error_code="SECRET_NOT_CONFIGURED",
            )
        return await connectors.mysql_diagnostics.read_mysql_diagnostics(
            host=host,
            port=int(resolved.config.get("port", 3306)),
            user=str(resolved.config.get("user", "")),
            password=password,
            database=resolved.config.get("database") and str(resolved.config["database"]) or None,
            timeout_seconds=timeout,
        )

    # ---- Evidence 归一化与持久化（只追加）----

    async def _persist_evidence(
        self,
        ctx: RunToolContext,
        spec: CapabilitySpec,
        asset: Asset,
        result: ConnectorResult,
        result_max_bytes: int,
        resolved: ResolvedInstance | None = None,
    ) -> tuple[str, str]:
        summary_source = _build_summary(spec, result)
        summary, summary_truncated = truncate_text(
            summary_source, self._settings.evidence_summary_max_bytes
        )
        data_ref, payload_truncated = redact_payload(
            result.payload,
            min(self._settings.evidence_payload_max_bytes, result_max_bytes),
        )
        evidence_id = new_id("ev_")
        correlation = {"service": asset.name}
        if resolved is not None:
            # 审计追溯：本次取证使用了哪个连接器实例（多实例隔离证据）
            correlation["instance_id"] = resolved.instance_id
        async with self._session_factory() as session:
            # D3 TOCTOU 修复：租约校验与写入同一事务（行锁持有至提交，
            # 接管方在 commit 前无法完成 claim，杜绝"校验后失效仍写入"窗口）
            if ctx.lease_fence is not None:
                guard = cast(
                    "CursorResult[Any]",
                    await session.execute(
                        self._LEASE_GUARD_SQL,
                        {
                            "run_id": ctx.run_id,
                            "fence_token": ctx.lease_fence.fence_token,
                            "worker_id": ctx.lease_fence.worker_id,
                        },
                    ),
                )
                if guard.rowcount == 0:
                    raise LeaseLostError(f"run {ctx.run_id} lease lost before evidence write")
            session.add(
                Evidence(
                    id=evidence_id,
                    run_id=ctx.run_id,
                    asset_id=asset.id,
                    environment=str(asset.environment),
                    source_type=spec.source_type.value,
                    collector=spec.collector,
                    observed_at=datetime.now(UTC),
                    status=result.status.value,
                    data_ref=data_ref,
                    summary_redacted=summary,
                    correlation=correlation,
                    error_code=result.error_code,
                    truncated=summary_truncated or payload_truncated,
                )
            )
            await session.commit()
        return evidence_id, summary

    async def _audit(
        self,
        ctx: RunToolContext,
        capability: str,
        asset: Asset,
        outcome: str,
        started: float,
        evidence_id: str | None = None,
    ) -> None:
        async with self._session_factory() as session:
            await audit_service.record(
                session,
                actor_type=ActorType.AGENT.value,
                actor_id=f"run:{ctx.run_id}",
                action="tool.invoke",
                resource_type="capability",
                resource_id=capability,
                environment=str(asset.environment),
                detail={
                    "run_id": ctx.run_id,
                    "asset_id": asset.id,
                    "outcome": outcome,
                    "latency_ms": int((time.monotonic() - started) * 1000),
                    **({"evidence_id": evidence_id} if evidence_id else {}),
                },
            )
            await session.commit()


class _InstanceResolutionFailure(Exception):
    """实例解析失败 → 映射为 unavailable Evidence（携带稳定 error_code）。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _build_summary(spec: CapabilitySpec, result: ConnectorResult) -> str:
    """自然语言摘要（进 Prompt 的唯一内容）；失败语义不伪装为空数据。"""
    if result.status is EvidenceStatus.OK:
        parts = [
            f"{k}={result.payload[k]}"
            for k in sorted(result.payload)
            if result.payload[k] is not None
        ]
        return f"{spec.collector} OK: " + ", ".join(parts)
    detail = result.payload.get("reason") or result.payload.get("error_type") or ""
    base = f"{spec.collector} {result.status.value}"
    if result.error_code:
        base += f" ({result.error_code})"
    return f"{base}: {detail}" if detail else base


_T = TypeVar("_T", bound=BaseModel)


def input_validate(model: type[_T], raw: dict[str, Any]) -> _T:
    try:
        return model.model_validate(raw)
    except Exception as exc:  # noqa: BLE001
        raise validation_error(str(exc)) from exc
