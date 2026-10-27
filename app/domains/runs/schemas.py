"""诊断运行 Schema（CONTRACTS §2.4–§2.7 / §2.16 / §5 Runs 端点）。"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class InvestigationCreateRequest(BaseModel):
    """POST /incidents/{id}/investigations 请求体（全部可选）。"""

    model_config = ConfigDict(extra="forbid")

    max_tool_calls: int | None = Field(default=None, ge=1, le=200)
    max_iterations: int | None = Field(default=None, ge=1, le=500)
    max_tokens: int | None = Field(default=None, ge=1000, le=10_000_000)
    deadline_seconds: int | None = Field(default=None, ge=30, le=3600)


class RunResponse(BaseModel):
    id: str
    incident_id: str
    status: str
    stop_reason: str | None
    triggered_by: str
    model_config_snapshot: dict[str, Any]
    budgets: dict[str, Any]
    usage: dict[str, Any]
    langgraph_thread_id: str
    cancel_requested: bool
    started_at: datetime | None
    finished_at: datetime | None
    created_at: datetime
    updated_at: datetime


class RunCreatedData(BaseModel):
    """202 受理响应（CONTRACTS §1.3：data 含 run_id 与 status）。"""

    run_id: str
    status: str
    incident_id: str


class RunEventData(BaseModel):
    id: int
    run_id: str
    seq: int
    type: str
    payload: dict[str, Any] | None
    created_at: datetime


class RunEventListData(BaseModel):
    """事件流 JSON 分页（CONTRACTS §1.3：D2 无 SSE，after_seq 增量拉取）。"""

    items: list[RunEventData]
    next_after_seq: int | None
    has_more: bool


class EvidenceResponse(BaseModel):
    id: str
    run_id: str | None
    asset_id: str | None
    environment: str
    source_type: str
    collector: str
    observed_at: datetime
    time_range: dict[str, Any] | None
    status: str
    data_ref: dict[str, Any] | None
    summary_redacted: str
    correlation: dict[str, Any] | None
    error_code: str | None
    truncated: bool
    schema_version: str
    created_at: datetime


class EvidenceListData(BaseModel):
    items: list[EvidenceResponse]
    next_cursor: str | None
    has_more: bool


class HypothesisResponse(BaseModel):
    id: str
    rank: int
    statement: str
    status: str
    supporting_evidence_ids: list[str]
    contradicting_evidence_ids: list[str]
    missing_information: list[str]
    confidence: str
    created_at: datetime


class ReportResponse(BaseModel):
    """GET /runs/{id}/report：diagnostic_reports 最新版本快照（ADR-13）。"""

    id: str
    run_id: str
    version_no: int
    status: str
    conclusion: dict[str, Any]
    hypotheses: list[HypothesisResponse]
    evidence_refs: list[str]
    unknown_note: dict[str, Any] | None
    generator: dict[str, Any] | None
    generated_at: datetime
    created_at: datetime
