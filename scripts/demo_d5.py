"""D5 真实恢复演示脚本（sandbox 演示容器，绝不动 OpsPilot 自身组件）。

用法：
  uv run python scripts/demo_d5.py prepare      # 建审批人/资产/事件/Runbook（幂等）
  uv run python scripts/demo_d5.py s1           # S1：诊断→提案→他人审批→受控重启→验证 PASSED
  uv run python scripts/demo_d5.py s9           # S9：动作成功但业务不健康 → VERIFY_FAILED
  uv run python scripts/demo_d5.py denials      # 拒绝矩阵（production/四眼/未审批/篡改）
  uv run python scripts/demo_d5.py crash        # ops-action 停止时执行入队（消息滞留）
  uv run python scripts/demo_d5.py crash-resume PRP_ID  # 重启后验证滞留消息恢复执行

前置：docker compose up -d（api/worker/ops-action/rabbitmq/postgres），
API 在 http://localhost:8000，admin 凭据在 .env。
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
from datetime import UTC, datetime, timedelta

import httpx
import psycopg

API = os.environ.get("DEMO_API", "http://localhost:8000") + "/api/v1"
DSN = "postgresql://opspilot:opspilot@127.0.0.1:5432/opspilot"
ADMIN_USER = os.environ.get("BOOTSTRAP_ADMIN_USERNAME", "admin")
ADMIN_PASS = os.environ.get("BOOTSTRAP_ADMIN_PASSWORD", "BootstrapAdmin_2026!")
# 改密后的稳定演示密码（prepare 幂等：第二次起直接登录）
ADMIN_READY_PASS = "DemoAdmin2026!Ready"
APPROVER_PASS = "DemoApprover2026!"

ASSET_NAME = "demo-redis"
ASSET_ID_CACHE: str | None = None
INCIDENT_ID = "inc_demo_d5"


def p(msg: str) -> None:
    print(f"[demo] {msg}", flush=True)


def db_exec(sql: str, params: dict | None = None) -> None:
    with psycopg.connect(DSN) as conn:
        conn.execute(sql, params or {})
        conn.commit()


async def asset_id(client: httpx.AsyncClient, admin: dict) -> str:
    """按名称解析演示资产 id（prepare 创建后跨脚本复用）。"""
    global ASSET_ID_CACHE
    if ASSET_ID_CACHE:
        return ASSET_ID_CACHE
    r = await client.get(f"{API}/assets?limit=200", headers=admin)
    data = r.json()["data"]
    for a in data.get("assets") or data.get("items") or []:
        if a["name"] == ASSET_NAME:
            ASSET_ID_CACHE = a["id"]
            return ASSET_ID_CACHE
    raise SystemExit("demo asset not found; run prepare first")


async def _login_user(
    client: httpx.AsyncClient, username: str, passwords: list[str], ready_pass: str
) -> dict[str, str]:
    """登录（多密码尝试，覆盖"首登密码已改密"状态）；必要时自动完成首登改密。"""
    token, login_pwd = None, None
    for pwd in passwords:
        r = await client.post(f"{API}/auth/login", json={"username": username, "password": pwd})
        if r.status_code == 200:
            token, login_pwd = r.json()["data"]["access_token"], pwd
            break
    assert token, f"login failed for {username}"
    headers = {"Authorization": f"Bearer {token}"}
    probe = await client.get(f"{API}/assets?limit=1", headers=headers)
    if probe.status_code == 403 and "PASSWORD_CHANGE_REQUIRED" in probe.text:
        ch = await client.post(
            f"{API}/auth/change-password",
            json={"old_password": login_pwd, "new_password": ready_pass},
            headers=headers,
        )
        assert ch.status_code == 200, ch.text
        r = await client.post(
            f"{API}/auth/login", json={"username": username, "password": ready_pass}
        )
        assert r.status_code == 200, r.text
        token = r.json()["data"]["access_token"]
    return {"Authorization": f"Bearer {token}"}


async def admin_headers(client: httpx.AsyncClient) -> dict[str, str]:
    return await _login_user(client, ADMIN_USER, [ADMIN_PASS, ADMIN_READY_PASS], ADMIN_READY_PASS)


async def approver_headers(client: httpx.AsyncClient) -> dict[str, str]:
    return await _login_user(
        client, "demo_approver", [APPROVER_PASS, "DemoApprover2026!Ready"], "DemoApprover2026!Ready"
    )


async def prepare() -> None:
    """幂等播种：审批人用户、资产（容器绑定 + 真实 redis 探测）、事件、已发布 Runbook。"""
    # DB 直建审批人（四眼第二人；approver 角色）
    with psycopg.connect(DSN) as conn:
        exists = conn.execute("SELECT 1 FROM users WHERE username = 'demo_approver'").fetchone()
        if not exists:
            conn.execute(
                "INSERT INTO users (id, username, email, password_hash, display_name, "
                "is_active, must_change_password) VALUES "
                "('usr_demo_approver', 'demo_approver', 'demo_approver@demo.local', "
                "%s, '演示审批人', true, false)",
                (_hash(APPROVER_PASS),),
            )
            role = conn.execute("SELECT id FROM roles WHERE code = 'approver'").fetchone()
            conn.execute(
                "INSERT INTO user_roles (user_id, role_id) VALUES ('usr_demo_approver', %s)",
                (role[0],),
            )
        conn.commit()
    p("审批人 demo_approver 就绪")

    admin, client = await _admin_client()
    # 资产：绑定演示 redis 容器 + 真实健康探测（ops-action 容器内经 compose 网络访问）
    body = {
        "name": ASSET_NAME,
        "environment": "sandbox",
        "asset_type": "cache",
        "criticality": "high",
        "endpoints": {
            "container_name": "opspilot-demo-redis",
            "health_check": {"kind": "redis_ping", "host": "opspilot-demo-redis", "port": 6379},
        },
        "tags": ["demo", "d5"],
    }
    r = await client.post(f"{API}/assets", json=body, headers=admin)
    if r.status_code == 201:
        p(f"资产创建: {r.json()['data']['id']}")
    else:
        p(f"资产已存在（{r.status_code}）")

    # Runbook（published）+ allowlist 动作
    rb = await client.post(
        f"{API}/runbooks",
        headers=admin,
        json={
            "name": "D5 演示 Redis 受控重启",
            "description": "sandbox 演示容器受控重启（allowlist：restart_container@v1）",
            "risk_level": "r1",
            "steps": [
                {
                    "action": "restart_container",
                    "params": {"container_name": "opspilot-demo-redis"},
                }
            ],
            "allowed_environments": ["sandbox", "staging"],
            "allowlist_action_keys": ["restart_container@v1"],
        },
    )
    if rb.status_code == 201:
        vid = rb.json()["data"]["version_id"]
        pub = await client.post(f"{API}/runbooks/versions/{vid}/publish", headers=admin)
        p(f"Runbook 发布: {pub.status_code}")
    else:
        p(f"Runbook 已存在（{rb.status_code}）")
    await client.aclose()


def _hash(password: str) -> str:
    from argon2 import PasswordHasher

    return PasswordHasher().hash(password)


async def _admin_client() -> tuple[dict[str, str], httpx.AsyncClient]:
    client = httpx.AsyncClient(timeout=30)
    return await admin_headers(client), client


async def _wait_run(
    client: httpx.AsyncClient, admin: dict, run_id: str, timeout_s: int = 240
) -> str:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        r = await client.get(f"{API}/runs/{run_id}", headers=admin)
        data = r.json()["data"]
        status = data["status"]
        if status in ("completed", "failed", "needs_human", "canceled"):
            return status
        await asyncio.sleep(3)
    return "timeout"


async def _latest_runbook_version(client: httpx.AsyncClient, admin: dict) -> str:
    r = await client.get(f"{API}/runbooks", headers=admin)
    for rb in r.json()["data"]["runbooks"]:
        if rb["name"] == "D5 演示 Redis 受控重启":
            for v in rb["versions"]:
                if v["status"] == "published":
                    return v["id"]
    raise SystemExit("published runbook version not found")


async def s1() -> None:
    """S1 全链路：停 Redis → 诊断 → 提案 → 他人审批 → 受控重启 → Redis PING 成功 → 验证 PASSED。"""
    admin, client = await _admin_client()
    aid = await asset_id(client, admin)
    # 1. 故障注入：停止演示 redis（真实容器，仅演示环境）
    p("故障注入：docker stop opspilot-demo-redis")
    os.system("docker stop opspilot-demo-redis > NUL 2>&1")

    # 2. 创建事件并发起诊断（真实 Worker 采集：容器状态/日志经 roproxy）
    incident = await client.post(
        f"{API}/incidents",
        headers={**admin, "Idempotency-Key": f"demo-s1-{int(time.time())}"},
        json={
            "title": "D5 演示：Redis 不可达（sandbox）",
            "environment": "sandbox",
            "primary_asset_id": aid,
            "severity": "high",
            "description": "演示容器 opspilot-demo-redis 被停止，业务连接失败",
        },
    )
    if incident.status_code == 409:
        dup = incident.json()
        inc_id = dup["error"]["details"]["existing_incident_id"]
    else:
        assert incident.status_code == 201, incident.text
        inc_id = incident.json()["data"]["id"]
    p(f"事件: {inc_id}")

    ack = await client.post(f"{API}/incidents/{inc_id}/acknowledge", headers=admin)
    p(f"确认事件: {ack.status_code}")

    run = await client.post(
        f"{API}/incidents/{inc_id}/investigations",
        headers={**admin, "Idempotency-Key": f"demo-s1-run-{int(time.time())}"},
        json={},
    )
    assert run.status_code in (202, 201), run.text
    run_id = run.json()["data"]["run_id"]
    p(f"诊断 Run 启动: {run_id}（等待完成…）")
    status = await _wait_run(client, admin, run_id)
    p(f"诊断完成: {status}")
    report = await client.get(f"{API}/runs/{run_id}/report", headers=admin)
    if report.status_code == 200:
        summary = report.json()["data"]["conclusion"].get("summary", "")
        p(f"报告摘要: {summary[:120]}")

    # 3. Proposal（admin 发起）
    rbv = await _latest_runbook_version(client, admin)
    aid = await asset_id(client, admin)
    proposal = await client.post(
        f"{API}/proposals",
        headers={**admin, "Idempotency-Key": f"demo-s1-prp-{int(time.time())}"},
        json={
            "run_id": run_id,
            "runbook_version_id": rbv,
            "target_asset_id": aid,
            "environment": "sandbox",
            "parameters": {"container_name": "opspilot-demo-redis"},
        },
    )
    assert proposal.status_code == 201, proposal.text
    prp = proposal.json()["data"]["proposal"]
    p(f"提案: {prp['id']} 状态={prp['status']} 哈希={prp['parameter_hash'][:12]}…")

    dry = await client.post(f"{API}/proposals/{prp['id']}/dry-run", headers=admin)
    checks = dry.json()["data"]["checks"]
    p("dry-run: " + " ".join(f"{c['name']}={'✓' if c['passed'] else '✗'}" for c in checks))

    # 4. 另一名用户审批（四眼）
    approver = await approver_headers(client)
    ok = await client.post(
        f"{API}/proposals/{prp['id']}/approve",
        json={"comment": "S1 演示审批：同意受控重启"},
        headers=approver,
    )
    assert ok.status_code == 200, ok.text
    p("审批: demo_approver 批准（四眼通过）")

    # 5. 执行（Outbox → RabbitMQ → ops-action 真实重启容器）
    ex = await client.post(f"{API}/proposals/{prp['id']}/execute", headers=admin)
    assert ex.status_code == 200, ex.text
    execution = ex.json()["data"]["execution"]
    p(f"执行入队: {execution['id']}（等待 ops-action 消费…）")

    # 6. 轮询最终状态（验证窗口 30s）
    deadline = time.monotonic() + 120
    final = None
    while time.monotonic() < deadline:
        detail = (await client.get(f"{API}/proposals/{prp['id']}", headers=admin)).json()["data"]
        final = detail["proposal"]["status"]
        if final in (
            "verified",
            "verify_failed",
            "verify_unknown",
            "execution_failed",
            "execution_unknown",
        ):
            p(f"执行状态: {final}")
            windows = [
                (v["status"], (v["observation_window"] or {}).get("sample_count"))
                for v in detail["verifications"]
            ]
            p(f"验证: {windows}")
            break
        await asyncio.sleep(4)
    inc = (await client.get(f"{API}/incidents/{inc_id}", headers=admin)).json()["data"]
    p(f"事件最终状态: {inc['status']}")
    assert final == "verified", f"S1 演示期望 verified，实际 {final}"
    assert inc["status"] == "resolved", f"期望 resolved，实际 {inc['status']}"
    p("S1 演示通过：受控重启 + 真实 PING 探测 + RESOLVED")
    await client.aclose()


async def s9() -> None:
    """S9：动作返回成功但业务仍不健康 → VERIFY_FAILED，绝不 RESOLVED。

    构造：资产健康探测指向真实但持续不可用的 HTTP 端点（演示容器内
    一个确定关闭的端口）——redis 重启动作真实成功，业务观测真实失败。
    """
    admin, client = await _admin_client()
    db_exec(
        "UPDATE assets SET endpoints = %s WHERE name = %s",
        (
            '{"container_name": "opspilot-demo-redis", "health_check": '
            '{"kind": "http_health", "url": "http://opspilot-demo-app:8080/healthz"}}',
            ASSET_NAME,
        ),
    )
    p("S9 场景：健康探测指向 demo-app:8080/healthz（业务观测点不存在 → 真实 404 失败）")

    incident = await client.post(
        f"{API}/incidents",
        headers={**admin, "Idempotency-Key": f"demo-s9-{int(time.time())}"},
        json={
            "title": "D5 S9 演示：重启成功但业务不健康",
            "environment": "sandbox",
            "primary_asset_id": await asset_id(client, admin),
            "severity": "medium",
            "description": "S9：验证动作成功≠恢复成功",
        },
    )
    inc_id = (
        incident.json()["error"]["details"]["existing_incident_id"]
        if incident.status_code == 409
        else incident.json()["data"]["id"]
    )
    await client.post(f"{API}/incidents/{inc_id}/acknowledge", headers=admin)
    run = await client.post(
        f"{API}/incidents/{inc_id}/investigations",
        headers={**admin, "Idempotency-Key": f"demo-s9-run-{int(time.time())}"},
        json={},
    )
    run_id = run.json()["data"]["run_id"]
    await _wait_run(client, admin, run_id)
    rbv = await _latest_runbook_version(client, admin)
    proposal = await client.post(
        f"{API}/proposals",
        headers={**admin, "Idempotency-Key": f"demo-s9-prp-{int(time.time())}"},
        json={
            "run_id": run_id,
            "runbook_version_id": rbv,
            "target_asset_id": await asset_id(client, admin),
            "environment": "sandbox",
            "parameters": {"container_name": "opspilot-demo-redis"},
        },
    )
    prp = proposal.json()["data"]["proposal"]
    approver = await approver_headers(client)
    await client.post(f"{API}/proposals/{prp['id']}/approve", json={}, headers=approver)
    await client.post(f"{API}/proposals/{prp['id']}/execute", headers=admin)
    p("执行入队（ops-action 将真实重启 redis 成功，但探测失败）")

    deadline = time.monotonic() + 120
    final = None
    while time.monotonic() < deadline:
        detail = (await client.get(f"{API}/proposals/{prp['id']}", headers=admin)).json()["data"]
        final = detail["proposal"]["status"]
        if final.startswith("verify") or final.startswith("execution"):
            break
        await asyncio.sleep(4)
    inc = (await client.get(f"{API}/incidents/{inc_id}", headers=admin)).json()["data"]
    p(f"S9 结果: proposal={final} incident={inc['status']}")
    assert final in ("verify_failed", "verify_unknown"), f"期望 verify_failed/unknown，实际 {final}"
    assert inc["status"] in ("investigating", "needs_human"), f"绝不 RESOLVED，实际 {inc['status']}"
    p("S9 演示通过：动作成功但未宣告恢复（VERIFY_FAILED → 转回调查）")

    # 还原健康探测配置（供 S1 重复演示）
    db_exec(
        "UPDATE assets SET endpoints = %s WHERE name = %s",
        (
            '{"container_name": "opspilot-demo-redis", "health_check": '
            '{"kind": "redis_ping", "host": "opspilot-demo-redis", "port": 6379}}',
            ASSET_NAME,
        ),
    )
    await client.aclose()


async def denials() -> None:
    """拒绝矩阵：production / 四眼 / 未审批 / 参数篡改。"""
    admin, client = await _admin_client()
    approver = await approver_headers(client)
    rbv = await _latest_runbook_version(client, admin)

    def _proposal_json(env: str, run_id: str, aid: str) -> dict:
        return {
            "run_id": run_id,
            "runbook_version_id": rbv,
            "target_asset_id": aid,
            "environment": env,
            "parameters": {"container_name": "opspilot-demo-redis"},
        }

    # 用最近完成的 run 作为提案来源（runs 无列表端点，直查 DB）
    with psycopg.connect(DSN) as conn:
        row = conn.execute(
            "SELECT id FROM investigation_runs WHERE status = 'completed' "
            "ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
    assert row, "no completed run found; run s1 first"
    run_id = row[0]
    aid = await asset_id(client, admin)

    # 1. production：Admin 提交也拒绝
    r = await client.post(
        f"{API}/proposals",
        headers={**admin, "Idempotency-Key": f"demo-den-prod-{int(time.time())}"},
        json=_proposal_json("production", run_id, aid),
    )
    p(f"production 目标（Admin）: {r.status_code} {r.json()['error']['code']}")
    assert r.status_code == 403

    # 2. 四眼：创建人自批
    prp = (
        await client.post(
            f"{API}/proposals",
            headers={**admin, "Idempotency-Key": f"demo-den-four-{int(time.time())}"},
            json=_proposal_json("sandbox", run_id, aid),
        )
    ).json()["data"]["proposal"]
    self_ok = await client.post(f"{API}/proposals/{prp['id']}/approve", json={}, headers=admin)
    p(f"创建人自批（四眼违规）: {self_ok.status_code}")
    assert self_ok.status_code == 403

    # 3. 未审批执行
    early = await client.post(f"{API}/proposals/{prp['id']}/execute", headers=admin)
    p(f"未审批执行: {early.status_code}")
    assert early.status_code == 409

    # 4. 审批后参数篡改（DB 直改模拟绕过）
    await client.post(f"{API}/proposals/{prp['id']}/approve", json={}, headers=approver)
    db_exec(
        "UPDATE proposals SET parameters = parameters || %s WHERE id = %s",
        ('{"timeout_seconds": 999}', prp["id"]),
    )
    tampered = await client.post(f"{API}/proposals/{prp['id']}/execute", headers=admin)
    p(f"参数篡改后执行: {tampered.status_code} {tampered.json()['error']['code']}")
    assert tampered.status_code == 403

    # 5. 审批过期（DB 把审批窗口改为过去）
    prp2 = (
        await client.post(
            f"{API}/proposals",
            headers={**admin, "Idempotency-Key": f"demo-den-exp-{int(time.time())}"},
            json=_proposal_json("sandbox", run_id, aid),
        )
    ).json()["data"]["proposal"]
    await client.post(f"{API}/proposals/{prp2['id']}/approve", json={}, headers=approver)
    db_exec(
        "UPDATE approvals SET expires_at = %s WHERE proposal_id = %s",
        (datetime.now(UTC) - timedelta(minutes=5), prp2["id"]),
    )
    expired = await client.post(f"{API}/proposals/{prp2['id']}/execute", headers=admin)
    p(f"审批过期后执行: {expired.status_code}")
    assert expired.status_code == 409
    p("拒绝矩阵演示全部通过")
    await client.aclose()


async def crash() -> None:
    """崩溃恢复：ops-action 停止期间 execute → 消息滞留队列 → 重启后恢复投递并完成。"""
    admin, client = await _admin_client()
    approver = await approver_headers(client)
    with psycopg.connect(DSN) as conn:
        row = conn.execute(
            "SELECT id FROM investigation_runs WHERE status = 'completed' "
            "ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
    assert row, "no completed run found; run s1 first"
    rbv = await _latest_runbook_version(client, admin)
    aid = await asset_id(client, admin)
    proposal = await client.post(
        f"{API}/proposals",
        headers={**admin, "Idempotency-Key": f"demo-crash-{int(time.time())}"},
        json={
            "run_id": row[0],
            "runbook_version_id": rbv,
            "target_asset_id": aid,
            "environment": "sandbox",
            "parameters": {"container_name": "opspilot-demo-redis"},
        },
    )
    prp = proposal.json()["data"]["proposal"]
    await client.post(f"{API}/proposals/{prp['id']}/approve", json={}, headers=approver)
    ex = await client.post(f"{API}/proposals/{prp['id']}/execute", headers=admin)
    assert ex.status_code == 200, ex.text
    p(f"ops-action 已停止时执行入队: {ex.json()['data']['execution']['status']}（消息滞留队列）")
    print("PROPOSAL_ID=" + prp["id"])
    await client.aclose()


async def crash_resume(proposal_id: str) -> None:
    """ops-action 恢复后验证滞留消息被消费并完成（幂等：仅一次外部动作）。"""
    admin, client = await _admin_client()
    deadline = time.monotonic() + 120
    final = None
    while time.monotonic() < deadline:
        detail = (await client.get(f"{API}/proposals/{proposal_id}", headers=admin)).json()["data"]
        final = detail["proposal"]["status"]
        if final in ("verified", "verify_failed", "verify_unknown", "execution_failed"):
            break
        await asyncio.sleep(4)
    executions = detail["executions"]
    summary = [(e["status"], e["executor"]) for e in executions]
    p(f"恢复后结果: proposal={final} executions={summary}")
    assert final == "verified", f"期望 verified，实际 {final}"
    assert len(executions) == 1, "滞留消息只应触发一次执行"
    p("崩溃恢复演示通过：消息滞留 → 重启恢复投递 → 恰好一次受控执行 → 验证通过")
    await client.aclose()


def main() -> None:
    cmd = sys.argv[1] if len(sys.argv) > 1 else "prepare"
    if cmd == "crash-resume":
        asyncio.run(crash_resume(sys.argv[2]))
        return
    asyncio.run({"prepare": prepare, "s1": s1, "s9": s9, "denials": denials, "crash": crash}[cmd]())


if __name__ == "__main__":
    main()
