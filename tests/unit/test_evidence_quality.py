"""D3 证据质量分类单元测试（任务五：向后兼容语义）。"""

from __future__ import annotations

from app.domains.tools.evidence_quality import (
    EvidenceQuality,
    classify_evidence,
    root_cause_capable,
)


def test_ok_is_healthy() -> None:
    assert classify_evidence("ok", None, {}) is EvidenceQuality.HEALTHY


def test_http_504_is_fault_observed() -> None:
    """成功接收到 HTTP 504：有效故障观测（status=error 但不是无效证据）。"""
    assert (
        classify_evidence("error", "HTTP_504", {"http_status": 504, "body": "gateway timeout"})
        is EvidenceQuality.FAULT_OBSERVED
    )


def test_http_502_is_fault_observed() -> None:
    assert (
        classify_evidence("error", "HTTP_502", {"http_status": 502})
        is EvidenceQuality.FAULT_OBSERVED
    )


def test_payload_status_code_marker_is_fault_observed() -> None:
    assert (
        classify_evidence("error", None, {"status_code": 500, "body": "boom"})
        is EvidenceQuality.FAULT_OBSERVED
    )


def test_redis_connection_error_is_indeterminate() -> None:
    """连接错误：无法区分目标故障与采集链路问题 → 不单独支撑根因。"""
    assert (
        classify_evidence("error", "REDIS_ERROR", {"error_type": "ConnectionError"})
        is EvidenceQuality.INDETERMINATE
    )


def test_timeout_is_indeterminate() -> None:
    assert classify_evidence("timeout", "CONNECTOR_TIMEOUT", {}) is EvidenceQuality.INDETERMINATE


def test_unavailable_is_collection_failed() -> None:
    assert (
        classify_evidence("unavailable", "CONTAINER_NOT_FOUND", {})
        is EvidenceQuality.COLLECTION_FAILED
    )
    assert (
        classify_evidence("unavailable", "CONNECTOR_NOT_CONFIGURED", {})
        is EvidenceQuality.COLLECTION_FAILED
    )


def test_root_cause_capability() -> None:
    assert root_cause_capable(EvidenceQuality.HEALTHY)
    assert root_cause_capable(EvidenceQuality.FAULT_OBSERVED)
    assert not root_cause_capable(EvidenceQuality.INDETERMINATE)
    assert not root_cause_capable(EvidenceQuality.COLLECTION_FAILED)
