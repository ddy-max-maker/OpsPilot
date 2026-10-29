"""FastAPI 应用工厂 + 全局异常处理 + lifespan（引擎初始化 / bootstrap）。

响应规范：所有响应统一包裹 {request_id, data, error}（PRD §10 / CONTRACTS §1.2）。
异常映射：RequestValidationError -> 400 VALIDATION_ERROR；未匹配路由 -> 404 NOT_FOUND；
未知异常 -> 500 INTERNAL（不泄漏内部细节）。
"""

from __future__ import annotations

import asyncio
import contextlib
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
from app.domains.integrations import service as integrations_service

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

logger = logging.getLogger("opspilot")


async def _warm_embeddings() -> None:
    """API 侧后台预热 Embedding 模型（D6）。

    知识发布链路（knowledge/versions/{id}/publish）无论 RAG_MODE 都会建向量索引，
    因此模型冷加载（本地 e5 首次加载可达数十秒）会落在第一个发布请求上。这里在
    启动后异步预热，不阻塞 startup / 健康检查；失败只记录日志并置 load_error，
    由发布链路显式报错（不伪装成功）。
    """
    from app.domains.rag.embeddings import get_embedding_provider  # noqa: PLC0415

    try:
        provider = get_embedding_provider()
    except Exception:  # noqa: BLE001 — provider 配置错误不应拖垮 API 启动
        logger.exception("embedding provider unavailable; knowledge publish will fail fast")
        return
    if provider.ready:
        return
    try:
        await asyncio.to_thread(provider.warm_up)
    except Exception:  # noqa: BLE001 — 预热失败：知识发布显式报错，其余功能不受影响
        logger.warning("embedding warm-up failed: %s", provider.load_error, exc_info=True)
        return
    logger.info("embedding model warmed up (%s)", provider.model_version)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """启动：初始化引擎并执行幂等 bootstrap（角色/系统账号/初始 Admin）。"""
    settings = get_settings()
    init_engine(settings.database_url)
    factory = get_session_factory()
    async with factory() as session:
        await auth_service.bootstrap(session, settings)
        # D3：Capability Registry 启动同步（内置 manifest 兼容校验后落库；
        # 不兼容的连接器拒绝注册并记录日志）
        rejected = await integrations_service.sync_capability_manifests(session)
    if rejected:
        logger.warning("capability manifests rejected (incompatible): %s", rejected)
    # 后台预热 Embedding 模型（fake provider 无需预热；不阻塞启动）
    warmup: asyncio.Task[None] | None = None
    if settings.embedding_provider != "fake":
        warmup = asyncio.create_task(_warm_embeddings())
    logger.info("OpsPilot Next started (database ready, bootstrap complete)")
    try:
        yield
    finally:
        if warmup is not None and not warmup.done():
            warmup.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await warmup  # 不等待线程收尾（模型加载线程自行结束），只回收任务
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
