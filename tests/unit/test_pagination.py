"""cursor 分页单元测试：签名编码/解码/防篡改。"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.core.errors import ApiError, ErrorCode
from app.core.pagination import Cursor, decode_cursor, encode_cursor

SECRET = "unit-test-secret"


def _cursor() -> Cursor:
    return Cursor(created_at=datetime(2026, 10, 27, 8, 0, 0, tzinfo=UTC), id="as_01abc")


def test_roundtrip() -> None:
    encoded = encode_cursor(_cursor(), SECRET)
    assert encoded and "=" not in encoded  # urlsafe 无填充
    decoded = decode_cursor(encoded, SECRET)
    assert decoded.id == "as_01abc"
    assert decoded.created_at == datetime(2026, 10, 27, 8, 0, 0, tzinfo=UTC)


def test_tampered_payload_rejected() -> None:
    encoded = encode_cursor(_cursor(), SECRET)
    # 篡改中位字符（末位字符可能落在 base64 填充位，篡改后解码字节不变，是无效篡改）
    mid = len(encoded) // 2
    tampered = encoded[:mid] + ("0" if encoded[mid] != "0" else "1") + encoded[mid + 1 :]
    assert tampered != encoded
    with pytest.raises(ApiError) as exc_info:
        decode_cursor(tampered, SECRET)
    assert exc_info.value.code == ErrorCode.VALIDATION_ERROR


def test_wrong_secret_rejected() -> None:
    encoded = encode_cursor(_cursor(), SECRET)
    with pytest.raises(ApiError):
        decode_cursor(encoded, "another-secret")


def test_garbage_rejected() -> None:
    for garbage in ("!!!not-base64!!!", "eyJwIjogMX0", ""):
        with pytest.raises(ApiError):
            decode_cursor(garbage, SECRET)


def test_cursor_deterministic() -> None:
    c = _cursor()
    assert encode_cursor(c, SECRET) == encode_cursor(c, SECRET)
