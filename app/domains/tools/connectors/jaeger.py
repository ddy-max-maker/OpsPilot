"""OpenTelemetry 链路只读查询连接器（collector: otel.query，D3）。

接入方式（PRD §18 边界）：OTel 数据经**企业既有可观测后端**暴露给 OpsPilot；
本连接器实现 Jaeger HTTP API 只读查询（GET /api/traces、GET /api/traces/{id}）。

安全约束：
- 仅 GET；service 标签由 Gateway 按授权资产强制注入；
- 返回摘要视为不可信文本（Gateway 脱敏/截断）；span 属性不当指令。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import httpx

from app.core.enums import EvidenceStatus
from app.domains.tools.connectors.base import ConnectorResult

_RECENT_LIMIT = 5
_SPAN_SUMMARY_LIMIT = 20


async def query_traces(
    *,
    base_url: str,
    service: str,
    trace_id: str | None = None,
    time_range: dict[str, str] | None = None,
    timeout_seconds: float = 5.0,
) -> ConnectorResult:
    """trace_id 精确查询；否则按服务+时间窗取最近链路（条数受限）。"""
    try:
        async with httpx.AsyncClient(follow_redirects=False) as client:
            if trace_id:
                resp = await client.get(
                    base_url.rstrip("/") + f"/api/traces/{trace_id}", timeout=timeout_seconds
                )
            else:
                params: dict[str, Any] = {"service": service, "limit": _RECENT_LIMIT}
                start, end = _range_bounds(time_range)
                if start and end:
                    params.update(
                        {
                            "start": int(start.timestamp() * 1_000_000),
                            "end": int(end.timestamp() * 1_000_000),
                            "lookback": "custom",
                        }
                    )
                resp = await client.get(
                    base_url.rstrip("/") + "/api/traces", params=params, timeout=timeout_seconds
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
            error_code="OTEL_UNREACHABLE",
        )

    if resp.status_code == 404:
        return ConnectorResult(
            status=EvidenceStatus.UNAVAILABLE,
            payload={"reason": f"trace '{trace_id}' not found"},
            error_code="TRACE_NOT_FOUND",
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
            payload={"reason": "invalid JSON from tracing backend"},
            error_code="OTEL_BAD_RESPONSE",
        )
    data = body.get("data") or []
    if not data:
        return ConnectorResult(
            status=EvidenceStatus.OK,
            payload={"traces": [], "note": "no traces matched (service/time range)"},
        )
    return ConnectorResult(
        status=EvidenceStatus.OK,
        payload={"traces": [_summarize_trace(t) for t in data[:_RECENT_LIMIT]]},
    )


def _summarize_trace(trace: dict[str, Any]) -> dict[str, Any]:
    """单条链路摘要：ID/根操作/时长/span 数/错误 span 数/涉及服务。"""
    spans = trace.get("spans") or []
    processes = trace.get("processes") or {}
    services = sorted({(p or {}).get("serviceName", "?") for p in processes.values()})
    error_spans = 0
    for span in spans:
        for tag in span.get("tags") or []:
            if tag.get("key") == "error" and tag.get("value"):
                error_spans += 1
                break
    root = min(spans, key=lambda s: s.get("startTime", 0)) if spans else {}
    return {
        "trace_id": trace.get("traceID"),
        "root_operation": root.get("operationName"),
        "span_count": len(spans[:_SPAN_SUMMARY_LIMIT]),
        "duration_us": trace.get("duration") or root.get("duration"),
        "services": services,
        "error_spans": error_spans,
    }


def _range_bounds(time_range: dict[str, str] | None) -> tuple[datetime | None, datetime | None]:
    if not time_range:
        return None, None

    def _p(key: str) -> datetime | None:
        raw = time_range.get(key)
        if not raw:
            return None
        try:
            return datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone(UTC)
        except ValueError:
            return None

    return _p("start"), _p("end")
