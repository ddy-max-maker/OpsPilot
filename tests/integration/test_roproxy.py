"""docker-ro-proxy 白名单红线测试（ADR-11）。

只测拒绝路径（不需要 docker.sock；CI 环境无 Docker daemon 也可运行）：
非 GET 方法、非白名单路径、filters 注入一律 403。
放行路径的完整链路由 E2E（S1/S2/S4 真实容器取证）覆盖。
"""

from __future__ import annotations

import json

import httpx
import pytest

from roproxy.app import app


@pytest.fixture
async def proxy_client() -> httpx.AsyncClient:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://roproxy") as client:
        yield client


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("POST", "/containers/json"),
        ("POST", "/containers/abc123/start"),
        ("DELETE", "/containers/abc123"),
        ("POST", "/containers/abc123/exec"),
        ("POST", "/exec/abc/start"),
        ("POST", "/images/create"),
        ("DELETE", "/volumes/foo"),
        ("POST", "/networks/create"),
        ("GET", "/containers/abc123/archive"),  # 下载文件
        ("GET", "/swarm"),
        ("GET", "/secrets"),
    ],
)
async def test_non_whitelisted_requests_denied(
    proxy_client: httpx.AsyncClient, method: str, path: str
) -> None:
    resp = await proxy_client.request(method, path)
    assert resp.status_code == 403
    assert resp.json()["error"] == "forbidden"


async def test_get_non_whitelisted_path_denied(proxy_client: httpx.AsyncClient) -> None:
    resp = await proxy_client.get("/containers/abc123/stats?stream=true")
    assert resp.status_code == 403


async def test_filters_injection_denied(proxy_client: httpx.AsyncClient) -> None:
    resp = await proxy_client.get(
        "/containers/json", params={"filters": json.dumps({"label": ["x"]})}
    )
    assert resp.status_code == 403


async def test_filters_malformed_json_denied(proxy_client: httpx.AsyncClient) -> None:
    resp = await proxy_client.get("/containers/json", params={"filters": "not-json"})
    assert resp.status_code == 403


async def test_whitelisted_paths_recognized_but_need_daemon(
    proxy_client: httpx.AsyncClient,
) -> None:
    """白名单路径被放行转发（无 daemon 时表现为连接错误而非 403 拒绝）。"""
    resp = await proxy_client.get(
        "/containers/json", params={"filters": json.dumps({"name": ["x"]})}
    )
    # 200 = docker.sock 可达；502 = 无 daemon（CI/本地）——两者都证明未被白名单拒绝
    assert resp.status_code in (200, 502)
