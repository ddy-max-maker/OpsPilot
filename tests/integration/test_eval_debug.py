"""评估 runner 调试（临时）：暴露案例级错误明细。"""

from __future__ import annotations

import httpx
import pytest

from app.core.config import get_settings
from app.core.db import get_session_factory
from app.domains.evaluations.runner import EvaluationRunner
from tests.conftest import ADMIN_BOOT_PASSWORD, ADMIN_BOOT_USERNAME, api_login, auth_headers

pytestmark = pytest.mark.integration


async def test_debug_runner_case_by_case(client: httpx.AsyncClient) -> None:
    login = await api_login(client, ADMIN_BOOT_USERNAME, ADMIN_BOOT_PASSWORD)
    token = login.json()["data"]["access_token"]
    change = await client.post(
        "/api/v1/auth/change-password",
        json={"old_password": ADMIN_BOOT_PASSWORD, "new_password": "AdminEval123!"},
        headers=auth_headers(token),
    )
    assert change.status_code == 200
    settings = get_settings()
    runner = EvaluationRunner(get_session_factory(), settings)
    from app.domains.evaluations import cases as cases_mod

    async with get_session_factory()() as session:
        stats = await cases_mod.seed_dataset(session)
    print("SEEDED", stats)
    await runner._stubs.start()
    try:
        from sqlalchemy import select

        from app.models import EvaluationCase, EvaluationSuite

        async with get_session_factory()() as session:
            suite = (
                (
                    await session.execute(
                        select(EvaluationSuite).where(EvaluationSuite.name == "fault-core")
                    )
                )
                .scalars()
                .one()
            )
            cases = (
                (
                    await session.execute(
                        select(EvaluationCase)
                        .where(EvaluationCase.suite_id == suite.id)
                        .order_by(EvaluationCase.case_key)
                    )
                )
                .scalars()
                .all()
            )
        from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
        from psycopg_pool import AsyncConnectionPool

        pool = AsyncConnectionPool(
            conninfo=settings.psycopg_database_url, max_size=4, open=False,
            kwargs={"autocommit": True},
        )
        await pool.open()
        checkpointer = AsyncPostgresSaver(pool)
        await checkpointer.setup()
        runner._pool = (pool, checkpointer)

        for case in cases:
            try:
                passed, metrics, detail, incident_id = await runner._exec_case(case)
                print(
                    f"CASE {case.case_key}: passed={passed} metrics_keys={list(metrics)[:6]} "
                    f"detail={str(detail)[:900]}"
                )
            except Exception as exc:  # noqa: BLE001
                import traceback

                print(f"CASE {case.case_key}: CRASHED {type(exc).__name__}: {exc}")
                traceback.print_exc()
                break
    finally:
        await runner._stubs.stop()
        if runner._pool is not None:
            await runner._pool[0].close()
