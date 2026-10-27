"""统一错误模型（对齐 CONTRACTS.md §1.4 首版错误码枚举）。

所有业务错误通过 ApiError 抛出，由全局异常处理器包装为统一响应包裹：
{request_id, data: null, error: {code, message, details}}。
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any


class ErrorCode(StrEnum):
    """稳定错误码枚举（首版子集，仅包含 D1-A 用到的码）。"""

    VALIDATION_ERROR = "VALIDATION_ERROR"
    UNAUTHENTICATED = "UNAUTHENTICATED"
    FORBIDDEN = "FORBIDDEN"
    FORBIDDEN_ASSET_SCOPE = "FORBIDDEN_ASSET_SCOPE"
    PASSWORD_CHANGE_REQUIRED = "PASSWORD_CHANGE_REQUIRED"
    NOT_FOUND = "NOT_FOUND"
    CONFLICT_STATE = "CONFLICT_STATE"
    IDEMPOTENCY_KEY_MISMATCH = "IDEMPOTENCY_KEY_MISMATCH"
    INTERNAL = "INTERNAL"


class ApiError(Exception):
    """携带 HTTP 状态码与稳定错误码的业务异常。"""

    def __init__(
        self,
        status_code: int,
        code: ErrorCode,
        message: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.details = details


def validation_error(message: str, details: dict[str, Any] | None = None) -> ApiError:
    return ApiError(400, ErrorCode.VALIDATION_ERROR, message, details)


def unauthenticated(message: str = "authentication required or failed") -> ApiError:
    return ApiError(401, ErrorCode.UNAUTHENTICATED, message)


def forbidden(
    message: str = "role not allowed for this operation",
    details: dict[str, Any] | None = None,
) -> ApiError:
    return ApiError(403, ErrorCode.FORBIDDEN, message, details)


def password_change_required() -> ApiError:
    return ApiError(
        403,
        ErrorCode.PASSWORD_CHANGE_REQUIRED,
        "password change required before using business APIs",
    )


def not_found(message: str = "resource not found") -> ApiError:
    """404 语义同时承担越权伪装：不暴露资源存在性（CONTRACTS §7.2）。"""
    return ApiError(404, ErrorCode.NOT_FOUND, message)


def conflict_state(message: str, details: dict[str, Any] | None = None) -> ApiError:
    return ApiError(409, ErrorCode.CONFLICT_STATE, message, details)


def internal(message: str = "internal server error") -> ApiError:
    return ApiError(500, ErrorCode.INTERNAL, message)
