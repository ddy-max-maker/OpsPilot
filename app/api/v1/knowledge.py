"""Knowledge API（CONTRACTS §5，D4 交付）：

- GET/POST /knowledge/documents                       文档列表 / 创建草稿（D4）
- GET  /knowledge/documents/{id}                      文档详情（含版本与状态）
- POST /knowledge/documents/{id}/versions             追加草稿版本（D4）
- POST /knowledge/versions/{id}/review                人工复核（四眼：reviewer≠author）
- POST /knowledge/versions/{id}/publish               发布（构建向量索引；旧版本自动 superseded）
- POST /knowledge/versions/{id}/retire                退役（退出检索范围）
- GET  /knowledge/search                              权限前置过滤 + text/hybrid 检索
- POST /knowledge/from-report/{run_id}                诊断报告 → 知识草稿（人工触发，不自动发布）

RBAC：创建/版本追加 = operator+admin；复核/发布/退役 = admin（四眼在服务端强制）；
检索 = 认证用户（按资产范围过滤）。草稿/未发布知识对 Agent 不可见。
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy import select

from app.api.deps import CurrentUser, ReadyUserDep, SessionDep, require_roles
from app.core.config import get_settings
from app.core.db import get_session_factory
from app.core.responses import ok
from app.domains.knowledge import service as knowledge_service
from app.domains.knowledge.schemas import (
    KnowledgeCreateRequest,
    KnowledgeVersionCreateRequest,
)
from app.domains.rag.embeddings import EmbeddingProvider, get_embedding_provider
from app.domains.rag.retrieval import search_knowledge
from app.models import KnowledgeDocument, KnowledgeVersion

router = APIRouter(tags=["knowledge"])

AdminDep = Annotated[CurrentUser, Depends(require_roles("admin"))]
AuthorDep = Annotated[CurrentUser, Depends(require_roles("admin", "operator"))]


def _provider() -> EmbeddingProvider:
    """进程级复用的 Embedding Provider（D6）。

    此前每次发布都新建 Provider：本地 e5 模型会被反复加载（单次数十秒 + 每份
    数百 MB 常驻内存），发布接口因此长时间阻塞并由 GC 慢慢释放。复用单例后
    首次发布命中启动预热（app.main.lifespan 后台 warm_up），后续发布为纯推理。
    """
    return get_embedding_provider()


def _version_view(v: KnowledgeVersion) -> dict[str, Any]:
    return {
        "id": v.id,
        "document_id": v.document_id,
        "version_no": v.version_no,
        "status": v.status,
        "change_note": v.change_note,
        "created_by": v.created_by,
        "reviewed_by": v.reviewed_by,
        "published_at": v.published_at.isoformat() if v.published_at else None,
        "valid_from": v.valid_from.isoformat() if v.valid_from else None,
        "valid_until": v.valid_until.isoformat() if v.valid_until else None,
        "sensitivity": v.sensitivity,
        "applies_to": v.applies_to,
        "embedding_model": v.embedding_model,
        "embedding_model_version": v.embedding_model_version,
        "vector_dimension": v.vector_dimension,
        "chunking_version": v.chunking_version,
        "created_at": v.created_at.isoformat() if v.created_at else None,
    }


def _document_view(doc: KnowledgeDocument, versions: list[KnowledgeVersion]) -> dict[str, Any]:
    return {
        "id": doc.id,
        "doc_type": doc.doc_type,
        "title": doc.title,
        "source_incident_id": doc.source_incident_id,
        "owner": doc.owner,
        "tags": doc.tags,
        "versions": [_version_view(v) for v in versions],
    }


@router.get("/knowledge/documents")
async def list_documents(session: SessionDep, _user: ReadyUserDep) -> Any:
    docs = (
        await session.execute(select(KnowledgeDocument).order_by(KnowledgeDocument.created_at))
    ).scalars()
    result = []
    for doc in docs:
        versions = (
            await session.execute(
                select(KnowledgeVersion)
                .where(KnowledgeVersion.document_id == doc.id)
                .order_by(KnowledgeVersion.version_no)
            )
        ).scalars()
        result.append(_document_view(doc, list(versions)))
    return ok({"documents": result})


@router.get("/knowledge/documents/{document_id}")
async def get_document(session: SessionDep, _user: ReadyUserDep, document_id: str) -> Any:
    doc = await knowledge_service.get_document_or_404(session, document_id)
    versions = (
        await session.execute(
            select(KnowledgeVersion)
            .where(KnowledgeVersion.document_id == document_id)
            .order_by(KnowledgeVersion.version_no)
        )
    ).scalars()
    return ok(_document_view(doc, list(versions)))


@router.post("/knowledge/documents", status_code=status.HTTP_201_CREATED)
async def create_document(
    session: SessionDep, current: AuthorDep, payload: KnowledgeCreateRequest
) -> Any:
    doc, version = await knowledge_service.create_document(
        session,
        actor_id=current.user.id,
        doc_type=payload.doc_type,
        title=payload.title,
        content=payload.content,
        tags=payload.tags,
        source_incident_id=payload.source_incident_id,
        sensitivity=payload.sensitivity,
        applies_to=payload.applies_to,
        valid_from=payload.valid_from,
        valid_until=payload.valid_until,
        change_note=payload.change_note,
    )
    await session.commit()
    return ok({"document_id": doc.id, "version_id": version.id, "status": version.status})


@router.post("/knowledge/documents/{document_id}/versions", status_code=status.HTTP_201_CREATED)
async def create_version(
    session: SessionDep,
    current: AuthorDep,
    document_id: str,
    payload: KnowledgeVersionCreateRequest,
) -> Any:
    version = await knowledge_service.create_version(
        session,
        actor_id=current.user.id,
        document_id=document_id,
        content=payload.content,
        change_note=payload.change_note,
        applies_to=payload.applies_to,
        valid_from=payload.valid_from,
        valid_until=payload.valid_until,
    )
    await session.commit()
    return ok({"version_id": version.id, "version_no": version.version_no})


@router.post("/knowledge/versions/{version_id}/review")
async def review_version(session: SessionDep, current: AdminDep, version_id: str) -> Any:
    version = await knowledge_service.review_version(
        session, actor_id=current.user.id, version_id=version_id
    )
    await session.commit()
    return ok(
        {"version_id": version.id, "status": version.status, "reviewed_by": version.reviewed_by}
    )


@router.post("/knowledge/versions/{version_id}/publish")
async def publish_version(session: SessionDep, current: AdminDep, version_id: str) -> Any:
    settings = get_settings()
    version = await knowledge_service.publish_version(
        session,
        session_factory=get_session_factory(),
        actor_id=current.user.id,
        version_id=version_id,
        provider=_provider(),
    )
    return ok(
        {
            "version_id": version.id,
            "status": version.status,
            "published_at": version.published_at.isoformat() if version.published_at else None,
            "embedding_model_version": version.embedding_model_version,
            "rag_mode": settings.rag_mode,
        }
    )


@router.post("/knowledge/versions/{version_id}/retire")
async def retire_version(session: SessionDep, current: AdminDep, version_id: str) -> Any:
    version = await knowledge_service.retire_version(
        session, actor_id=current.user.id, version_id=version_id
    )
    await session.commit()
    return ok({"version_id": version.id, "status": version.status})


@router.get("/knowledge/search")
async def search(
    session: SessionDep,
    current: ReadyUserDep,
    query: Annotated[str, Query(min_length=1, max_length=500)],
    environment: Annotated[str | None, Query(pattern=r"^(sandbox|staging|production)$")] = None,
    limit: Annotated[int, Query(ge=1, le=20)] = 5,
) -> Any:
    """知识检索（权限过滤前置；RAG mode 由服务端配置，off 时返回空集说明）。"""
    settings = get_settings()
    mode = settings.rag_mode
    if mode == "off":
        return ok({"mode": mode, "hits": [], "note": "RAG disabled (rag_mode=off)"})
    from app.domains.access import service as access_service

    scope = await access_service.get_effective_scope(
        session, current.user.id, current.role_codes, permission="read"
    )
    response = await search_knowledge(
        get_session_factory(),
        query=query,
        mode=mode,
        authorized_asset_ids=scope.asset_ids,
        all_assets=scope.is_admin or scope.all_assets,
        environment=environment,
        limit=limit,
        used_by_agent=False,
    )
    return ok(
        {
            "mode": response.mode,
            "latency_ms": response.latency_ms,
            "hits": [_hit_view(h) for h in response.hits],
            "route_ranks": response.route_ranks,
        }
    )


@router.post("/knowledge/from-report/{run_id}", status_code=status.HTTP_201_CREATED)
async def create_from_report(session: SessionDep, current: AuthorDep, run_id: str) -> Any:
    """诊断报告 → 知识草稿（人工触发；Agent 不得自动复核/发布自己的结论）。"""
    doc, version = await knowledge_service.create_draft_from_report(
        session, actor_id=current.user.id, run_id=run_id
    )
    await session.commit()
    return ok({"document_id": doc.id, "version_id": version.id, "status": version.status})


def _hit_view(h: Any) -> dict[str, Any]:
    return {
        "version_id": h.version_id,
        "document_id": h.document_id,
        "doc_type": h.doc_type,
        "title": h.title,
        "version_no": h.version_no,
        "published_at": h.published_at,
        "valid_until": h.valid_until,
        "applies_to": h.applies_to,
        "content": h.content,
        "scores": h.scores,
        "ranks": h.ranks,
        "final_rank": h.final_rank,
        "rrf_score": h.rrf_score,
    }
