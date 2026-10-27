"""演示目标应用（S1/S4 被诊断对象）：纯 stdlib HTTP 服务，无第三方依赖。

观测面：
- GET /health  → 200 {"status": "ok", ...}（资产 endpoints.health_url 指向此端点）
- GET /redis-check → 经原始 RESP PING 检查 Redis 连通（S1 注入后变失败）
故障注入不由本应用承担：S1 = 停 demo-redis 容器；S4 = 停 demo-app 容器（网关 502）。
"""

from __future__ import annotations

import json
import os
import socket
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

REDIS_HOST = os.environ.get("DEMO_REDIS_HOST", "demo-redis")
REDIS_PORT = int(os.environ.get("DEMO_REDIS_PORT", "6379"))
APP_VERSION = "demo-1.0"


def redis_ping(timeout: float = 2.0) -> dict[str, object]:
    """原始 RESP PING（只读命令）。"""
    try:
        with socket.create_connection((REDIS_HOST, REDIS_PORT), timeout=timeout) as sock:
            sock.sendall(b"*1\r\n$4\r\nPING\r\n")
            data = sock.recv(64)
        ok = data.startswith(b"+PONG")
        return {"redis": "pong" if ok else f"unexpected:{data[:20]!r}"}
    except OSError as exc:
        return {"redis": "error", "error_type": type(exc).__name__}


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/health":
            body = {"status": "ok", "app": "demo-app", "version": APP_VERSION}
            self._json(200, body)
        elif self.path == "/redis-check":
            self._json(200, redis_ping())
        else:
            self._json(404, {"error": "not found"})

    def _json(self, code: int, payload: dict[str, object]) -> None:
        data = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, fmt: str, *args: object) -> None:
        print(f"[demo-app] {self.address_string()} {fmt % args}", flush=True)


if __name__ == "__main__":
    port = int(os.environ.get("DEMO_APP_PORT", "8080"))
    print(f"[demo-app] listening on :{port} (redis={REDIS_HOST}:{REDIS_PORT})", flush=True)
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()
