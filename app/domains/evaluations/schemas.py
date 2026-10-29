"""评估 API Schema（CONTRACTS 扩展，D6）：套件 / 运行 / 结果。"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class SuiteSummary(BaseModel):
    id: str
    name: str
    kind: str
    description: str | None = None
    case_count: int = 0
    holdout_count: int = 0


class SuiteListData(BaseModel):
    items: list[SuiteSummary]
    dataset_version: str
    dataset_digest: str
    seed_stats: dict[str, int]


class RunCreateRequest(BaseModel):
    suite_id: str = Field(min_length=1, max_length=64)
    wait: bool = Field(default=False, description="同步等待执行完成（评估 CLI/CI 使用）")
    note: str | None = Field(default=None, max_length=500)


class RunCreatedData(BaseModel):
    run_id: str
    suite_id: str
    status: str


class CaseResultView(BaseModel):
    case_id: str
    case_key: str
    title: str
    holdout: bool
    passed: bool
    metrics: dict[str, Any] | None = None
    detail: dict[str, Any] | None = None


class RunSummary(BaseModel):
    cases_total: int = 0
    passed: int = 0
    failed: int = 0

    # 质量指标（仅可判定案例参与；样本量显式给出，不虚构）
    top1_hit_rate: float | None = None
    top3_hit_rate: float | None = None
    unknown_exit_rate: float | None = None
    knowledge_misuse_total: int = 0
    wrong_reference_total: int = 0
    p50_latency_ms: int | None = None
    p95_latency_ms: int | None = None
    avg_tool_calls: float | None = None

    # 强门禁（PRD §13：必须为 0）
    gate_unauthorized_success: int = 0
    gate_repeated_side_effects: int = 0
    gate_verification_misreport: int = 0
    gates_passed: bool = True


class EvaluationRunData(BaseModel):
    id: str
    suite_id: str
    suite_name: str | None = None
    status: str
    config: dict[str, Any]
    started_at: datetime | None = None
    finished_at: datetime | None = None
    error: str | None = None
    summary: RunSummary
    results: list[CaseResultView]


class RunListData(BaseModel):
    items: list[EvaluationRunData]
