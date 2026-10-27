"""Alembic 迁移集成测试：升级建全表、降级完整回滚（独立临时库执行）。

说明：本测试为同步函数——Alembic 异步 env.py 内部自行 asyncio.run，
不能在已运行的事件循环中调用，因此不使用 async 测试。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from alembic import command

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SCRATCH_DB = "opspilot_migration_test"
SERVER_URL = "postgresql+asyncpg://opspilot:opspilot@localhost:5432/postgres"
SCRATCH_URL = f"postgresql+asyncpg://opspilot:opspilot@localhost:5432/{SCRATCH_DB}"

EXPECTED_TABLES = {
    "users",
    "roles",
    "user_roles",
    "asset_access_policies",
    "assets",
    "asset_edges",
    "alerts",
    "incidents",
    "audit_events",
    "api_idempotency_keys",
    "outbox_events",
}

D1_TABLES = (
    "users",
    "roles",
    "user_roles",
    "asset_access_policies",
    "assets",
    "asset_edges",
    "alerts",
    "incidents",
    "audit_events",
    "api_idempotency_keys",
    "outbox_events",
)


def _alembic_config() -> Config:
    cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))
    return cfg


async def _create_scratch_db() -> None:
    engine = create_async_engine(SERVER_URL, isolation_level="AUTOCOMMIT")
    try:
        async with engine.connect() as conn:
            await conn.execute(text(f"DROP DATABASE IF EXISTS {SCRATCH_DB} WITH (FORCE)"))
            await conn.execute(text(f"CREATE DATABASE {SCRATCH_DB}"))
    finally:
        await engine.dispose()


async def _drop_scratch_db() -> None:
    engine = create_async_engine(SERVER_URL, isolation_level="AUTOCOMMIT")
    try:
        async with engine.connect() as conn:
            await conn.execute(text(f"DROP DATABASE IF EXISTS {SCRATCH_DB} WITH (FORCE)"))
    finally:
        await engine.dispose()


async def _tables_in(url: str) -> set[str]:
    engine = create_async_engine(url)
    try:
        async with engine.connect() as conn:
            rows = await conn.execute(
                text(
                    "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'"
                )
            )
            return {row[0] for row in rows}
    finally:
        await engine.dispose()


def test_upgrade_head_creates_all_d1_tables(monkeypatch) -> None:
    """空库 upgrade head：D1 全部表 + alembic_version；唯一约束真实存在。"""
    asyncio.run(_create_scratch_db())
    monkeypatch.setenv("DATABASE_URL", SCRATCH_URL)
    try:
        command.upgrade(_alembic_config(), "head")
        tables = asyncio.run(_tables_in(SCRATCH_URL))
        assert tables >= EXPECTED_TABLES
        assert "alembic_version" in tables
        assert len(EXPECTED_TABLES) == 11  # DATA_MODEL §2 D1 清单
    finally:
        monkeypatch.undo()
        asyncio.run(_drop_scratch_db())


def test_downgrade_base_drops_all_tables(monkeypatch) -> None:
    """downgrade base：完整回滚全部 D1 表。"""
    asyncio.run(_create_scratch_db())
    monkeypatch.setenv("DATABASE_URL", SCRATCH_URL)
    try:
        command.upgrade(_alembic_config(), "head")
        command.downgrade(_alembic_config(), "base")
        tables = asyncio.run(_tables_in(SCRATCH_URL))
        business_tables = tables - {"alembic_version"}
        assert business_tables == set()
    finally:
        monkeypatch.undo()
        asyncio.run(_drop_scratch_db())


def test_unique_constraint_materialized(monkeypatch) -> None:
    """迁移后真实约束抽查：assets(name, environment) 唯一约束存在。"""
    asyncio.run(_create_scratch_db())
    monkeypatch.setenv("DATABASE_URL", SCRATCH_URL)
    try:
        command.upgrade(_alembic_config(), "head")
        engine = create_async_engine(SCRATCH_URL)
        try:

            async def _fetch() -> object:
                async with engine.connect() as conn:
                    rows = await conn.execute(
                        text(
                            "SELECT conname FROM pg_constraint "
                            "WHERE contype = 'u' AND conrelid = 'assets'::regclass"
                        )
                    )
                    return [r[0] for r in rows]

            names = asyncio.run(_fetch())
        finally:
            asyncio.run(engine.dispose())
        assert "uq_assets_name_environment" in names
    finally:
        monkeypatch.undo()
        asyncio.run(_drop_scratch_db())
