"""Runbooks API（CONTRACTS §2.10/§5，D4 交付）：

- GET/POST /runbooks                          列表 / 创建（附 v1 草稿版本）
- GET  /runbooks/{id}                         详情（含版本）
- POST /runbooks/{id}/versions                追加草稿版本
- POST /runbooks/versions/{id}/publish        发布（旧 published 版本退役）
- POST /runbooks/versions/{id}/retire         退役

红线：草稿不直接可执行——本阶段无 Proposal/Approval/Execution（D5）；
allowed_environments 硬排除 production；RBAC 全部 Admin（CONTRACTS §7.1）。
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, status
from sqlalchemy import select

from app.api.deps import CurrentUser, ReadyUserDep, SessionDep, require_roles
from app.core.errors import not_found
from app.core.responses import ok
from app.domains.knowledge import service as knowledge_service
from app.domains.knowledge.schemas import RunbookCreateRequest, RunbookVersionCreateRequest
from app.models import Runbook, RunbookVersion

router = APIRouter(tags=["runbooks"])

AdminDep = Annotated[CurrentUser, Depends(require_roles("admin"))]


def _version_view(v: RunbookVersion) -> dict[str, Any]:
    return {
        "id": v.id,
        "runbook_id": v.runbook_id,
        "version_no": v.version_no,
        "status": v.status,
        "steps": v.steps,
        "preconditions": v.preconditions,
        "parameters_schema": v.parameters_schema,
        "allowed_environments": v.allowed_environments,
        "allowlist_action_keys": v.allowlist_action_keys,
        "risk_level": v.risk_level,
        "published_at": v.published_at.isoformat() if v.published_at else None,
    }


def _runbook_view(r: Runbook, versions: list[RunbookVersion]) -> dict[str, Any]:
    return {
        "id": r.id,
        "name": r.name,
        "description": r.description,
        "risk_level": r.risk_level,
        "status": r.status,
        "versions": [_version_view(v) for v in versions],
    }


@router.get("/runbooks")
async def list_runbooks(session: SessionDep, _user: ReadyUserDep) -> Any:
    runbooks = (await session.execute(select(Runbook).order_by(Runbook.created_at))).scalars()
    result = []
    for rb in runbooks:
        versions = (
            await session.execute(
                select(RunbookVersion)
                .where(RunbookVersion.runbook_id == rb.id)
                .order_by(RunbookVersion.version_no)
            )
        ).scalars()
        result.append(_runbook_view(rb, list(versions)))
    return ok({"runbooks": result})


@router.get("/runbooks/{runbook_id}")
async def get_runbook(session: SessionDep, _user: ReadyUserDep, runbook_id: str) -> Any:
    rb = await session.get(Runbook, runbook_id)
    if rb is None:
        raise not_found("runbook not found")
    versions = (
        await session.execute(
            select(RunbookVersion)
            .where(RunbookVersion.runbook_id == runbook_id)
            .order_by(RunbookVersion.version_no)
        )
    ).scalars()
    return ok(_runbook_view(rb, list(versions)))


@router.post("/runbooks", status_code=status.HTTP_201_CREATED)
async def create_runbook(
    session: SessionDep, current: AdminDep, payload: RunbookCreateRequest
) -> Any:
    runbook, version = await knowledge_service.create_runbook(
        session,
        actor_id=current.user.id,
        name=payload.name,
        description=payload.description,
        risk_level=payload.risk_level,
        steps=payload.steps,
        preconditions=payload.preconditions,
        parameters_schema=payload.parameters_schema,
        allowed_environments=payload.allowed_environments,
        allowlist_action_keys=payload.allowlist_action_keys,
    )
    await session.commit()
    return ok({"runbook_id": runbook.id, "version_id": version.id, "status": runbook.status})


@router.post("/runbooks/{runbook_id}/versions", status_code=status.HTTP_201_CREATED)
async def create_runbook_version(
    session: SessionDep, current: AdminDep, runbook_id: str, payload: RunbookVersionCreateRequest
) -> Any:
    version = await knowledge_service.create_runbook_version(
        session,
        actor_id=current.user.id,
        runbook_id=runbook_id,
        steps=payload.steps,
        preconditions=payload.preconditions,
        parameters_schema=payload.parameters_schema,
        allowed_environments=payload.allowed_environments,
        allowlist_action_keys=payload.allowlist_action_keys,
        risk_level=payload.risk_level,
    )
    await session.commit()
    return ok({"version_id": version.id, "version_no": version.version_no})


@router.post("/runbooks/versions/{version_id}/publish")
async def publish_runbook_version(session: SessionDep, current: AdminDep, version_id: str) -> Any:
    version = await knowledge_service.publish_runbook_version(
        session, actor_id=current.user.id, version_id=version_id
    )
    await session.commit()
    return ok({"version_id": version.id, "status": version.status})


@router.post("/runbooks/versions/{version_id}/retire")
async def retire_runbook_version(session: SessionDep, current: AdminDep, version_id: str) -> Any:
    version = await knowledge_service.retire_runbook_version(
        session, actor_id=current.user.id, version_id=version_id
    )
    await session.commit()
    return ok({"version_id": version.id, "status": version.status})
