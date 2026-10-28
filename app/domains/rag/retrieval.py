"""混合检索（D4）：权限前置过滤 → 文本/向量双路召回 → RRF 融合 → 引用输出。

红线（IMPLEMENTATION_PLAN §3.2 测试要求）：
- 权限/状态/有效期/资产范围/环境过滤**必须发生在排序与模型上下文之前**——
  本模块在 SQL WHERE 中完成全部过滤，禁止"全局 Top-K 后 Python 剔除越权"；
- 不同 embedding 版本的向量不静默混合（检索强制过滤 model_version）；
- 小规模知识集采用**精确向量排序**（事务内关闭索引扫描），保证选择性过滤下
  的召回正确性；HNSW 索引按契约建立，规模化后可切换迭代扫描。

融合：RRF（Reciprocal Rank Fusion，k=60）；每路得分/排名/最终排名进 retrieval_events。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.domains.rag.embeddings import EmbeddingProvider
from app.models import RetrievalEvent

_RRF_K = 60
# 每路召回的候选上限（融合前；>= 最终 limit，保证单路也有足够候选）
_ROUTE_LIMIT_DEFAULT = 8


@dataclass(frozen=True)
class RetrievalHit:
    """单条检索命中（chunk 级；引用元数据齐全，供 Evidence 与 Prompt 输出）。"""

    version_id: str
    document_id: str
    doc_type: str
    title: str
    version_no: int
    chunk_id: str
    content: str
    published_at: str | None
    valid_until: str | None
    applies_to: dict[str, Any] | None
    embedding_model_version: str | None
    # 各路原始得分（text：相关性越大越好；vector：余弦距离越小越好）
    scores: dict[str, float] = field(default_factory=dict)
    # 各路排名（1 起）
    ranks: dict[str, int] = field(default_factory=dict)
    # 融合最终排名（1 起）与 RRF 得分
    final_rank: int = 0
    rrf_score: float = 0.0


@dataclass(frozen=True)
class RetrievalResponse:
    """检索结果（含每路得分/最终排名，透传 retrieval_events 记录）。"""

    mode: str
    query: str
    hits: list[RetrievalHit]
    route_ranks: dict[str, list[dict[str, Any]]]
    latency_ms: int
    filters: dict[str, Any]


# ---- 权限/时效过滤（SQL 前置，两条检索路共用）----
# jsonb_exists_any/jsonb_exists 为 ?| / ? 运算符的函数形式（避免驱动参数解析歧义）
# D5 修复④：sensitivity=restricted 仅对全量授权视角（admin/all_assets）可见——
# 不能仅凭环境标签或"通用知识（applies_to 为空）"泄露给非全量授权用户。

_FILTER_SQL = """
    v.status = 'published'
    AND (v.valid_from IS NULL OR v.valid_from <= now())
    AND (v.valid_until IS NULL OR v.valid_until > now())
    AND (v.sensitivity = 'internal' OR CAST(:all_assets AS boolean))
    AND (
        CAST(:all_assets AS boolean)
        OR COALESCE(jsonb_array_length(v.applies_to->'asset_ids'), 0) = 0
        OR jsonb_exists_any(v.applies_to->'asset_ids', :asset_ids)
    )
    AND (
        CAST(:env AS text) IS NULL
        OR COALESCE(jsonb_array_length(v.applies_to->'environments'), 0) = 0
        OR jsonb_exists(v.applies_to->'environments', CAST(:env AS text))
    )
"""

_SELECT_COLUMNS = """
    v.id AS version_id, v.document_id, d.doc_type, d.title, v.version_no,
    v.published_at, v.valid_until, v.applies_to, v.embedding_model_version,
    c.id AS chunk_id, c.content
