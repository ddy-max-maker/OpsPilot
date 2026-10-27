"""S2 故障注入容器：持续分配内存直至被 OOM Killer 杀死（真实 OOMKilled/exit 137）。"""

import time

buf: list[bytes] = []
while True:
    buf.append(b"x" * (1024 * 1024))  # 每秒 ~64MB
    if len(buf) % 64 == 0:
        print(f"[crasher] allocated {len(buf)} MB", flush=True)
    time.sleep(0.015)
