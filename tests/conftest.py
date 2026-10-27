"""pytest 全局夹具：真实 PostgreSQL 集成测试基础设施。

约定：
- conftest 导入时（收集阶段）先设置测试环境变量，再导入 app；
- 一次性重建 `opspilot_test` 库并通过 Alembic 迁移建 schema（禁 create_all）；
- 每个测试前 TRUNCATE 全部业务表，应用 lifespan 重新执行 bootstrap，
  从而获得干净且确定的起点（初始 Admin 每测试重建，must_change_password=True）。

需要 Docker PostgreSQL 已启动：`docker compose up -d postgres`。
"""

from __future__ import annotations

import asyncio
import os
import sys
from collections.abc import AsyncIterator
from pathlib import Path

# Windows 默认 ProactorEventLoop 不支持 psycopg（worker checkpoint 池）异步模式；
# 统一切换 SelectorEventLoop（asyncpg/httpx 兼容）。
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

# ---- 1. 先设置测试环境变量（必须在导入 app 之前）----
# 注意：用 127.0.0.1 而非 localhost——本机存在两个 PostgreSQL（Docker 容器 0.0.0.0:5432
# 与本地服务 [::1]:5432），localhost 的 ::1 优先解析会连到本地服务，导致
# truncate 与应用各连一个库。127.0.0.1 强制走 Docker 容器。
PROJECT_ROOT = Path(__file__).resolve().parents[1]
TEST_DB_NAME = "opspilot_test"
PG_SERVER_URL = "postgresql+asyncpg://opspilot:opspilot@127.0.0.1:5432/postgres"
TEST_DATABASE_URL = f"postgresql+asyncpg://opspilot:opspilot@127.0.0.1:5432/{TEST_DB_NAME}"
ADMIN_BOOT_USERNAME = "admin"
ADMIN_BOOT_PASSWORD = "AdminBoot123!"

os.environ["DATABASE_URL"] = TEST_DATABASE_URL
os.environ["JWT_SECRET"] = "test-jwt-secret-0123456789abcdef"
os.environ["WEBHOOK_ALERTS_SECRET"] = "test-webhook-secret-0123456789abcdef"
os.environ["BOOTSTRAP_ADMIN_USERNAME"] = ADMIN_BOOT_USERNAME
os.environ["BOOTSTRAP_ADMIN_EMAIL"] = "admin@example.com"
os.environ["BOOTSTRAP_ADMIN_PASSWORD"] = ADMIN_BOOT_PASSWORD

# ---- 2. 导入 app（Settings 以测试环境变量缓存）----
import httpx  # noqa: E402
import pytest  # noqa: E402
from alembic.config import Config  # noqa: E402
from sqlalchemy import select, text  # noqa: E402
from sqlalchemy.ext.asyncio import (  # noqa: E402
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from alembic import command  # noqa: E402
from app.core.ids import new_id  # noqa: E402
from app.core.security import hash_password  # noqa: E402
from app.models import AssetAccessPolicy, Role, User, UserRole  # noqa: E402

ALL_TABLES = (
    "users, roles, user_roles, asset_access_policies, assets, asset_edges, "
    "incidents, alerts, audit_events, api_idempotency_keys, outbox_events, "
    "investigation_runs, run_events, evidence, hypotheses, diagnostic_reports, "
    "connector_types, connector_instances, secret_refs"
)


async def _recreate_database() -> None:
    """删除并重建测试库（连接 postgres 管理库，AUTOCOMMIT）。"""
    engine = create_async_engine(PG_SERVER_URL, isolation_level="AUTOCOMMIT")
    try:
        async with engine.connect() as conn:
            await conn.execute(text(f"DROP DATABASE IF EXISTS {TEST_DB_NAME} WITH (FORCE)"))
            await conn.execute(text(f"CREATE DATABASE {TEST_DB_NAME}"))
    finally:
        await engine.dispose()


def _run_alembic_upgrade() -> None:
    """对测试库执行 alembic upgrade head（env.py 读取 DATABASE_URL）。"""
    cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))
    command.upgrade(cfg, "head")


# ---- 3. 一次性准备测试数据库（conftest 导入时执行一次）----
asyncio.run(_recreate_database())
_run_alembic_upgrade()

# ---- 4. 夹具 ----


