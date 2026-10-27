"""ops-worker 入口（D2 预留）。

D2 将在此启动 Agent Worker：消费 RabbitMQ 的调查任务、运行 DeepAgents/LangGraph
诊断、写回 run_events/evidence 与诊断报告快照，并通过 lease_owner + fence_token
防止旧 Worker 复活并发写脏数据（DATA_MODEL §4）。

本阶段不实现任何逻辑，仅保留扩展位置。
"""

from __future__ import annotations

import sys


def main() -> None:
    sys.exit("ops-worker 将在 D2 阶段实现（RabbitMQ 消费 + LangGraph 执行 + checkpoint）")


if __name__ == "__main__":
    main()
