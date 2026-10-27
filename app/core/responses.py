"""统一响应包裹（PRD §10 / CONTRACTS §1.2）：所有响应形如
{"request_id": ..., "data": ..., "error": null}。

request_id 由服务端生成（HTTP 中间件写入 contextvar），贯穿日志与审计。
"""

from __future__ import annotations

from contextvars import ContextVar
from typing import Generic, TypeVar

from pydantic import BaseModel

from app.core.errors import ApiError

_request_id: ContextVar[str] = ContextVar("request_id", default="")

T = TypeVar("T")


class ErrorBody(BaseModel):
    code: str
    message: str
    details: dict[str, object] | None = None


class Envelope(BaseModel, Generic[T]):
    request_id: str
    data: T | None = None
    error: ErrorBody | None = None


def set_request_id(request_id: str) -> None:
    _request_id.set(request_id)


def current_request_id() -> str:
    return _request_id.get()


def ok(data: T) -> Envelope[T]:
    """成功响应包裹。"""
    return Envelope(request_id=current_request_id(), data=data, error=None)


def error_envelope(exc: ApiError) -> Envelope[None]:
    """失败响应包裹。"""
    body = ErrorBody(code=exc.code.value, message=exc.message, details=exc.details)
    return Envelope(request_id=current_request_id(), data=None, error=body)
