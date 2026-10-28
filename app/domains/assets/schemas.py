"""资产领域 Schema（Pydantic v2，对齐 CONTRACTS §2.1）。"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.core.enums import AssetStatus, AssetType, Criticality, Environment


class AssetEndpoints(BaseModel):
    """已登记观测入口（均可空，供连接器使用；extra=forbid 防字段名拼写被静默忽略）。

    D5 新增：
    - container_name：执行目标容器精确绑定（提案/执行侧与 allowlist 双重校验）；
    - health_check：恢复验证探测配置（redis_ping: {host,port} / http_health: {url}），
      仅 sandbox/staging 资产会进入执行链路（production 由提案层硬拒绝）。
    """

    model_config = ConfigDict(extra="forbid")

    health_url: str | None = None
    metrics_ref: str | None = None
    logs_ref: str | None = None
    container_name: str | None = Field(
        default=None,
        max_length=100,
        pattern=r"^[a-z0-9][a-z0-9_.-]{0,99}$",
    )
    health_check: dict[str, object] | None = None


class AssetCreateRequest(BaseModel):
    """POST /assets 请求体（extra=forbid：未知字段直接 400）。"""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=200)
    environment: Environment
    asset_type: AssetType
    criticality: Criticality = Criticality.MEDIUM
    status: AssetStatus = AssetStatus.ACTIVE
    endpoints: AssetEndpoints | None = None
    owner: str | None = Field(default=None, max_length=200)
    tags: list[str] = Field(default_factory=list, max_length=20)


class AssetUpdateRequest(BaseModel):
    """PATCH /assets/{id} 请求体：全部可选，未提供的字段保持不变。"""

    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=200)
    environment: Environment | None = None
    asset_type: AssetType | None = None
    criticality: Criticality | None = None
    status: AssetStatus | None = None
    endpoints: AssetEndpoints | None = None
    owner: str | None = Field(default=None, max_length=200)
    tags: list[str] | None = Field(default=None, max_length=20)


class AssetResponse(BaseModel):
    """资产响应（含 id/时间戳）。"""

    id: str
    name: str
    environment: Environment
    asset_type: AssetType
    status: AssetStatus
    criticality: Criticality
    endpoints: AssetEndpoints | None
    owner: str | None
    tags: list[str]
    created_at: datetime
    updated_at: datetime


class AssetListData(BaseModel):
    """cursor 分页列表（契约 §1.3：items + next_cursor + has_more）。"""

    items: list[AssetResponse]
    next_cursor: str | None
    has_more: bool
