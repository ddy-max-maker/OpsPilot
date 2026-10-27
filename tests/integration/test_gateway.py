"""Tool Gateway 集成测试（CONTRACTS §6 / IMPLEMENTATION_PLAN §2.5 Gateway 用例）。

覆盖：参数越权拒绝、目标不在白名单、manifest 范围、限流、预算、
失败映射为 Evidence（不伪空）、run_events 轨迹、写操作审计红线。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select

from app.core.config import Settings
from app.core.enums import ActorType, EvidenceSourceType
from app.core.ids import new_id
from app.domains.tools.context import RunBudget, RunEventWriter, RunToolContext, ToolBudgetExhausted
from app.domains.tools.gateway import ToolGateway
from app.domains.tools.manifests import ALL_CAPABILITY_KEYS
from app.models import Asset, AuditEvent, Evidence, InvestigationRun, RunEvent


@pytest.fixture
def settings() -> Settings:
    return Settings()


async def _never_cancel() -> bool:
    return False


@pytest.fixture
async def seeded_assets(db_session, db_engine, clean_db):
    """三资产：sandbox service / sandbox cache / production service（幂等插入）。"""
    from sqlalchemy import select
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    from tests.conftest import truncate_all

    await truncate_all(db_engine)

    rows = [
        {"name": "gw-svc", "environment": "sandbox", "asset_type": "service"},
        {"name": "gw-cache", "environment": "sandbox", "asset_type": "cache"},
        {"name": "gw-prod", "environment": "production", "asset_type": "service"},
    ]
    for row in rows:
        stmt = (
            pg_insert(Asset)
            .values(
                **row,
                id=f"as_gw_{row['name'].replace('-', '_')}",
                status="active",
                criticality="medium",
                endpoints={},
                owner=None,
                tags=[],
            )
            .on_conflict_do_nothing(index_elements=["name", "environment"])
        )
        await db_session.execute(stmt)
    await db_session.commit()

    assets: dict[str, Asset] = {}
    for row in rows:
        asset = (
            await db_session.execute(
                select(Asset).where(
                    Asset.name == row["name"], Asset.environment == row["environment"]
                )
            )
        ).scalar_one()
        assets[row["name"]] = asset
    return assets


@pytest.fixture
async def gw_setup(db_session_factory, seeded_assets):
    """创建 run（真实 FK 链：incident + run）→ 返回 (ctx, events, run_id)。"""
    from app.models import Incident

    target = seeded_assets["gw-cache"]
    async with db_session_factory() as session:
        from app.models import User

        user_id = "usr_gw_probe"
        exists = await session.get(User, user_id)
        if exists is None:
            from app.core.security import hash_password

            session.add(
                User(
                    id=user_id,
                    username="gw_probe",
                    email="gw_probe@test.local",
                    password_hash=hash_password("GwProbe123!"),
                    is_active=True,
                    must_change_password=False,
                )
            )
            await session.flush()
        incident = Incident(
            id=new_id("inc_"),
            title="gateway probe",
            environment="sandbox",
            primary_asset_id=target.id,
            source="manual",
            severity="medium",
            status="investigating",
            created_by=user_id,
        )
        session.add(incident)
        await session.flush()
        run = InvestigationRun(
            id=new_id("run_"),
            incident_id=incident.id,
            status="running",
            triggered_by=user_id,
            model_config_json={"provider": "fake"},
            budgets={},
            usage={},
            authorization_snapshot={"is_admin": True, "all_assets": True},
            langgraph_thread_id=new_id("run_"),
        )
        session.add(run)
        await session.commit()
        run_id = run.id
    return run_id, target.id


def _make_ctx(run_id: str, *, max_tool_calls: int = 10) -> RunToolContext:
    return RunToolContext(
        run_id=run_id,
        environment="sandbox",
        primary_asset_id="",
        is_admin=True,
        authorized_asset_ids=frozenset(),
        authorized_environments=frozenset({"sandbox"}),
        budget=RunBudget(
            max_tool_calls=max_tool_calls,
            max_iterations=100,
            max_tokens=100000,
            deadline_at=datetime.now(UTC) + timedelta(seconds=300),
        ),
        cancel_checker=_never_cancel,
        connector_configs={},
        all_assets=True,
    )


@pytest.fixture
async def events(db_session_factory, gw_setup) -> RunEventWriter:
    run_id, _ = gw_setup
    return RunEventWriter(db_session_factory, run_id)


@pytest.fixture
async def gateway(db_session_factory, settings) -> ToolGateway:
    return ToolGateway(db_session_factory, settings)


async def _evidence_count(db_session_factory) -> int:
    async with db_session_factory() as session:
        return int(await session.scalar(select(func.count()).select_from(Evidence)))


async def test_unknown_asset_rejected_no_evidence(
    gateway, events, gw_setup, db_session_factory
) -> None:
    run_id, _ = gw_setup
    ctx = _make_ctx(run_id)
    spec = ALL_CAPABILITY_KEYS["get_service_health"]
    out = await gateway.execute_capability(ctx, events, spec.key, spec, {"asset_id": "as_missing"})
    assert out["error"] == "ASSET_NOT_FOUND"
    assert await _evidence_count(db_session_factory) == 0


async def test_unauthorized_asset_denied(
    gateway, events, gw_setup, seeded_assets, db_session_factory
) -> None:
    """非 admin 且资产不在快照内 → 拒绝（默认拒绝语义）；拒绝写审计。"""
    run_id, _ = gw_setup
    target = seeded_assets["gw-svc"]
    ctx = _make_ctx(run_id)
    ctx.is_admin = False
    ctx.all_assets = False
    ctx.authorized_asset_ids = frozenset({seeded_assets["gw-cache"].id})
    ctx.authorized_environments = frozenset({"staging"})
    spec = ALL_CAPABILITY_KEYS["check_redis_health"]
    out = await gateway.execute_capability(ctx, events, spec.key, spec, {"asset_id": target.id})
    assert out["error"] == "ASSET_NOT_AUTHORIZED"
    assert await _evidence_count(db_session_factory) == 0
    async with db_session_factory() as session:
        rows = (
            (
                await session.execute(
                    select(AuditEvent).where(
                        AuditEvent.action == "tool.invoke",
                        AuditEvent.resource_id == "check_redis_health",
                    )
                )
            )
            .scalars()
            .all()
        )
    assert rows and rows[-1].detail["outcome"] == "denied"
    assert rows[-1].actor_type == ActorType.AGENT.value


async def test_environment_scope_allows(gateway, events, gw_setup) -> None:
    """环境命中快照（sandbox）即放行；连接器未配置 → unavailable Evidence。"""
    run_id, cache_id = gw_setup
    ctx = _make_ctx(run_id)
    ctx.is_admin = False
    ctx.all_assets = False
    ctx.authorized_environments = frozenset({"sandbox"})
    spec = ALL_CAPABILITY_KEYS["check_redis_health"]
    out = await gateway.execute_capability(ctx, events, spec.key, spec, {"asset_id": cache_id})
    assert out["status"] == "unavailable"
    assert out["evidence_id"].startswith("ev_")


async def test_manifest_asset_type_scope(gateway, events, gw_setup, seeded_assets) -> None:
    """redis manifest 仅允许 cache；service 资产 → 范围拒绝。"""
    run_id, _ = gw_setup
    svc = seeded_assets["gw-svc"]
    ctx = _make_ctx(run_id)
    spec = ALL_CAPABILITY_KEYS["check_redis_health"]
    out = await gateway.execute_capability(ctx, events, spec.key, spec, {"asset_id": svc.id})
    assert out["error"] == "ASSET_TYPE_OUT_OF_MANIFEST_SCOPE"


async def test_manifest_environment_scope_production(
    gateway, events, gw_setup, seeded_assets
) -> None:
    """production 资产不在 redis manifest.environments → 拒绝。"""
    run_id, _ = gw_setup
    prod = seeded_assets["gw-prod"]
    ctx = _make_ctx(run_id)
    spec = ALL_CAPABILITY_KEYS["check_redis_health"]
    out = await gateway.execute_capability(ctx, events, spec.key, spec, {"asset_id": prod.id})
    assert out["error"] in ("ENVIRONMENT_OUT_OF_MANIFEST_SCOPE", "ASSET_TYPE_OUT_OF_MANIFEST_SCOPE")


async def test_invalid_params_rejected(gateway, events, gw_setup) -> None:
    run_id, cache_id = gw_setup
    ctx = _make_ctx(run_id)
    spec = ALL_CAPABILITY_KEYS["get_service_health"]
    out = await gateway.execute_capability(
        ctx, events, spec.key, spec, {"asset_id": cache_id, "evil_param": "x"}
    )
    assert out["error"] == "VALIDATION_ERROR"


async def test_connector_rate_limit(gateway, events, gw_setup) -> None:
    run_id, cache_id = gw_setup
    ctx = _make_ctx(run_id)
    spec = ALL_CAPABILITY_KEYS["check_redis_health"]
    for _ in range(2):
        out = await gateway.execute_capability(ctx, events, spec.key, spec, {"asset_id": cache_id})
        assert "evidence_id" in out
    ctx.rate_state["redis-ro"] = 30  # manifest 上限 30
    out = await gateway.execute_capability(ctx, events, spec.key, spec, {"asset_id": cache_id})
    assert out["error"] == "RATE_LIMITED"


async def test_tool_budget_exhausted(gateway, events, gw_setup) -> None:
    run_id, cache_id = gw_setup
    ctx = _make_ctx(run_id, max_tool_calls=1)
    spec = ALL_CAPABILITY_KEYS["get_service_health"]
    await gateway.execute_capability(ctx, events, spec.key, spec, {"asset_id": cache_id})
    with pytest.raises(ToolBudgetExhausted):
        await gateway.execute_capability(ctx, events, spec.key, spec, {"asset_id": cache_id})


async def test_connector_unavailable_becomes_evidence(
    gateway, events, gw_setup, db_session_factory
) -> None:
    """失败不伪空：连接器未配置 → unavailable Evidence（error_code 落库）。"""
    run_id, cache_id = gw_setup
    ctx = _make_ctx(run_id)
    spec = ALL_CAPABILITY_KEYS["check_redis_health"]
    out = await gateway.execute_capability(ctx, events, spec.key, spec, {"asset_id": cache_id})
    assert out["status"] == "unavailable"
    assert out["summary_redacted"].startswith("redis.health unavailable")
    async with db_session_factory() as session:
        evidence = await session.get(Evidence, out["evidence_id"])
        assert evidence is not None
        assert evidence.source_type == EvidenceSourceType.HEALTH.value
        assert evidence.status == "unavailable"
        assert evidence.error_code == "CONNECTOR_NOT_CONFIGURED"
        assert evidence.run_id == run_id
        assert evidence.schema_version == "1"


async def test_run_events_written_per_call(gateway, events, gw_setup) -> None:
    from app.core.enums import RunEventType

    run_id, cache_id = gw_setup
    ctx = _make_ctx(run_id)
    spec = ALL_CAPABILITY_KEYS["check_redis_health"]
    await gateway.execute_capability(ctx, events, spec.key, spec, {"asset_id": cache_id})
    async with events._session_factory() as session:  # noqa: SLF001 — 测试内检查
        types = (
            (
                await session.execute(
                    select(RunEvent.type).where(RunEvent.run_id == run_id).order_by(RunEvent.seq)
                )
            )
            .scalars()
            .all()
        )
    assert RunEventType.TOOL_CALLED.value in types
    assert RunEventType.TOOL_RETURNED.value in types
    assert RunEventType.EVIDENCE_ADDED.value in types


async def test_no_write_operations_in_audit(
    gateway, events, gw_setup, seeded_assets, db_session_factory
) -> None:
    """断言红线：全部 capability 调用后，Agent 审计中无任何写操作。"""
    run_id, _ = gw_setup
    cache_id = seeded_assets["gw-cache"].id
    ctx = _make_ctx(run_id)
    for key, spec in ALL_CAPABILITY_KEYS.items():
        await gateway.execute_capability(ctx, events, key, spec, {"asset_id": cache_id})
    async with db_session_factory() as session:
        actions = (
            (
                await session.execute(
                    select(AuditEvent.action).where(AuditEvent.actor_type == ActorType.AGENT.value)
                )
            )
            .scalars()
            .all()
        )
    assert set(actions) <= {"tool.invoke"}
