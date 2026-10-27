"""SQLAlchemy 2 异步引擎与会话管理。

事务边界约定（一个请求一个 Session）：
- FastAPI 依赖 get_db 为每个请求开启一个 AsyncSession；
- 业务用例的提交/回滚由领域服务显式执行（见各 domains/*/service.py）；
- 依赖在请求结束时关闭 Session（未提交事务自动回滚）。
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def init_engine(database_url: str, **engine_kwargs: object) -> AsyncEngine:
    """初始化（或替换）全局异步引擎。重复调用会先释放旧引擎。"""
    global _engine, _session_factory
    if _engine is not None:
        # 引擎是同步对象，dispose 可安全调用（真正断连在事件循环中完成）。
        _engine.sync_engine.dispose()
    _engine = create_async_engine(database_url, pool_pre_ping=True, **engine_kwargs)
    _session_factory = async_sessionmaker(_engine, expire_on_commit=False)
    return _engine


def get_engine() -> AsyncEngine:
    if _engine is None:
        msg = "数据库引擎未初始化：请先调用 init_engine()（应用 lifespan 中完成）"
        raise RuntimeError(msg)
    return _engine


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    if _session_factory is None:
        msg = "会话工厂未初始化：请先调用 init_engine()"
        raise RuntimeError(msg)
    return _session_factory


async def dispose_engine() -> None:
    """释放全局引擎（应用关停时调用）。"""
    global _engine, _session_factory
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _session_factory = None


async def get_db() -> AsyncIterator[AsyncSession]:
    """FastAPI 依赖：每请求一个 Session；异常时由上下文管理器回滚未提交事务。"""
    factory = get_session_factory()
    async with factory() as session:
        yield session
