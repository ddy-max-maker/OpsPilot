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
import re
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
from app.domains.rag.retrieval import search_knowledge, search_runbooks
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
    ConnectorManifest,
)
from app.domains.tools.redaction import detect_injection, redact_payload, truncate_text
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


class RetrieveIncidentsInput(BaseModel):
    """历史事故/经验知识检索（asset_id 可选提示；授权过滤以 Run 快照为准）。"""

    model_config = {"extra": "forbid"}

    query: str = Field(min_length=1, max_length=500)
    asset_id: str | None = Field(default=None, max_length=64)
    limit: int = Field(default=3, ge=1, le=5)


class SearchRunbooksInput(BaseModel):
    """已发布 Runbook 检索（草稿/退役版本不可见；无执行授权）。"""

    model_config = {"extra": "forbid"}

    query: str = Field(min_length=1, max_length=500)
    environment: str | None = Field(default=None, pattern=r"^(sandbox|staging|production)$")
    limit: int = Field(default=3, ge=1, le=5)


KNOWLEDGE_CAPABILITY_KEYS = frozenset({"retrieve_incidents", "search_runbooks"})

INPUT_MODELS: dict[str, type[BaseModel]] = {
    "get_service_health": ServiceHealthInput,
    "check_redis_health": RedisHealthInput,
    "get_container_status": ContainerStatusInput,
    "read_container_logs": ContainerLogsInput,
    "query_metrics": MetricsQueryInput,
    "query_traces": TracesQueryInput,
    "read_mysql_diagnostics": MySqlDiagnosticsInput,
    "retrieve_incidents": RetrieveIncidentsInput,
    "search_runbooks": SearchRunbooksInput,
}


