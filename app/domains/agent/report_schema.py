"""诊断报告的结构化输出 Schema（report-schema-v2）。

设计边界：
- 本 Schema 只约束**结构**（字段名/类型/枚举/长度），不做语义裁决；
  「supported 必须有实时证据」等业务红线仍在 runtime._finalize_report 中
  以服务端逻辑强制执行（模型无法通过输出字段绕过）。
- `extra="forbid"`：模型多写字段一律判为 schema_error（宁可重试一次，
  也不允许静默接受未定义语义的字段）。
- 长度上限防止上下文被单条报告撑爆；超长截断由调用方处理。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

REPORT_SCHEMA_VERSION = "report-schema-v2"

HypothesisStatusLiteral = Literal["candidate", "supported", "refuted", "insufficient_data"]
ConfidenceLiteral = Literal["low", "medium", "high"]


class ReportFinding(BaseModel):
    """关键发现：一条结论 + 其引用的实时证据。"""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    finding: str = Field(min_length=1, max_length=600)
    evidence_ids: list[str] = Field(default_factory=list, max_length=20)
    detail: str | None = Field(default=None, max_length=1200)


class ReportHypothesis(BaseModel):
    """候选根因（rank 越小越靠前）。"""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    rank: int = Field(default=0, ge=0, le=50)
    statement: str = Field(min_length=1, max_length=1200)
    status: HypothesisStatusLiteral = "candidate"
    supporting_evidence_ids: list[str] = Field(default_factory=list, max_length=20)
    contradicting_evidence_ids: list[str] = Field(default_factory=list, max_length=20)
    missing_information: list[str] = Field(default_factory=list, max_length=20)
    confidence: ConfidenceLiteral = "low"


class ReportUnknown(BaseModel):
    """证据不足声明（is_unknown=true 时禁止存在 supported 根因——由服务端裁决）。"""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    is_unknown: bool = False
    reason: str | None = Field(default=None, max_length=1200)
    missing_information: list[str] = Field(default_factory=list, max_length=20)


class DiagnosticReportPayload(BaseModel):
    """最终诊断报告的严格结构（报告阶段唯一允许的输出形状）。"""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    summary: str = Field(default="", max_length=2000)
    key_findings: list[ReportFinding] = Field(default_factory=list, max_length=20)
    hypotheses: list[ReportHypothesis] = Field(default_factory=list, max_length=8)
    unknown: ReportUnknown = Field(default_factory=ReportUnknown)
    next_manual_steps: list[str] = Field(default_factory=list, max_length=12)
