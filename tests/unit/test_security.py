"""安全原语单元测试：argon2 哈希 / JWT 签发与校验。"""

from __future__ import annotations

import time
import uuid
from datetime import UTC, datetime, timedelta

import jwt as pyjwt
import pytest

from app.core.security import (
    TokenValidationError,
    create_access_token,
    decode_access_token,
    hash_password,
    verify_password,
)

SECRET = "unit-test-secret"


# ---- 密码哈希 ----


def test_password_hash_roundtrip() -> None:
    hashed = hash_password("S3curePass!")
    assert hashed != "S3curePass!"
    assert hashed.startswith("$argon2")
    assert verify_password(hashed, "S3curePass!")
    assert not verify_password(hashed, "wrong-password")


def test_password_hash_unique_per_call() -> None:
    assert hash_password("same") != hash_password("same")  # 随机盐


def test_invalid_hash_never_verifies() -> None:
    """系统账号的占位哈希 '!' 校验恒失败（防登录兜底）。"""
    assert not verify_password("!", "anything")
    assert not verify_password("", "anything")


# ---- JWT ----


def test_access_token_roundtrip() -> None:
    token, expires_in = create_access_token("usr_1", SECRET, 30)
    assert expires_in == 1800
    payload = decode_access_token(token, SECRET)
    assert payload["sub"] == "usr_1"
    assert payload["type"] == "access"
    assert payload["jti"]
    assert payload["exp"] - payload["iat"] == 1800


def test_expired_token_rejected() -> None:
    token, _ = create_access_token("usr_1", SECRET, -1)  # 已过期
    with pytest.raises(TokenValidationError):
        decode_access_token(token, SECRET)


def test_wrong_secret_rejected() -> None:
    token, _ = create_access_token("usr_1", SECRET, 30)
    with pytest.raises(TokenValidationError):
        decode_access_token(token, "other-secret")


def test_non_access_type_rejected() -> None:
    now = datetime.now(UTC)
    forged = pyjwt.encode(
        {
            "sub": "usr_1",
            "type": "refresh",  # 伪造非 access 类型
            "iat": int(now.timestamp()),
            "exp": int((now + timedelta(minutes=5)).timestamp()),
            "jti": uuid.uuid4().hex,
        },
        SECRET,
        algorithm="HS256",
    )
    with pytest.raises(TokenValidationError):
        decode_access_token(forged, SECRET)


def test_future_issued_token_accepted() -> None:
    """时钟容差：刚签发的 token（iat=now）立即可用。"""
    token, _ = create_access_token("usr_1", SECRET, 30)
    time.sleep(0.01)
    assert decode_access_token(token, SECRET)["sub"] == "usr_1"
