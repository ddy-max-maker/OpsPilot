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

import time
from datetime import UTC, datetime
from typing import Any, TypeVar, cast

from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field
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
from app.domains.tools.context import RunEventWriter, RunToolContext, ToolBudgetExhausted
from app.domains.tools.manifests import (
    ALL_CAPABILITY_KEYS,
    BUILTIN_MANIFESTS,
    CapabilitySpec,
)
from app.domains.tools.redaction import redact_payload, truncate_text
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


INPUT_MODELS: dict[str, type[BaseModel]] = {
    "get_service_health": ServiceHealthInput,
    "check_redis_health": RedisHealthInput,
    "get_container_status": ContainerStatusInput,
    "read_container_logs": ContainerLogsInput,
}


class ToolGateway:
    """按 manifest 生成受控只读工具；每个 run 绑定独立上下文。"""

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
        result = await self._invoke_connector(ctx, spec, asset, params)
        evidence_id, evidence_summary = await self._persist_evidence(
            ctx, spec, asset, result, manifest.result_max_bytes
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

    async def _invoke_connector(
        self,
        ctx: RunToolContext,
        spec: CapabilitySpec,
        asset: Asset,
        params: BaseModel,
    ) -> ConnectorResult:
        config = ctx.connector_configs.get(spec.connector_code)
        if config is None:
            return ConnectorResult(
                status=EvidenceStatus.UNAVAILABLE,
                payload={"reason": "connector instance not configured"},
                error_code="CONNECTOR_NOT_CONFIGURED",
            )
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
        return ConnectorResult(
            status=EvidenceStatus.UNAVAILABLE,
            payload={"reason": "capability not implemented"},
            error_code="NOT_IMPLEMENTED",
        )

    # ---- Evidence 归一化与持久化（只追加）----

    async def _persist_evidence(
        self,
        ctx: RunToolContext,
        spec: CapabilitySpec,
        asset: Asset,
        result: ConnectorResult,
        result_max_bytes: int,
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
        # D2.1 fencing：旧 Worker 失去租约后禁止证据写入
        await ctx.assert_lease()
        async with self._session_factory() as session:
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
                    correlation={"service": asset.name},
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
