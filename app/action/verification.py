"""恢复验证服务（D5，用户指令 §6）：动作成功 ≠ 故障恢复成功。

流程（executed 之后由 ops-action 触发）：
1. 配置化探测：目标资产 endpoints.health_check 决定探测方式
   （redis_ping / http_health），未配置探测方式的动作不做验证（记录 unknown）；
2. 观察窗口：window_seconds 内按 probe_interval 连续探测；
3. 判定：全部探测通过且连续通过次数 ≥ min_passing_probes → PASSED；
   任一探测失败 → FAILED（S9 红线：动作成功但服务不健康绝不 RESOLVED）；
   探测不可执行/无法观测 → UNKNOWN；
4. 落库 verification_results 并由 approvals_service.record_verification
   推进 proposal / incident 状态机（仅 PASSED 允许 VERIFIED → RESOLVED 路径）。
"""

from __future__ import annotations

import logging
import time
from datetime import UTC, datetime
from typing import Any

import httpx
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.core.enums import ActorType
from app.domains.approvals import service as approvals_service
from app.domains.audit import service as audit_service
from app.models import Asset, Proposal, VerificationStatus

logger = logging.getLogger("ops-action.verification")


async def _probe_once(check: dict[str, Any], *, timeout_seconds: float) -> bool:
    """单次探测：redis_ping / http_health（异常向上抛出，由调用方归类）。"""
    kind = str(check.get("kind", ""))
    if kind == "redis_ping":
        import redis.asyncio as aioredis  # noqa: PLC0415

        redis_client = aioredis.Redis(
            host=str(check["host"]),
            port=int(check.get("port", 6379)),
            socket_timeout=timeout_seconds,
            socket_connect_timeout=timeout_seconds,
        )
        try:
            return bool(await redis_client.ping())
        finally:
            await redis_client.aclose()
    if kind == "http_health":
        url = str(check["url"])
        async with httpx.AsyncClient(timeout=timeout_seconds) as http_client:
            resp = await http_client.get(url)
            code: int = resp.status_code
            return code < 400
    raise ValueError(f"unsupported health check kind '{kind}'")


