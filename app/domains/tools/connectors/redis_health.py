"""Redis 只读健康检查连接器（collector: redis.health）。

仅执行只读命令 PING / INFO（redis-py 客户端，socket_timeout 受 Gateway 控制）；
连接串来自 connector_instances.config（密钥经 secret_refs 间接引用，不入日志）。
"""

from __future__ import annotations

from typing import Any

from app.core.enums import EvidenceStatus
from app.domains.tools.connectors.base import ConnectorResult

_INFO_SAFE_SECTIONS = ("server", "clients", "memory", "stats", "replication")


async def check_redis_health(
    *,
    dsn: str,
    timeout_seconds: float,
) -> ConnectorResult:
    """PING + INFO 基线；失败映射为 timeout/unavailable 的 ConnectorResult。"""
    import redis.asyncio as aioredis
    import redis.exceptions as _redis_exceptions

    client = aioredis.from_url(
        dsn, socket_timeout=timeout_seconds, socket_connect_timeout=timeout_seconds
    )
    try:
        pong = await client.ping()
        info = await client.info(section=",".join(_INFO_SAFE_SECTIONS))
        payload: dict[str, Any] = {
            "ping": bool(pong),
            "redis_version": info.get("redis_version"),
            "uptime_seconds": info.get("uptime_in_seconds"),
            "connected_clients": info.get("connected_clients"),
            "used_memory_human": info.get("used_memory_human"),
            "role": info.get("role"),
            "status": info.get("master_link_status") or info.get("slave0") or None,
            "total_connections_received": info.get("total_connections_received"),
            "rejected_connections": info.get("rejected_connections"),
            "evicted_keys": info.get("evicted_keys"),
        }
        return ConnectorResult(status=EvidenceStatus.OK, payload=payload)
    except TimeoutError:
        return ConnectorResult(
            status=EvidenceStatus.TIMEOUT,
            payload={"target": _safe_endpoint(dsn)},
            error_code="CONNECTOR_TIMEOUT",
        )
    except _redis_exceptions.TimeoutError:
        # redis-py 的 TimeoutError 继承 RedisError 而非内建 TimeoutError，
        # 读超时（socket_timeout）走这里
        return ConnectorResult(
            status=EvidenceStatus.TIMEOUT,
            payload={"target": _safe_endpoint(dsn)},
            error_code="CONNECTOR_TIMEOUT",
        )
    except _redis_exceptions.ConnectionError as exc:
        # redis-py 的 ConnectionError 继承 RedisError 而非内建 OSError，
        # 连接拒绝/网络不可达走这里（语义：目标不可用）
        return ConnectorResult(
            status=EvidenceStatus.UNAVAILABLE,
            payload={"target": _safe_endpoint(dsn), "error_type": type(exc).__name__},
            error_code="REDIS_UNAVAILABLE",
        )
    except OSError as exc:
        # 套接字级错误（内建 ConnectionError/TimeoutError 等）
        return ConnectorResult(
            status=EvidenceStatus.UNAVAILABLE,
            payload={"target": _safe_endpoint(dsn), "error_type": type(exc).__name__},
            error_code="REDIS_UNAVAILABLE",
        )
    except Exception as exc:  # noqa: BLE001 — redis 库异常族庞杂，统一不可用语义
        return ConnectorResult(
            status=EvidenceStatus.ERROR,
            payload={"target": _safe_endpoint(dsn), "error_type": type(exc).__name__},
            error_code="REDIS_ERROR",
        )
    finally:
        await client.aclose()


def _safe_endpoint(dsn: str) -> str:
    """错误上下文只保留 host:port，凭据已被 redaction 层兜底。"""
    try:
        from redis.connection import parse_url

        parsed = parse_url(dsn)
        return f"{parsed.get('host', '?')}:{parsed.get('port', '?')}"
    except Exception:  # noqa: BLE001
        return "<unparsed>"
