"""评估专用确定性演示环境 stub（D6，PRD §13 故障注入）。

评估执行器在进程内启动的轻量模拟对端，覆盖只读连接器依赖的协议：
- http.health_check：GET /health/{name} → 200 / 5xx / 挂起超时；
- docker-ro-proxy：GET /containers/json、/containers/{id}/json、/containers/{id}/logs；
- jaeger：GET /api/traces?service=；
- prometheus：GET /api/v1/query?query=；
- redis：RESP 协议 PING / INFO（正常应答或挂起超时）。

确定性原则：
- 行为完全由 behaviors 配置驱动（端口分配后按 case 覆写）；
- 未监听的端口 = 连接拒绝（确定性故障，如服务进程不存在）；
- 仅监听 127.0.0.1 回环，评估结束即关闭。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from typing import Any


def _http_response(status: int, body: dict[str, Any] | list[Any]) -> bytes:
    payload = json.dumps(body).encode()
    reason = {
        200: "OK",
        404: "Not Found",
        403: "Forbidden",
        500: "Internal Server Error",
        503: "Service Unavailable",
    }.get(status, "OK")
    head = (
        f"HTTP/1.1 {status} {reason}\r\n"
        "Content-Type: application/json\r\n"
        f"Content-Length: {len(payload)}\r\n"
        "Connection: close\r\n\r\n"
    ).encode()
    return head + payload


class DemoStubs:
    """演示环境 stub：单 HTTP 端口（健康/Docker 代理/Jaeger/Prom）+ 可选 Redis 端口。"""

    def __init__(self) -> None:
        self.http_port: int | None = None
        self.redis_port: int | None = None
        self._servers: list[asyncio.Server] = []
        # HTTP 行为表：path 前缀 → ("json", status, body) | ("timeout",)
        #              | ("fail_after_n", 通过次数, 之后的状态码)（先健康后失败）
        self.http_behaviors: dict[str, tuple[str, Any]] = {}
        self.http_counters: dict[str, int] = {}
        # Docker 代理模拟：容器名 → inspect 状态（state/exit_code/oom_killed/running）
        self.containers: dict[str, dict[str, Any]] = {}
        # 容器日志：容器名 → 行列表
        self.container_logs: dict[str, list[str]] = {}
        # jaeger：服务名 → trace 载荷（可含 anomaly 标记）
        self.jaeger: dict[str, dict[str, Any]] = {}
        # prometheus：服务名 → 查询结果载荷（可含 anomaly 标记）
        self.prometheus: dict[str, dict[str, Any]] = {}
        # redis："ok"（+PONG/INFO）| "timeout"（挂起）
        self.redis_behavior: str = "ok"

    # ---- 生命周期 ----

    async def start(self, *, with_redis: bool = True) -> None:
        http_server = await asyncio.start_server(self._handle_http, "127.0.0.1", 0)
        self._servers.append(http_server)
        self.http_port = int(http_server.sockets[0].getsockname()[1])
        if with_redis:
            redis_server = await asyncio.start_server(self._handle_redis, "127.0.0.1", 0)
            self._servers.append(redis_server)
            self.redis_port = int(redis_server.sockets[0].getsockname()[1])

    async def stop(self) -> None:
        for server in self._servers:
            server.close()
            await server.wait_closed()
        self._servers.clear()
        self.http_port = None
        self.redis_port = None

    def base_url(self) -> str:
        assert self.http_port is not None, "stubs not started"
        return f"http://127.0.0.1:{self.http_port}"

    def redis_dsn(self) -> str:
        assert self.redis_port is not None, "stubs not started"
        return f"redis://127.0.0.1:{self.redis_port}/0"

    # ---- HTTP ----

    async def _handle_http(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            request_line = await asyncio.wait_for(reader.readline(), timeout=5.0)
            # 消费其余头部（内容无关紧要：GET 无 body）
            while True:
                line = await asyncio.wait_for(reader.readline(), timeout=2.0)
                if line in (b"\r\n", b"\n", b""):
                    break
            parts = request_line.decode("latin-1").split()
            if len(parts) < 2:
                writer.write(_http_response(400, {"error": "bad request"}))
                return
            path = parts[1]
            await self._route(writer, path)
        except Exception:  # noqa: BLE001 — stub 端异常不影响评估主流程
            pass
        finally:
            try:
                await writer.drain()
                writer.close()
            except Exception:  # noqa: BLE001
                pass

    async def _route(self, writer: asyncio.StreamWriter, raw_path: str) -> None:
        path, _, query = raw_path.partition("?")
        # timeout 行为：挂起超过连接器超时（评估配置 1s，此处挂 3s）
        for prefix, behavior in self.http_behaviors.items():
            if path.startswith(prefix) and behavior[0] == "timeout":
                await asyncio.sleep(3.0)
                writer.write(_http_response(200, {"note": "late"}))
                return
        if path.startswith("/health/"):
            for prefix, behavior in self.http_behaviors.items():
                if path == prefix or path.startswith(prefix):
                    kind, arg = behavior
                    if kind == "fail_after_n":
                        passed_n, fail_status = int(arg[0]), int(arg[1])
                        count = self.http_counters.get(path, 0)
                        self.http_counters[path] = count + 1
                        if count < passed_n:
                            writer.write(_http_response(200, {"status": "ok"}))
                        else:
                            writer.write(_http_response(fail_status, {"status": "degraded"}))
                        return
                    if kind == "json":
                        writer.write(_http_response(int(arg), {"status": "stub"}))
                        return
            writer.write(_http_response(200, {"status": "ok", "service": path}))
            return
        if path == "/containers/json":
            name_filter = ""
            if "filters=" in query:
                raw = query.split("filters=", 1)[1]
                try:
                    filters = json.loads(raw)
                    names = filters.get("name") or []
                    name_filter = str(names[0]) if names else ""
                except json.JSONDecodeError:
                    name_filter = ""
            matched = []
            for cname in self.containers:
                if name_filter and name_filter not in cname:
                    continue
                matched.append({"Id": f"ctr_{cname}", "Names": [f"/{cname}"], "State": "running"})
            writer.write(_http_response(200, {"containers": matched}))
            return
        if path.startswith("/containers/") and path.endswith("/json"):
            cid = path[len("/containers/") : -len("/json")]
            name = cid.removeprefix("ctr_")
            spec: dict[str, Any] | None = self.containers.get(name)
            if spec is None:
                writer.write(_http_response(404, {"message": "no such container"}))
                return
            state = {
                "Status": str(spec.get("state", "running")),
                "Running": bool(spec.get("running", spec.get("state", "running") == "running")),
                "OOMKilled": bool(spec.get("oom_killed", False)),
                "ExitCode": int(spec.get("exit_code", 0)),
                "StartedAt": "2026-10-28T00:00:00Z",
                "FinishedAt": (
                    "2026-10-28T01:00:00Z"
                    if spec.get("state") == "exited"
                    else "0001-01-01T00:00:00Z"
                ),
            }
            writer.write(
                _http_response(
                    200,
                    {
                        "Id": cid,
                        "Name": f"/{name}",
                        "State": state,
                        "RestartCount": int(spec.get("restart_count", 0)),
                    },
                )
            )
            return
        if path.startswith("/containers/") and path.endswith("/logs"):
            cid = path[len("/containers/") : -len("/logs")]
            name = cid.removeprefix("ctr_")
            lines = self.container_logs.get(name)
            if name not in self.containers:
                writer.write(_http_response(404, {"message": "no such container"}))
                return
            writer.write(_http_response(200, {"logs": lines or []}))
            return
        if path == "/api/traces":
            service = ""
            if "service=" in query:
                service = query.split("service=", 1)[1].split("&", 1)[0]
            payload = self.jaeger.get(service)
            if payload is None:
                writer.write(_http_response(200, {"data": []}))
            else:
                writer.write(_http_response(200, payload))
            return
        if path == "/api/v1/query":
            result: dict[str, Any] = {
                "status": "success",
                "data": {"resultType": "vector", "result": []},
            }
            # 解析 PromQL 中的 service="..."（URL 编码 service%3D%22）按服务返回注入数据
            service = ""
            if "service%3D%22" in query:
                service = query.split("service%3D%22", 1)[1].split("%22", 1)[0]
            elif 'service="' in query:
                service = query.split('service="', 1)[1].split('"', 1)[0]
            payload = self.prometheus.get(service) if service else None
            if payload is not None:
                result = payload
            writer.write(_http_response(200, result))
            return
        writer.write(_http_response(404, {"message": f"no route for {path}"}))

    # ---- Redis RESP ----

    async def _handle_redis(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            while True:
                line = await asyncio.wait_for(reader.readline(), timeout=10.0)
                if not line:
                    return
                if line.startswith(b"*"):
                    count = int(line[1:].strip())
                    args: list[bytes] = []
                    for _ in range(count):
                        head = await asyncio.wait_for(reader.readline(), timeout=5.0)
                        if not head.startswith(b"$"):
                            return
                        length = int(head[1:].strip())
                        payload = await reader.readexactly(length + 2)  # 含 \r\n
                        args.append(payload[:length])
                else:
                    args = [line.strip()]
                cmd = args[0].decode("latin-1").upper() if args else ""
                if self.redis_behavior == "timeout":
                    # 挂起超过连接器 socket 超时（评估配置 1s）→ 客户端超时
                    await asyncio.sleep(3.0)
                if cmd == "PING":
                    writer.write(b"+PONG\r\n")
                elif cmd == "INFO":
                    info = (
                        b"# Server\r\nredis_version:7.2.0\r\n"
                        b"# Clients\r\nconnected_clients:4\r\n"
                        b"# Memory\r\nused_memory_human:2.5M\r\n"
                        b"# Stats\r\ntotal_connections_received:100\r\n"
                        b"rejected_connections:0\r\nevicted_keys:0\r\n"
                        b"# Replication\r\nrole:master\r\n"
                    )
                    writer.write(b"$" + str(len(info)).encode() + b"\r\n" + info + b"\r\n")
                else:
                    writer.write(b"-ERR unknown command\r\n")
                await writer.drain()
        except Exception:  # noqa: BLE001 — stub 端异常等同连接关闭
            pass
        finally:
            with contextlib.suppress(Exception):
                writer.close()