@pytest.fixture
async def db_engine() -> AsyncIterator:
    """直连测试库的引擎（与 app 内部引擎独立，同一测试内同 loop）。"""
    engine = create_async_engine(TEST_DATABASE_URL)
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest.fixture
async def db_session_factory(db_engine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(db_engine, expire_on_commit=False)


@pytest.fixture
async def db_session(db_session_factory) -> AsyncIterator[AsyncSession]:
    async with db_session_factory() as session:
        yield session


async def truncate_all(engine) -> None:
    """清空全部业务表与 checkpoint（供 seed 夹具在插入前确定性调用）。

    注意：业务表 TRUNCATE 与 langgraph checkpoint 清理必须分事务——
    checkpoint 表由 checkpointer.setup() 创建（不属于 Alembic 迁移），在尚未
    建表的库上 DELETE 会报 undefined_table；若与 TRUNCATE 同事务，错误被吞掉后
    事务仍处于 aborted 状态，COMMIT 失败导致整体回滚、TRUNCATE 失效（脏数据
    跨测试存活）。此处先探测表存在，避免错误路径。
    """
    async with engine.begin() as conn:
        await conn.execute(text(f"TRUNCATE TABLE {ALL_TABLES} RESTART IDENTITY CASCADE"))
    async with engine.begin() as conn:
        for table in ("checkpoint_writes", "checkpoint_blobs", "checkpoints"):
            exists = await conn.scalar(
                text(
                    "SELECT EXISTS (SELECT 1 FROM pg_class c JOIN pg_namespace n "
                    "ON n.oid = c.relnamespace WHERE n.nspname = 'langgraph' "
                    "AND c.relname = :t)"
                ),
                {"t": table},
            )
            if exists:
                await conn.execute(text(f"DELETE FROM langgraph.{table}"))


@pytest.fixture
async def clean_db(db_engine) -> None:
    """清空全部业务表（保留 schema）；必须先于 app bootstrap 生效。"""
    await truncate_all(db_engine)


@pytest.fixture
async def client(clean_db) -> AsyncIterator[httpx.AsyncClient]:
    """每个测试独立 ASGI app：lifespan 初始化引擎并重新执行 bootstrap。"""
    from app.main import create_app

    application = create_app()
    async with application.router.lifespan_context(application):
        transport = httpx.ASGITransport(app=application)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as async_client:
            yield async_client


async def api_login(client: httpx.AsyncClient, username: str, password: str) -> httpx.Response:
    """登录辅助（非夹具）。"""
    return await client.post(
        "/api/v1/auth/login", json={"username": username, "password": password}
    )


def auth_headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
async def ready_admin(client) -> dict[str, str]:
    """已通过首登改密的 Admin 请求头（业务可用状态）。"""
    resp = await api_login(client, ADMIN_BOOT_USERNAME, ADMIN_BOOT_PASSWORD)
    assert resp.status_code == 200, resp.text
    token = resp.json()["data"]["access_token"]
    change = await client.post(
        "/api/v1/auth/change-password",
        json={"old_password": ADMIN_BOOT_PASSWORD, "new_password": "AdminReady123!"},
        headers=auth_headers(token),
    )
    assert change.status_code == 200, change.text
    return auth_headers(token)


async def create_test_user(
    db_session_factory: async_sessionmaker[AsyncSession],
    *,
    username: str,
    password: str,
    role_codes: list[str],
    policies: list[dict[str, str | None]] | None = None,
    is_active: bool = True,
) -> User:
    """直连测试库创建用户（含角色与资产范围策略），供权限用例使用。"""
    async with db_session_factory() as session:
        user = User(
            id=new_id("usr_"),
            username=username,
            email=f"{username}@test.local",
            password_hash=hash_password(password),
            display_name=username,
            is_active=is_active,
            must_change_password=False,
        )
        session.add(user)
        for code in role_codes:
            role = (await session.execute(select(Role).where(Role.code == code))).scalar_one()
            session.add(UserRole(user_id=user.id, role_id=role.id))
        for policy in policies or []:
            session.add(
                AssetAccessPolicy(
                    id=new_id("pol_"),
                    user_id=user.id,
                    asset_id=policy.get("asset_id"),
                    environment=policy.get("environment"),
                    permission=policy.get("permission", "read"),
                )
            )
        await session.commit()
        await session.refresh(user)
        return user


@pytest.fixture
async def operator_headers(client, db_session_factory) -> dict[str, str]:
    """拥有 sandbox 只读范围的操作员请求头（user 级策略）。"""
    await create_test_user(
        db_session_factory,
        username="operator1",
        password="Operator123!",
        role_codes=["operator"],
        policies=[{"environment": "sandbox", "permission": "read"}],
    )
    resp = await api_login(client, "operator1", "Operator123!")
    assert resp.status_code == 200, resp.text
    return auth_headers(resp.json()["data"]["access_token"])


@pytest.fixture
async def viewer_headers(client, db_session_factory) -> dict[str, str]:
    """无任何资产策略的只读用户（默认拒绝路径用例）。"""
    await create_test_user(
        db_session_factory,
        username="viewer1",
        password="Viewer123!",
        role_codes=["viewer"],
    )
    resp = await api_login(client, "viewer1", "Viewer123!")
    assert resp.status_code == 200, resp.text
    return auth_headers(resp.json()["data"]["access_token"])
