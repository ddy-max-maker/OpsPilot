"""D4 单元测试：Fake Embedding 确定性、分块约束、RRF 融合纯函数。"""

from __future__ import annotations

import sys
import threading
import time
import types
from typing import Any

import pytest

from app.domains.rag.chunking import chunk_content
from app.domains.rag.embeddings import (
    EmbeddingError,
    FakeEmbeddingProvider,
    LocalE5EmbeddingProvider,
)
from app.domains.rag.retrieval import _rrf_fuse
from app.domains.tools.evidence_quality import (
    EvidenceQuality,
    classify_evidence,
    root_cause_capable,
)


class _FakeSTModel:
    def __init__(self, name: str) -> None:
        self.name = name
        self.max_seq_length = 0


def _install_fake_sentence_transformers(
    monkeypatch: pytest.MonkeyPatch, ctor: Any
) -> dict[str, int]:
    """注入伪 sentence_transformers 模块（不联网、不装依赖组）。"""
    calls = {"n": 0}

    def _wrapped(name: str) -> _FakeSTModel:
        calls["n"] += 1
        return ctor(name)

    module = types.ModuleType("sentence_transformers")
    module.SentenceTransformer = _wrapped  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "sentence_transformers", module)
    return calls


# ---- LocalE5 预热：并发互斥 + 失败显式记录（D6）----


def test_local_e5_warm_up_is_single_load_under_concurrency(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """API 启动预热与首个发布请求并发时只加载一份模型（否则双倍内存/耗时）。"""

    def _slow_ctor(name: str) -> _FakeSTModel:
        time.sleep(0.15)  # 放大竞态窗口
        return _FakeSTModel(name)

    calls = _install_fake_sentence_transformers(monkeypatch, _slow_ctor)
    provider = LocalE5EmbeddingProvider(
        model_name="intfloat/multilingual-e5-small",
        dimension=384,
        chunking_version="1",
    )
    assert provider.ready is False
    errors: list[BaseException] = []

    def _worker() -> None:
        try:
            provider.warm_up()
        except BaseException as exc:  # noqa: BLE001 — 线程内异常需回传到主线程断言
            errors.append(exc)

    threads = [threading.Thread(target=_worker) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []
    assert calls["n"] == 1  # 只构造一次 SentenceTransformer
    assert provider.ready is True
    assert provider.load_error is None


def test_local_e5_load_failure_is_recorded_not_disguised(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """模型加载失败必须抛 EmbeddingError 且记录原因，不静默降级为可用。"""

    def _boom(name: str) -> _FakeSTModel:
        raise OSError("connection reset by peer")

    _install_fake_sentence_transformers(monkeypatch, _boom)
    provider = LocalE5EmbeddingProvider(
        model_name="intfloat/multilingual-e5-small", dimension=384, chunking_version="1"
    )
    with pytest.raises(EmbeddingError, match="connection reset"):
        provider.warm_up()
    assert provider.ready is False
    assert provider.load_error is not None and "connection reset" in provider.load_error
    # 已知失败不反复重试（避免每个请求都触发一次数十秒加载）
    with pytest.raises(EmbeddingError):
        provider.warm_up()


# ---- Fake Embedding（CI 确定性）----


def test_fake_embedding_deterministic_and_normalized() -> None:
    provider = FakeEmbeddingProvider()
    a1 = provider.embed_query("Redis 连接池耗尽")
    a2 = provider.embed_query("Redis 连接池耗尽")
    assert a1 == a2  # 同文本恒同向量（确定性）
    assert len(a1) == 384
    norm = sum(v * v for v in a1) ** 0.5
    assert abs(norm - 1.0) < 1e-6  # 归一化


def test_fake_embedding_different_text_different_vector() -> None:
    provider = FakeEmbeddingProvider()
    a = provider.embed_query("Redis 故障")
    b = provider.embed_query("MySQL 故障")
    assert a != b


def test_fake_embedding_query_passage_prefix_separated() -> None:
    """query/passage 前缀进入哈希域——同一文本两种语义前缀向量不同。"""
    provider = FakeEmbeddingProvider()
    q = provider.embed_query("磁盘写满")
    p = provider.embed_documents(["磁盘写满"])[0]
    assert q != p
    assert len(p) == 384


# ---- 分块 ----


def test_chunking_respects_max_chars() -> None:
    content = "段落一。" * 300 + "\n" + "段落二。" * 300
    chunks = chunk_content(content, max_chars=450)
    assert len(chunks) >= 3
    for chunk in chunks:
        assert len(chunk.content) <= 450
    assert [c.index for c in chunks] == list(range(len(chunks)))
    # 全部内容保留（无丢失）
    assert content.replace("\n", "").replace("。", "") in "".join(
        c.content.replace("\n", "") for c in chunks
    ).replace("。", "")


def test_chunking_empty_content() -> None:
    assert chunk_content("") == []
    assert chunk_content("   \n  ") == []


# ---- 证据质量：knowledge 绝不支撑根因（D4 红线）----


def test_knowledge_evidence_never_root_cause_capable() -> None:
    """即使采集状态为 ok，knowledge 证据也不得判定为实时根因证据。"""
    quality = classify_evidence("ok", None, None, source_type="knowledge")
    assert quality is EvidenceQuality.KNOWLEDGE
    assert root_cause_capable(quality) is False


def test_realtime_quality_semantics_unchanged() -> None:
    """向后兼容：无 source_type 时保持 D3 语义。"""
    assert classify_evidence("ok", None, None) is EvidenceQuality.HEALTHY
    assert classify_evidence("unavailable", "CONNECTOR_NOT_CONFIGURED", None).value == (
        "collection_failed"
    )
    assert classify_evidence("error", "HTTP_504", {"status_code": 504}).value == "fault_observed"


# ---- RRF 融合（纯函数）----


def _hit(chunk_id: str, route: str, rank: int) -> object:
    from app.domains.rag.retrieval import RetrievalHit

    return RetrievalHit(
        version_id=f"kbv_{chunk_id}",
        document_id="kb_1",
        doc_type="postmortem",
        title="t",
        version_no=1,
        chunk_id=chunk_id,
        content="c",
        published_at=None,
        valid_until=None,
        applies_to=None,
        embedding_model_version=None,
        scores={route: 0.5},
        ranks={route: rank},
    )


def test_rrf_fusion_merges_routes_and_ranks() -> None:
    from app.domains.rag.retrieval import RetrievalHit

    text_hits = [_hit("c1", "text", 1), _hit("c2", "text", 2), _hit("c3", "text", 3)]
    vector_hits = [_hit("c2", "vector", 1), _hit("c4", "vector", 2)]
    fused = _rrf_fuse({"text": text_hits, "vector": vector_hits})  # type: ignore[arg-type]
    # c2 双路命中（text#2 + vector#1）→ RRF 最高
    assert fused[0].chunk_id == "c2"
    assert fused[0].ranks == {"text": 2, "vector": 1}
    assert fused[0].final_rank == 1
    # 双路 RRF > 单路最高
    single = 1.0 / (60 + 1)
    assert fused[0].rrf_score > single
    assert all(isinstance(h, RetrievalHit) for h in fused)
