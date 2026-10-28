"""docker-ro-proxy：Docker Engine API 只读白名单代理（ADR-11）。

安全模型：
- 本容器是唯一挂载 docker.sock 的组件；Worker 仅经 HTTP 访问本代理；
- 方法白名单：仅 GET；路径白名单：容器列表/inspect/日志 + daemon ping/version；
  容器启动/停止/创建/删除/exec 及其余一切一律 403；
- /logs 原始流帧解析为 JSON 行列表（Worker 不接触 docker 流协议）。
"""

from __future__ import annotations

import json
import logging
import re

import httpx
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse

DOCKER_SOCK = "/var/run/docker.sock"
DOCKER_API = "http://docker"  # UDS transport 的虚拟 base_url

logger = logging.getLogger("docker-ro-proxy")

app = FastAPI(title="docker-ro-proxy", docs_url=None, redoc_url=None, openapi_url=None)

# 白名单：方法 + 路径（完全匹配；cid 限制为 Docker 十六进制 ID 或容器名字符集）
_CONTAINER_ID = r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}"
ALLOWLIST_GET: tuple[re.Pattern[str], ...] = (
    re.compile(r"^/_ping$"),
    re.compile(r"^/version$"),
    re.compile(r"^/containers/json$"),
    re.compile(rf"^/containers/{_CONTAINER_ID}/json$"),
    re.compile(rf"^/containers/{_CONTAINER_ID}/logs$"),
    re.compile(rf"^/containers/{_CONTAINER_ID}/stats\?stream=false$"),
)

_client: httpx.AsyncClient | None = None


def _client_instance() -> httpx.AsyncClient:
    global _client
    if _client is None:
        transport = httpx.AsyncHTTPTransport(uds=DOCKER_SOCK)
        _client = httpx.AsyncClient(transport=transport, base_url=DOCKER_API)
    return _client


@app.on_event("shutdown")
async def _close() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


def _deny(reason: str) -> JSONResponse:
    # 统一 403，不区分越权细节（避免白名单探测面）
    return JSONResponse(status_code=403, content={"error": "forbidden", "reason": reason})


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.api_route(
    "/{path:path}",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"],
)
async def proxy(path: str, request: Request) -> Response:
    """唯一入口：先做方法+路径白名单，再转发到 docker.sock。"""
    if request.method != "GET":
        return _deny("only GET is allowed (read-only proxy)")
    upstream_path = "/" + path
    matched = any(p.match(upstream_path) for p in ALLOWLIST_GET)
    if not matched:
        return _deny("path not in read-only allowlist")

    # 只透传受控查询参数
    allowed_params = {
        k: v
        for k, v in request.query_params.items()
        if k in {"filters", "tail", "stdout", "stderr", "stream", "limit", "all"}
    }
    if upstream_path == "/containers/json" and "filters" in allowed_params:
        # 防注入：filters 必须是合法 JSON 且仅允许 name 键
        try:
            parsed = json.loads(allowed_params["filters"])
            if not isinstance(parsed, dict) or set(parsed) - {"name"}:
                return _deny("filters only support name")
        except json.JSONDecodeError:
            return _deny("invalid filters")

    if upstream_path.endswith("/logs"):
        return await _proxy_logs(upstream_path, allowed_params)

    try:
        client = _client_instance()
    except Exception:  # noqa: BLE001 — 无 UDS 支持的环境（如 Windows CI）
        return JSONResponse(status_code=502, content={"error": "docker transport unavailable"})
    try:
        resp = await client.get(upstream_path, params=allowed_params, timeout=10.0)
    except Exception:  # noqa: BLE001 — daemon 不可达/传输错误统一 502
        return JSONResponse(status_code=502, content={"error": "docker daemon unreachable"})
    if resp.status_code == 403 or resp.status_code == 404:
        return JSONResponse(status_code=resp.status_code, content={"error": "not found"})
    if resp.status_code != 200:
        return JSONResponse(status_code=502, content={"error": f"upstream {resp.status_code}"})
    if upstream_path == "/containers/json":
        return JSONResponse(content={"containers": resp.json()})
    if upstream_path == "/_ping":
        # Docker /_ping 返回纯文本 "OK"（非 JSON），按文本透传
        return JSONResponse(content={"ping": resp.text.strip()[:16]})
    try:
        return JSONResponse(content=resp.json())
    except ValueError:
        # 非 JSON 上游响应按文本透传（不伪造 JSON）
        return JSONResponse(content={"body": resp.text[:4096]})


async def _proxy_logs(upstream_path: str, params: dict[str, str]) -> Response:
    try:
        client = _client_instance()
    except Exception:  # noqa: BLE001 — 无 UDS 支持的环境
        return JSONResponse(status_code=502, content={"error": "docker transport unavailable"})
    query = {"stdout": "true", "stderr": "true", "tail": params.get("tail", "100")}
    try:
        resp = await client.get(upstream_path, params=query, timeout=15.0)
    except Exception:  # noqa: BLE001 — daemon 不可达统一 502
        return JSONResponse(status_code=502, content={"error": "docker daemon unreachable"})
    if resp.status_code != 200:
        return JSONResponse(status_code=502, content={"error": f"upstream {resp.status_code}"})
    return JSONResponse(content={"logs": _parse_docker_stream(resp.content)})


def _parse_docker_stream(raw: bytes) -> list[str]:
    """解析 Docker 多路复用流帧（8 字节头：type + 3 保留 + BE32 长度）为日志行。"""
    if not raw:
        return []
    # TTY 模式下无帧头：直接按行拆分
    if raw[:1] not in (b"\x00", b"\x01", b"\x02"):
        return [line for line in raw.decode("utf-8", errors="replace").splitlines() if line]
    chunks: list[bytes] = []
    offset = 0
    while offset + 8 <= len(raw):
        size = int.from_bytes(raw[offset + 4 : offset + 8], "big")
        payload = raw[offset + 8 : offset + 8 + size]
        chunks.append(payload)
        offset += 8 + size
    text = b"".join(chunks).decode("utf-8", errors="replace")
    return [line for line in text.splitlines() if line]
