"""Docker 只读诊断连接器（collector: docker.inspect / docker.logs）。

ADR-11 硬约束：Worker 不持有 docker.sock；所有 Docker API 请求经
docker-ro-proxy（方法+路径白名单）转发。本连接器仅发出白名单内的 GET 请求，
目标 base_url 来自 connector_instances.config（注册时校验必须指向 ro-proxy）。
"""

from __future__ import annotations

from typing import Any

import httpx

from app.core.enums import EvidenceStatus
from app.domains.tools.connectors.base import ConnectorResult


async def _proxy_get(
    *,
    base_url: str,
    path: str,
    params: dict[str, str] | None,
    timeout_seconds: float,
) -> ConnectorResult:
    url = f"{base_url.rstrip('/')}{path}"
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(url, params=params, timeout=timeout_seconds)
        if resp.status_code == 403:
            return ConnectorResult(
                status=EvidenceStatus.ERROR,
                payload={"proxy": "denied"},
                error_code="PROXY_FORBIDDEN",
            )
        if resp.status_code == 404:
            return ConnectorResult(
                status=EvidenceStatus.UNAVAILABLE,
                payload={"target": "container not found"},
                error_code="CONTAINER_NOT_FOUND",
            )
        if resp.status_code != 200:
            return ConnectorResult(
                status=EvidenceStatus.ERROR,
                payload={"proxy_status": resp.status_code},
                error_code=f"PROXY_HTTP_{resp.status_code}",
            )
        return ConnectorResult(status=EvidenceStatus.OK, payload=dict(resp.json()))
    except httpx.TimeoutException:
        return ConnectorResult(
            status=EvidenceStatus.TIMEOUT,
            payload={"target": base_url},
            error_code="CONNECTOR_TIMEOUT",
        )
    except httpx.HTTPError as exc:
        return ConnectorResult(
            status=EvidenceStatus.UNAVAILABLE,
            payload={"target": base_url, "error_type": type(exc).__name__},
            error_code="PROXY_UNAVAILABLE",
        )


def _field(payload: dict[str, Any], *names: str) -> Any:
    """Docker API 不同版本字段大小写不一致（Names/State...），取第一个命中。"""
    for name in names:
        if name in payload and payload[name] is not None:
            return payload[name]
    return None


async def find_container_id(
    *, base_url: str, container_name: str, timeout_seconds: float
) -> str | None:
    """按名称精确查找容器 ID（白名单端点：GET /containers/json?all=1&filters=...）。

    all=true 使已停止/已退出的容器也可被发现（故障诊断的关键观测面）。
    """
    import json

    result = await _proxy_get(
        base_url=base_url,
        path="/containers/json",
        params={"all": "true", "filters": json.dumps({"name": [container_name]})},
        timeout_seconds=timeout_seconds,
    )
    if not result.ok:
        return None
    containers = result.payload.get("containers", [])
    for item in containers:
        raw_names = _field(item, "names", "Names") or []
        normalized = {str(n).lstrip("/") for n in raw_names}
        if container_name in normalized:
            return str(_field(item, "id", "Id") or item.get("id"))
    return None


async def get_container_status(
    *,
    base_url: str,
    container_id: str,
    timeout_seconds: float,
) -> ConnectorResult:
    """容器 inspect（白名单端点：GET /containers/{id}/json）。

    归一化摘要字段：state/oom_killed/exit_code/restart_count/started_at。
    """
    result = await _proxy_get(
        base_url=base_url,
        path=f"/containers/{container_id}/json",
        params=None,
        timeout_seconds=timeout_seconds,
    )
    if not result.ok:
        return result
    state = _field(result.payload, "state", "State") or {}
    payload: dict[str, Any] = {
        "container_id": container_id,
        "state": _field(state, "status", "Status"),
        "running": _field(state, "running", "Running"),
        "oom_killed": _field(state, "oom_killed", "OOMKilled"),
        "exit_code": _field(state, "exit_code", "ExitCode"),
        "error": _field(state, "error", "Error") or "",
        "started_at": _field(state, "started_at", "StartedAt"),
        "finished_at": _field(state, "finished_at", "FinishedAt"),
        "restart_count": _field(result.payload, "restart_count", "RestartCount"),
    }
    return ConnectorResult(status=EvidenceStatus.OK, payload=payload)


async def read_container_logs(
    *,
    base_url: str,
    container_id: str,
    tail_limit: int,
    timeout_seconds: float,
) -> ConnectorResult:
    """容器日志尾部（白名单端点：GET /containers/{id}/logs?stdout=1&stderr=1&tail=N）。"""
    tail = max(1, min(int(tail_limit), 500))
    result = await _proxy_get(
        base_url=base_url,
        path=f"/containers/{container_id}/logs",
        params={"stdout": "true", "stderr": "true", "tail": str(tail)},
        timeout_seconds=timeout_seconds,
    )
    if not result.ok:
        return result
    raw = result.payload.get("logs")
    lines = raw if isinstance(raw, list) else []
    payload: dict[str, Any] = {"container_id": container_id, "logs": lines[:tail]}
    return ConnectorResult(status=EvidenceStatus.OK, payload=payload)
