"""D4 单元测试：Fake Embedding 确定性、分块约束、RRF 融合纯函数。"""

from __future__ import annotations

from app.domains.rag.chunking import chunk_content
from app.domains.rag.embeddings import FakeEmbeddingProvider
from app.domains.rag.retrieval import _rrf_fuse
from app.domains.tools.evidence_quality import (
    EvidenceQuality,
    classify_evidence,
    root_cause_capable,
)

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
