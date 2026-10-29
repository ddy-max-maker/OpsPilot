"""演示环境种子：连接器类型/实例 + 三个演示资产 + 拓扑边（幂等，可重复运行）。

直连 PostgreSQL 写入（连接器实例的 POST /integrations API 属 D3 范围）。
需 Docker PostgreSQL 已启动且业务库已迁移。

连接串优先级：`SEED_DSN` > `DATABASE_URL`（容器内为 postgres:5432）>
默认宿主机端口映射 127.0.0.1:5432。因此既可在宿主机运行，
也可在容器内运行（`docker compose exec api python scripts/seed_demo.py`）。
"""

from __future__ import annotations

import asyncio
import json
import os
import sys

import asyncpg

# 127.0.0.1 强制走 Docker 容器（本机 ::1 上可能有另一个 PostgreSQL 服务）
DEFAULT_DSN = "postgresql://opspilot:opspilot@127.0.0.1:5432/opspilot"
DSN = (
    os.environ.get("SEED_DSN")
    or os.environ.get("DATABASE_URL", "").replace("postgresql+asyncpg://", "postgresql://")
    or DEFAULT_DSN
)

CONNECTOR_TYPES = [
    ("ct_redis_ro", "redis-ro", "Redis 只读健康检查"),
    ("ct_docker_ro", "docker-ro", "Docker 只读诊断（经 docker-ro-proxy）"),
    ("ct_http_ro", "http-ro", "HTTP 只读健康检查"),
    ("ct_prom_ro", "prom-ro", "Prometheus 只读指标查询（D3）"),
    ("ct_otel", "otel", "OpenTelemetry 链路只读查询（D3）"),
    ("ct_mysql_ro", "mysql-ro", "MySQL 只读诊断（D3）"),
]

CONNECTOR_INSTANCES = [
    (
        "integ_redis_demo",
        "redis-ro",
        "redis-demo",
        {"dsn": "redis://demo-redis:6379/0", "timeout_seconds": 3.0},
    ),
    (
        "integ_docker_demo",
        "docker-ro",
        "docker-demo",
        {
            "base_url": "http://docker-ro-proxy:8800",
            "container_prefix": "opspilot-demo-",
            "timeout_seconds": 10.0,
        },
    ),
    (
        "integ_http_demo",
        "http-ro",
        "http-demo",
        {"timeout_seconds": 5.0, "probe_url": "http://demo-app:8080/health"},
    ),
    (
        "integ_prom_demo",
        "prom-ro",
        "prom-demo",
        {
            "base_url": "http://prometheus:9090",
            "timeout_seconds": 5.0,
            # 受控查询模板白名单（D3：Agent 只能传模板键，service 标签由网关注入）
            "allowed_templates": {
                "up": 'up{job="{service}"}',
                "http_request_duration_p95": (
                    "histogram_quantile(0.95, sum(rate("
                    'http_server_requests_seconds_bucket{service="{service}"}[5m]'
                    ")) by (le))"
                ),
                "http_requests_error_rate": (
                    'sum(rate(http_server_requests_seconds_count{service="{service}",'
                    'status=~"5.."}[5m])) / sum(rate('
                    'http_server_requests_seconds_count{service="{service}"}[5m]))'
                ),
                "process_resident_memory": (
                    'max(process_resident_memory_bytes{service="{service}"})'
                ),
            },
        },
    ),
    (
        "integ_otel_demo",
        "otel",
        "otel-demo",
        {"base_url": "http://jaeger:16686", "timeout_seconds": 5.0},
    ),
    (
        "integ_mysql_demo",
        "mysql-ro",
        "mysql-demo",
        {
            "host": "demo-mysql",
            "port": 3306,
            "user": "opspilot_ro",
            "database": "orders_demo",
            "password_env": "D3_MYSQL_RO_PASSWORD",
            "timeout_seconds": 5.0,
        },
    ),
]

# 资产命名约定：docker 连接器按 "container_prefix + asset.name" 查找容器
DEMO_ASSETS = [
    {
        "id": "as_demo_gateway",
        "name": "gateway",
        "environment": "sandbox",
        "asset_type": "gateway",
        "criticality": "high",
        "endpoints": {"health_url": "http://demo-gateway/health"},
    },
    {
        "id": "as_demo_app",
        "name": "app",
        "environment": "sandbox",
        "asset_type": "service",
        "criticality": "high",
        "endpoints": {"health_url": "http://demo-app:8080/health"},
    },
    {
        "id": "as_demo_redis",
        "name": "redis",
        "environment": "sandbox",
        "asset_type": "cache",
        "criticality": "high",
        # D7：容器身份显式登记（D6 规则：资产登记是容器身份唯一权威），
        # 不再依赖「实例前缀 + 资产名」推导
        "endpoints": {"container_name": "opspilot-demo-redis"},
    },
    {
        # S2 注入对象：opspilot-demo-crasher（经 docker-ro-proxy 只读观测）
        "id": "as_demo_crasher",
        "name": "crasher",
        "environment": "sandbox",
        "asset_type": "container",
        "criticality": "low",
        "endpoints": {},
    },
    {
        # D3 跨语言：Java Spring Boot 服务（同一 AgentRuntime 诊断）
        # name=java：docker 连接器按 container_prefix+name 定位容器 opspilot-demo-java
        "id": "as_demo_java",
        "name": "java",
        "environment": "sandbox",
        "asset_type": "service",
        "criticality": "high",
        "endpoints": {"health_url": "http://demo-java:8080/actuator/health"},
    },
    {
        # D3：MySQL 只读诊断对象
        "id": "as_demo_mysql",
        "name": "mysql",
        "environment": "sandbox",
        "asset_type": "database",
        "criticality": "medium",
        "endpoints": {},
    },
]