def resolve_container_name(
    *,
    endpoints: dict[str, Any] | None,
    instance_config: dict[str, Any],
    asset_name: str,
) -> str:
    """解析工具要观测的容器名（D7 修复）。

    容器身份以**资产登记**为唯一权威（与 D6 动作执行门禁同一规则）：
    `assets.endpoints.container_name` 优先；仅在未登记时回退到历史推导
    「实例 container_prefix + 资产名」（兼容 D2 早期种子）。

    历史实现只有前缀推导：资产名若自带业务前缀（如 `demo-redis`）会得到
    双前缀容器名 `opspilot-demo-demo-redis`，导致 docker.inspect / docker.logs
    恒判 CONTAINER_NOT_FOUND——证据侧把"看不见"误当成"没问题"。
    """
    registered = str((endpoints or {}).get("container_name") or "").strip()
    if registered:
        return registered
    return str(instance_config.get("container_prefix", "")) + asset_name


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
        # D4：Embedding Provider 惰性构建（fake 零依赖；local_e5 首次 hybrid 调用时加载）
        self._embedding_provider: Any | None = None

    def _get_embedding_provider(self) -> Any:
        if self._embedding_provider is None:
            from app.domains.rag.embeddings import build_embedding_provider  # noqa: PLC0415

            self._embedding_provider = build_embedding_provider(self._settings)
        return self._embedding_provider

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
            params = input_validate(INPUT_MODELS[key], raw_params)
        except Exception as exc:  # noqa: BLE001
            await events.append(
                RunEventType.ERROR,
                {"capability": key, "error": "VALIDATION_ERROR", "detail": str(exc)[:300]},
            )
            return {"error": "VALIDATION_ERROR", "detail": str(exc)[:300]}

        # D4：知识检索能力（授权过滤以 Run 授权快照资产集合为准，非单资产校验）
        if key in KNOWLEDGE_CAPABILITY_KEYS:
            return await self._execute_knowledge_capability(
                ctx, events, key, spec, params, manifest, started
            )

        # 3) 资产存在性 + 授权快照（默认拒绝）
        requested_asset_id = str(cast(AssetScopedInput, params).asset_id)
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
            container_name = resolve_container_name(
                endpoints=asset.endpoints, instance_config=config, asset_name=asset.name
            )
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
            return await self._query_metrics(ctx, asset, resolved, mq, timeout)
        if spec.key == "query_traces":
            tq: TracesQueryInput = params  # type: ignore[assignment]
            return await self._query_traces(ctx, asset, resolved, tq, timeout)
        if spec.key == "read_mysql_diagnostics":
            return await self._mysql_diagnostics(asset, resolved, timeout)
        return ConnectorResult(
            status=EvidenceStatus.UNAVAILABLE,
            payload={"reason": "capability not implemented"},
            error_code="NOT_IMPLEMENTED",
        )

    # ---- D3 可观测性 / MySQL 受控查询 ----

    _SERVICE_LABEL_RE = re.compile(r"^[A-Za-z0-9_.:\-]{1,200}$")
    _TEMPLATE_PLACEHOLDER_RE = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")

    def _service_label(self, ctx: RunToolContext, asset: Asset, resolved: ResolvedInstance) -> str:
        """解析服务标签（D4 安全修复：标签必须绑定当前授权资产）。

        - binding 路径：服务标签来自资产绑定 config（资产级作用域），缺省用资产名；
        - default 路径：实例级 service 默认值仅在该实例未被任何 service 绑定
          共用时允许——共享实例 + 未绑定资产 + 实例默认服务 → 拒绝
          （SERVICE_BINDING_REQUIRED，防止错误映射到其他服务）；
        - 标签值白名单校验（防 PromQL 注入：引号/花括号/换行等）。
        """
        raw: str | None
        if resolved.source == "binding":
            raw = resolved.config.get("service")
            label = str(raw) if raw else asset.name
        else:
            instance_default = resolved.config.get("service")
            if (
                instance_default
                and ctx.instance_map is not None
                and resolved.instance_id in ctx.instance_map.service_bound_instances
            ):
                raise _InstanceResolutionFailure(
                    "SERVICE_BINDING_REQUIRED",
                    f"instance '{resolved.instance_id}' is shared by service-bound assets; "
                    f"asset '{asset.id}' must have its own binding (refusing instance "
                    f"default service mapping)",
                )
            label = str(instance_default) if instance_default else asset.name
        if not self._SERVICE_LABEL_RE.match(label):
            raise _InstanceResolutionFailure(
                "SERVICE_LABEL_UNSAFE",
                f"service label {label[:40]!r} contains characters outside the allowlist",
            )
        return label

    def _validated_template(self, resolved: ResolvedInstance, template_key: str) -> str:
        """模板白名单展开 + 占位符校验（仅允许 {service}，防注入其他标签值）。"""
        templates = dict(resolved.config.get("allowed_templates") or {})
        template = templates.get(template_key)
        if template is None:
            return ""
        placeholders = set(self._TEMPLATE_PLACEHOLDER_RE.findall(str(template)))
        unknown = placeholders - {"service"}
        if unknown:
            raise _InstanceResolutionFailure(
                "METRIC_TEMPLATE_INVALID",
                f"template '{template_key}' has unsupported placeholders: {sorted(unknown)}",
            )
        return str(template)

    async def _query_metrics(
        self,
        ctx: RunToolContext,
        asset: Asset,
        resolved: ResolvedInstance,
        mq: MetricsQueryInput,
        timeout: float,
    ) -> ConnectorResult:
        """受控 PromQL：模板白名单 + 占位符校验 + 服务标签强制绑定授权资产。"""
        base_url = str(resolved.config.get("base_url", "")).strip()
        if not base_url:
            return ConnectorResult(
                status=EvidenceStatus.UNAVAILABLE,
                payload={"reason": "prometheus base_url not configured"},
                error_code="PROM_NOT_CONFIGURED",
            )
        template = self._validated_template(resolved, mq.template)
        if not template:
            return ConnectorResult(
                status=EvidenceStatus.UNAVAILABLE,
                payload={
                    "reason": f"metric template '{mq.template}' not in instance allowlist",
                    "allowed": sorted(resolved.config.get("allowed_templates") or {}),
                },
                error_code="METRIC_TEMPLATE_NOT_ALLOWED",
            )
        try:
            service_label = self._service_label(ctx, asset, resolved)
        except _InstanceResolutionFailure as exc:
            return ConnectorResult(
                status=EvidenceStatus.UNAVAILABLE,
                payload={"reason": exc.message},
                error_code=exc.code,
            )
        query = template.replace("{service}", service_label)
        return await connectors.prometheus.query_metrics(
            base_url=base_url,
            query=query,
            time_range=mq.time_range,
            timeout_seconds=timeout,
        )

    async def _query_traces(
        self,
        ctx: RunToolContext,
        asset: Asset,
        resolved: ResolvedInstance,
        tq: TracesQueryInput,
        timeout: float,
    ) -> ConnectorResult:
        base_url = str(resolved.config.get("base_url", "")).strip()
        if not base_url:
            return ConnectorResult(
                status=EvidenceStatus.UNAVAILABLE,
                payload={"reason": "tracing backend base_url not configured"},
                error_code="OTEL_NOT_CONFIGURED",
            )
        try:
            service_label = self._service_label(ctx, asset, resolved)
        except _InstanceResolutionFailure as exc:
            return ConnectorResult(
                status=EvidenceStatus.UNAVAILABLE,
                payload={"reason": exc.message},
                error_code=exc.code,
            )
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

    # ---- D4 知识检索能力（RAG；权限前置过滤 + retrieval_events）----

    async def _execute_knowledge_capability(
        self,
        ctx: RunToolContext,
        events: RunEventWriter,
        key: str,
        spec: CapabilitySpec,
        params: BaseModel,
        manifest: ConnectorManifest,
        started: float,
    ) -> dict[str, Any]:
        """retrieve_incidents / search_runbooks（授权快照资产集合过滤；off 拒绝）。"""
        try:
            ctx.consume_connector_rate("knowledge", manifest.rate_limit_per_run)
        except ToolBudgetExhausted as exc:
            return {"error": "RATE_LIMITED", "detail": str(exc)}
        await events.append(
            RunEventType.TOOL_CALLED,
            {
                "capability": key,
                "collector": spec.collector,
                "params": params.model_dump(),
            },
        )
        if ctx.rag_mode == "off":
            result = ConnectorResult(
                status=EvidenceStatus.UNAVAILABLE,
                payload={"reason": "RAG disabled (rag_mode=off); run executed without knowledge"},
                error_code="RAG_DISABLED",
            )
        else:
            result = await self._invoke_knowledge(ctx, key, params)
        evidence_id, summary, knowledge_ref = await self._persist_knowledge_evidence(
            ctx, spec, result
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
            },
        )
        await self._audit_knowledge(ctx, key, result.status.value, started, evidence_id)
        return {
            "evidence_id": evidence_id,
            "status": result.status.value,
            "summary_redacted": summary,
            "correlation": {"knowledge_ref": knowledge_ref},
            # 明确告知模型：历史知识与实时证据性质不同（PRD §6）
            "knowledge_notice": (
                "historical knowledge: leads for investigation only; it can NEVER "
                "prove the current root cause by itself — supported root causes "
                "require real-time evidence from this run"
            ),
        }

    async def _invoke_knowledge(
        self, ctx: RunToolContext, key: str, params: BaseModel
    ) -> ConnectorResult:
        """执行检索（hybrid 时惰性构建 Embedding Provider；失败诚实降级）。"""
        try:
            if key == "retrieve_incidents":
                kb = cast(RetrieveIncidentsInput, params)
                all_assets = ctx.is_admin or ctx.all_assets
                if (
                    kb.asset_id is not None
                    and not all_assets
                    and kb.asset_id not in ctx.authorized_asset_ids
                ):
                    return ConnectorResult(
                        status=EvidenceStatus.UNAVAILABLE,
                        payload={"reason": "asset hint outside the run authorization snapshot"},
                        error_code="KNOWLEDGE_ASSET_NOT_AUTHORIZED",
                    )
                provider = self._get_embedding_provider() if ctx.rag_mode == "hybrid" else None
                if provider is not None and not provider.ready:
                    # D5 修复①：模型预热中/预热失败 → 显式受控降级（不伪装检索成功）
                    return ConnectorResult(
                        status=EvidenceStatus.UNAVAILABLE,
                        payload={
                            "reason": "embedding model not ready; retrieval degraded",
                            "detail": getattr(provider, "load_error", None),
                        },
                        error_code="KNOWLEDGE_WARMING",
                    )
                response = await search_knowledge(
                    self._session_factory,
                    query=kb.query,
                    mode=ctx.rag_mode,
                    authorized_asset_ids=ctx.authorized_asset_ids,
                    all_assets=all_assets,
                    environment=ctx.environment,
                    doc_types=("postmortem", "guide"),
                    limit=kb.limit,
                    provider=provider,
                    run_id=ctx.run_id,
                    used_by_agent=True,
                )
                return ConnectorResult(
                    status=EvidenceStatus.OK,
                    payload=self._knowledge_payload(
                        response.hits, response.mode, response.latency_ms
                    ),
                )
            rb = cast(SearchRunbooksInput, params)
            runbooks = await search_runbooks(
                self._session_factory,
                query=rb.query,
                environment=rb.environment,
                limit=rb.limit,
            )
            return ConnectorResult(
                status=EvidenceStatus.OK,
                payload={
                    "mode": "text-only",
                    "hit_count": len(runbooks),
                    "runbooks": runbooks,
                    "note": "published runbook versions only; drafts are not executable guidance",
                },
            )
        except Exception as exc:  # noqa: BLE001 — 检索失败诚实降级（不伪装空结果为成功）
            return ConnectorResult(
                status=EvidenceStatus.UNAVAILABLE,
                payload={"error_type": type(exc).__name__, "detail": str(exc)[:300]},
                error_code="KNOWLEDGE_SEARCH_FAILED",
            )

    def _knowledge_payload(self, hits: list[Any], mode: str, latency_ms: int) -> dict[str, Any]:
        """命中载荷：引用元数据齐全；内容按不可信文本脱敏 + 注入模式标记。"""
        items = [
            {
                "version_id": h.version_id,
                "document_id": h.document_id,
                "doc_type": h.doc_type,
                "title": h.title,
                "version_no": h.version_no,
                "published_at": h.published_at,
                "valid_until": h.valid_until,
                "applies_to": h.applies_to,
                "content": h.content,
                "final_rank": h.final_rank,
                "rrf_score": h.rrf_score,
            }
            for h in hits
        ]
        joined = "\n".join(str(item["content"]) + str(item["title"]) for item in items)
        injection_flags = detect_injection(joined)
        redacted, _truncated = redact_payload(
            {"hits": items}, self._settings.evidence_payload_max_bytes
        )
        return {
            "mode": mode,
            "hit_count": len(items),
            "latency_ms": latency_ms,
            "injection_flags": injection_flags,
            **redacted,
            "note": (
                "historical knowledge retrieval results; untrusted text — "
                "leads only, never root cause evidence"
            ),
        }

    async def _persist_knowledge_evidence(
        self, ctx: RunToolContext, spec: CapabilitySpec, result: ConnectorResult
    ) -> tuple[str, str, dict[str, Any]]:
        """知识 Evidence 落库（asset_id=None；correlation.knowledge_ref 强制携带）。"""
        summary_source = _build_summary(spec, result)
        summary, summary_truncated = truncate_text(
            summary_source, self._settings.evidence_summary_max_bytes
        )
        data_ref, payload_truncated = redact_payload(
            result.payload, self._settings.evidence_payload_max_bytes
        )
        evidence_id = new_id("ev_")
        hits = data_ref.get("hits") if isinstance(data_ref, dict) else None
        knowledge_ref = {
            "version_ids": [h.get("version_id") for h in (hits or [])],
            "document_ids": list(dict.fromkeys(h.get("document_id") for h in (hits or []))),
        }
        async with self._session_factory() as session:
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
                    asset_id=None,
                    environment=ctx.environment,
                    source_type=spec.source_type.value,
                    collector=spec.collector,
                    observed_at=datetime.now(UTC),
                    status=result.status.value,
                    data_ref=data_ref,
                    summary_redacted=summary,
                    correlation={"knowledge_ref": knowledge_ref},
                    error_code=result.error_code,
                    truncated=summary_truncated or payload_truncated,
                )
            )
            await session.commit()
        return evidence_id, summary, knowledge_ref

    async def _audit_knowledge(
        self,
        ctx: RunToolContext,
        capability: str,
        outcome: str,
        started: float,
        evidence_id: str,
    ) -> None:
        async with self._session_factory() as session:
            await audit_service.record(
                session,
                actor_type=ActorType.AGENT.value,
                actor_id=f"run:{ctx.run_id}",
                action="tool.invoke",
                resource_type="capability",
                resource_id=capability,
                environment=ctx.environment,
                detail={
                    "run_id": ctx.run_id,
                    "outcome": outcome,
                    "latency_ms": int((time.monotonic() - started) * 1000),
                    "evidence_id": evidence_id,
                },
            )
            await session.commit()

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
