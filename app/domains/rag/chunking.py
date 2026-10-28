"""知识分块（D4）：段落感知切分，长度受 e5 有效上下文约束。

- max_chars 缺省 450（中文约 1 token/字，e5 上限 512 token，预留 query/passage
  前缀与特殊 token 余量）；
- chunking_version 随知识版本快照留档——分块策略变更 = 新版本重建索引；
- token_count 为估算值（多语言单 token 平均约 2-3 字符），仅作上限参考。
"""

from __future__ import annotations

from dataclasses import dataclass

CHUNKING_VERSION = "1"


@dataclass(frozen=True)
class Chunk:
    """单个知识分块（chunk_index 从 0 递增）。"""

    index: int
    content: str
    token_count: int


def chunk_content(content: str, *, max_chars: int = 450) -> list[Chunk]:
    """段落优先、硬截断兜底的确定性分块（纯函数，便于测试）。"""
    text = content.strip()
    if not text:
        return []
    pieces: list[str] = []
    for para in text.split("\n"):
        para = para.strip()
        if not para:
            continue
        while len(para) > max_chars:
            pieces.append(para[:max_chars])
            para = para[max_chars:].strip()
        if para:
            pieces.append(para)
    chunks: list[str] = []
    buf = ""
    for piece in pieces:
        candidate = f"{buf}\n{piece}" if buf else piece
        if len(candidate) <= max_chars:
            buf = candidate
            continue
        if buf:
            chunks.append(buf)
        buf = piece
    if buf:
        chunks.append(buf)
    return [
        Chunk(index=i, content=c, token_count=max(1, (len(c) + 2) // 3))
        for i, c in enumerate(chunks)
    ]
