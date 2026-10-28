"""知识管理请求 Schema（D4 REST API 入参校验）。"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class AppliesTo(BaseModel):
    """知识适用范围（检索权限过滤依据；asset_ids 为空 = 通用知识）。"""

    model_config = {"extra": "forbid"}

    asset_ids: list[str] = Field(default_factory=list, max_length=50)
    environments: list[str] = Field(default_factory=list, max_length=10)
    asset_tags: list[str] = Field(default_factory=list, max_length=20)
    service_versions: list[str] = Field(default_factory=list, max_length=20)


class KnowledgeCreateRequest(BaseModel):
    """创建知识文档（附 v1 草稿）。"""

    model_config = {"extra": "forbid"}

    doc_type: str = Field(pattern=r"^(runbook|postmortem|guide)$")
    title: str = Field(min_length=1, max_length=200)
    content: str = Field(min_length=1, max_length=100_000)
    tags: list[str] = Field(default_factory=list, max_length=20)
    source_incident_id: str | None = None
    sensitivity: str = Field(default="internal", pattern=r"^(internal|restricted)$")
    applies_to: AppliesTo = Field(default_factory=AppliesTo)
    valid_from: datetime | None = None
    valid_until: datetime | None = None
    change_note: str | None = Field(default=None, max_length=500)


class KnowledgeVersionCreateRequest(BaseModel):
    """在既有文档上追加新草稿版本。"""

    model_config = {"extra": "forbid"}

    content: str = Field(min_length=1, max_length=100_000)
    change_note: str | None = Field(default=None, max_length=500)
    applies_to: AppliesTo | None = None
    valid_from: datetime | None = None
    valid_until: datetime | None = None


class RunbookCreateRequest(BaseModel):
    """创建 Runbook（附 v1 草稿版本；草稿不可执行）。"""

    model_config = {"extra": "forbid"}

    name: str = Field(min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=2000)
    risk_level: str = Field(pattern=r"^(r0|r1)$")  # r2/r3 首版不实现（CONTRACTS §2.10）
    steps: list[dict[str, Any]] = Field(default_factory=list, max_length=50)
    preconditions: dict[str, Any] | None = None
    parameters_schema: dict[str, Any] | None = None
    allowed_environments: list[str] = Field(default_factory=list, max_length=5)
    allowlist_action_keys: list[str] = Field(default_factory=list, max_length=50)


class RunbookVersionCreateRequest(BaseModel):
    """追加 Runbook 草稿版本。"""

    model_config = {"extra": "forbid"}

    steps: list[dict[str, Any]] = Field(default_factory=list, max_length=50)
    preconditions: dict[str, Any] | None = None
    parameters_schema: dict[str, Any] | None = None
    allowed_environments: list[str] = Field(default_factory=list, max_length=5)
    allowlist_action_keys: list[str] = Field(default_factory=list, max_length=50)
    risk_level: str | None = Field(default=None, pattern=r"^(r0|r1)$")
