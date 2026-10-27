"""FastAPI 应用工厂 + 全局异常处理 + lifespan（引擎初始化 / bootstrap）。

响应规范：所有响应统一包裹 {request_id, data, error}（PRD §10 / CONTRACTS §1.2）。
异常映射：RequestValidationError -> 400 VALIDATION_ERROR；未匹配路由 -> 404 NOT_FOUND；
未知异常 -> 500 INTERNAL（不泄漏内部细节）。
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.responses import Response

from app.api.v1.router import api_router
from app.core.config import get_settings
from app.core.db import dispose_engine, get_session_factory, init_engine
from app.core.errors import ApiError, ErrorCode
from app.core.responses import Envelope, ErrorBody, current_request_id, set_request_id
from app.domains.auth import service as auth_service

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

logger = logging.getLogger("opspilot")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """启动：初始化引擎并执行幂等 bootstrap（角色/系统账号/初始 Admin）。"""
    settings = get_settings()
    init_engine(settings.database_url)
    factory = get_session_factory()
    async with factory() as session:
        await auth_service.bootstrap(session, settings)
    logger.info("OpsPilot Next started (database ready, bootstrap complete)")
    yield
    await dispose_engine()
    logger.info("OpsPilot Next stopped, engine disposed")


def _envelope_response(status_code: int, error: ErrorBody, request_id: str) -> JSONResponse:
    envelope: Envelope[None] = Envelope(request_id=request_id, data=None, error=error)
    return JSONResponse(status_code=status_code, content=envelope.model_dump())


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(ApiError)
    async def api_error_handler(request: Request, exc: ApiError) -> JSONResponse:
        body = ErrorBody(code=exc.code.value, message=exc.message, details=exc.details)
        return _envelope_response(exc.status_code, body, current_request_id())

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        # 只保留安全字段（字段名/类型/原因），不含原始输入值
        details = {
            "errors": [
                {"loc": [str(loc) for loc in err.get("loc", [])], "type": err.get("type")}
                for err in exc.errors()
            ]
        }
        body = ErrorBody(
            code=ErrorCode.VALIDATION_ERROR.value,
            message="request validation failed",
            details=details,
        )
        return _envelope_response(400, body, current_request_id())

    @app.exception_handler(StarletteHTTPException)
    async def http_exception_handler(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        code_by_status = {
            401: ErrorCode.UNAUTHENTICATED,
            403: ErrorCode.FORBIDDEN,
            404: ErrorCode.NOT_FOUND,
            405: ErrorCode.NOT_FOUND,
        }
        code = code_by_status.get(exc.status_code, ErrorCode.INTERNAL).value
        body = ErrorBody(code=code, message=str(exc.detail), details=None)
        return _envelope_response(exc.status_code, body, current_request_id())

    @app.exception_handler(Exception)
    async def internal_error_handler(request: Request, exc: Exception) -> JSONResponse:
        logger.exception("unhandled exception on %s %s", request.method, request.url.path)
        body = ErrorBody(
            code=ErrorCode.INTERNAL.value,
            message="internal server error",
            details=None,
        )
        return _envelope_response(500, body, current_request_id())


def create_app() -> FastAPI:
    """应用工厂。"""
    settings = get_settings()
    app = FastAPI(
        title=settings.app_name,
        version="0.1.0",
        description="OpsPilot Next｜企业级智能运维故障诊断与受控恢复平台（D1-A 基础工程）",
        lifespan=lifespan,
    )

    @app.middleware("http")
    async def request_context_middleware(
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        """生成 request_id（贯穿日志/审计/响应包裹）并回写 X-Request-ID 头。"""
        request_id = f"req_{uuid.uuid4().hex[:24]}"
        set_request_id(request_id)
        response = await call_next(request)
        response.headers["X-Request-ID"] = request_id
        return response

    app.include_router(api_router, prefix=settings.api_v1_prefix)
    register_exception_handlers(app)
    return app


app = create_app()
