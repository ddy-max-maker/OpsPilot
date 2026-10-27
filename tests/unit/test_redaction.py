"""脱敏与截断单元测试（CONTRACTS §3 约束）。"""

from __future__ import annotations

import json

from app.domains.tools.redaction import (
    redact_payload,
    redact_text,
    truncate_text,
)


def test_redact_password_kv() -> None:
    text = "connect failed: password=SuperSecret123 user=app"
    out = redact_text(text)
    assert "SuperSecret123" not in out
    assert "password=" in out  # 键名保留
    assert "***REDACTED***" in out


def test_redact_bearer_token() -> None:
    out = redact_text("Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.xxx")
    assert "eyJhbGciOiJIUzI1NiJ9" not in out


def test_redact_dsn_credentials() -> None:
    out = redact_text("redis://default:secretpw@redis-host:6379/0")
    assert "secretpw" not in out
    assert "redis-host:6379" in out  # 目标保留，凭据抹除


def test_redact_api_key_prefixes() -> None:
    out = redact_text("key sk-abcdefgh12345678 leaked")
    assert "sk-abcdefgh12345678" not in out


def test_redact_case_insensitive_and_variants() -> None:
    for variant in ("PASSWORD: xyz", "api_key=xyz", "API-KEY: xyz", "TOKEN:xyz"):
        assert "***REDACTED***" in redact_text(variant), variant


def test_truncate_text_byte_boundary() -> None:
    text = "中文" * 100
    clipped, truncated = truncate_text(text, 10)
    assert truncated is True
    assert len(clipped.encode("utf-8")) <= 10  # 不切断多字节字符


def test_truncate_text_no_truncation() -> None:
    clipped, truncated = truncate_text("short", 100)
    assert clipped == "short"
    assert truncated is False


def test_redact_payload_within_limit() -> None:
    payload = {"k": "v", "password": "topsecret"}
    out, truncated = redact_payload(payload, 10_000)
    assert truncated is False
    assert "topsecret" not in json.dumps(out)
    assert out["k"] == "v"


def test_redact_payload_truncated_on_limit() -> None:
    payload = {"items": [{"i": i, "data": "x" * 100} for i in range(50)]}
    out, truncated = redact_payload(payload, 512)
    assert truncated is True
    assert len(json.dumps(out).encode("utf-8")) <= 600  # 元素级丢弃，保留前部
    assert len(out["items"]) < 50
