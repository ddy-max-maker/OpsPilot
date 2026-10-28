"""审批与受控执行 REST 模型（CONTRACTS §2.11–§2.14）。"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.core.enums import Environment


class ProposalCreateRequest(BaseModel):
    """创建处置提案（只能引用已发布 Runbook 版本；production 服务端硬拒绝）。"""

    run_id: str = Field(min_length=1, max_length=64)
    runbook_version_id: str = Field(min_length=1, max_length=64)
    target_asset_id: str = Field(min_length=1, max_length=64)
    environment: Environment  # production 由服务层显式 403 + DB CHECK 兜底
    parameters: dict[str, Any] = Field(default_factory=dict)
    comment: str | None = Field(default=None, max_length=2000)


class ProposalDecisionRequest(BaseModel):
    """approve / reject 请求（审批意见可选）。"""

    comment: str | None = Field(default=None, max_length=2000)


class CheckView(BaseModel):
    """前置检查单项（dry-run 与执行前检查共用视图）。"""

    model_config = ConfigDict(extra="allow")

    name: str
    passed: bool
    detail: str | None = None


class DryRunResponse(BaseModel):
    """只读前置检查结果（不做任何外部变更）。"""

    proposal_id: str
    passed: bool
    checks: list[CheckView]
    preconditions_result: dict[str, Any]


class ProposalView(BaseModel):
    """提案详情（含参数、风险、审批状态）。"""

    model_config = ConfigDict(from_attributes=True)

    id: str
    run_id: str
    incident_id: str
    runbook_version_id: str
    target_asset_id: str
    environment: str
    parameters: dict[str, Any]
    parameter_hash: str
    risk_level: str
    preconditions_result: dict[str, Any] | None
    status: str
    created_by: str
    created_by_user: str | None
    expires_at: datetime
    created_at: datetime
    updated_at: datetime


class ApprovalView(BaseModel):
    """审批记录视图（只追加日志）。"""

    model_config = ConfigDict(from_attributes=True)

    id: str
    proposal_id: str
    decision: str
    decided_by: str
    proposal_snapshot: dict[str, Any]
    expires_at: datetime
    comment: str | None
    decided_at: datetime


class ExecutionView(BaseModel):
    """动作执行视图。"""

    model_config = ConfigDict(from_attributes=True)

    id: str
    proposal_id: str
    action_key: str
    idempotency_key: str
    status: str
    executor: str | None
    external_ref: str | None
    result_detail: dict[str, Any] | None
    started_at: datetime | None
    finished_at: datetime | None
    created_at: datetime


class VerificationView(BaseModel):
    """恢复验证视图（观察窗口 + 每项检查）。"""

    model_config = ConfigDict(from_attributes=True)

    id: str
    execution_id: str
    status: str
    checks: list[dict[str, Any]] | None
    observation_window: dict[str, Any] | None
    concluded_at: datetime | None
    created_at: datetime


class ProposalDetailResponse(BaseModel):
    """提案聚合详情：proposal + approvals + executions + verifications。"""

    proposal: ProposalView
    approvals: list[ApprovalView]
    executions: list[ExecutionView]
    verifications: list[VerificationView]
