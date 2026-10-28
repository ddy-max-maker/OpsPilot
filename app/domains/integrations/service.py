"""Integrations / Capabilities 领域服务（D3，CONTRACTS §2.8/§4/§5）。

- integrations = connector_instances 的 REST 视图（D0 评审统一命名）；
- 连接器生命周期：创建（Admin，manifest 兼容校验）→ 测试 → 启用/禁用；
- 连通性测试按连接器类型分派（全部只读探针），结果记录 capability_test_runs；
- capability 发现：BUILTIN_MANIFESTS 导出（manifest_version/compatibility）；
- 新连接器仅允许代码审查 + 部署（仓库内置），不支持 API 上传脚本执行。
"""

from __future__ import annotations

import os
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.enums import ActorType
from app.domains.audit import service as audit_service
from app.domains.tools import connectors
from app.domains.tools.manifests import BUILTIN_MANIFESTS, manifest_payload
from app.domains.tools.registry import PLATFORM_VERSION, validate_manifest
from app.models import (
    CapabilityManifest,
    CapabilityTestRun,
    ConnectorInstance,
    ConnectorType,
)

# connector_type.code -> 只读连通性探针是否可执行（无探针目标的类型返回明确失败原因）
_PROBE_NOTES: dict[str, str] = {
    "http-ro": "probe target comes from assets' health_url (per-asset test)",
}


async def list_types_with_instances(db: AsyncSession) -> dict[str, Any]:
    """连接器类型（含 manifest）与全部实例视图。"""
    types = (await db.execute(select(ConnectorType))).scalars().all()
    instances = (await db.execute(select(ConnectorInstance))).scalars().all()
    manifests = {
        m.connector_type_id: m for m in (await db.execute(select(CapabilityManifest))).scalars()
    }
    return {
        "types": [
            {
                "id": t.id,
                "code": t.code,
                "display_name": t.display_name,
                "read_only": t.read_only,
                "manifest": (manifests[t.id].manifest if t.id in manifests else None),
                "manifest_version": (
                    manifests[t.id].manifest_version if t.id in manifests else None
                ),
                "compatibility": (manifests[t.id].compatibility if t.id in manifests else None),
            }
            for t in types
        ],
        "instances": [
            {
                "id": i.id,
                "connector_type": next((t.code for t in types if t.id == i.connector_type_id), "?"),
                "name": i.name,
                "config": i.config,
                "status": i.status,
                "enabled_at": i.enabled_at,
                "disabled_at": i.disabled_at,
            }
            for i in instances
        ],
    }


async def ensure_builtin_types(db: AsyncSession) -> dict[str, str]:
    """内置连接器类型幂等注册（bootstrap 同步；返回 code -> id）。"""
    ids: dict[str, str] = {}
    for code, manifest in BUILTIN_MANIFESTS.items():
        row = (
            await db.execute(select(ConnectorType).where(ConnectorType.code == code))
        ).scalar_one_or_none()
        if row is None:
            from app.core.ids import new_id  # noqa: PLC0415

            row = ConnectorType(
                id=new_id("ct_"), code=code, display_name=manifest.display_name, read_only=True
            )
            db.add(row)
            await db.flush()
        ids[code] = row.id
    await db.commit()
    return ids


async def sync_capability_manifests(db: AsyncSession) -> list[str]:
    """内置 manifest 经兼容校验后落库 capability_manifests（幂等 upsert）。

    返回被拒绝的 connector_id（不兼容 → 拒绝注册，CONTRACTS §4/§9）。
    """
    rejected: list[str] = []
    type_ids = await ensure_builtin_types(db)
    for code, manifest in BUILTIN_MANIFESTS.items():
        payload = manifest_payload(manifest)
        errors = validate_manifest(payload)
        if errors:
            rejected.append(code)
            continue
        existing = (
            await db.execute(
                select(CapabilityManifest).where(
                    CapabilityManifest.connector_type_id == type_ids[code]
                )
            )
        ).scalar_one_or_none()
        payload["compatibility"] = {
            "min_platform": "0.1.0",
            "platform_version": PLATFORM_VERSION,
            "tested_versions": [manifest.version],
        }
        if existing is None:
            from app.core.ids import new_id  # noqa: PLC0415

            db.add(
                CapabilityManifest(
                    id=new_id("cmf_"),
                    connector_type_id=type_ids[code],
                    manifest=payload,
                    manifest_version=str(payload["manifest_version"]),
                    compatibility=payload["compatibility"],
                )
            )
        else:
            existing.manifest = payload
            existing.manifest_version = str(payload["manifest_version"])
            existing.compatibility = payload["compatibility"]
    await db.commit()
    return rejected


