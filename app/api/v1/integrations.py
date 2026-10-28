"""Integrations / Capabilities API（CONTRACTS §5，D3 交付）：

- GET/POST /integrations                       连接器实例视图与创建（Admin；create D3）
- POST /integrations/{id}/test|disable|enable  连通性测试 / 禁用 / 启用（Admin，D3）
- GET /capabilities、GET /capabilities/{id}    能力发现与 Schema（D3；认证用户可见）
- POST /capabilities/{id}/tests                受控能力测试（Admin，记录 capability_test_runs）
- GET/POST /assets/{id}/observability-bindings 资产观测绑定（Admin，D3 实例解析依据）
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Path, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.api.deps import CurrentUser, ReadyUserDep, SessionDep, require_roles
from app.core.errors import not_found, validation_error
from app.core.ids import new_id
from app.core.responses import ok
from app.domains.audit import service as audit_service
from app.domains.integrations import service as integrations_service
from app.domains.tools.manifests import ALL_CAPABILITY_KEYS, BUILTIN_MANIFESTS, manifest_payload
from app.domains.tools.registry import PLATFORM_VERSION
from app.models import AssetObservabilityBinding, ConnectorInstance, ConnectorType

router = APIRouter(tags=["integrations"])

AdminDep = Annotated[CurrentUser, Depends(require_roles("admin"))]


# ---- 请求模型 ----


class IntegrationCreateRequest(BaseModel):
    """创建连接器实例（连接器代码仅允许内置已注册类型；不支持脚本上传）。"""

    model_config = {"extra": "forbid"}

    connector_type: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=100)
    config: dict[str, Any] = Field(default_factory=dict)


class BindingUpsertRequest(BaseModel):
    """资产观测绑定（UNIQUE(asset_id, binding_kind)，重复即更新）。"""

    model_config = {"extra": "forbid"}

    instance_id: str = Field(min_length=1, max_length=64)
    binding_kind: str = Field(pattern=r"^(metrics|logs|traces|health)$")
    config: dict[str, Any] = Field(default_factory=dict)


class CapabilityTestRequest(BaseModel):
    """受控能力测试（可选指定测试资产）。"""

    model_config = {"extra": "forbid"}

    asset_id: str | None = Field(default=None, max_length=64)


# ---- Integrations（connector_instances 视图）----


@router.get("/integrations")
async def list_integrations(session: SessionDep, _admin: AdminDep) -> Any:
    data = await integrations_service.list_types_with_instances(session)
    data["tests"] = await integrations_service.latest_test_results(session)
    return ok(data)


@router.post("/integrations", status_code=status.HTTP_201_CREATED)
async def create_integration(
    session: SessionDep, _admin: AdminDep, payload: IntegrationCreateRequest
) -> Any:
    """创建连接器实例；connector_type 必须为内置已注册类型（代码审查过的连接器）。"""
    if payload.connector_type not in BUILTIN_MANIFESTS:
        raise validation_error(
            f"unknown connector_type '{payload.connector_type}'",
            details={"supported": sorted(BUILTIN_MANIFESTS)},
        )
    if any(k in str(payload.config).lower() for k in ("password", "secret", "token", "api_key")):
        # 密钥只允许 secret_ref 引用（config 存环境变量名，不存密文）
        raise validation_error(
            "config must not contain secret values; reference secret env names instead"
        )
    ctype = (
        await session.execute(
            select(ConnectorType).where(ConnectorType.code == payload.connector_type)
        )
    ).scalar_one_or_none()
    if ctype is None:
        raise not_found("connector type not registered")
    instance = ConnectorInstance(
        id=new_id("integ_"),
        connector_type_id=ctype.id,
        name=payload.name,
        config=payload.config,
        secret_ref_ids=[],
        status="enabled",
    )
    session.add(instance)
    await audit_service.record(
        session,
        actor_type="user",
        actor_id=_admin.user.id,
        action="integration.create",
        resource_type="connector_instance",
        resource_id=instance.id,
        detail={"connector_type": payload.connector_type, "name": payload.name},
        commit=True,
    )
    return ok(
        {
            "id": instance.id,
            "name": instance.name,
            "connector_type": payload.connector_type,
            "status": instance.status,
        }
    )


@router.post("/integrations/{instance_id}/test")
async def test_integration(
    session: SessionDep, admin: AdminDep, instance_id: Annotated[str, Path(max_length=64)]
) -> Any:
    result = await integrations_service.test_instance(
        session, admin_user_id=admin.user.id, instance_id=instance_id
    )
    if result is None:
        raise not_found()
    return ok(result)


@router.post("/integrations/{instance_id}/disable")
async def disable_integration(
    session: SessionDep, admin: AdminDep, instance_id: Annotated[str, Path(max_length=64)]
) -> Any:
    result = await integrations_service.set_instance_status(
        session, admin_user_id=admin.user.id, instance_id=instance_id, enabled=False
    )
    if result is None:
        raise not_found()
    return ok(result)


@router.post("/integrations/{instance_id}/enable")
async def enable_integration(
    session: SessionDep, admin: AdminDep, instance_id: Annotated[str, Path(max_length=64)]
) -> Any:
    result = await integrations_service.set_instance_status(
        session, admin_user_id=admin.user.id, instance_id=instance_id, enabled=True
    )
    if result is None:
        raise not_found()
    return ok(result)


# ---- Capabilities（能力发现）----


def _capability_payload(key: str) -> dict[str, Any] | None:
    spec = ALL_CAPABILITY_KEYS.get(key)
    if spec is None:
        return None
    manifest = next(m for m in BUILTIN_MANIFESTS.values() if spec in m.capabilities)
    payload = manifest_payload(manifest)
    cap = next(c for c in payload["capabilities"] if c["capability_key"] == key)
    result: dict[str, Any] = dict(cap)
    result["scopes"] = payload["scopes"]
    result["rate_limit"] = payload["rate_limit"]
    result["timeout_ms"] = payload["timeout_ms"]
    result["enabled"] = True
    result["compatibility"] = {"min_platform": "0.1.0", "platform_version": PLATFORM_VERSION}
    return result


@router.get("/capabilities")
async def list_capabilities(_user: ReadyUserDep) -> Any:
    """能力发现：全部内置 capability（含 Schema/范围/兼容性，CONTRACTS §2.8）。"""
    items = []
    for key in ALL_CAPABILITY_KEYS:
        cap = _capability_payload(key)
        if cap:
            items.append(cap)
    return ok({"capabilities": items, "manifest_version": "1"})


@router.get("/capabilities/{capability_key}")
async def get_capability(
    _user: ReadyUserDep, capability_key: Annotated[str, Path(max_length=64)]
) -> Any:
    cap = _capability_payload(capability_key)
    if cap is None:
        raise not_found()
    return ok(cap)


@router.post("/capabilities/{capability_key}/tests")
async def run_capability_test(
    session: SessionDep,
    admin: AdminDep,
    capability_key: Annotated[str, Path(max_length=64)],
    payload: CapabilityTestRequest | None = None,
) -> Any:
    """受控能力测试：对指定类型实例执行只读探针并记录 capability_test_runs。

    连接器目标级测试在 /integrations/{id}/test；本端点面向 capability 维度，
    记录主体为 capability_test_runs（触发者 = Admin）。
    """
    if capability_key not in ALL_CAPABILITY_KEYS:
        raise not_found()
    spec = ALL_CAPABILITY_KEYS[capability_key]
    instance = (
        (
            await session.execute(
                select(ConnectorInstance)
                .join(ConnectorType, ConnectorInstance.connector_type_id == ConnectorType.id)
                .where(ConnectorType.code == spec.connector_code)
                .where(ConnectorInstance.status == "enabled")
            )
        )
        .scalars()
        .first()
    )
    if instance is None:
        raise validation_error(
            f"no enabled instance for connector '{spec.connector_code}'",
            details={"capability": capability_key},
        )
    result = await integrations_service.test_instance(
        session, admin_user_id=admin.user.id, instance_id=instance.id
    )
    assert result is not None
    result["capability"] = capability_key
    return ok(result)


# ---- 资产观测绑定（实例解析依据）----


@router.get("/assets/{asset_id}/observability-bindings")
async def list_bindings(
    session: SessionDep, _admin: AdminDep, asset_id: Annotated[str, Path(max_length=64)]
) -> Any:
    rows = (
        (
            await session.execute(
                select(AssetObservabilityBinding).where(
                    AssetObservabilityBinding.asset_id == asset_id
                )
            )
        )
        .scalars()
        .all()
    )
    return ok(
        {
            "items": [
                {
                    "id": b.id,
                    "asset_id": b.asset_id,
                    "instance_id": b.instance_id,
                    "binding_kind": b.binding_kind,
                    "config": b.config,
                }
                for b in rows
            ]
        }
    )


@router.put("/assets/{asset_id}/observability-bindings")
async def upsert_binding(
    session: SessionDep,
    admin: AdminDep,
    asset_id: Annotated[str, Path(max_length=64)],
    payload: BindingUpsertRequest,
) -> Any:
    """绑定/更新资产观测实例（UNIQUE(asset_id, binding_kind)；upsert 语义）。"""
    instance = await session.get(ConnectorInstance, payload.instance_id)
    if instance is None:
        raise not_found("connector instance not found")
    stmt = (
        pg_insert(AssetObservabilityBinding)
        .values(
            id=new_id("bind_"),
            asset_id=asset_id,
            instance_id=payload.instance_id,
            binding_kind=payload.binding_kind,
            config=payload.config,
        )
        .on_conflict_do_update(
            index_elements=["asset_id", "binding_kind"],
            set_={
                "instance_id": payload.instance_id,
                "config": payload.config,
            },
        )
    )
    await session.execute(stmt)
    await audit_service.record(
        session,
        actor_type="user",
        actor_id=admin.user.id,
        action="asset.binding.upsert",
        resource_type="asset",
        resource_id=asset_id,
        detail={
            "instance_id": payload.instance_id,
            "binding_kind": payload.binding_kind,
        },
        commit=True,
    )
    return ok(
        {
            "asset_id": asset_id,
            "instance_id": payload.instance_id,
            "binding_kind": payload.binding_kind,
        }
    )


@router.delete("/assets/{asset_id}/observability-bindings/{binding_kind}")
async def delete_binding(
    session: SessionDep,
    admin: AdminDep,
    asset_id: Annotated[str, Path(max_length=64)],
    binding_kind: Annotated[str, Path(pattern=r"^(metrics|logs|traces|health)$")],
) -> Any:
    row = (
        await session.execute(
            select(AssetObservabilityBinding).where(
                AssetObservabilityBinding.asset_id == asset_id,
                AssetObservabilityBinding.binding_kind == binding_kind,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise not_found()
    await session.delete(row)
    await audit_service.record(
        session,
        actor_type="user",
        actor_id=admin.user.id,
        action="asset.binding.delete",
        resource_type="asset",
        resource_id=asset_id,
        detail={"binding_kind": binding_kind},
        commit=True,
    )
    return ok({"deleted": True})
