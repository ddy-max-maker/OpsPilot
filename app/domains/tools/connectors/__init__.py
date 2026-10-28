"""只读连接器包：HTTP / Docker(ro-proxy) / Redis / Prometheus / OTel / MySQL。

子模块在此显式导入，保证 `connectors.<name>` 属性访问可用
（gateway 经属性路径分发连接器）。
"""

from app.domains.tools.connectors import (
    base,
    docker_proxy,
    http_health,
    jaeger,
    mysql_diagnostics,
    prometheus,
    redis_health,
)

__all__ = [
    "base",
    "docker_proxy",
    "http_health",
    "jaeger",
    "mysql_diagnostics",
    "prometheus",
    "redis_health",
]
