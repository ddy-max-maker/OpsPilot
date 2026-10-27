"""ops-action 入口（D5 预留）。

D5 将在此启动 Action Service：独立于 API 进程执行经审批的有副作用操作，
携带服务端审批快照复核、幂等账本（action_idempotency）与结果 UNKNOWN 处理，
首版仅允许 sandbox/staging 的 R1 allowlist 动作（PRD §8）。

本阶段不实现任何逻辑，仅保留扩展位置。
"""

from __future__ import annotations

import sys


def main() -> None:
    sys.exit("ops-action 将在 D5 阶段实现（受控执行 + 幂等账本 + 恢复验证）")


if __name__ == "__main__":
    main()
