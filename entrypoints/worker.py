"""ops-worker：DeepAgents/LangGraph 诊断执行进程（D2）。

职责（ARCHITECTURE §2.2/§3.2/§3.3）：
- DB 任务认领（FOR UPDATE SKIP LOCKED + 租约 + fence_token，ADR-4/ADR-12）；
- DeepAgents 单主 Agent 诊断执行（ADR-D2：单执行核心）；
- run_events / evidence / hypotheses / diagnostic_reports 业务落库；
- 预算与取消的服务端强制；incident 终态裁决（unknown → NEEDS_HUMAN）。

本进程不挂载 docker.sock（Docker 诊断经 docker-ro-proxy）；不持有任何写凭据。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import sys

from app.core.config import get_settings
from app.worker.loop import AgentWorker


def main() -> None:
    # Windows 默认 ProactorEventLoop 不支持 psycopg 异步模式（checkpoint 连接池）
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    settings = get_settings()
    worker = AgentWorker(settings)

    async def _run() -> None:
        task = asyncio.create_task(worker.run_forever())
        with contextlib.suppress(asyncio.CancelledError):
            await task

    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        print("ops-worker stopped", file=sys.stderr)


if __name__ == "__main__":
    main()