# 资产 ↔ 连接器实例观测绑定（D3：按授权资产解析实例，多实例不串用）
DEMO_BINDINGS = [
    # 双 Prom 实例演示隔离：java-app 绑定 prom-java（S4 验收时启用第二个实例）
    ("bind_java_prom", "as_demo_java", "integ_prom_demo", "metrics", {"service": "java-app"}),
    ("bind_java_otel", "as_demo_java", "integ_otel_demo", "traces", {"service": "java-app"}),
    ("bind_java_docker", "as_demo_java", "integ_docker_demo", "logs", {}),
    ("bind_mysql_mysql", "as_demo_mysql", "integ_mysql_demo", "health", {}),
    ("bind_mysql_prom", "as_demo_mysql", "integ_prom_demo", "metrics", {"service": "mysql"}),
]

DEMO_EDGES = [
    ("as_demo_gateway", "as_demo_app", "serves"),
    ("as_demo_app", "as_demo_redis", "depends_on"),
    ("as_demo_java", "as_demo_app", "depends_on"),
    ("as_demo_java", "as_demo_mysql", "depends_on"),
]


async def main() -> int:
    conn = await asyncpg.connect(DSN)
    try:
        # 连接器类型按 code 幂等（API lifespan 启动同步可能已创建）；
        # 实例引用的 type id 以 DB 实际行为准，避免固定 id 与已有行冲突
        for _, code, name in CONNECTOR_TYPES:
            await conn.execute(
                """INSERT INTO connector_types (id, code, display_name, read_only)
                   VALUES ($1, $2, $3, true)
                   ON CONFLICT (code) DO NOTHING""",
                "ct_" + code.replace("-", "_"),
                code,
                name,
            )
        type_ids = {
            code: await conn.fetchval("SELECT id FROM connector_types WHERE code = $1", code)
            for _, code, _ in CONNECTOR_TYPES
        }
        for iid, type_code, name, config in CONNECTOR_INSTANCES:
            await conn.execute(
                """INSERT INTO connector_instances
                       (id, connector_type_id, name, config, status, enabled_at)
                   VALUES ($1, $2, $3, $4::jsonb, 'enabled', now())
                   ON CONFLICT (name) DO UPDATE SET config = EXCLUDED.config,
                       connector_type_id = EXCLUDED.connector_type_id""",
                iid,
                type_ids[type_code],
                name,
                json.dumps(config),
            )
        for asset in DEMO_ASSETS:
            await conn.execute(
                """INSERT INTO assets
                       (id, name, environment, asset_type, status, criticality,
                        endpoints, owner, tags, created_at, updated_at)
                   VALUES ($1, $2, $3, $4, 'active', $5, $6::jsonb, 'demo', '[]'::jsonb,
                           now(), now())
                   ON CONFLICT (id) DO UPDATE
                       SET name = EXCLUDED.name,
                           endpoints = EXCLUDED.endpoints,
                           updated_at = now()""",
                asset["id"],
                asset["name"],
                asset["environment"],
                asset["asset_type"],
                asset["criticality"],
                json.dumps(asset["endpoints"]),
            )
        for from_id, to_id, relation in DEMO_EDGES:
            await conn.execute(
                """INSERT INTO asset_edges
                       (id, from_asset_id, to_asset_id, relation, created_at)
                   VALUES ('edge_' || $1 || '_' || $2, $1, $2, $3, now())
                   ON CONFLICT DO NOTHING""",
                from_id,
                to_id,
                relation,
            )
        for bid, asset_id, iid, kind, cfg in DEMO_BINDINGS:
            await conn.execute(
                """INSERT INTO asset_observability_bindings
                       (id, asset_id, instance_id, binding_kind, config)
                   VALUES ($1, $2, $3, $4, $5::jsonb)
                   ON CONFLICT (asset_id, binding_kind) DO UPDATE
                       SET instance_id = EXCLUDED.instance_id,
                           config = EXCLUDED.config""",
                bid,
                asset_id,
                iid,
                kind,
                json.dumps(cfg),
            )
        print("[seed-demo] 连接器实例与演示资产就绪：")
        for asset in DEMO_ASSETS:
            print(
                f"  {asset['id']}  {asset['name']}（{asset['environment']}/{asset['asset_type']}）"
            )
    finally:
        await conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
