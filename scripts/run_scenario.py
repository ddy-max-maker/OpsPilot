"""端到端诊断场景运行器：建事件 → 确认 → 发起 run → 轮询 → 摘要证据与报告。

用法：
    python scripts/run_scenario.py --password 'AdminReady123!' \
        --asset-id as_demo_redis --title "Redis 连接不可达" [--max-tool-calls 24]
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
import uuid

TERMINAL = {"completed", "needs_human", "failed", "canceled"}


def _request(
    base_url: str,
    method: str,
    path: str,
    *,
    token: str | None = None,
    body: dict | None = None,
    headers: dict | None = None,
) -> tuple[int, dict]:
    hdrs = {"Content-Type": "application/json"}
    if token:
        hdrs["Authorization"] = f"Bearer {token}"
    if headers:
        hdrs.update(headers)
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        base_url.rstrip("/") + path, data=data, headers=hdrs, method=method
    )
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode() or "{}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://localhost:8000")
    parser.add_argument("--username", default="admin")
    parser.add_argument("--password", required=True)
    parser.add_argument("--asset-id", required=True)
    parser.add_argument("--title", required=True)
    parser.add_argument("--description", default="由 run_scenario.py 创建")
    parser.add_argument("--max-tool-calls", type=int, default=None)
    parser.add_argument("--timeout", type=int, default=180)
    args = parser.parse_args()

    base = args.base_url
    status, env = _request(
        base,
        "POST",
        "/api/v1/auth/login",
        body={"username": args.username, "password": args.password},
    )
    assert status == 200, f"login failed: {env}"
    token = env["data"]["access_token"]

    # 建事件（幂等键随机；重复运行同 asset+fingerprint 会 409 → 复用既有活跃单）
    status, env = _request(
        base,
        "POST",
        "/api/v1/incidents",
        token=token,
        headers={"Idempotency-Key": str(uuid.uuid4())},
        body={
            "title": args.title,
            "description": args.description,
            "environment": "sandbox",
            "primary_asset_id": args.asset_id,
            "severity": "high",
            "fingerprint": f"scenario:{args.asset_id}:{args.title}",
        },
    )
    if status == 201:
        incident_id = env["data"]["id"]
        print(f"[scenario] incident created: {incident_id}")
    elif status == 409 and env["error"]["code"] == "CONFLICT_STATE":
        query = f"/api/v1/incidents?primary_asset_id={args.asset_id}&limit=5"
        _, env2 = _request(base, "GET", query, token=token)
        active = ("open", "acknowledged", "investigating", "mitigating", "monitoring")
        items = [i for i in env2["data"]["items"] if i["status"] in active]
        assert items, "conflict reported but no active incident found"
        incident_id = items[0]["id"]
        print(f"[scenario] reuse active incident: {incident_id} (status={items[0]['status']})")
    else:
        print(f"[scenario] incident create failed: {status} {env}")
        return 1

    incident_status = env["data"]["status"] if status == 201 else items[0]["status"]
    if incident_status in ("open", "acknowledged"):
        status, env = _request(
            base, "POST", f"/api/v1/incidents/{incident_id}/acknowledge", token=token
        )
        assert status == 200, f"acknowledge failed: {env}"
        print(f"[scenario] acknowledged: {env['data']['status']}")
    else:
        print(f"[scenario] incident already {incident_status}, skip acknowledge")

    budget_headers = {"Idempotency-Key": str(uuid.uuid4())}
    body: dict = {}
    if args.max_tool_calls:
        body["max_tool_calls"] = args.max_tool_calls
    status, env = _request(
        base,
        "POST",
        f"/api/v1/incidents/{incident_id}/investigations",
        token=token,
        headers=budget_headers,
        body=body,
    )
    assert status == 202, f"run create failed: {status} {env}"
    run_id = env["data"]["run_id"]
    print(f"[scenario] run accepted: {run_id}")

    started = time.time()
    status, env = _request(base, "GET", f"/api/v1/runs/{run_id}", token=token)
    while env["data"]["status"] not in TERMINAL and time.time() - started < args.timeout:
        time.sleep(2)
        status, env = _request(base, "GET", f"/api/v1/runs/{run_id}", token=token)
    run = env["data"]
    usage = json.dumps(run["usage"])
    print(f"[scenario] run status={run['status']} stop_reason={run['stop_reason']} usage={usage}")

    _, env = _request(base, "GET", f"/api/v1/runs/{run_id}/evidence?limit=50", token=token)
    print(f"[scenario] evidence count={len(env['data']['items'])}")
    for e in env["data"]["items"]:
        print(f"  - {e['id']} {e['collector']} status={e['status']} {e['summary_redacted'][:110]}")

    _, env = _request(base, "GET", f"/api/v1/runs/{run_id}/report", token=token)
    if env.get("error"):
        print(f"[scenario] report: {env['error']['code']}")
        return 0
    report = env["data"]
    generator = json.dumps(report["generator"])
    print(f"[scenario] report v{report['version_no']} generator={generator}")
    print(f"  summary: {report['conclusion']['summary'][:150]}")
    for f in report["conclusion"]["key_findings"]:
        print(f"  finding: {f['finding'][:120]} evidence={f['evidence_ids']}")
    for h in report["hypotheses"]:
        refs = h["supporting_evidence_ids"]
        print(
            f"  hyp#{h['rank']} [{h['status']}/{h['confidence']}] "
            f"{h['statement'][:120]} evidence={refs}"
        )
    print(f"  unknown: {json.dumps(report['unknown_note'], ensure_ascii=False)}")
    print(f"[scenario] incident_id={incident_id} run_id={run_id}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
