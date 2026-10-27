"""cursor 分页（CONTRACTS §1.3）：base64(json({created_at, id})) + HMAC-SHA256 签名。

排序键为 (created_at, id)，仅支持顺序翻页（不支持跳页）；签名防篡改。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from dataclasses import dataclass
from datetime import datetime

from app.core.errors import validation_error


@dataclass(frozen=True)
class Cursor:
    """排序键快照（created_at 降序 + id 降序游标）。"""

    created_at: datetime
    id: str


def encode_cursor(cursor: Cursor, secret: str) -> str:
    """编码并签名 cursor：base64({"p": {...}, "s": <hmac>})。"""
    payload = json.dumps(
        {"created_at": cursor.created_at.isoformat(), "id": cursor.id},
        separators=(",", ":"),
    )
    signature = hmac.new(secret.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256)
    raw = json.dumps({"p": payload, "s": signature.hexdigest()[:32]}, separators=(",", ":"))
    return base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii").rstrip("=")


def decode_cursor(cursor: str, secret: str) -> Cursor:
    """校验签名并解码；任何失败（格式/篡改/过期签名）统一 400 VALIDATION_ERROR。"""
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        raw = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")))
        if not isinstance(raw, dict):
            raise ValueError("cursor envelope must be an object")
        payload = raw["p"]
        signature = raw["s"]
        if not isinstance(payload, str) or not isinstance(signature, str):
            raise ValueError("cursor payload/signature must be strings")
        expected = hmac.new(secret.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256)
        if not hmac.compare_digest(signature, expected.hexdigest()[:32]):
            raise ValueError("signature mismatch")
        decoded = json.loads(payload)
        if not isinstance(decoded, dict):
            raise ValueError("cursor payload must be an object")
        created_at = datetime.fromisoformat(decoded["created_at"])
        return Cursor(created_at=created_at, id=str(decoded["id"]))
    except (KeyError, ValueError, TypeError, UnicodeError) as exc:
        raise validation_error("invalid pagination cursor") from exc