"""


def _vec_literal(vector: list[float]) -> str:
    """pgvector 文本字面量（:qvec::vector 绑定）。"""
    return "[" + ",".join(f"{v:.7f}" for v in vector) + "]"


def _hit_from_row(row: Any) -> RetrievalHit:
    return RetrievalHit(
        version_id=str(row.version_id),
        document_id=str(row.document_id),
        doc_type=str(row.doc_type),
        title=str(row.title),
        version_no=int(row.version_no),
        chunk_id=str(row.chunk_id),
        content=str(row.content),
        published_at=row.published_at.isoformat() if row.published_at else None,
        valid_until=row.valid_until.isoformat() if row.valid_until else None,
        applies_to=dict(row.applies_to) if row.applies_to else None,
        embedding_model_version=row.embedding_model_version,
    )


async def _text_route(
    session: AsyncSession,
    *,
    query: str,
    doc_types: tuple[str, ...] | None,
    asset_ids: list[str],
    all_assets: bool,
    environment: str | None,
    limit: int,
) -> list[tuple[RetrievalHit, float]]:
    """文本路：PostgreSQL 全文（simple 配置）+ 逐词 ILIKE 兜底（多语言关键词）。

    D5 修复③：返回 (hit, route_score)——真实文本相关性分数进 retrieval_events。
    """
    tokens = [t for t in query.split() if t]
    likes = [f"%{t}%" for t in tokens]
    sql = text(
        f"""
        SELECT {_SELECT_COLUMNS},
               GREATEST(
                   ts_rank(c.content_tsv, websearch_to_tsquery('simple', :q)),
                   CASE WHEN c.content ILIKE ANY(CAST(:likes AS text[])) THEN 0.01 ELSE 0 END
               ) AS route_score
        FROM knowledge_chunks c
        JOIN knowledge_versions v ON v.id = c.version_id
        JOIN knowledge_documents d ON d.id = v.document_id
        WHERE {_FILTER_SQL}
          AND (
              c.content_tsv @@ websearch_to_tsquery('simple', :q)
              OR c.content ILIKE ANY(CAST(:likes AS text[]))
          )
          {"" if doc_types is None else "AND d.doc_type = ANY(:doc_types)"}
        ORDER BY route_score DESC, c.id
        LIMIT :k
        """
    )
    params: dict[str, Any] = {
        "q": query,
        "likes": likes or ["%"],  # 空查询 → 匹配所有（由调用方空查询守卫兜底）
        "asset_ids": asset_ids,
        "all_assets": all_assets,
        "env": environment,
        "k": limit,
    }
    if doc_types is not None:
        params["doc_types"] = list(doc_types)
    rows = (await session.execute(sql, params)).all()
    return [(_hit_from_row(r), float(r.route_score)) for r in rows]


async def _vector_route(
    session: AsyncSession,
    *,
    query_vector: list[float],
    doc_types: tuple[str, ...] | None,
    asset_ids: list[str],
    all_assets: bool,
    environment: str | None,
    model_version: str,
    limit: int,
) -> list[tuple[RetrievalHit, float]]:
    """向量路：pgvector 余弦距离；**过滤在排序前**（SQL WHERE），精确距离排序。

    D5 修复③：返回 (hit, route_score)——真实余弦距离进 retrieval_events。
    """
    sql = text(
        f"""
        SELECT {_SELECT_COLUMNS},
               (e.vector <=> CAST(:qvec AS vector)) AS route_score
        FROM knowledge_embeddings e
        JOIN knowledge_chunks c ON c.id = e.chunk_id
        JOIN knowledge_versions v ON v.id = c.version_id
        JOIN knowledge_documents d ON d.id = v.document_id
        WHERE e.model_version = :mv
          AND {_FILTER_SQL}
          {"" if doc_types is None else "AND d.doc_type = ANY(:doc_types)"}
        ORDER BY e.vector <=> CAST(:qvec AS vector)
        LIMIT :k
        """
    )
    params: dict[str, Any] = {
        "qvec": _vec_literal(query_vector),
        "mv": model_version,
        "asset_ids": asset_ids,
        "all_assets": all_assets,
        "env": environment,
        "k": limit,
    }
    if doc_types is not None:
        params["doc_types"] = list(doc_types)
    # 小规模知识集：事务内关闭索引扫描 → 精确距离排序（过滤后召回正确性优先；
    # HNSW 索引仍按契约建立，规模化后切 pgvector 迭代扫描）
    await session.execute(text("SET LOCAL enable_indexscan = off"))
    await session.execute(text("SET LOCAL enable_bitmapscan = off"))
    rows = (await session.execute(sql, params)).all()
    return [(_hit_from_row(r), float(r.route_score)) for r in rows]


def _annotate(route: str, hits: list[RetrievalHit], scores: list[float]) -> list[RetrievalHit]:
    """为单路命中补充该路的原始得分与排名。"""
    annotated: list[RetrievalHit] = []
    for rank, (hit, score) in enumerate(zip(hits, scores, strict=True), start=1):
        annotated.append(
            RetrievalHit(
                version_id=hit.version_id,
                document_id=hit.document_id,
                doc_type=hit.doc_type,
                title=hit.title,
                version_no=hit.version_no,
                chunk_id=hit.chunk_id,
                content=hit.content,
                published_at=hit.published_at,
                valid_until=hit.valid_until,
                applies_to=hit.applies_to,
                embedding_model_version=hit.embedding_model_version,
                scores={route: score},
                ranks={route: rank},
            )
        )
    return annotated


def _rrf_fuse(routes: dict[str, list[RetrievalHit]]) -> list[RetrievalHit]:
    """RRF 融合：score = Σ 1/(k + rank)；同 chunk 多路命中分数累加、排名合并。"""
    fused: dict[str, dict[str, Any]] = {}
    for hits in routes.values():
        for hit in hits:
            entry = fused.get(hit.chunk_id)
            contribution = 1.0 / (_RRF_K + hit.ranks.get("text", hit.ranks.get("vector", 999)))
            if entry is None:
                fused[hit.chunk_id] = {
                    "hit": hit,
                    "rrf": contribution,
                    "scores": dict(hit.scores),
                    "ranks": dict(hit.ranks),
                }
            else:
                entry["rrf"] += contribution
                entry["scores"].update(hit.scores)
                entry["ranks"].update(hit.ranks)
    ordered = sorted(fused.values(), key=lambda e: -e["rrf"])
    result: list[RetrievalHit] = []
    for rank, entry in enumerate(ordered, start=1):
        base: RetrievalHit = entry["hit"]
        result.append(
            RetrievalHit(
                version_id=base.version_id,
                document_id=base.document_id,
                doc_type=base.doc_type,
                title=base.title,
                version_no=base.version_no,
                chunk_id=base.chunk_id,
                content=base.content,
                published_at=base.published_at,
                valid_until=base.valid_until,
                applies_to=base.applies_to,
                embedding_model_version=base.embedding_model_version,
                scores=entry["scores"],
                ranks=entry["ranks"],
                final_rank=rank,
                rrf_score=entry["rrf"],
            )
        )
    return result


async def search_knowledge(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    query: str,
    mode: str,
    authorized_asset_ids: frozenset[str] | set[str],
    all_assets: bool = False,
    environment: str | None = None,
    doc_types: tuple[str, ...] | None = ("postmortem", "guide"),
    limit: int = 5,
    provider: EmbeddingProvider | None = None,
    run_id: str | None = None,
    used_by_agent: bool = False,
) -> RetrievalResponse:
    """混合检索入口（mode ∈ {text-only, hybrid}；off 由调用方拦截）。

    权限语义：all_assets（Admin/全量授权）时不做资产过滤；否则仅召回
    applies_to.asset_ids 为空（通用知识）或与授权资产集合相交的已发布知识。
    每次调用写 retrieval_events（模式/过滤/命中/每路排名/耗时）。
    """
    started = time.monotonic()
    asset_ids = sorted(authorized_asset_ids)
    if not query.strip():
        # 空查询守卫：不做任何检索（不产生全表匹配）
        return RetrievalResponse(
            mode=mode,
            query=query,
            hits=[],
            route_ranks={},
            latency_ms=0,
            filters={"mode": mode, "empty_query": True},
        )
    route_limit = max(limit, _ROUTE_LIMIT_DEFAULT)
    routes: dict[str, list[RetrievalHit]] = {}
    async with session_factory() as session:
        if mode in ("text-only", "hybrid"):
            # D5 修复③：携带真实 route_score（此前 getattr(hits) 恒 0.0）
            hits_with_scores = await _text_route(
                session,
                query=query,
                doc_types=doc_types,
                asset_ids=asset_ids,
                all_assets=all_assets,
                environment=environment,
                limit=route_limit,
            )
            routes["text"] = _annotate(
                "text", [h for h, _ in hits_with_scores], [s for _, s in hits_with_scores]
            )
        provider_version: str | None = None
        if mode == "hybrid":
            if provider is None:
                from app.domains.rag.embeddings import get_embedding_provider  # noqa: PLC0415

                provider = get_embedding_provider()
            provider_version = provider.model_version
            # D5 修复①：查询向量在工作线程推理（不阻塞事件循环）
            query_vector = await provider.aembed_query(query)
            hits_with_scores = await _vector_route(
                session,
                query_vector=query_vector,
                doc_types=doc_types,
                asset_ids=asset_ids,
                all_assets=all_assets,
                environment=environment,
                model_version=provider.model_version,
                limit=route_limit,
            )
            routes["vector"] = _annotate(
                "vector", [h for h, _ in hits_with_scores], [s for _, s in hits_with_scores]
            )

    if not routes:
        final: list[RetrievalHit] = []
    elif len(routes) == 1:
        final = [
            RetrievalHit(
                version_id=h.version_id,
                document_id=h.document_id,
                doc_type=h.doc_type,
                title=h.title,
                version_no=h.version_no,
                chunk_id=h.chunk_id,
                content=h.content,
                published_at=h.published_at,
                valid_until=h.valid_until,
                applies_to=h.applies_to,
                embedding_model_version=h.embedding_model_version,
                scores=h.scores,
                ranks=h.ranks,
                final_rank=rank,
                rrf_score=1.0 / (_RRF_K + rank),
            )
            for rank, h in enumerate(next(iter(routes.values())), start=1)
        ]
    else:
        final = _rrf_fuse(routes)
    final = final[:limit]
    latency_ms = int((time.monotonic() - started) * 1000)

    route_ranks = {
        route: [
            {
                "chunk_id": h.chunk_id,
                "version_id": h.version_id,
                "rank": h.ranks.get(route, rank),
                "score": h.scores.get(route, 0.0),
            }
            for rank, h in enumerate(hits, start=1)
        ]
        for route, hits in routes.items()
    }
    filters = {
        "mode": mode,
        "all_assets": all_assets,
        "authorized_asset_count": len(asset_ids),
        "environment": environment,
        "doc_types": list(doc_types) if doc_types else None,
        "embedding_model_version": provider_version,
    }
    async with session_factory() as session:
        session.add(
            RetrievalEvent(
                run_id=run_id,
                query=query[:2000],
                filters=filters,
                result_version_ids=list(dict.fromkeys(h.version_id for h in final)),
                ranks={
                    **route_ranks,
                    "final": [
                        {
                            "chunk_id": h.chunk_id,
                            "version_id": h.version_id,
                            "rank": h.final_rank,
                            "rrf": h.rrf_score,
                        }
                        for h in final
                    ],
                },
                used_by_agent=used_by_agent,
                latency_ms=latency_ms,
            )
        )
        await session.commit()
    return RetrievalResponse(
        mode=mode,
        query=query,
        hits=final,
        route_ranks=route_ranks,
        latency_ms=latency_ms,
        filters=filters,
    )


async def search_runbooks(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    query: str,
    environment: str | None = None,
    limit: int = 5,
) -> list[dict[str, Any]]:
    """Runbook 检索（text-only；published 状态 + 环境白名单过滤，D5 执行模板准备）。"""
    sql = text(
        """
        SELECT rv.id AS version_id, rv.runbook_id, r.name, r.description, r.risk_level,
               rv.version_no, rv.steps, rv.preconditions, rv.parameters_schema,
               rv.allowed_environments, rv.published_at
        FROM runbook_versions rv
        JOIN runbooks r ON r.id = rv.runbook_id
        WHERE rv.status = 'published'
          AND r.status = 'published'
          AND (
              CAST(:env AS text) IS NULL
              OR COALESCE(jsonb_array_length(rv.allowed_environments), 0) = 0
              OR jsonb_exists(rv.allowed_environments, CAST(:env AS text))
          )
          AND (
              r.name ILIKE :like
              OR COALESCE(r.description, '') ILIKE :like
              OR COALESCE(rv.steps::text, '') ILIKE :like
          )
        ORDER BY r.name, rv.version_no DESC
        LIMIT :k
        """
    )
    async with session_factory() as session:
        rows = (
            await session.execute(
                sql,
                {"env": environment, "like": f"%{query}%", "k": limit},
            )
        ).all()
    return [
        {
            "version_id": str(r.version_id),
            "runbook_id": str(r.runbook_id),
            "name": str(r.name),
            "description": r.description,
            "risk_level": str(r.risk_level),
            "version_no": int(r.version_no),
            "steps": r.steps,
            "preconditions": r.preconditions,
            "parameters_schema": r.parameters_schema,
            "allowed_environments": r.allowed_environments,
            "published_at": r.published_at.isoformat() if r.published_at else None,
        }
        for r in rows
    ]


async def build_version_embeddings(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    version_id: str,
    provider: EmbeddingProvider,
    max_chunk_chars: int = 450,
) -> int:
    """知识版本分块 + 向量索引（发布时调用；幂等，已存在的块/向量不重建）。

    版本快照写入 knowledge_versions.embedding_model/_version/vector_dimension/
    chunking_version；向量行主键 (chunk_id, model_version)——换模型 = 新增行，
    不覆盖历史向量（DATA_MODEL §3.6）。
    """
    from app.domains.rag.chunking import chunk_content  # noqa: PLC0415
    from app.models import KnowledgeChunk, KnowledgeVersion  # noqa: PLC0415

    async with session_factory() as session:
        version = await session.get(KnowledgeVersion, version_id)
        if version is None:
            raise ValueError(f"knowledge version '{version_id}' not found")
        chunks = (
            await session.execute(
                text(
                    "SELECT id, content FROM knowledge_chunks "
                    "WHERE version_id = :vid ORDER BY chunk_index"
                ),
                {"vid": version_id},
            )
        ).all()
        if not chunks:
            pieces = chunk_content(version.content, max_chars=max_chunk_chars)
            for piece in pieces:
                session.add(
                    KnowledgeChunk(
                        id=f"kcc_{version_id[4:]}_{piece.index}",  # kbv_ → kcc_ 前缀派生
                        version_id=version_id,
                        chunk_index=piece.index,
                        content=piece.content,
                        token_count=piece.token_count,
                    )
                )
            await session.flush()
            chunks = (
                await session.execute(
                    text(
                        "SELECT id, content FROM knowledge_chunks "
                        "WHERE version_id = :vid ORDER BY chunk_index"
                    ),
                    {"vid": version_id},
                )
            ).all()
        existing = (
            await session.execute(
                text(
                    "SELECT chunk_id FROM knowledge_embeddings "
                    "WHERE model_version = :mv AND chunk_id = ANY(:cids)"
                ),
                {
                    "mv": provider.model_version,
                    "cids": [str(c.id) for c in chunks],
                },
            )
        ).scalars()
        done = set(existing)
        missing = [c for c in chunks if str(c.id) not in done]
        if missing:
            vectors = provider.embed_documents([str(c.content) for c in missing])
            for chunk, vec in zip(missing, vectors, strict=True):
                await session.execute(
                    text(
                        "INSERT INTO knowledge_embeddings "
                        "(chunk_id, model, model_version, vector, created_at) "
                        "VALUES (:cid, :model, :mv, CAST(:vec AS vector), now())"
                    ),
                    {
                        "cid": str(chunk.id),
                        "model": provider.model_name,
                        "mv": provider.model_version,
                        "vec": _vec_literal(vec),
                    },
                )
        version.embedding_model = provider.model_name
        version.embedding_model_version = provider.model_version
        version.vector_dimension = provider.dimension
        version.chunking_version = provider.chunking_version
        await session.commit()
        return len(chunks)
