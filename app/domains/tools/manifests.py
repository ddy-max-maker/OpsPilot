"""内置最小 Capability Manifest（CONTRACTS §4；D3 升级为 DB 注册表）。

所有 capability read_only=true；Docker 连接器目标端点强制为 docker-ro-proxy
白名单端点（ADR-11）。工具工厂按 manifest 生成，Agent 无法绕过。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.core.enums import EvidenceSourceType


@dataclass(frozen=True)
class CapabilitySpec:
    """单个只读能力声明（输入用 Pydantic 校验，见 schemas.py）。"""

    key: str
    description: str
    source_type: EvidenceSourceType
    collector: str
    connector_code: str  # 依赖的 connector_types.code
    input_model: str  # Pydantic 模型名（gateway 内解析）


@dataclass(frozen=True)
class ConnectorManifest:
    """连接器能力清单（CONTRACTS §4 最小子集，冻结字段语义）。"""

    connector_id: str  # connector_types.code
    display_name: str
    version: str
    read_only: bool = True
    capabilities: tuple[CapabilitySpec, ...] = ()
    environments: tuple[str, ...] = ("sandbox", "staging", "production")
    asset_types: tuple[str, ...] = ()
    rate_limit_per_run: int = 30
    rate_limit_per_second: float = 2.0
    timeout_ms: int = 5000
    result_max_bytes: int = 262_144


REDIS_MANIFEST = ConnectorManifest(
    connector_id="redis-ro",
    display_name="Redis 只读健康检查",
    version="1.0.0",
    capabilities=(
        CapabilitySpec(
            key="check_redis_health",
            description="读取 Redis PING/INFO 基线（只读）",
            source_type=EvidenceSourceType.HEALTH,
            collector="redis.health",
            connector_code="redis-ro",
            input_model="RedisHealthInput",
        ),
    ),
    asset_types=("cache",),
)

DOCKER_MANIFEST = ConnectorManifest(
    connector_id="docker-ro",
    display_name="Docker 只读诊断（经 docker-ro-proxy 白名单代理）",
    version="1.0.0",
    capabilities=(
        CapabilitySpec(
            key="get_container_status",
            description="容器状态/事件/OOM/退出码（经只读代理 inspect，无任何写操作）",
            source_type=EvidenceSourceType.INFRA,
            collector="docker.inspect",
            connector_code="docker-ro",
            input_model="ContainerStatusInput",
        ),
        CapabilitySpec(
            key="read_container_logs",
            description="读取容器近期日志（脱敏截断；经只读代理 logs）",
            source_type=EvidenceSourceType.LOG,
            collector="docker.logs",
            connector_code="docker-ro",
            input_model="ContainerLogsInput",
        ),
    ),
)

HTTP_MANIFEST = ConnectorManifest(
    connector_id="http-ro",
    display_name="HTTP 只读健康检查",
    version="1.0.0",
    capabilities=(
        CapabilitySpec(
            key="get_service_health",
            description="GET 资产已登记 health_url（仅 GET，超时受限）",
            source_type=EvidenceSourceType.HEALTH,
            collector="http.health_check",
            connector_code="http-ro",
            input_model="ServiceHealthInput",
        ),
    ),
)

PROM_MANIFEST = ConnectorManifest(
    connector_id="prom-ro",
    display_name="Prometheus 只读指标查询（受控模板）",
    version="1.0.0",
    capabilities=(
        CapabilitySpec(
            key="query_metrics",
            description=(
                "按授权资产执行受控 PromQL 模板查询（模板白名单 + 服务标签强制注入 + "
                "时间范围/点数上限）；query 为实例配置 allowed_templates 中的模板键"
            ),
            source_type=EvidenceSourceType.METRIC,
            collector="prometheus.query",
            connector_code="prom-ro",
            input_model="MetricsQueryInput",
        ),
    ),
    asset_types=("service", "gateway", "database", "cache", "container", "host"),
)

OTEL_MANIFEST = ConnectorManifest(
    connector_id="otel",
    display_name="OpenTelemetry 链路只读查询（经既有可观测后端）",
    version="1.0.0",
    capabilities=(
        CapabilitySpec(
            key="query_traces",
            description=(
                "按授权资产查询链路（trace_id 精确查询或按服务+时间窗最近链路，"
                "数量与时间范围受限）；返回摘要为不可信文本（脱敏）"
            ),
            source_type=EvidenceSourceType.TRACE,
            collector="otel.query",
            connector_code="otel",
            input_model="TracesQueryInput",
        ),
    ),
    asset_types=("service", "gateway", "database", "cache", "container", "host"),
)

MYSQL_MANIFEST = ConnectorManifest(
    connector_id="mysql-ro",
    display_name="MySQL 只读诊断（最小权限账号 + 预定义查询）",
    version="1.0.0",
    capabilities=(
        CapabilitySpec(
            key="read_mysql_diagnostics",
            description=(
                "连接状态/当前连接数/连接使用情况/慢查询摘要（全部为预定义只读查询，"
                "无通用 SQL 执行能力）"
            ),
            source_type=EvidenceSourceType.INFRA,
            collector="mysql.diagnostics",
            connector_code="mysql-ro",
            input_model="MySqlDiagnosticsInput",
        ),
    ),
    asset_types=("database",),
)

BUILTIN_MANIFESTS: dict[str, ConnectorManifest] = {
    m.connector_id: m
    for m in (
        REDIS_MANIFEST,
        DOCKER_MANIFEST,
        HTTP_MANIFEST,
        PROM_MANIFEST,
        OTEL_MANIFEST,
        MYSQL_MANIFEST,
    )
}


def manifest_payload(manifest: ConnectorManifest) -> dict[str, Any]:
    """导出 manifest 校验载荷（测试与 GET /capabilities 复用）。"""
    return {
        "manifest_version": "1",
        "connector_id": manifest.connector_id,
        "connector_version": manifest.version,
        "display_name": manifest.display_name,
        "read_only": manifest.read_only,
        "capabilities": [
            {
                "capability_key": c.key,
                "read_only": True,
                "description": c.description,
                "source_type": c.source_type.value,
                "collector": c.collector,
                "input_model": c.input_model,
            }
            for c in manifest.capabilities
        ],
        "scopes": {
            "environments": list(manifest.environments),
            "asset_types": list(manifest.asset_types),
        },
        "rate_limit": {
            "max_calls_per_run": manifest.rate_limit_per_run,
            "per_second": manifest.rate_limit_per_second,
        },
        "timeout_ms": manifest.timeout_ms,
        "result_max_bytes": manifest.result_max_bytes,
        "compatibility": {"min_platform": "0.1.0"},
    }


ALL_CAPABILITY_KEYS: dict[str, CapabilitySpec] = {
    spec.key: spec for manifest in BUILTIN_MANIFESTS.values() for spec in manifest.capabilities
}
