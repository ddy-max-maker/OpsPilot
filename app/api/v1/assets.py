"""资产管理 API（CONTRACTS §5）：

- GET   /assets        列表（cursor 分页、environment/asset_type/status 过滤、范围过滤）
- POST  /assets        创建（仅 Admin）
- PATCH /assets/{id}   部分更新（仅 Admin；不存在 -> 404，含越权伪装语义）
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query, status

from app.api.deps import (
    AssetScopeDep,
    CurrentUser,
    PageLimitDep,
    SessionDep,
    SettingsDep,
    require_roles,
)
from app.core.enums import AssetStatus, AssetType, Environment
from app.core.responses import Envelope, ok
from app.domains.assets import service as asset_service
from app.domains.assets.schemas import (
    AssetCreateRequest,
    AssetListData,
    AssetResponse,
    AssetUpdateRequest,
)

router = APIRouter(prefix="/assets", tags=["assets"])

AdminDep = Annotated[CurrentUser, Depends(require_roles("admin"))]


@router.get("", response_model=Envelope[AssetListData])
async def list_assets(
    session: SessionDep,
    settings: SettingsDep,
    scope: AssetScopeDep,
    environment: Annotated[Environment | None, Query()] = None,
    asset_type: Annotated[AssetType | None, Query()] = None,
    asset_status: Annotated[AssetStatus | None, Query(alias="status")] = None,
    limit: PageLimitDep = 50,
    cursor: Annotated[str | None, Query(max_length=512)] = None,
) -> Envelope[AssetListData]:
    """资产列表：按当前用户资产范围过滤（默认拒绝；Admin 全量）。"""
    data = await asset_service.list_assets(
        session,
        settings,
        scope,
        environment=environment.value if environment else None,
        asset_type=asset_type.value if asset_type else None,
        status=asset_status.value if asset_status else None,
        limit=limit,
        cursor=cursor,
    )
    return ok(data)


@router.post(
    "",
    response_model=Envelope[AssetResponse],
    status_code=status.HTTP_201_CREATED,
)
async def create_asset(
    body: AssetCreateRequest,
    session: SessionDep,
    settings: SettingsDep,
    admin: AdminDep,
) -> Envelope[AssetResponse]:
    """创建资产（仅 Admin；UNIQUE(name, environment) 冲突 -> 409 CONFLICT_STATE）。"""
    data = await asset_service.create_asset(session, settings, body, admin.user.id)
    return ok(data)


@router.patch("/{asset_id}", response_model=Envelope[AssetResponse])
async def update_asset(
    asset_id: str,
    body: AssetUpdateRequest,
    session: SessionDep,
    admin: AdminDep,
) -> Envelope[AssetResponse]:
    """部分更新资产（仅 Admin；未提供字段保持不变）。"""
    data = await asset_service.update_asset(session, body, asset_id, admin.user.id)
    return ok(data)