async def _probe_instance(instance: ConnectorInstance, code: str) -> dict[str, Any]:
    """按类型执行只读连通性探针；返回 {ok, detail}。"""
    config = dict(instance.config or {})
    timeout = float(str(config.get("timeout_seconds", 5)))
    try:
        if code == "docker-ro":
            base_url = str(config.get("base_url", ""))
            async with httpx.AsyncClient(follow_redirects=False) as client:
                resp = await client.get(base_url.rstrip("/") + "/_ping", timeout=timeout)
            ok = resp.status_code == 200
            return {"ok": ok, "detail": f"docker /_ping -> {resp.status_code}"}
        if code == "redis-ro":
            result = await connectors.redis_health.check_redis_health(
                dsn=str(config.get("dsn", "")),
                timeout_seconds=timeout,
            )
            return {"ok": result.ok, "detail": result.error_code or "PING ok"}
        if code == "http-ro":
            probe_url = str(config.get("probe_url", ""))
            if not probe_url:
                return {"ok": False, "detail": "probe_url not configured"}
            async with httpx.AsyncClient(follow_redirects=False) as client:
                resp = await client.get(probe_url, timeout=timeout)
            ok = resp.status_code < 500
            return {"ok": ok, "detail": f"probe -> {resp.status_code}"}
        if code == "prom-ro":
            base_url = str(config.get("base_url", ""))
            async with httpx.AsyncClient(follow_redirects=False) as client:
                resp = await client.get(
                    base_url.rstrip("/") + "/api/v1/query",
                    params={"query": "up"},
                    timeout=timeout,
                )
            ok = resp.status_code == 200
            return {"ok": ok, "detail": f"prom /api/v1/query -> {resp.status_code}"}
        if code == "otel":
            base_url = str(config.get("base_url", ""))
            async with httpx.AsyncClient(follow_redirects=False) as client:
                resp = await client.get(
                    base_url.rstrip("/") + "/api/services",
                    timeout=timeout,
                )
            ok = resp.status_code == 200
            return {"ok": ok, "detail": f"jaeger /api/services -> {resp.status_code}"}
        if code == "mysql-ro":
            import aiomysql  # noqa: PLC0415

            password_env = str(config.get("password_env", ""))
            password = os.environ.get(password_env, "") if password_env else ""
            if not password:
                return {"ok": False, "detail": "password secret not configured"}
            conn = await aiomysql.connect(
                host=str(config.get("host", "")),
                port=int(str(config.get("port", 3306))),
                user=str(config.get("user", "")),
                password=password,
                connect_timeout=int(timeout),
            )
            cur = await conn.cursor()
            await cur.execute("SELECT 1")
            await cur.fetchall()
            await cur.close()
            conn.close()
            return {"ok": True, "detail": "SELECT 1 ok"}
        return {"ok": False, "detail": f"no probe for connector type '{code}'"}
    except Exception as exc:  # noqa: BLE001 — 探针失败即 failed（不泄漏敏感细节）
        return {"ok": False, "detail": f"{type(exc).__name__}"}


async def test_instance(
    db: AsyncSession, *, admin_user_id: str, instance_id: str
) -> dict[str, Any] | None:
    """连通性测试（POST /integrations/{id}/test）；结果记录 capability_test_runs。"""
    instance = await db.get(ConnectorInstance, instance_id)
    if instance is None:
        return None
    ctype = await db.get(ConnectorType, instance.connector_type_id)
    assert ctype is not None
    manifest_row = (
        await db.execute(
            select(CapabilityManifest).where(
                CapabilityManifest.connector_type_id == instance.connector_type_id
            )
        )
    ).scalar_one_or_none()
    probe = await _probe_instance(instance, ctype.code)
    detail = {
        "instance_id": instance.id,
        "instance_name": instance.name,
        "connector_type": ctype.code,
        "probe": probe["detail"],
    }
    if manifest_row is not None:
        from app.core.ids import new_id  # noqa: PLC0415

        db.add(
            CapabilityTestRun(
                id=new_id("ctrun_"),
                manifest_id=manifest_row.id,
                triggered_by=admin_user_id,
                result="passed" if probe["ok"] else "failed",
                detail=detail,
            )
        )
    await audit_service.record(
        db,
        actor_type=ActorType.USER.value,
        actor_id=admin_user_id,
        action="integration.test",
        resource_type="connector_instance",
        resource_id=instance.id,
        detail={"result": "passed" if probe["ok"] else "failed"},
        commit=True,
    )
    return {"result": "passed" if probe["ok"] else "failed", **detail}


async def set_instance_status(
    db: AsyncSession, *, admin_user_id: str, instance_id: str, enabled: bool
) -> dict[str, Any] | None:
    """启用/禁用连接器实例（POST /integrations/{id}/disable|enable）。"""
    instance = await db.get(ConnectorInstance, instance_id)
    if instance is None:
        return None
    from datetime import UTC, datetime  # noqa: PLC0415

    instance.status = "enabled" if enabled else "disabled"
    instance.enabled_at = datetime.now(UTC) if enabled else instance.enabled_at
    instance.disabled_at = None if enabled else datetime.now(UTC)
    session_obj = instance
    db.add(session_obj)
    await audit_service.record(
        db,
        actor_type=ActorType.USER.value,
        actor_id=admin_user_id,
        action="integration.enable" if enabled else "integration.disable",
        resource_type="connector_instance",
        resource_id=instance.id,
        detail={"status": instance.status},
        commit=True,
    )
    return {
        "id": instance.id,
        "name": instance.name,
        "status": instance.status,
    }


async def latest_test_results(db: AsyncSession, limit: int = 20) -> list[dict[str, Any]]:
    """最近的能力/连通性测试结果（前端展示）。"""
    rows = (
        (
            await db.execute(
                select(CapabilityTestRun).order_by(CapabilityTestRun.created_at.desc()).limit(limit)
            )
        )
        .scalars()
        .all()
    )
    return [
        {
            "id": r.id,
            "manifest_id": r.manifest_id,
            "result": r.result,
            "detail": r.detail,
            "created_at": r.created_at,
        }
        for r in rows
    ]


__all__ = [
    "_PROBE_NOTES",
    "ensure_builtin_types",
    "latest_test_results",
    "list_types_with_instances",
    "set_instance_status",
    "sync_capability_manifests",
    "test_instance",
]
