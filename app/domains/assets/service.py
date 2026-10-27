"""资产服务：用例编排 + 显式事务边界。

事务边界约定：每个用例一个事务（校验 → flush → 审计 → commit），
唯一约束竞态由 IntegrityError 捕获后回滚并映射为 409 CONFLICT_STATE。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import conflict_state, not_found, validation_error
from app.core.ids import new_id
from app.core.pagination import Cursor, decode_cursor, encode_cursor
from app.domains.access.scope import AssetScope
from app.domains.assets import repository
from app.domains.assets.schemas import (
    AssetCreateRequest,
    AssetListData,
    AssetResponse,
    AssetUpdateRequest,
)
from app.domains.audit import service as audit_service
from app.models import Asset

if TYPE_CHECKING:
    from app.core.config import Settings


def _to_response(asset: Asset) -> AssetResponse:
    from app.domains.assets.schemas import AssetEndpoints

    endpoints = None
    if asset.endpoints:
        endpoints = AssetEndpoints(**asset.endpoints)
    return AssetResponse(
        id=asset.id,
        name=asset.name,
        environment=asset.environment,
        asset_type=asset.asset_type,
        status=asset.status,
        criticality=asset.criticality,
        endpoints=endpoints,
        owner=asset.owner,
        tags=list(asset.tags or []),
        created_at=asset.created_at,
        updated_at=asset.updated_at,
    )


async def create_asset(
    session: AsyncSession,
    settings: Settings,
    req: AssetCreateRequest,
    actor_id: str,
) -> AssetResponse:
    """创建资产（Admin）。事务：预检查唯一 → insert+flush → 审计 → commit。"""
    if await repository.name_environment_exists(session, req.name, str(req.environment.value)):
        raise conflict_state(
            "asset with same name and environment already exists",
            details={"name": req.name, "environment": str(req.environment.value)},
        )
    asset = Asset(
        id=new_id("as_"),
        name=req.name,
        environment=req.environment.value,
        asset_type=req.asset_type.value,
        status=req.status.value,
        criticality=req.criticality.value,
        endpoints=req.endpoints.model_dump(exclude_none=True) if req.endpoints else None,
        owner=req.owner,
        tags=list(req.tags),
    )
    session.add(asset)
    await session.flush()  # 拿到服务端默认值，审计可引用 resource_id
    await audit_service.record(
        session,
        actor_type="user",
        actor_id=actor_id,
        action="asset.create",
        resource_type="asset",
        resource_id=asset.id,
        environment=asset.environment,
        detail={"name": asset.name, "asset_type": asset.asset_type},
    )
    try:
        await session.commit()
    except IntegrityError as exc:
        # 并发竞态：UNIQUE(name, environment) 由数据库兜底
        await session.rollback()
        raise conflict_state(
            "asset with same name and environment already exists",
            details={"name": req.name, "environment": str(req.environment.value)},
        ) from exc
    await session.refresh(asset)
    return _to_response(asset)


async def update_asset(
    session: AsyncSession,
    req: AssetUpdateRequest,
    asset_id: str,
    actor_id: str,
) -> AssetResponse:
    """部分更新资产（Admin）。事务：加载 → 校验 → 变更 → 审计 → commit。

    不存在的资源按 404 返回（含越权伪装语义，见 scope.py）。
    """
    asset = await repository.get_asset(session, asset_id)
    if asset is None:
        raise not_found("asset not found")
    new_name = req.name or asset.name
    new_environment = req.environment.value if req.environment else asset.environment
    if (new_name, new_environment) != (asset.name, asset.environment) and (
        await repository.name_environment_exists(
            session, new_name, new_environment, exclude_id=asset.id
        )
    ):
        raise conflict_state(
            "asset with same name and environment already exists",
            details={"name": new_name, "environment": new_environment},
        )
    changes: dict[str, object] = {}
    if req.name is not None and req.name != asset.name:
        asset.name = req.name
        changes["name"] = req.name
    if req.environment is not None and req.environment.value != asset.environment:
        asset.environment = req.environment.value
        changes["environment"] = req.environment.value
    if req.asset_type is not None and req.asset_type.value != asset.asset_type:
        asset.asset_type = req.asset_type.value
        changes["asset_type"] = req.asset_type.value
    if req.criticality is not None and req.criticality.value != asset.criticality:
        asset.criticality = req.criticality.value
        changes["criticality"] = req.criticality.value
    if req.status is not None and req.status.value != asset.status:
        asset.status = req.status.value
        changes["status"] = req.status.value
    if "endpoints" in req.model_fields_set:
        # 区分三种语义：未提交（保持不变）、显式 null（整体清空）、
        # 嵌套字段级显式 null（仅清除该字段，保留其余）。
        if req.endpoints is None:
            if asset.endpoints is not None:
                asset.endpoints = None
                changes["endpoints"] = None
        else:
            current = dict(asset.endpoints) if asset.endpoints else {}
            submitted = req.endpoints.model_fields_set
            for key in ("health_url", "metrics_ref", "logs_ref"):
                if key not in submitted:
                    continue
                value = getattr(req.endpoints, key)
                if value is None:
                    current.pop(key, None)
                else:
                    current[key] = value
            new_endpoints = current or None
            if new_endpoints != asset.endpoints:
                asset.endpoints = new_endpoints
                changes["endpoints"] = new_endpoints
    if req.owner is not None and req.owner != asset.owner:
        asset.owner = req.owner
        changes["owner"] = req.owner
    if req.tags is not None:
        asset.tags = list(req.tags)
        changes["tags"] = asset.tags
    session.add(asset)
    if changes:
        await audit_service.record(
            session,
            actor_type="user",
            actor_id=actor_id,
            action="asset.update",
            resource_type="asset",
            resource_id=asset.id,
            environment=asset.environment,
            detail={"changes": changes},
        )
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise conflict_state(
            "asset with same name and environment already exists",
            details={"name": new_name, "environment": new_environment},
        ) from exc
    await session.refresh(asset)
    return _to_response(asset)


async def list_assets(
    session: AsyncSession,
    settings: Settings,
    scope: AssetScope,
    *,
    environment: str | None = None,
    asset_type: str | None = None,
    status: str | None = None,
    limit: int,
    cursor: str | None = None,
) -> AssetListData:
    """范围过滤的资产列表（cursor 分页）。

    校验：limit 范围由 API 层约束；cursor 非法/篡改 → 400 VALIDATION_ERROR。
    """
    if limit < 1:
        raise validation_error("limit must be >= 1")
    if limit > settings.max_page_size:
        raise validation_error(f"limit must be <= {settings.max_page_size}")
    decoded: Cursor | None = None
    if cursor:
        decoded = decode_cursor(cursor, settings.signing_secret)
    rows = await repository.list_assets(
        session,
        scope,
        environment=environment,
        asset_type=asset_type,
        status=status,
        limit=limit,
        cursor=decoded,
    )
    has_more = len(rows) > limit
    items = rows[:limit]
    next_cursor = None
    if has_more and items:
        last = items[-1]
        next_cursor = encode_cursor(
            Cursor(created_at=last.created_at, id=last.id), settings.signing_secret
        )
    return AssetListData(
        items=[_to_response(a) for a in items],
        next_cursor=next_cursor,
        has_more=has_more,
    )
