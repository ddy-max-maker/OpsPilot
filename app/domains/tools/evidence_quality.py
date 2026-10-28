"""证据质量判定（D3 任务五，向后兼容：不改 Evidence 冻结 Schema）。

区分（PRD §5 / D3 未知故障语义）：
- HEALTHY：成功采集且目标正常（status=ok）；
- FAULT_OBSERVED：**成功采集到目标服务返回的错误响应**（如 HTTP 504/5xx——
  这是有效故障观测，不能因 status=error 视为无效证据；但也不足以单独证明
  具体上游根因）；
- INDETERMINATE：采集超时/连接错误（无法区分目标故障与采集链路问题，可作为
  故障现象线索，不单独支撑根因）；
- COLLECTION_FAILED：采集通道/配置问题（unavailable，如 CONTAINER_NOT_FOUND、
  CONNECTOR_NOT_CONFIGURED）。

判定依据现有列（status + error_code + data_ref），schema_version 保持 "1"。
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

# 目标服务真实返回错误响应的 error_code 前缀（http.health_check 收到响应体时）
_RESPONSE_ERROR_PREFIXES = ("HTTP_",)
# 采集通道/配置问题（unavailable 的既有 error_code）
_COLLECTION_FAILURE_CODES = {
    "CONNECTOR_NOT_CONFIGURED",
    "CONTAINER_NOT_FOUND",
    "HEALTH_URL_NOT_REGISTERED",
    "NOT_IMPLEMENTED",
    "INSTANCE_AMBIGUOUS",
    "INSTANCE_NOT_BOUND",
}


class EvidenceQuality(StrEnum):
    """证据质量分类（运行时计算，不落库——向后兼容）。

    D4 新增 KNOWLEDGE：历史知识检索结果（source_type=knowledge）——即使
    采集状态为 ok，也**绝不参与当前实时根因支撑**（PRD §6：历史相似性
    不能当作当前已确认根因；supported 需实时业务观测）。
    """

    HEALTHY = "healthy"
    FAULT_OBSERVED = "fault_observed"
    INDETERMINATE = "indeterminate"
    COLLECTION_FAILED = "collection_failed"
    KNOWLEDGE = "knowledge"


def classify_evidence(
    status: str,
    error_code: str | None,
    data_ref: dict[str, Any] | None,
    source_type: str | None = None,
) -> EvidenceQuality:
    """按既有 Evidence 列计算质量分类（向后兼容：source_type 缺省走 D3 语义）。"""
    if source_type == "knowledge":
        # 历史知识检索：与实时观测严格区分（CONTRACTS §3 知识隔离）
        return EvidenceQuality.KNOWLEDGE
    if status == "ok":
        return EvidenceQuality.HEALTHY
    if status == "unavailable":
        return EvidenceQuality.COLLECTION_FAILED
    # status ∈ {error, timeout}
    if status == "error" and error_code and error_code.startswith(_RESPONSE_ERROR_PREFIXES):
        # 目标服务真实返回了错误响应（如 HTTP_504/HTTP_502）：有效故障观测
        return EvidenceQuality.FAULT_OBSERVED
    if status == "error" and isinstance(data_ref, dict):
        marker = data_ref.get("status_code") or data_ref.get("response_received")
        if marker:
            return EvidenceQuality.FAULT_OBSERVED
    return EvidenceQuality.INDETERMINATE


def root_cause_capable(quality: EvidenceQuality) -> bool:
    """该质量等级的证据是否可参与 supported 根因支撑。

    HEALTHY / FAULT_OBSERVED 可以（504 是有效故障观测）；
    INDETERMINATE / COLLECTION_FAILED 不可以（采集失败不自动等于业务根因）；
    KNOWLEDGE 绝不可以（历史经验只引导排查方向，不证明当前根因——D4 红线）。
    """
    return quality in (EvidenceQuality.HEALTHY, EvidenceQuality.FAULT_OBSERVED)
