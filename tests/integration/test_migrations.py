"""迁移演练测试：独立临时库执行 upgrade head + downgrade base（DATA_MODEL §5）。

同步实现：alembic 的 async env.py 在同步上下文中自行 asyncio.run；
库状态检查用 psycopg（已随 checkpoint 依赖安装）。
"""

from __future__ import annotations

import os

import psycopg
from alembic.config import Config

from alembic import command
from tests.conftest import PROJECT_ROOT

# 127.0.0.1 强制走 Docker 容器（本机 ::1 上可能有另一个 PostgreSQL 服务）
ADMIN_DSN = "postgresql://opspilot:opspilot@127.0.0.1:5432/postgres"
DRILL_DB = "opspilot_drill_test"


def _drop_and_create() -> None:
    with psycopg.connect(ADMIN_DSN, autocommit=True) as conn:
        conn.execute(f"DROP DATABASE IF EXISTS {DRILL_DB} WITH (FORCE)")
        conn.execute(f"CREATE DATABASE {DRILL_DB}")


def _drop() -> None:
    with psycopg.connect(ADMIN_DSN, autocommit=True) as conn:
        conn.execute(f"DROP DATABASE IF EXISTS {DRILL_DB} WITH (FORCE)")


def _drill_dsn() -> str:
    return f"postgresql://opspilot:opspilot@localhost:5432/{DRILL_DB}"


def test_alembic_upgrade_and_downgrade_cycle() -> None:
    _drop_and_create()
    # env.py 优先读 DATABASE_URL 环境变量：演练期间显式指向临时库（asyncpg 驱动）
    drill_asyncpg_url = f"postgresql+asyncpg://opspilot:opspilot@localhost:5432/{DRILL_DB}"
    saved_url = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = drill_asyncpg_url
    try:
        cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
        cfg.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))

        # 空库 → head：D1 + D2 全部表
        command.upgrade(cfg, "head")
        with psycopg.connect(_drill_dsn()) as conn:
            tables = {
                row[0]
                for row in conn.execute(
                    "SELECT tablename FROM pg_tables WHERE schemaname='public'"
                ).fetchall()
            }
            version = conn.execute("SELECT version_num FROM alembic_version").fetchone()
            schemas = {
                row[0]
                for row in conn.execute(
                    "SELECT nspname FROM pg_namespace WHERE nspname='langgraph'"
                ).fetchall()
            }
        for expected in (
            "investigation_runs",
            "run_events",
            "evidence",
            "hypotheses",
            "diagnostic_reports",
            "connector_types",
            "connector_instances",
            "secret_refs",
            "capability_manifests",
            "capability_test_runs",
            "asset_observability_bindings",
            # D4：知识库与 RAG 表族
            "knowledge_documents",
            "knowledge_versions",
            "knowledge_chunks",
            "knowledge_embeddings",
            "retrieval_events",
            "runbooks",
            "runbook_versions",
            # D5：审批与受控执行表族
            "proposals",
            "approvals",
            "action_executions",
            "action_idempotency",
            "verification_results",
            # D6：评估体系表族
            "evaluation_suites",
            "evaluation_cases",
            "evaluation_runs",
            "evaluation_results",
            "incidents",
            "alerts",
        ):
            assert expected in tables, expected
        assert version is not None and version[0] == "0006_d6_evaluation"
        assert schemas == {"langgraph"}  # ADR-5

        # head → base：可完整回滚
        command.downgrade(cfg, "base")
        with psycopg.connect(_drill_dsn()) as conn:
            remaining = {
                row[0]
                for row in conn.execute(
                    "SELECT tablename FROM pg_tables WHERE schemaname='public' "
                    "AND tablename NOT IN ('alembic_version')"
                ).fetchall()
            }
        assert remaining == set()
    finally:
        if saved_url is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = saved_url
        _drop()
