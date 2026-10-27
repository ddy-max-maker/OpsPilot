"""访问控制领域 Schema。"""

from __future__ import annotations

from pydantic import BaseModel


class AssetScopeData(BaseModel):
    """GET /auth/me 中的资产范围摘要。"""

    is_admin: bool
    all_assets: bool
    environments: list[str]
    asset_ids: list[str]
