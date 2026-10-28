"""D3 集成测试：Integrations/Capabilities API + 多实例隔离 + 受控查询语义。

覆盖（用户验收项）：
- Capability Registry 启动同步（capability_manifests 落库）与能力发现 API；
- Integrations 生命周期：创建/测试/禁用/启用（Admin RBAC）；
- 同类型多连接器实例互不串用（绑定解析 vs 未绑定歧义拒绝）；
- 指标模板白名单（不可查询其他服务的指标）；
- 无权限资产无法通过指标/日志/Trace 查询绕过权限（授权快照拒绝）。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.core.config import Settings
from app.core.ids import new_id
from app.domains.tools.context import RunBudget, RunEventWriter, RunToolContext
from app.domains.tools.gateway import ToolGateway
from app.domains.tools.manifests import ALL_CAPABILITY_KEYS
from app.domains.tools.registry import load_resolution_map
from app.models import (
    Asset,
    AssetObservabilityBinding,
    CapabilityManifest,
    ConnectorInstance,
    ConnectorType,
    Evidence,
    Incident,
    InvestigationRun,
    User,
)
from tests.conftest import truncate_all

pytestmark = pytest.mark.integration


@pytest.fixture
async def seeded_instances(db_session, db_engine, clean_db):
    """两个同类型 prom-ro 实例（不同模板）+ mysql/docker 实例 + 两个资产。"""
    await truncate_all(db_engine)
    from app.core.security import hash_password

    db_session.add(
        User(
            id="usr_d3_probe",
            username="d3_probe",
            email="d3@test.local",
            password_hash=hash_password("D3Probe123!"),
            is_active=True,
            must_change_password=False,
        )
    )
    db_session.add_all(
        [
            ConnectorType(id="ct_prom", code="prom-ro", display_name="Prom", read_only=True),
            ConnectorType(id="ct_mysql", code="mysql-ro", display_name="MySQL", read_only=True),
            ConnectorType(id="ct_docker", code="docker-ro", display_name="Docker", read_only=True),
        ]
    )
    await db_session.flush()
    # 同类型两个实例：模板集合不同（隔离验证关键）
    db_session.add_all(
        [
            ConnectorInstance(
                id="integ_prom_a",
                connector_type_id="ct_prom",
                name="prom-a",
                config={
                    "base_url": "http://prom-a:9090",
                    "allowed_templates": {"up": 'up{service="{service}"}'},
                },
                status="enabled",
            ),
            ConnectorInstance(
                id="integ_prom_b",
                connector_type_id="ct_prom",
                name="prom-b",
                config={
                    "base_url": "http://prom-b:9090",
                    "allowed_templates": {"other_metric": 'other{service="{service}"}'},
                },
                status="enabled",
            ),
            ConnectorInstance(
                id="integ_mysql_a",
                connector_type_id="ct_mysql",
                name="mysql-a",
                config={
                    "host": "mysql-a",
                    "port": 3306,
                    "user": "ro",
                    "password_env": "NO_SUCH_ENV_D3_TEST",
                },
                status="enabled",
            ),
            ConnectorInstance(
                id="integ_docker_a",
                connector_type_id="ct_docker",
                name="docker-a",
                config={"base_url": "http://roproxy:8800", "container_prefix": "p-"},
                status="enabled",
            ),
        ]
    )
    db_session.add_all(
        [
            Asset(
                id="as_d3_svc",
                name="svc-a",
                environment="sandbox",
                asset_type="service",
                status="active",
                criticality="medium",
                endpoints={},
                owner=None,
                tags=[],
            ),
            Asset(
                id="as_d3_db",
                name="db-a",
                environment="sandbox",
                asset_type="database",
                status="active",
                criticality="medium",
                endpoints={},
                owner=None,
                tags=[],
            ),
        ]
    )
    await db_session.commit()


@pytest.fixture
async def run_id_factory(db_session_factory):
    async def _make(asset_id: str) -> str:
        async with db_session_factory() as session:
            incident = Incident(
                id=new_id("inc_"),
                title="d3 probe",
                environment="sandbox",
                primary_asset_id=asset_id,
                source="manual",
                severity="medium",
                status="investigating",
                created_by="usr_d3_probe",
            )
            session.add(incident)
            await session.flush()
            rid = new_id("run_")
            session.add(
                InvestigationRun(
                    id=rid,
                    incident_id=incident.id,
                    status="running",
                    triggered_by="usr_d3_probe",
                    model_config_json={"provider": "fake"},
                    budgets={},
                    usage={},
                    authorization_snapshot={"is_admin": True, "all_assets": True},
                    langgraph_thread_id=rid,
                )
            )
            await session.commit()
        return rid

    return _make


def _ctx(run_id: str, instance_map) -> RunToolContext:
    return RunToolContext(
        run_id=run_id,
        environment="sandbox",
        primary_asset_id="",
        is_admin=True,
        authorized_asset_ids=frozenset(),
        authorized_environments=frozenset({"sandbox"}),
        budget=RunBudget(
            max_tool_calls=10,
            max_iterations=100,
            max_tokens=100000,
            deadline_at=datetime.now(UTC) + timedelta(seconds=300),
        ),
        cancel_checker=_never_cancel,
        connector_configs={},
        all_assets=True,
        instance_map=instance_map,
    )


async def _never_cancel() -> bool:
    return False


@pytest.fixture
async def gw(db_session_factory, worker_settings):
    return ToolGateway(db_session_factory, worker_settings)


@pytest.fixture
def worker_settings() -> Settings:
    return Settings()


# ---- Registry 同步与能力发现 API ----


async def test_registry_synced_at_startup(client, db_session_factory) -> None:
    """lifespan 同步内置 manifest → capability_manifests 落库且兼容字段齐备。"""
    async with db_session_factory() as session:
        manifests = (await session.execute(select(CapabilityManifest))).scalars().all()
    assert len(manifests) == 6  # redis/docker/http/prom/otel/mysql
    for m in manifests:
        assert m.manifest["read_only"] is True
        assert m.manifest["compatibility"]["platform_version"]


async def test_capabilities_discovery_api(client, ready_admin) -> None:
    """GET /capabilities 可发现全部能力（验收标准：能力发现 API）。"""
    resp = await client.get("/api/v1/capabilities", headers=ready_admin)
    assert resp.status_code == 200
    keys = {c["capability_key"] for c in resp.json()["data"]["capabilities"]}
    assert {
        "get_service_health",
        "check_redis_health",
        "get_container_status",
        "read_container_logs",
        "query_metrics",
        "query_traces",
        "read_mysql_diagnostics",
    } <= keys
    single = await client.get("/api/v1/capabilities/query_metrics", headers=ready_admin)
    assert single.status_code == 200
    assert single.json()["data"]["read_only"] is True


async def test_integrations_admin_only(client, ready_admin, viewer_headers) -> None:
    resp = await client.get("/api/v1/integrations", headers=ready_admin)
    assert resp.status_code == 200
    viewer = await client.get("/api/v1/integrations", headers=viewer_headers)
    assert viewer.status_code == 403


async def test_integration_lifecycle_create_test_disable_enable(
    client, ready_admin, db_session_factory
) -> None:
    headers = ready_admin
    created = await client.post(
        "/api/v1/integrations",
        headers=headers,
        json={
            "connector_type": "prom-ro",
            "name": "prom-extra",
            "config": {"base_url": "http://prom-extra:9090"},
        },
    )
    assert created.status_code == 201
    iid = created.json()["data"]["id"]

    tested = await client.post(f"/api/v1/integrations/{iid}/test", headers=headers)
    assert tested.status_code == 200
    # prom-extra 无真实服务 → 探针 failed（不伪造成功）
    assert tested.json()["data"]["result"] == "failed"

    disabled = await client.post(f"/api/v1/integrations/{iid}/disable", headers=headers)
    assert disabled.status_code == 200
    async with db_session_factory() as session:
        inst = await session.get(ConnectorInstance, iid)
        assert inst is not None and inst.status == "disabled"

    enabled = await client.post(f"/api/v1/integrations/{iid}/enable", headers=headers)
    assert enabled.status_code == 200
    assert enabled.json()["data"]["status"] == "enabled"


async def test_create_integration_rejects_unknown_type(client, ready_admin) -> None:
    resp = await client.post(
        "/api/v1/integrations",
        headers=ready_admin,
        json={"connector_type": "evil-script", "name": "x", "config": {}},
    )
    assert resp.status_code == 400


async def test_create_integration_rejects_inline_secret(client, ready_admin) -> None:
    resp = await client.post(
        "/api/v1/integrations",
        headers=ready_admin,
        json={
            "connector_type": "mysql-ro",
            "name": "leaky",
            "config": {"host": "h", "password": "super-secret"},
        },
    )
    assert resp.status_code == 400


# ---- 多实例隔离与受控查询 ----


async def test_same_type_instances_isolated_by_binding(
    gw, db_session_factory, seeded_instances, run_id_factory
) -> None:
    """同类型两个 prom 实例：绑定 prom-a 后只用 prom-a 的模板（不串用 prom-b）。"""
    async with db_session_factory() as session:
        session.add(
            AssetObservabilityBinding(
                id="bind_a",
                asset_id="as_d3_svc",
                instance_id="integ_prom_a",
                binding_kind="metrics",
                config={},
            )
        )
        await session.commit()
    run_id = await run_id_factory("as_d3_svc")
    instance_map = await load_resolution_map(db_session_factory)
    ctx = _ctx(run_id, instance_map)
    events = await RunEventWriter.create(db_session_factory, run_id)
    spec = ALL_CAPABILITY_KEYS["query_metrics"]

    # prom-a 的模板 → 通过解析（连接器级失败：无真实 prometheus 服务，不伪造成功）
    out_a = await gw.execute_capability(
        ctx, events, spec.key, spec, {"asset_id": "as_d3_svc", "template": "up"}
    )
    assert "evidence_id" in out_a
    # prom-b 独有的模板 → 模板白名单拒绝（证明没有串用 prom-b 配置）；
    # D3 语义：拒绝映射为 unavailable Evidence（error_code 落库，不伪空）
    out_b = await gw.execute_capability(
        ctx, events, spec.key, spec, {"asset_id": "as_d3_svc", "template": "other_metric"}
    )
    assert out_b["status"] == "unavailable"
    assert "evidence_id" in out_b
    async with db_session_factory() as session:
        row_b = await session.get(Evidence, out_b["evidence_id"])
        assert row_b is not None
        assert row_b.error_code == "METRIC_TEMPLATE_NOT_ALLOWED"
        # 证据记录使用的实例（correlation）必须是绑定的 prom-a
        row_a = await session.get(Evidence, out_a["evidence_id"])
        assert row_a is not None
        assert (row_a.correlation or {}).get("instance_id") == "integ_prom_a"


async def test_multiple_instances_without_binding_rejected(
    gw, db_session_factory, seeded_instances, run_id_factory
) -> None:
    """同类型多实例且未绑定资产 → INSTANCE_AMBIGUOUS（不可猜测凭据）。"""
    run_id = await run_id_factory("as_d3_svc")
    instance_map = await load_resolution_map(db_session_factory)
    ctx = _ctx(run_id, instance_map)
    events = await RunEventWriter.create(db_session_factory, run_id)
    spec = ALL_CAPABILITY_KEYS["query_metrics"]
    out = await gw.execute_capability(
        ctx, events, spec.key, spec, {"asset_id": "as_d3_svc", "template": "up"}
    )
    # D3 语义：多实例歧义拒绝 → unavailable Evidence（error_code 落库，不伪空）
    assert out["status"] == "unavailable"
    assert "evidence_id" in out
    async with db_session_factory() as session:
        ev = (
            (await session.execute(select(Evidence).where(Evidence.run_id == run_id)))
            .scalars()
            .all()
        )
        assert len(ev) == 1 and ev[0].error_code == "INSTANCE_AMBIGUOUS"


async def test_mysql_password_secret_not_configured(
    gw, db_session_factory, seeded_instances, run_id_factory
) -> None:
    """MySQL 密码未配置（环境变量缺失）→ SECRET_NOT_CONFIGURED（不泄漏凭据细节）。"""
    async with db_session_factory() as session:
        session.add(
            AssetObservabilityBinding(
                id="bind_db",
                asset_id="as_d3_db",
                instance_id="integ_mysql_a",
                binding_kind="health",
                config={},
            )
        )
        await session.commit()
    run_id = await run_id_factory("as_d3_db")
    instance_map = await load_resolution_map(db_session_factory)
    ctx = _ctx(run_id, instance_map)
    events = await RunEventWriter.create(db_session_factory, run_id)
    spec = ALL_CAPABILITY_KEYS["read_mysql_diagnostics"]
    out = await gw.execute_capability(ctx, events, spec.key, spec, {"asset_id": "as_d3_db"})
    assert out["status"] == "unavailable"
    async with db_session_factory() as session:
        ev = (
            (await session.execute(select(Evidence).where(Evidence.run_id == run_id)))
            .scalars()
            .first()
        )
        assert ev is not None and ev.error_code == "SECRET_NOT_CONFIGURED"
        # 真实密码值不得出现在证据载荷（reason 文案含 "password" 字样不是泄漏）
        assert "OpsPilotRo" not in str(ev.data_ref)
        assert "OpsPilotRo" not in ev.summary_redacted


# ---- 权限：无权限资产不能经观测查询绕过 ----


async def test_unauthorized_asset_blocked_on_observability_queries(
    gw, db_session_factory, seeded_instances, run_id_factory
) -> None:
    """非授权资产 → 指标/链路/MySQL 查询同样被授权快照拒绝（无绕过路径）。"""
    async with db_session_factory() as session:
        session.add(
            AssetObservabilityBinding(
                id="bind_a",
                asset_id="as_d3_svc",
                instance_id="integ_prom_a",
                binding_kind="metrics",
                config={},
            )
        )
        await session.commit()
    run_id = await run_id_factory("as_d3_svc")
    instance_map = await load_resolution_map(db_session_factory)
    ctx = _ctx(run_id, instance_map)
    ctx.is_admin = False
    ctx.all_assets = False
    ctx.authorized_asset_ids = frozenset()  # 未授权任何资产
    ctx.authorized_environments = frozenset()  # 环境维度也未授权（默认拒绝）
    events = await RunEventWriter.create(db_session_factory, run_id)
    for key, params in (
        ("query_metrics", {"asset_id": "as_d3_svc", "template": "up"}),
        ("query_traces", {"asset_id": "as_d3_svc"}),
        ("read_mysql_diagnostics", {"asset_id": "as_d3_svc"}),
    ):
        spec = ALL_CAPABILITY_KEYS[key]
        out = await gw.execute_capability(ctx, events, spec.key, spec, params)
        assert out["error"] == "ASSET_NOT_AUTHORIZED", key
