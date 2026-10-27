"""安全原语：密码哈希（argon2）与 JWT Access Token（契约：30 分钟，无 Refresh）。

注意：JWT 是无状态凭证——签发后无法即时吊销；首版以短有效期（30 分钟）
作为注销/吊销的补偿手段，密码修改后旧 Token 在剩余有效期内仍有效的
窗口是已登记的技术债务。
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import Argon2Error, InvalidHash, VerifyMismatchError

ACCESS_TOKEN_TYPE = "access"
_ALGORITHM = "HS256"
_hasher = PasswordHasher()


def hash_password(plain: str) -> str:
    """argon2id 哈希（禁明文存储）。"""
    return _hasher.hash(plain)


def verify_password(password_hash: str, plain: str) -> bool:
    """恒定语义校验：任何失败（错密码/非法哈希）都返回 False，不抛异常。"""
    try:
        return _hasher.verify(password_hash, plain)
    except (VerifyMismatchError, InvalidHash, Argon2Error):
        return False


def create_access_token(subject: str, secret: str, expires_minutes: int) -> tuple[str, int]:
    """签发 HS256 access token，返回 (token, expires_in 秒)。"""
    now = datetime.now(UTC)
    expires_delta = timedelta(minutes=expires_minutes)
    payload: dict[str, Any] = {
        "sub": subject,
        "type": ACCESS_TOKEN_TYPE,
        "iat": int(now.timestamp()),
        "exp": now + expires_delta,
        "jti": uuid.uuid4().hex,
    }
    token = jwt.encode(payload, secret, algorithm=_ALGORITHM)
    return token, int(expires_delta.total_seconds())


class TokenValidationError(Exception):
    """Token 缺失、非法或过期。"""


def decode_access_token(token: str, secret: str) -> dict[str, Any]:
    """校验并解码 access token；任何异常统一转为 TokenValidationError。"""
    try:
        payload = jwt.decode(token, secret, algorithms=[_ALGORITHM])
    except jwt.PyJWTError as exc:  # 过期/签名不符/格式错误等
        msg = "invalid or expired token"
        raise TokenValidationError(msg) from exc
    if payload.get("type") != ACCESS_TOKEN_TYPE:
        msg = "invalid or expired token"
        raise TokenValidationError(msg)
    return payload
