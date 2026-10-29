"""报告生成：从「最终 AI 消息」到「符合 Schema 的结构化报告」。

链路（每一步都不调用任何基础设施工具——本模块只持有 ChatModel，不持有 ToolGateway）：

1. **直读**：模型最后一条消息本身就是 JSON（提示词要求），直接校验；
2. **格式化**：直读失败（无 JSON/截断/schema 不合法/引用全错）时，发起一次
   **独立的报告格式化调用**（无工具绑定），把「模型原始输出 + 本 run 证据清单」
   转成严格 Schema；
3. **受控修复**：格式化仍不合法时，最多 **1 次**修复调用，回灌上一次的校验错误；
4. **失败**：仍然不合法 → 明确记录失败类型（不是「诊断 unknown」，而是报告生成失败）。

诚实性约束由代码强制（不是靠提示词）：
- 引用的 evidence_id 必须属于本 run 已落库证据，否则被剔除并计入 reference_errors；
- 格式化/修复阶段没有工具可调用（结构上无法重新取证），也不重新执行诊断；
- 格式化输出的 summary/hypotheses 只允许来自原始输出的重排；服务端不再补充新的
  根因文本（本模块不生成任何结论语句，只做结构转换）。
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import ValidationError

from app.core.config import Settings
from app.domains.agent.report_schema import (
    REPORT_SCHEMA_VERSION,
    DiagnosticReportPayload,
    ReportHypothesis,
)

logger = logging.getLogger(__name__)

# 报告状态（写入 diagnostic_reports.conclusion / generator，供审计与评估区分）
REPORT_STATUS_OK = "ok"  # 直读成功
REPORT_STATUS_FORMATTED = "formatted"  # 独立格式化调用成功
REPORT_STATUS_REPAIRED = "repaired"  # 一次受控修复后成功
REPORT_STATUS_FAILED = "failed"  # 报告生成失败（明确记录，不伪装成诊断结论）


class ReportFailureKind(StrEnum):
    """报告生成失败类型（评估与审计的失败分类口径）。"""

    NO_JSON = "no_json"  # 文本里没有 JSON 对象
    TRUNCATED = "truncated"  # JSON 不完整（输出被截断）
    SCHEMA_ERROR = "schema_error"  # JSON 合法但不符合 Schema
    REFERENCE_ERROR = "reference_error"  # 引用全部无效（证据不可追溯）
    MODEL_ERROR = "model_error"  # 模型调用异常/空输出/超时


@dataclass(frozen=True)
class EvidenceBrief:
    """格式化阶段可见的证据清单（只读摘要，不含原始日志）。"""

    evidence_id: str
    source_type: str
    status: str
    error_code: str | None
    summary: str


@dataclass
class ReportResult:
    """报告生成结果（runtime 消费）。"""

    payload: dict[str, Any] | None
    status: str
    failure_kind: ReportFailureKind | None = None
    attempts: int = 0
    mode: str = "none"  # "direct" | "structured:json_schema" | "prompt" | "none"
    dropped_references: int = 0
    detail: dict[str, Any] = field(default_factory=dict)

    @property
    def failed(self) -> bool:
        return self.payload is None

    @property
    def failure_reason(self) -> str | None:
        if self.failure_kind is None:
            return None
        return str(self.detail.get("message") or self.failure_kind.value)


_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE)
_JSON_START = "{"


def _content_text(message: Any) -> str | None:
    content = getattr(message, "content", None)
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        # 部分 provider 以 content blocks 返回
        parts = [
            str(block.get("text") or "")
            for block in content
            if isinstance(block, dict) and block.get("type") in ("text", "output_text")
        ]
        joined = "".join(parts)
        return joined or None
    return None


def _looks_truncated(text: str) -> bool:
    """括号未闭合 = 输出被截断（区分 truncated 与 no_json）。"""
    depth = 0
    in_string = False
    escaped = False
    for ch in text:
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth < 0:
                return False
    return depth > 0 or in_string


def extract_json_object(text: str) -> tuple[str | None, ReportFailureKind | None]:
    """从文本中提取最外层 JSON 对象；失败时返回失败类型。"""
    stripped = text.strip()
    if not stripped:
        return None, ReportFailureKind.NO_JSON
    start = stripped.find(_JSON_START)
    if start < 0:
        return None, ReportFailureKind.NO_JSON
    candidate = stripped[start:]
    end = candidate.rfind("}")
    if end < 0:
        return None, ReportFailureKind.TRUNCATED
    body = candidate[: end + 1]
    body = _FENCE_RE.sub("", body).strip()
    try:
        json.loads(body)
    except json.JSONDecodeError:
        # 只有开头没有闭合，或尾部被截断；其余非法文本按 no_json 计
        return None, (
            ReportFailureKind.TRUNCATED
            if _looks_truncated(candidate)
            else ReportFailureKind.NO_JSON
        )
    return body, None


def _finish_reason(message: Any) -> str | None:
    meta = getattr(message, "response_metadata", None)
    if isinstance(meta, dict):
        raw = meta.get("finish_reason") or meta.get("stop_reason")
        return str(raw) if raw else None
    return None


@dataclass
class _ValidationOutcome:
    payload: dict[str, Any] | None
    kind: ReportFailureKind | None
    errors: list[str]

    @property
    def ok(self) -> bool:
        return self.payload is not None


def _truncate(value: Any, limit: int) -> str:
    if value is None:
        return ""
    text = value if isinstance(value, str) else str(value)
    return text[:limit]


def _normalize_report(raw: dict[str, Any]) -> dict[str, Any]:
    """确定性归一：截断超长文本 / 限制列表长度 / 丢弃空条目。

    只处理「长度与计数」，**不修正类型**：字段类型错误（如 hypotheses 是字符串）
    会原样交给 Pydantic 报 schema_error —— 归一化不得把结构错误悄悄抹平。
    """
    out: dict[str, Any] = dict(raw)
    summary = out.get("summary")
    if summary is None:
        out["summary"] = ""
    elif isinstance(summary, str):
        out["summary"] = summary[:2000]

    findings_raw = out.get("key_findings")
    if isinstance(findings_raw, list):
        findings: list[dict[str, Any]] = []
        for item in findings_raw[:20]:
            if not isinstance(item, dict):
                return out  # 结构错误：交给 Pydantic
            finding = dict(item)
            statement = finding.get("finding")
            finding["finding"] = _truncate(statement, 600)
            if not finding["finding"]:
                continue
            detail = finding.get("detail")
            finding["detail"] = None if detail is None else _truncate(detail, 1200)
            refs = finding.get("evidence_ids")
            if isinstance(refs, list):
                finding["evidence_ids"] = [str(x) for x in refs][:20]
            findings.append(finding)
        out["key_findings"] = findings

    hypotheses_raw = out.get("hypotheses")
    if isinstance(hypotheses_raw, list):
        hypotheses: list[dict[str, Any]] = []
        for item in hypotheses_raw[:8]:
            if not isinstance(item, dict):
                return out  # 结构错误：交给 Pydantic
            hypothesis = dict(item)
            hypothesis["statement"] = _truncate(hypothesis.get("statement"), 1200)
            if not hypothesis["statement"]:
                continue
            rank = hypothesis.get("rank")
            if isinstance(rank, int) and 0 <= rank <= 50:
                hypothesis["rank"] = rank
            elif rank is None:
                hypothesis["rank"] = 0
            for key in ("supporting_evidence_ids", "contradicting_evidence_ids"):
                refs = hypothesis.get(key)
                if isinstance(refs, list):
                    hypothesis[key] = [str(x) for x in refs][:20]
            missing = hypothesis.get("missing_information")
            if isinstance(missing, list):
                hypothesis["missing_information"] = [_truncate(x, 300) for x in missing][:20]
            hypotheses.append(hypothesis)
        out["hypotheses"] = hypotheses

    unknown = out.get("unknown")
    if isinstance(unknown, dict):
        note = dict(unknown)
        if note.get("reason") is not None:
            note["reason"] = _truncate(note.get("reason"), 1200)
        missing = note.get("missing_information")
        if isinstance(missing, list):
            note["missing_information"] = [_truncate(x, 300) for x in missing][:20]
        out["unknown"] = note

    steps = out.get("next_manual_steps")
    if isinstance(steps, list):
        out["next_manual_steps"] = [_truncate(x, 300) for x in steps][:12]
    return out


def _validate_payload(candidate: str | dict[str, Any]) -> _ValidationOutcome:
    try:
        raw = json.loads(candidate) if isinstance(candidate, str) else candidate
    except json.JSONDecodeError:
        return _ValidationOutcome(None, ReportFailureKind.NO_JSON, ["invalid json"])
    if not isinstance(raw, dict):
        return _ValidationOutcome(
            None, ReportFailureKind.SCHEMA_ERROR, ["report must be an object"]
        )
    try:
        model = DiagnosticReportPayload.model_validate(_normalize_report(raw))
    except ValidationError as exc:
        errors = [
            f"{'.'.join(str(p) for p in err['loc'])}: {err['msg']}" for err in exc.errors()[:12]
        ]
        return _ValidationOutcome(None, ReportFailureKind.SCHEMA_ERROR, errors)
    return _ValidationOutcome(model.model_dump(), None, [])


def _sanitize_references(
    payload: dict[str, Any], evidence_ids: set[str]
) -> tuple[dict[str, Any], int]:
    """剔除不存在的 evidence_id（禁止引用编造证据）。返回 (payload, 被剔除数)。"""
    dropped = 0

    def _filter(ids: Any) -> list[str]:
        nonlocal dropped
        if not isinstance(ids, list):
            return []
        kept: list[str] = []
        for raw in ids:
            value = str(raw)
            if value in evidence_ids:
                if value not in kept:
                    kept.append(value)
            else:
                dropped += 1
        return kept

    for finding in payload.get("key_findings") or []:
        finding["evidence_ids"] = _filter(finding.get("evidence_ids"))
    for hypothesis in payload.get("hypotheses") or []:
        hypothesis["supporting_evidence_ids"] = _filter(hypothesis.get("supporting_evidence_ids"))
        hypothesis["contradicting_evidence_ids"] = _filter(
            hypothesis.get("contradicting_evidence_ids")
        )
    return payload, dropped


def _cited_total(payload: dict[str, Any]) -> int:
    total = 0
    for finding in payload.get("key_findings") or []:
        total += len(finding.get("evidence_ids") or [])
    for hypothesis in payload.get("hypotheses") or []:
        total += len(hypothesis.get("supporting_evidence_ids") or [])
        total += len(hypothesis.get("contradicting_evidence_ids") or [])
    return total


FORMATTER_SYSTEM_PROMPT = """你是报告格式化器，只做结构转换，不做诊断。

