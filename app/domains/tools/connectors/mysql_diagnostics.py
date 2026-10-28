"""MySQL 只读诊断连接器（collector: mysql.diagnostics，D3）。

安全约束：
- **仅预定义只读查询**（SHOW STATUS/VARIABLES + performance_schema 摘要视图），
  无通用 SQL 执行器（PRD：模型不会拿到通用 SQL）；
- 使用最小权限只读账号（部署侧授权 SELECT on performance_schema + SHOW 权限）；
- 密码经环境变量注入（secret_ref），不落库不入日志。
"""

from __future__ import annotations

from typing import Any

import aiomysql

from app.core.enums import EvidenceStatus
from app.domains.tools.connectors.base import ConnectorResult

# 预定义只读查询（白名单常量；顺序即诊断语义分组）
_PREDEFINED_QUERIES: tuple[tuple[str, str], ...] = (
    ("status", "SHOW GLOBAL STATUS LIKE 'Threads_connected'"),
    ("status", "SHOW GLOBAL STATUS LIKE 'Threads_running'"),
    ("status", "SHOW GLOBAL STATUS LIKE 'Connections'"),
    ("status", "SHOW GLOBAL STATUS LIKE 'Aborted_connects'"),
    ("status", "SHOW GLOBAL STATUS LIKE 'Slow_queries'"),
    ("variable", "SHOW GLOBAL VARIABLES LIKE 'max_connections'"),
    (
        "slow_digest",
        "SELECT DIGEST_TEXT, COUNT_STAR, "
        "ROUND(SUM_TIMER_WAIT/1e12, 3) AS total_seconds, "
        "ROUND(AVG_TIMER_WAIT/1e9, 2) AS avg_ms, "
        "SUM_ROWS_EXAMINED AS rows_examined "
        "FROM performance_schema.events_statements_summary_by_digest "
        "ORDER BY SUM_TIMER_WAIT DESC LIMIT 5",
    ),
)


async def read_mysql_diagnostics(
    *,
    host: str,
    port: int,
    user: str,
    password: str,
    database: str | None = None,
    timeout_seconds: float = 5.0,
) -> ConnectorResult:
    """执行预定义只读查询集；任一查询失败不影响其余查询的已取结果。"""
    payload: dict[str, Any] = {"sections": {}}
    try:
        conn = await aiomysql.connect(
            host=host,
            port=port,
            user=user,
            password=password,
            db=database,
            connect_timeout=int(timeout_seconds),
        )
    except Exception as exc:  # noqa: BLE001 — 连接失败统一映射，不泄漏凭据细节
        kind = type(exc).__name__
        return ConnectorResult(
            status=EvidenceStatus.UNAVAILABLE,
            payload={"host": host, "port": port, "error_type": kind},
            error_code="MYSQL_CONNECT_ERROR",
        )
    try:
        cur = await conn.cursor()
        try:
            for section, sql in _PREDEFINED_QUERIES:
                try:
                    await cur.execute(sql)
                    rows = await cur.fetchall()
                    # 同节多条查询按行聚合（D3 修复：此前逐条覆盖只剩最后一条）
                    bucket = payload["sections"].setdefault(section, {"rows": [], "row_count": 0})
                    bucket["rows"].extend(_row_to_list(r) for r in rows[:20])
                    bucket["row_count"] += len(rows)
                except Exception as exc:  # noqa: BLE001 — 单查询失败不拖垮其余部分
                    payload["sections"].setdefault("errors", {})[section] = f"{type(exc).__name__}"
        finally:
            await cur.close()
    finally:
        conn.close()
    return ConnectorResult(status=EvidenceStatus.OK, payload=payload)


def _row_to_list(row: tuple[Any, ...]) -> list[str]:
    return [str(v) for v in row]
