"""Docker Engine API 受控客户端（UDS；仅封装 allowlist 动作所需的极小操作面）。

安全模型（与 docker-ro-proxy 相互独立、互不扩权）：
- 本模块只提供 inspect 与 restart 两个操作，没有通用命令执行器；
- 容器名必须先经 allowlist 校验（opspilot-demo-* 前缀）才会到达这里；
- 所有异常向上抛出，由执行器归类为 failed / unknown。
"""

from __future__ import annotations

from typing import Any

import httpx

DOCKER_SOCK = "/var/run/docker.sock"
DOCKER_API = "http://docker"  # UDS transport 的虚拟 base_url
# Windows 测试环境可通过环境变量切换命名管道（真实部署仅 UDS）
DOCKER_SOCKET_OVERRIDE: str | None = None


class DockerClient:
    """极小 Docker Engine API 客户端（无 SDK 依赖；UDS/命名管道二选一）。"""

    def __init__(self, *, socket_path: str | None = None, timeout: float = 30.0) -> None:
        self._socket = socket_path or DOCKER_SOCKET_OVERRIDE or DOCKER_SOCK
        self._timeout = timeout
        self._client: httpx.AsyncClient | None = None

    def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None:
            transport = httpx.AsyncHTTPTransport(uds=self._socket)
            self._client = httpx.AsyncClient(
                transport=transport, base_url=DOCKER_API, timeout=self._timeout
            )
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def inspect_container(self, name: str) -> dict[str, Any] | None:
        """inspect 容器；404 返回 None（容器不存在是确定事实）。"""
        client = self._ensure_client()
        resp = await client.get(f"/containers/{name}/json")
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        data: dict[str, Any] = resp.json()
        return data

    async def restart_container(self, name: str, *, timeout_seconds: int = 30) -> None:
        """重启容器（Docker Engine API POST /containers/{id}/restart?t=）。"""
        client = self._ensure_client()
        resp = await client.post(f"/containers/{name}/restart", params={"t": timeout_seconds})
        resp.raise_for_status()
