"""D4 RAG 消融实验运行器（真实 DeepSeek + 真实 Embedding，单 run 驱动）。

用法：
    uv run python scripts/rag_experiment.py --mode hybrid --scenario S1 \
        --password 'AdminReady123!' [--out experiments/d4_rag_results.json]

前置：
- worker 以对应 RAG_MODE + DeepSeek 启动（shell 覆盖环境变量）；
- scripts/demo/seed_knowledge.py 已播种知识集。

每次执行：建事件 → 确认 → 创建 Run → 轮询至终态 → 汇总报告/证据/检索事件
→ 追加写入 JSON 结果文件（off/text-only/hybrid 对照数据，含失败样本）。
"""

from __future__ import annotations

import argparse
import json
import time
import urllib.error
import urllib.request

TERMINAL = {"completed", "needs_human", "failed", "canceled"}

SCENARIOS: dict[str, dict[str, str]] = {
    "S1": {
        "asset": "as_demo_redis",
        "title": "Redis 连接不可达（RAG 实验）",
        "description": "D4 消融：历史经验有帮助的场景（Redis 连接池/不可达类）",
    },
    "S2": {
        "asset": "as_demo_app",
        "title": "容器 OOM（RAG 实验）",
        "description": "D4 消融：Docker OOM，历史处理记录可提示检查方向",
    },
    "J2": {
        "asset": "as_demo_java",
        "title": "Java 服务异常响应（RAG 实验）",
        "description": "D4 消融：历史相似但根因不同的场景（5xx）",
    },
    "M1": {
        "asset": "as_demo_mysql",
        "title": "MySQL 连接数激增与慢查询（RAG 实验）",
        "description": "D4 消融：历史知识与实时指标部分冲突的场景",
    },
}


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
    parser.add_argument("--mode", required=True, choices=["off", "text-only", "hybrid"])
    parser.add_argument("--scenario", required=True, choices=sorted(SCENARIOS))
    parser.add_argument("--out", default="experiments/d4_rag_results.json")
    parser.add_argument("--timeout", type=int, default=540)
    args = parser.parse_args()
    base = args.base_url.rstrip("/") + "/api/v1"
    spec = SCENARIOS[args.scenario]

    status, env = _request(
        base, "POST", "/auth/login", body={"username": args.username, "password": args.password}
    )
    assert status == 200, env
    token = env["data"]["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    status, env = _request(base, "GET", "/assets", headers=headers)
    assert status == 200, env
    asset = next((a for a in env["data"]["items"] if a["id"] == spec["asset"]), None)
    assert asset, f"asset {spec['asset']} not found"

    status, env = _request(
        base,
        "POST",
        "/incidents",
        headers={
            **headers,
            "Idempotency-Key": f"rag-{args.mode}-{args.scenario}-{int(time.time())}",
        },
        body={
            "title": spec["title"],
            "environment": str(asset["environment"]),
            "primary_asset_id": spec["asset"],
            "severity": "high",
            "description": spec["description"],
        },
    )
    assert status == 201, env
    incident_id = env["data"]["id"]
    _request(base, "POST", f"/incidents/{incident_id}/acknowledge", headers=headers)
    status, env = _request(
        base,
        "POST",
        f"/incidents/{incident_id}/investigations",
        headers={
            **headers,
            "Idempotency-Key": f"rag-run-{args.mode}-{args.scenario}-{int(time.time())}",
        },
        body={},
    )
    assert status == 202, env
    run_id = env["data"]["run_id"]
    print(f"[experiment] mode={args.mode} scenario={args.scenario} run={run_id}")

    report = None
    usage = {}
    run_status = None
    stop_reason = None
    deadline = time.time() + args.timeout
    while time.time() < deadline:
        status, env = _request(base, "GET", f"/runs/{run_id}", headers=headers)
        run_status = env["data"]["status"]
        stop_reason = env["data"].get("stop_reason")
        usage = env["data"].get("usage") or {}
        if run_status in TERMINAL:
            break
        time.sleep(5)
    status, env = _request(base, "GET", f"/runs/{run_id}/report", headers=headers)
    if status == 200:
        report = env["data"]
    status, env = _request(base, "GET", f"/runs/{run_id}/evidence", headers=headers)
    evidence = env["data"]["items"] if status == 200 else []

    # 直接读 DB 的 retrieval_events（经 psql，由调用方数据库连接）
    retrieval_events: list[dict] = []
    try:
        import subprocess  # noqa: PLC0415

        sql = (
            "SELECT json_agg(t) FROM (SELECT query, filters, result_version_ids, ranks, "
            "used_by_agent, latency_ms, created_at FROM retrieval_events "
            f"WHERE run_id = '{run_id}' ORDER BY id) t;"
        )
        proc = subprocess.run(
            [
                "docker",
                "exec",
                "opspilot-postgres",
                "psql",
                "-U",
                "opspilot",
                "-d",
                "opspilot",
                "-t",
                "-A",
                "-c",
                sql,
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        raw = proc.stdout.strip()
        if raw and raw != "null":
            retrieval_events = json.loads(raw)
    except Exception as exc:  # noqa: BLE001 — 检索事件读取失败不阻塞主流程
        print(f"[experiment] retrieval_events 读取失败: {exc}")

    record = {
        "mode": args.mode,
        "scenario": args.scenario,
        "incident_id": incident_id,
        "run_id": run_id,
        "run_status": run_status,
        "stop_reason": stop_reason,
        "usage": usage,
        "report": report,
        "evidence": [
            {
                "id": e.get("id"),
                "source_type": e.get("source_type"),
                "collector": e.get("collector"),
                "status": e.get("status"),
                "summary": e.get("summary_redacted", "")[:200],
            }
            for e in evidence
        ],
        "retrieval_events": retrieval_events,
        "recorded_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }
    import os  # noqa: PLC0415

    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    existing: list[dict] = []
    if os.path.exists(args.out):
        with open(args.out, encoding="utf-8") as f:
            try:
                existing = json.load(f)
            except json.JSONDecodeError:
                existing = []
    existing = [
        r
        for r in existing
        if not (r["mode"] == record["mode"] and r["scenario"] == record["scenario"])
    ]
    existing.append(record)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(existing, f, ensure_ascii=False, indent=2)
    print(
        f"[experiment] 完成: status={run_status} stop={stop_reason} "
        f"tools={usage.get('tool_calls_used')} tokens={usage.get('tokens_in')}→"
        f"{usage.get('tokens_out')} 已写入 {args.out}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
