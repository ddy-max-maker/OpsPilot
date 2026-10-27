"""带前缀的可读 ID 生成（ULID：48bit 时间戳 + 80bit 随机，Crockford base32）。

契约要求（CONTRACTS §1.1）：ID 形如 `as_`/`usr_`/`pol_` + 小写字母数字。
同毫秒内单调递增（randomness +1），保证进程内不碰撞。
"""

from __future__ import annotations

import os
import threading
import time

_ENCODING = "0123456789abcdefghjkmnpqrstvwxyz"  # Crockford Base32（无 i/l/o/u）
_LOCK = threading.Lock()
_last_ts_ms = 0
_last_random = 0


def _encode_128bit(value: int) -> str:
    chars: list[str] = []
    for _ in range(26):
        value, rem = divmod(value, 32)
        chars.append(_ENCODING[rem])
    return "".join(reversed(chars))


def new_ulid() -> str:
    """生成 26 位小写 ULID 字符串。"""
    global _last_ts_ms, _last_random
    with _LOCK:
        ts_ms = int(time.time() * 1000)
        if ts_ms == _last_ts_ms:
            random_ = _last_random + 1
            if random_ >= 1 << 80:  # 同毫秒 2^80 次碰撞才会溢出
                ts_ms += 1
                random_ = int.from_bytes(os.urandom(10), "big")
        else:
            random_ = int.from_bytes(os.urandom(10), "big")
        _last_ts_ms = ts_ms
        _last_random = random_
    return _encode_128bit((ts_ms << 80) | random_)


def new_id(prefix: str) -> str:
    """生成 `前缀 + ULID`，如 as_01jdb3k2m4..."""
    return f"{prefix}{new_ulid()}"