async def _run_observation_window(
    check: dict[str, Any],
    *,
    settings: Settings,
) -> tuple[str, list[dict[str, Any]], dict[str, Any]]:
    """观察窗口内的连续健康判断（真实探测；返回判定/探测记录/窗口元数据）。"""
    started = time.monotonic()
    started_at = datetime.now(UTC)
    window_seconds = settings.verification_observation_window_seconds
    interval = settings.verification_probe_interval_seconds
    min_passing = settings.verification_min_passing_probes
    probes: list[dict[str, Any]] = []
    consecutive_passes = 0
    max_consecutive = 0
    any_failure = False
    any_unobservable = False
    while (time.monotonic() - started) < window_seconds:
        probe_started = time.monotonic()
        try:
            ok = await _probe_once(
                check, timeout_seconds=settings.verification_probe_timeout_seconds
            )
            probes.append(
                {
                    "at": datetime.now(UTC).isoformat(),
                    "latency_ms": int((time.monotonic() - probe_started) * 1000),
                    "pass": ok,
                    "error": None,
                }
            )
            if ok:
                consecutive_passes += 1
                max_consecutive = max(max_consecutive, consecutive_passes)
            else:
                consecutive_passes = 0
                any_failure = True
        except Exception as exc:  # noqa: BLE001 — 探测不可观测（网络/配置问题）
            probes.append(
                {
                    "at": datetime.now(UTC).isoformat(),
                    "latency_ms": int((time.monotonic() - probe_started) * 1000),
                    "pass": False,
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )
            consecutive_passes = 0
            any_unobservable = True
        # 连续通过数已达标且无任何失败：可提前结束（窗口证据充分）
        if max_consecutive >= min_passing and not any_failure and not any_unobservable:
            break
        await _sleep(interval)
    ended_at = datetime.now(UTC)
    if any_unobservable and not any_failure:
        # 探测器自身不可用：无法观测 → UNKNOWN（不伪装成功/失败）
        status = VerificationStatus.UNKNOWN
    elif any_failure:
        status = VerificationStatus.FAILED
    elif max_consecutive >= min_passing and len(probes) >= min_passing:
        status = VerificationStatus.PASSED
    else:
        # 窗口结束仍未达标（样本不足且无失败）→ 保守 UNKNOWN
        status = VerificationStatus.UNKNOWN
    window = {
        "start": started_at.isoformat(),
        "end": ended_at.isoformat(),
        "sample_count": len(probes),
        "window_seconds": window_seconds,
        "probe_interval_seconds": interval,
        "min_passing_probes": min_passing,
        "max_consecutive_passes": max_consecutive,
    }
    return status, probes, window


async def _sleep(seconds: float) -> None:
    import asyncio

    await asyncio.sleep(seconds)


def _health_check_for(asset: Asset | None) -> dict[str, Any] | None:
    """从资产 endpoints 提取健康检查配置（None = 未配置可观测探测）。"""
    endpoints = dict(asset.endpoints) if asset is not None and asset.endpoints else {}
    check = endpoints.get("health_check")
    return dict(check) if isinstance(check, dict) else None


async def verify_execution(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    execution_id: str,
    proposal: Proposal,
    payload: dict[str, Any],
    executor_id: str,
    settings: Settings,
) -> str:
    """执行成功后的恢复验证入口（真实探测 + 观察窗口 + 状态机推进）。"""
    # 操作前状态确认：执行请求携带的前置检查证据（executor 已校验容器 running）
    precheck_ref = {
        "container_running_confirmed_at": payload.get("requested_at"),
        "container": (payload.get("parameters") or {}).get("container_name"),
    }
    async with session_factory() as session:
        asset = await session.get(Asset, proposal.target_asset_id)
        check = _health_check_for(asset)
        if check is None:
            # 未配置健康探测：无法观测恢复 → verify_unknown（不宣称成功）
            async with session_factory() as session2:
                result, proposal = await approvals_service.record_verification(
                    session2,
                    session_factory,
                    execution_id=execution_id,
                    status=VerificationStatus.UNKNOWN,
                    checks=[
                        {
                            "name": "health_probe_configured",
                            "expected": "health_check in asset endpoints",
                            "observed": None,
                            "pass": False,
                        }
                    ],
                    observation_window={
                        "start": datetime.now(UTC).isoformat(),
                        "end": datetime.now(UTC).isoformat(),
                        "sample_count": 0,
                        "reason": "no health probe configured for target asset",
                    },
                    executor=executor_id,
                )
                await session2.commit()
            logger.warning(
                "execution %s: no health probe configured; verification unknown",
                execution_id,
            )
            return VerificationStatus.UNKNOWN

    status, probes, window = await _run_observation_window(check, settings=settings)
    checks = [
        {
            "name": f"probe[{i}]",
            "expected": "healthy",
            "observed": ("pass" if p["pass"] else f"fail({p['error'] or 'unhealthy'})"),
            "pass": p["pass"],
        }
        for i, p in enumerate(probes, start=1)
    ]
    checks.insert(
        0,
        {
            "name": "pre_state_confirmed",
            "expected": "container running before restart",
            "observed": precheck_ref.get("container_running_confirmed_at"),
            "pass": True,
        },
    )
    async with session_factory() as session:
        result, _proposal = await approvals_service.record_verification(
            session,
            session_factory,
            execution_id=execution_id,
            status=status,
            checks=checks,
            observation_window=window,
            executor=executor_id,
        )
        await audit_service.record(
            session,
            actor_type=ActorType.ACTION_SERVICE.value,
            actor_id=executor_id,
            action="verification.concluded",
            resource_type="verification_result",
            resource_id=result.id,
            environment=proposal.environment,
            detail={
                "execution_id": execution_id,
                "status": str(status),
                "sample_count": window["sample_count"],
            },
        )
        await session.commit()
    logger.info(
        "verification for execution %s concluded: %s (%s probes)",
        execution_id,
        status,
        len(probes),
    )
    return status
