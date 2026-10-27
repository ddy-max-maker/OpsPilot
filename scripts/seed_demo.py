"""演示环境种子：连接器类型/实例 + 三个演示资产 + 拓扑边（幂等，可重复运行）。

直连 PostgreSQL 写入（连接器实例的 POST /integrations API 属 D3 范围）。
需 Docker PostgreSQL 已启动且业务库已迁移。
"""

from __future__ import annotations

import asyncio
import json
import sys

import asyncpg

# 127.0.0.1 强制走 Docker 容器（本机 ::1 上可能有另一个 PostgreSQL 服务）
DSN = "postgresql://opspilot:opspilot@127.0.0.1:5432/opspilot"

CONNECTOR_TYPES = [
    ("ct_redis_ro", "redis-ro", "Redis 只读健康检查"),
    ("ct_docker_ro", "docker-ro", "Docker 只读诊断（经 docker-ro-proxy）"),
    ("ct_http_ro", "http-ro", "HTTP 只读健康检查"),
]

CONNECTOR_INSTANCES = [
    (
        "integ_redis_demo",
        "ct_redis_ro",
        "redis-demo",
        {"dsn": "redis://demo-redis:6379/0", "timeout_seconds": 3.0},
    ),
    (
        "integ_docker_demo",
        "ct_docker_ro",
        "docker-demo",
        {
            "base_url": "http://docker-ro-proxy:8800",
            "container_prefix": "opspilot-demo-",
            "timeout_seconds": 10.0,
        },
    ),
    (
        "integ_http_demo",
        "ct_http_ro",
        "http-demo",
        {"timeout_seconds": 5.0},
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
        "endpoints": {},
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
]

DEMO_EDGES = [
    ("as_demo_gateway", "as_demo_app", "serves"),
    ("as_demo_app", "as_demo_redis", "depends_on"),
]


async def main() -> int:
    conn = await asyncpg.connect(DSN)
    try:
        for cid, code, name in CONNECTOR_TYPES:
            await conn.execute(
                """INSERT INTO connector_types (id, code, display_name, read_only)
                   VALUES ($1, $2, $3, true)
                   ON CONFLICT (id) DO NOTHING""",
                cid,
                code,
                name,
            )
        for iid, tid, name, config in CONNECTOR_INSTANCES:
            await conn.execute(
                """INSERT INTO connector_instances
                       (id, connector_type_id, name, config, status, enabled_at)
                   VALUES ($1, $2, $3, $4::jsonb, 'enabled', now())
                   ON CONFLICT (id) DO NOTHING""",
                iid,
                tid,
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
                   ON CONFLICT (id) DO NOTHING""",
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
