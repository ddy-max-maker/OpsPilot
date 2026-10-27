"""健康检查集成测试。"""

from __future__ import annotations

import httpx


async def test_health_live(client: httpx.AsyncClient) -> None:
    resp = await client.get("/api/v1/health/live")
    assert resp.status_code == 200
    body = resp.json()
    assert body["error"] is None
    assert body["data"]["status"] == "ok"
    assert body["request_id"].startswith("req_")


async def test_health_ready_with_database_up(client: httpx.AsyncClient) -> None:
    resp = await client.get("/api/v1/health/ready")
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["status"] == "ok"
    assert data["database"] == "up"


async def test_request_id_header_echoed(client: httpx.AsyncClient) -> None:
    resp = await client.get("/api/v1/health/live")
    assert resp.headers["X-Request-ID"] == resp.json()["request_id"]
