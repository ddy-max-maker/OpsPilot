"""演示种子脚本：管理员登录并登记受监控资产（可重复运行）。

用法（venv 内运行，或任意 Python 3.12+）：
    python scripts/seed.py --base-url http://localhost:8000 \
        --username admin --password 'BootstrapAdmin_2026!' --new-password 'AdminReady123!'

行为：
- 登录指定管理员；若为引导账号首登则自动完成改密；
- 创建演示资产（UNIQUE(name, environment)，重复运行跳过已存在项）。
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request


def _request(
    base_url: str, method: str, path: str, *, token: str | None = None, body: dict | None = None
) -> tuple[int, dict]:
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    data = json.dumps(body).encode() if body is not None else None
    url = base_url.rstrip("/") + path
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode() or "{}")


DEMO_ASSETS = [
    {
        "name": "order-api",
        "environment": "sandbox",
        "asset_type": "container",
        "criticality": "high",
        "owner": "team-commerce",
    },
    {
        "name": "payment-db",
        "environment": "sandbox",
        "asset_type": "database",
        "criticality": "high",
        "owner": "team-commerce",
    },
    {
        "name": "search-service",
        "environment": "staging",
        "asset_type": "service",
        "criticality": "medium",
        "owner": "team-search",
    },
]


def main() -> int:
    parser = argparse.ArgumentParser(description="OpsPilot Next 演示种子脚本")
    parser.add_argument("--base-url", default="http://localhost:8000")
    parser.add_argument("--username", default="admin")
    parser.add_argument("--password", required=True, help="当前密码（引导密码或已改密码）")
    parser.add_argument("--new-password", default=None, help="首登改密目标密码（引导账号必填）")
    args = parser.parse_args()

    status, envelope = _request(
        args.base_url,
        "POST",
        "/api/v1/auth/login",
        body={"username": args.username, "password": args.password},
    )
    if status != 200:
        print(f"[seed] 登录失败 HTTP {status}: {envelope.get('error', {}).get('message')}")
        return 1
    token = envelope["data"]["access_token"]
    if envelope["data"]["must_change_password"]:
        if not args.new_password:
            print("[seed] 引导账号必须改密：请提供 --new-password")
            return 1
        status, envelope = _request(
            args.base_url,
            "POST",
            "/api/v1/auth/change-password",
            token=token,
            body={"old_password": args.password, "new_password": args.new_password},
        )
        if status != 200:
            print(f"[seed] 改密失败 HTTP {status}: {envelope.get('error', {}).get('message')}")
            return 1
        status, envelope = _request(
            args.base_url,
            "POST",
            "/api/v1/auth/login",
            body={"username": args.username, "password": args.new_password},
        )
        token = envelope["data"]["access_token"]
        print("[seed] 引导账号改密完成")
    print("[seed] 登录成功")

    created = 0
    for asset in DEMO_ASSETS:
        status, envelope = _request(
            args.base_url, "POST", "/api/v1/assets", token=token, body=asset
        )
        if status == 201:
            name = asset["name"]
            print(f"[seed] 资产已创建 {name}（{asset['environment']}） id={envelope['data']['id']}")
            created += 1
        elif status == 409:
            print(f"[seed] 资产已存在，跳过 {asset['name']}（{asset['environment']}）")
        else:
            print(f"[seed] 资产创建失败 HTTP {status}: {envelope.get('error', {}).get('message')}")
            return 1
    print(f"[seed] 完成：新建 {created} 个资产")
    return 0


if __name__ == "__main__":
    sys.exit(main())
