"""D7 三条演示路线的可复现驱动脚本（真实 API，不含 Mock）。

用法（仓库根目录，栈已启动且已执行 scripts/bootstrap_demo）：
  python scripts/demo_routes.py a            # A：故障登记 → 诊断 → Evidence → 报告
  python scripts/demo_routes.py b            # B：历史知识 → RAG 检索 → 辅助诊断（需 RAG_MODE!=off）
  python scripts/demo_routes.py c            # C：Proposal → 四眼审批 → 执行门禁 → 验证
  python scripts/demo_routes.py all          # 依次执行（B 在 RAG 关闭时自动跳过）
  python scripts/demo_routes.py recover      # 恢复演示容器（撤销 A 的故障注入）

设计约束（D7）：
- 只调用真实 HTTP API，不做任何 Mock；被诊断对象是真实运行的演示容器。
- C 的真实写动作默认**执行被门禁拒绝**：ops-action 挂载宿主 docker.sock 不是
  隔离环境，按 D6 安全默认值（ACTION_EXECUTION_ENABLED=false）拒绝执行。
  脚本因此只演示 dry-run / 四眼审批 / 拒绝路径，不开启宿主 Docker 写权限。
- 不做破坏性清理：事件/提案/诊断记录全部保留（演示证据）。

环境变量：
  DEMO_API（默认 http://127.0.0.1:8000）
  DEMO_PASSWORD（演示账号统一密码，默认 E2ePassw0rd!2026）
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

API = (os.environ.get("DEMO_API") or "http://127.0.0.1:8000") + "/api/v1"
PASSWORD = os.environ.get("DEMO_PASSWORD") or "E2ePassw0rd!2026"
TARGET_ASSET_ID = "as_demo_redis"
TARGET_CONTAINER = "opspilot-demo-redis"
POLL_TIMEOUT_SECONDS = 240


def p(step: str, msg: str) -> None:
    print(f"[{step}] {msg}", flush=True)


class _Response:
    """极简响应对象（只实现脚本用到的字段，便于阅读调用点）。"""

    def __init__(self, status_code: int, text: str) -> None:
        self.status_code = status_code
        self.text = text

    def json(self) -> Any:
        return json.loads(self.text) if self.text else None

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}: {self.text[:200]}")


class _Client:
    """标准库 HTTP 客户端：演示脚本不依赖任何第三方包（无需 uv sync）。

    空 ProxyHandler 显式忽略系统代理——本机 HTTP_PROXY 会把 127.0.0.1 请求
    也交给代理，导致 502。
    """

    def __init__(self, timeout: float = 60.0) -> None:
        self._timeout = timeout
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        json_body: Any = None,
        params: dict[str, Any] | None = None,
    ) -> _Response:
        if params:
            query = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
            url = f"{url}?{query}"
        data = None
        all_headers = {"Accept": "application/json", **(headers or {})}
        if json_body is not None:
            data = json.dumps(json_body).encode()
            all_headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, headers=all_headers, method=method)
        try:
            with self._opener.open(req, timeout=self._timeout) as resp:
                return _Response(resp.status, resp.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as exc:  # 4xx/5xx 也要能读到 envelope
            return _Response(exc.code, exc.read().decode("utf-8", "replace"))

    def __enter__(self) -> _Client:
        return self

    def __exit__(self, *exc_info: object) -> None:
        return None

    def get(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        params: dict[str, Any] | None = None,
    ) -> _Response:
        return self.request("GET", url, headers=headers, params=params)

    def post(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        json_body: Any = None,
    ) -> _Response:
        return self.request("POST", url, headers=headers, json_body=json_body)


def new_client() -> _Client:
    return _Client()


def login(client: _Client, username: str) -> dict[str, str]:
    resp = client.post(f"{API}/auth/login", json_body={"username": username, "password": PASSWORD})
    if resp.status_code != 200:
        raise SystemExit(f"登录失败 {username}: {resp.status_code} {resp.text[:200]}")
    token = resp.json()["data"]["access_token"]
    return {"Authorization": f"Bearer {token}"}


def find_asset(client: _Client, headers: dict[str, str]) -> tuple[str, str]:
    """返回 (asset_id, container_name)。以资产登记为容器身份权威（D6 规则）。"""
    resp = client.get(f"{API}/assets", params={"limit": 200}, headers=headers)
    resp.raise_for_status()
    items = resp.json()["data"]["items"]
    target = next((i for i in items if i["id"] == TARGET_ASSET_ID), None)
    if target is None:
        target = next((i for i in items if i["name"] in ("redis", "demo-redis")), None)
    if target is None:
        raise SystemExit(
            f"未找到演示资产 {TARGET_ASSET_ID}：请先执行 scripts/bootstrap_demo.sh（或 make seed）"
        )
    endpoints = target.get("endpoints") or {}
    return target["id"], str(endpoints.get("container_name") or TARGET_CONTAINER)


def wait_run(client: _Client, headers: dict[str, str], run_id: str) -> dict[str, Any]:
    deadline = time.monotonic() + POLL_TIMEOUT_SECONDS
    last = ""
    while time.monotonic() < deadline:
        data = client.get(f"{API}/runs/{run_id}", headers=headers).json()["data"]
        status = data["status"]
        if status != last:
            p("A", f"run 状态 → {status}")
            last = status
        if status in ("completed", "failed", "canceled", "needs_human"):
            return data
        time.sleep(3)
    raise SystemExit(f"run {run_id} 在 {POLL_TIMEOUT_SECONDS}s 内未结束（status={last}）")


def create_incident_and_diagnose(
    client: _Client,
    operator: dict[str, str],
    asset_id: str,
    *,
    title: str,
    description: str,
    tag: str,
) -> tuple[str, str]:
    """登记 → 确认 → 发起诊断 → 等终态。返回 (incident_id, run_id)。"""
    stamp = int(time.time())
    inc = client.post(
        f"{API}/incidents",
        headers={**operator, "Idempotency-Key": f"d7-{tag}-{stamp}"},
        json_body={
            "title": title,
            "environment": "sandbox",
            "primary_asset_id": asset_id,
            "severity": "high",
            "description": description,
        },
    )
    if inc.status_code != 201:
        raise SystemExit(f"创建事件失败: {inc.status_code} {inc.text[:300]}")
    incident_id = inc.json()["data"]["id"]
    p("A", f"事件已登记 {incident_id}（state=open, environment=sandbox）")

    ack = client.post(f"{API}/incidents/{incident_id}/acknowledge", headers=operator)
    p("A", f"事件确认 → {ack.json()['data']['status']}")

    run = client.post(
        f"{API}/incidents/{incident_id}/investigations",
        headers={**operator, "Idempotency-Key": f"d7-{tag}-run-{stamp}"},
        json_body={},
    )
    if run.status_code not in (201, 202):
        raise SystemExit(f"发起诊断失败: {run.status_code} {run.text[:300]}")
    run_id = run.json()["data"]["run_id"]
    p("A", f"诊断已受理 run={run_id}（Worker 认领后开始执行）")
    return incident_id, run_id


def print_evidence_and_report(
    client: _Client, headers: dict[str, str], run_id: str, run: dict[str, Any]
) -> None:
    ev = client.get(f"{API}/runs/{run_id}/evidence", params={"limit": 200}, headers=headers)
    items = ev.json()["data"]["items"] if ev.status_code == 200 else []
    p("A", f"证据 {len(items)} 条（真实采集，payload 已脱敏）：")
    for item in items:
        flag = f" error_code={item['error_code']}" if item.get("error_code") else ""
        print(f"      - [{item['source_type']}/{item['collector']}] status={item['status']}{flag}")
        print(f"        {item['summary_redacted'][:110]}")

    usage = run.get("usage") or {}
    p(
        "A",
        (
            f"用量：tool_calls={usage.get('tool_calls_used')} "
            f"iterations={usage.get('iterations_used')} "
            f"tokens_in={usage.get('tokens_in')} tokens_out={usage.get('tokens_out')}"
        ),
    )

    rep = client.get(f"{API}/runs/{run_id}/report", headers=headers)
    if rep.status_code != 200:
        p("A", f"报告不可用（{rep.status_code}）——报告生成失败时运行仍保留证据与事件")
        return
    data = rep.json()["data"]
    conclusion = data["conclusion"]
    p("A", f"报告 v{data['version_no']} status={data['status']} generator={data.get('generator')}")
    p("A", f"结论摘要：{conclusion.get('summary', '')[:160]}")
    p("A", f"是否未知故障：{conclusion.get('is_unknown')}")
    for hypo in data["hypotheses"]:
        p(
            "A",
            f"假设#{hypo['rank']} [{hypo['status']}] {hypo['statement'][:100]} "
            f"(support={len(hypo['supporting_evidence_ids'])})",
        )


def route_a(client: _Client) -> tuple[str, str]:
    print("\n=== 路线 A：故障登记 → DeepAgents 诊断 → Evidence → 报告 ===", flush=True)
    operator = login(client, "e2e_operator")
    asset_id, container = find_asset(client, operator)

    p("A", f"故障注入：docker stop {container}（真实演示容器，可 recover 撤销）")
    subprocess.run(
        ["docker", "stop", container],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    time.sleep(2)

    incident_id, run_id = create_incident_and_diagnose(
        client,
        operator,
        asset_id,
        title=f"D7 演示A：Redis 不可达（{container} 已停止）",
        description="演示容器被停止，业务连接失败；验证工具网关采集真实故障证据",
        tag="a",
    )
    run = wait_run(client, operator, run_id)
    p("A", f"诊断终态 status={run['status']} stop_reason={run['stop_reason']}")
    snap = run.get("model_config_snapshot") or {}
    rag = snap.get("rag") or snap.get("rag_mode")
    p(
        "A",
        f"运行配置快照：provider={snap.get('provider')} model={snap.get('model_name')} "
        f"rag={rag} prompt={snap.get('prompt_version')}",
    )
    print_evidence_and_report(client, operator, run_id, run)
    return incident_id, run_id


def route_b(client: _Client) -> None:
    print("\n=== 路线 B：历史知识审核发布 → RAG 检索 → 辅助诊断 ===", flush=True)
    admin = login(client, "e2e_admin")
    operator = login(client, "e2e_operator")

    # 1. 知识是否已发布（seed_knowledge 落库即 published，四眼由脚本内 reviewer≠author 保证）
    docs = client.get(f"{API}/knowledge/documents", headers=admin).json()["data"]
    items = docs.get("documents") or docs.get("items") or []
    published = [d for d in items if any(v["status"] == "published" for v in d.get("versions", []))]
    p("B", f"已发布知识文档 {len(published)} 篇（draft 对 Agent 不可见）")
    if not published:
        print("      知识集为空：先执行")
        print("      docker compose exec -T api python scripts/demo/seed_knowledge.py")
        return

    # 2. 检索验证（权限前置过滤 + 混合检索）
    search = client.get(
        f"{API}/knowledge/search",
        params={"query": "Redis 连接池耗尽", "limit": 5},
        headers=operator,
    )
    if search.status_code == 200:
        body = search.json()["data"]
        hits = body.get("hits") or []
        p(
            "B",
            f"检索模式 = {body.get('mode')}，命中 {len(hits)} 条，延迟 {body.get('latency_ms')}ms",
        )
        for hit in hits[:3]:
            ranks = hit.get("ranks") or {}
            p(
                "B",
                f"  - {hit.get('title')} v{hit.get('version_no')} "
                f"RRF 排名 text={ranks.get('text')} vector={ranks.get('vector')}",
            )
    else:
        p("B", f"检索失败 {search.status_code}: {search.text[:200]}")

    # 3. 诊断是否真的用到知识（看 rag_mode 与 knowledge 证据）
    asset_id, container = find_asset(client, operator)
    subprocess.run(
        ["docker", "stop", container],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    time.sleep(2)
    incident_id, run_id = create_incident_and_diagnose(
        client,
        operator,
        asset_id,
        title="D7 演示B：Redis 不可达（历史经验辅助）",
        description="同 A 的故障，用于对比 RAG 开关下的证据与报告差异",
        tag="b",
    )
    run = wait_run(client, operator, run_id)
    snap = run.get("model_config_snapshot") or {}
    rag_mode = snap.get("rag") or snap.get("rag_mode")
    p("B", f"本次 run 的 rag_mode = {rag_mode}")
    if rag_mode == "off":
        p("B", "RAG 关闭：Agent 未加载知识工具（这是预期行为，不是失败）")
        p("B", "开启方式：.env 设 RAG_MODE=hybrid 后 docker compose up -d api worker，再重跑 b")
        return
    ev = client.get(f"{API}/runs/{run_id}/evidence", params={"limit": 200}, headers=operator)
    items = ev.json()["data"]["items"] if ev.status_code == 200 else []
    knowledge = [i for i in items if i["source_type"] == "knowledge"]
    p("B", f"knowledge 来源证据 {len(knowledge)} 条（知识不参与根因支撑，只引导排查方向）")
    for k in knowledge[:3]:
        print(f"      - {k['summary_redacted'][:100]}")
    print_evidence_and_report(client, operator, run_id, run)


def route_c(client: _Client, run_id: str | None) -> None:
    print("\n=== 路线 C：Proposal → 四眼审批 → sandbox 执行门禁 → 恢复验证 ===", flush=True)
    admin = login(client, "e2e_admin")
    operator = login(client, "e2e_operator")
    approver = login(client, "e2e_approver")

    if run_id is None:
        p("C", "未提供 run_id：先跑一次诊断作为恢复方案的前置依据（可用 --run-id 复用已有 run）")
        asset_id0, _ = find_asset(client, operator)
        _, run_id = create_incident_and_diagnose(
            client,
            operator,
            asset_id0,
            title="D7 演示C：受控恢复前置诊断",
            description="为 Proposal 提供根因与证据依据",
            tag="c",
        )
        wait_run(client, operator, run_id)

    # 1. 已发布的 Runbook（草稿不可执行）
    rbs = client.get(f"{API}/runbooks", headers=admin).json()["data"]["runbooks"]
    version_id = None
    for rb in rbs:
        for v in rb.get("versions", []):
            if v["status"] == "published":
                version_id = v["id"]
                p(
                    "C",
                    f"Runbook {rb['name']} v{v['version_no']}"
                    f"（published，风险 {rb['risk_level']}）",
                )
                break
        if version_id:
            break
    if not version_id:
        raise SystemExit("没有已发布 Runbook：请先执行 python scripts/demo_d5.py prepare")

    asset_id, container = find_asset(client, operator)
    stamp = int(time.time())

    # 2. 建提案（operator 发起；执行快照含参数哈希）
    prp = client.post(
        f"{API}/proposals",
        headers={**operator, "Idempotency-Key": f"d7-c-{stamp}"},
        json_body={
            "run_id": run_id,
            "runbook_version_id": version_id,
            "target_asset_id": asset_id,
            "environment": "sandbox",
            "parameters": {"container_name": container},
        },
    )
    if prp.status_code != 201:
        raise SystemExit(f"建提案失败 {prp.status_code}: {prp.text[:300]}")
    proposal = prp.json()["data"]["proposal"]
    pid = proposal["id"]
    p("C", f"提案 {pid} status={proposal['status']} 参数哈希={proposal['parameter_hash'][:16]}…")

    # 3. dry-run（不产生副作用）
    dry = client.post(f"{API}/proposals/{pid}/dry-run", headers=operator)
    if dry.status_code == 200:
        checks = dry.json()["data"]["checks"]
        p(
            "C",
            "dry-run 前置检查："
            + " ".join(f"{c['name']}={'PASS' if c['passed'] else 'FAIL'}" for c in checks),
        )
    else:
        p("C", f"dry-run 失败 {dry.status_code}: {dry.text[:200]}")

    # 4. 四眼：发起人自己不能审批
    self_approve = client.post(
        f"{API}/proposals/{pid}/approve", json_body={"comment": "自审"}, headers=operator
    )
    p(
        "C",
        f"发起人自审批 → {self_approve.status_code} "
        f"{self_approve.json().get('error', {}).get('code')}（四眼拒绝，期望 403）",
    )

    # 5. 另一账号审批通过
    ok = client.post(
        f"{API}/proposals/{pid}/approve",
        json_body={"comment": "D7 演示：同意受控重启"},
        headers=approver,
    )
    if ok.status_code != 200:
        raise SystemExit(f"审批失败 {ok.status_code}: {ok.text[:300]}")
    p("C", f"e2e_approver 审批通过 → {ok.json()['data']['proposal']['status']}")

    # 6. 执行：门禁默认关闭 → 入队后由 ops-action 拒绝（不产生任何容器写操作）
    ex = client.post(f"{API}/proposals/{pid}/execute", headers=operator)
    if ex.status_code != 200:
        p("C", f"执行被拒 {ex.status_code}: {ex.text[:200]}")
        return
    p("C", f"执行已受理 execution={ex.json()['data']['execution']['id']}（等待 ops-action 消费）")

    deadline = time.monotonic() + 120
    final = None
    while time.monotonic() < deadline:
        detail = client.get(f"{API}/proposals/{pid}", headers=operator).json()["data"]
        final = detail["proposal"]["status"]
        if final in (
            "verified",
            "verify_failed",
            "verify_unknown",
            "execution_failed",
            "execution_unknown",
        ):
            break
        time.sleep(3)
    p("C", f"提案终态 = {final}")
    for execution in detail.get("executions", []):
        info = execution.get("result_detail") or {}
        p(
            "C",
            f"执行记录 status={execution['status']} "
            f"external_ref={execution.get('external_ref')} reason={info.get('reason')}",
        )
    for verification in detail.get("verifications", []):
        p(
            "C",
            f"验证 status={verification['status']} window={verification.get('observation_window')}",
        )
    p(
        "C",
        "说明：宿主 docker.sock 不是隔离环境，D6 起真实写操作默认拒绝"
        "（ACTION_EXECUTION_ENABLED=false）。此处演示的是门禁拒绝路径；"
        "verified 全链路证据见 D5 演示记录与 tests/integration/test_action_flow.py。",
    )


def recover() -> None:
    print(f"[recover] 启动演示容器 {TARGET_CONTAINER}", flush=True)
    subprocess.run(["docker", "start", TARGET_CONTAINER], check=False)
    print("[recover] 完成（demo-redis 可用）", flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="D7 演示路线驱动脚本")
    parser.add_argument("route", choices=["a", "b", "c", "all", "recover"])
    parser.add_argument("--run-id", default=None, help="路线 C 使用的诊断 run（缺省取最近一条）")
    args = parser.parse_args()

    if args.route == "recover":
        recover()
        return 0

    with new_client() as client:
        try:
            client.get(f"{API}/health/live").raise_for_status()
        except Exception as exc:  # noqa: BLE001 — 栈未启动时给出明确指引
            print(f"API 不可达（{API}）：先执行 docker compose up -d。{exc}", file=sys.stderr)
            return 2

        run_id = args.run_id
        if args.route in ("a", "all"):
            _, run_id = route_a(client)
        if args.route in ("b", "all"):
            route_b(client)
        if args.route in ("c", "all"):
            route_c(client, run_id)
    print("\n[done] 演示完成。清理/恢复：python scripts/demo_routes.py recover", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
