"""Prometheus 只读指标查询连接器（collector: prometheus.query，D3）。

安全约束：
- 仅允许 GET /api/v1/query 与 /api/v1/query_range；
- PromQL 由 Gateway 从实例配置的**模板白名单**展开（模型只能传模板键，
  服务标签由 Gateway 强制注入为授权资产值——不能查询其他服务的指标）；
- 时间范围与点数上限由 Gateway 与本连接器双重校验。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import httpx

from app.core.enums import EvidenceStatus
from app.domains.tools.connectors.base import ConnectorResult

_MAX_RANGE_SECONDS = 3600
_MAX_POINTS = 60
_RESULT_SERIES_LIMIT = 8


async def query_metrics(
    *,
    base_url: str,
    query: str,
    time_range: dict[str, str] | None,
    timeout_seconds: float,
) -> ConnectorResult:
    """执行受控 PromQL（instant 或 range）；时间范围超限直接拒绝。"""
    params: dict[str, str] = {"query": query}
    if time_range:
        start = _parse_ts(time_range.get("start"))
        end = _parse_ts(time_range.get("end"))
        if start is None or end is None or end <= start:
            return ConnectorResult(
                status=EvidenceStatus.UNAVAILABLE,
                payload={"reason": "invalid time_range"},
                error_code="INVALID_TIME_RANGE",
            )
        span = (end - start).total_seconds()
        if span > _MAX_RANGE_SECONDS:
            return ConnectorResult(
                status=EvidenceStatus.UNAVAILABLE,
                payload={"reason": f"time range {int(span)}s exceeds {_MAX_RANGE_SECONDS}s"},
                error_code="TIME_RANGE_TOO_WIDE",
            )
        step = max(15, int(span / _MAX_POINTS))
        params.update(
            {
                "start": f"{start.timestamp():.0f}",
                "end": f"{end.timestamp():.0f}",
                "step": f"{step}s",
            }
        )
        path = "/api/v1/query_range"
    else:
        path = "/api/v1/query"

    try:
        async with httpx.AsyncClient(follow_redirects=False) as client:
            resp = await client.get(
                base_url.rstrip("/") + path, params=params, timeout=timeout_seconds
            )
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
            error_code="PROM_UNREACHABLE",
        )

    if resp.status_code != 200:
        return ConnectorResult(
            status=EvidenceStatus.ERROR,
            payload={"http_status": resp.status_code, "body": resp.text[:2000]},
            error_code=f"HTTP_{resp.status_code}",
        )
    try:
        body = resp.json()
    except ValueError:
        return ConnectorResult(
            status=EvidenceStatus.ERROR,
            payload={"reason": "invalid JSON from prometheus"},
            error_code="PROM_BAD_RESPONSE",
        )
    if body.get("status") != "success":
        return ConnectorResult(
            status=EvidenceStatus.ERROR,
            payload={"prom_status": body.get("status"), "error": str(body.get("error"))[:300]},
            error_code="PROM_QUERY_ERROR",
        )
    data = body.get("data") or {}
    result = data.get("result") or []
    payload: dict[str, Any] = {
        "result_type": data.get("resultType"),
        "series": [_summarize_series(r) for r in result[:_RESULT_SERIES_LIMIT]],
        "series_truncated": len(result) > _RESULT_SERIES_LIMIT,
    }
    # 后端侧异常标注（如 mysql_up=0）原样透传进摘要，供分析层引用
    if body.get("anomaly") is not None:
        payload["anomaly"] = body["anomaly"]
    return ConnectorResult(status=EvidenceStatus.OK, payload=payload)


def _summarize_series(entry: dict[str, Any]) -> dict[str, Any]:
    """压缩单条序列：保留标签与值（instant：单值；range：最多 _MAX_POINTS 点）。"""
    metric = entry.get("metric") or {}
    value = entry.get("value") or entry.get("values")
    if isinstance(value, list) and value and isinstance(value[0], list):
        value = value[-_MAX_POINTS:]
    return {"metric": metric, "value": value}


def _parse_ts(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone(UTC)
    except ValueError:
        return None
