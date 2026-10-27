"""HTTP 只读健康检查连接器（collector: http.health_check）。

安全约束：
- 仅允许 GET；目标 URL 必须来自资产已登记的 endpoints.health_url（SSRF 白名单，
  PRD §8：目标白名单 = 已登记资产，Agent 不能指定任意 URL）；
- 强制超时（由 Gateway 传入）、响应大小上限（流式读取，超限截断）。
"""

from __future__ import annotations

from typing import Any

import httpx

from app.core.enums import EvidenceStatus
from app.domains.tools.connectors.base import ConnectorResult

_RESPONSE_READ_LIMIT = 262_144  # 256KB


async def get_service_health(
    *,
    health_url: str,
    timeout_seconds: float,
) -> ConnectorResult:
    """GET 已登记健康端点；失败映射为 error/timeout 的 ConnectorResult。"""
    try:
        async with (
            httpx.AsyncClient(follow_redirects=False) as client,
            client.stream("GET", health_url, timeout=timeout_seconds) as resp,
        ):
            body = await _read_limited(resp)
            payload: dict[str, Any] = {
                "http_status": resp.status_code,
                "content_type": resp.headers.get("content-type", ""),
                "body": body,
            }
            if resp.status_code >= 500:
                return ConnectorResult(
                    status=EvidenceStatus.ERROR,
                    payload=payload,
                    error_code=f"HTTP_{resp.status_code}",
                )
            if resp.status_code >= 400:
                return ConnectorResult(
                    status=EvidenceStatus.ERROR,
                    payload=payload,
                    error_code=f"HTTP_{resp.status_code}",
                )
            return ConnectorResult(status=EvidenceStatus.OK, payload=payload)
    except httpx.TimeoutException:
        return ConnectorResult(
            status=EvidenceStatus.TIMEOUT,
            payload={"target": _safe_origin(health_url)},
            error_code="CONNECTOR_TIMEOUT",
        )
    except httpx.HTTPError as exc:
        return ConnectorResult(
            status=EvidenceStatus.UNAVAILABLE,
            payload={"target": _safe_origin(health_url), "error_type": type(exc).__name__},
            error_code="CONNECTOR_UNAVAILABLE",
        )


async def _read_limited(resp: httpx.Response) -> str:
    chunks: list[bytes] = []
    total = 0
    async for chunk in resp.aiter_bytes():
        total += len(chunk)
        if total > _RESPONSE_READ_LIMIT:
            chunks.append(chunk[: max(0, _RESPONSE_READ_LIMIT - (total - len(chunk)))])
            break
        chunks.append(chunk)
    return b"".join(chunks).decode("utf-8", errors="replace")


def _safe_origin(url: str) -> str:
    """错误上下文只保留 origin，不泄漏完整路径与查询串。"""
    try:
        parsed = httpx.URL(url)
        return f"{parsed.scheme}://{parsed.host}" + (f":{parsed.port}" if parsed.port else "")
    except Exception:  # noqa: BLE001
        return "<unparsed>"