硬性约束：
1. 你没有、也不能调用任何工具；禁止重新取证、禁止执行诊断、禁止新增观测。
2. 只能使用「证据清单」中真实存在的 evidence_id；清单外的 id 一律不要写。
3. 只能重排下面给出的模型原始输出：不得新增原始输出中没有的结论、根因或建议。
4. 若原始输出未给出足够信息，输出 unknown.is_unknown=true，并把缺失信息写入
   unknown.missing_information；禁止为了填满字段而猜测。
   **不要改变原始输出的结论强度**：原始输出若已确证直接原因（或已列出根因假设），
   不得把整份报告改写成 is_unknown=true；你只做结构转换，不做重新诊断。
5. hypotheses 中 status=supported 必须至少引用 1 条实时证据 id；仅凭历史知识
   （source_type=knowledge）或采集失败证据不得标 supported。
6. 只输出符合 Schema 的 JSON，不要任何解释性文字。"""


def _build_format_messages(
    raw_output: str,
    evidence: list[EvidenceBrief],
    *,
    schema_json: str,
    previous_errors: list[str] | None = None,
) -> list[Any]:
    inventory = "\n".join(
        f"- {e.evidence_id} | {e.source_type} | status={e.status} | "
        f"error_code={e.error_code or '-'} | {e.summary[:200]}"
        for e in evidence
    )
    rules = [
        "把下面的「模型原始输出」转换为符合 JSON Schema 的诊断报告。",
        "",
        "## 证据清单（本 run 真实落库，唯一可引用的 id）",
        inventory or "（本 run 没有任何证据——此时必须 is_unknown=true）",
        "",
        "## JSON Schema",
        schema_json,
        "",
        "## 模型原始输出",
        raw_output[:12000],
    ]
    if previous_errors:
        rules.extend(
            [
                "",
                "## 上一次输出未通过校验，请修正下列问题后重新输出",
                *[f"- {err}" for err in previous_errors[:12]],
            ]
        )
    return [SystemMessage(content=FORMATTER_SYSTEM_PROMPT), HumanMessage(content="\n".join(rules))]


class ReportGenerator:
    """报告生成器（模型直读 → 独立格式化 → 一次受控修复 → 明确失败）。"""

    def __init__(
        self,
        model: BaseChatModel,
        settings: Settings,
        *,
        structured_methods: tuple[str, ...] = ("json_schema", "function_calling"),
        allow_formatting: bool = True,
    ) -> None:
        self._model = model
        self._settings = settings
        self._structured_methods = structured_methods
        # 确定性 Fake provider 不具备「重排/格式化」能力：对它发起格式化调用
        # 只会得到同样的确定性输出（甚至工具调用），因此直接关闭该路径，
        # 让结构不合法显式失败（而不是无限重试或伪造成功）。
        self._allow_formatting = allow_formatting

    # ---- 对外入口 ----

    async def generate(
        self,
        *,
        final_message: Any,
        evidence: list[EvidenceBrief],
        run_id: str = "",
    ) -> ReportResult:
        text = _content_text(final_message)
        if text is None or not text.strip():
            return ReportResult(
                payload=None,
                status=REPORT_STATUS_FAILED,
                failure_kind=ReportFailureKind.MODEL_ERROR,
                detail={"message": "final AI message is empty", "run_id": run_id},
            )

        evidence_ids = {e.evidence_id for e in evidence}
        finish = _finish_reason(final_message)
        candidate, extract_kind = extract_json_object(text)
        failure_kind = extract_kind
        errors: list[str] = []
        if candidate is not None:
            validation = _validate_payload(candidate)
            if validation.ok:
                payload = validation.payload or {}
                cited_before = _cited_total(payload)
                payload, dropped = _sanitize_references(payload, evidence_ids)
                if cited_before > 0 and dropped >= cited_before:
                    # 引用的 id 全部无效：报告不可追溯 → 交由格式化重写（不静默接受）
                    failure_kind = ReportFailureKind.REFERENCE_ERROR
                else:
                    return ReportResult(
                        payload=payload,
                        status=REPORT_STATUS_OK,
                        attempts=0,
                        mode="direct",
                        dropped_references=dropped,
                        detail={"finish_reason": finish},
                    )
            else:
                failure_kind = validation.kind or ReportFailureKind.SCHEMA_ERROR
                errors = validation.errors
        if failure_kind is None:
            failure_kind = ReportFailureKind.NO_JSON
        if finish == "length":
            # provider 明确截断：优先记为 truncated（比 schema_error 更贴近原因）
            failure_kind = ReportFailureKind.TRUNCATED
        if not self._allow_formatting:
            return ReportResult(
                payload=None,
                status=REPORT_STATUS_FAILED,
                failure_kind=failure_kind,
                attempts=0,
                detail={
                    "message": (
                        f"report output is not schema-valid ({failure_kind}) and the deterministic "
                        "provider cannot be used for a formatting call"
                    ),
                    "errors": errors[:12],
                    "finish_reason": finish,
                    "run_id": run_id,
                },
            )

        # 格式化 + 一次修复（均无工具、无诊断）
        schema_json = json.dumps(DiagnosticReportPayload.model_json_schema(), ensure_ascii=False)
        attempts = 0
        last_kind = failure_kind
        last_errors = errors
        for round_no in (1, 2):
            attempts = round_no
            messages = _build_format_messages(
                text,
                evidence,
                schema_json=schema_json,
                previous_errors=last_errors if round_no > 1 else None,
            )
            formatted, mode, round_kind, round_errors = await self._format_call(
                messages, run_id=run_id, round_no=round_no
            )
            if formatted is not None:
                cited_before = _cited_total(formatted)
                formatted, dropped = _sanitize_references(formatted, evidence_ids)
                if cited_before > 0 and dropped >= cited_before:
                    last_kind = ReportFailureKind.REFERENCE_ERROR
                    last_errors = ["all cited evidence_ids are not part of this run"]
                    continue
                return ReportResult(
                    payload=formatted,
                    status=REPORT_STATUS_FORMATTED if round_no == 1 else REPORT_STATUS_REPAIRED,
                    attempts=attempts,
                    mode=mode,
                    dropped_references=dropped,
                    detail={"initial_failure": str(failure_kind), "finish_reason": finish},
                )
            last_kind = round_kind or ReportFailureKind.SCHEMA_ERROR
            last_errors = round_errors or last_errors

        return ReportResult(
            payload=None,
            status=REPORT_STATUS_FAILED,
            failure_kind=last_kind,
            attempts=attempts,
            mode="none",
            detail={
                "message": f"report generation failed after {attempts} formatting attempts: "
                f"{last_kind}",
                "errors": last_errors[:12],
                "initial_failure": str(failure_kind),
                "finish_reason": finish,
                "schema_version": REPORT_SCHEMA_VERSION,
                "run_id": run_id,
            },
        )

    # ---- 内部：格式化调用 ----

    async def _format_call(
        self, messages: list[Any], *, run_id: str, round_no: int
    ) -> tuple[dict[str, Any] | None, str, ReportFailureKind | None, list[str]]:
        timeout = float(self._settings.report_format_timeout_seconds)
        last_kind: ReportFailureKind | None = None
        last_errors: list[str] = []
        for method in self._structured_methods:
            try:
                structured = self._model.with_structured_output(
                    DiagnosticReportPayload,
                    method=method,
                )
                result = await asyncio.wait_for(structured.ainvoke(messages), timeout=timeout)
            except TimeoutError:
                last_kind = ReportFailureKind.MODEL_ERROR
                last_errors = [f"structured output ({method}) timed out after {timeout}s"]
                logger.warning(
                    "report formatting timeout run=%s round=%s method=%s", run_id, round_no, method
                )
                continue
            except NotImplementedError:
                # 模型不支持结构化输出（如测试注入的 Fake/Custom ChatModel）
                last_kind = ReportFailureKind.MODEL_ERROR
                last_errors = [f"model does not support structured output method {method}"]
                continue
            except Exception as exc:  # noqa: BLE001 — provider 异常 → 尝试下一模式
                last_kind = ReportFailureKind.MODEL_ERROR
                last_errors = [f"{type(exc).__name__}: {str(exc)[:200]}"]
                logger.warning(
                    "report formatting failed run=%s round=%s method=%s: %s",
                    run_id,
                    round_no,
                    method,
                    exc,
                )
                continue
            if isinstance(result, DiagnosticReportPayload):
                return result.model_dump(), f"structured:{method}", None, []
            validation = _validate_payload(result if isinstance(result, dict) else {})
            if validation.ok:
                return validation.payload, f"structured:{method}", None, []
            last_kind = validation.kind
            last_errors = validation.errors

        # 回退：纯文本 JSON 输出（仍然无工具）
        try:
            response = await asyncio.wait_for(self._model.ainvoke(messages), timeout=timeout)
        except TimeoutError:
            return (
                None,
                "none",
                ReportFailureKind.MODEL_ERROR,
                [f"prompt json fallback timed out after {timeout}s"],
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("report prompt fallback failed run=%s: %s", run_id, exc)
            return (
                None,
                "none",
                ReportFailureKind.MODEL_ERROR,
                [f"{type(exc).__name__}: {str(exc)[:200]}"],
            )
        text = _content_text(response) or ""
        candidate, kind = extract_json_object(text)
        if candidate is None:
            # 结构化输出完全不可用时，模型调用错误比「没有 JSON」更贴近原因
            resolved = last_kind or kind or ReportFailureKind.NO_JSON
            return None, "prompt", resolved, last_errors or []
        validation = _validate_payload(candidate)
        if validation.ok:
            return validation.payload, "prompt", None, []
        return None, "prompt", validation.kind, validation.errors


def coerce_hypothesis_rank(payload: dict[str, Any]) -> dict[str, Any]:
    """补齐缺失 rank（模型常省略 rank，Schema 允许 0，由服务端按顺序编号）。"""
    for index, hypothesis in enumerate(payload.get("hypotheses") or [], start=1):
        if not isinstance(hypothesis, ReportHypothesis):
            rank = hypothesis.get("rank")
            if not isinstance(rank, int) or rank <= 0:
                hypothesis["rank"] = index
    return payload
