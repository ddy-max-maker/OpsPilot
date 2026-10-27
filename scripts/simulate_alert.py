"""外部告警模拟脚本：向 POST /webhooks/alerts/{connector_id} 发送 HMAC 签名告警。

用法：
    python scripts/simulate_alert.py --asset-id as_xxx [--secret ...] [--repeat 3 --interval 2]

- 默认密钥读取环境变量 WEBHOOK_ALERTS_SECRET（与 compose 中 api 一致）；
- --repeat N 可模拟同一告警的重复投递（同 delivery_id），用于验证去重；
- --new-delivery 每次生成新的 delivery_id，用于验证映射到既有活跃单。
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import sys
import time
import urllib.error
import urllib.request
import uuid


def _post(url: str, body: bytes, headers: dict[str, str]) -> tuple[int, dict]:
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode() or "{}")


def main() -> int:
    parser = argparse.ArgumentParser(description="OpsPilot Next 告警模拟脚本")
    parser.add_argument("--base-url", default="http://localhost:8000")
    parser.add_argument("--connector-id", default="webhook_default")
    parser.add_argument("--secret", default=os.environ.get("WEBHOOK_ALERTS_SECRET"))
    parser.add_argument("--asset-id", required=True, help="目标资产 ID（as_ 前缀）")
    parser.add_argument("--delivery-id", default=None, help="外部投递 ID（默认随机生成）")
    parser.add_argument(
        "--new-delivery", action="store_true", help="每次投递生成新 delivery_id（同指纹）"
    )
    parser.add_argument("--fingerprint", default="cpu-saturation@v1")
    parser.add_argument("--title", default="CPU 持续饱和")
    parser.add_argument("--severity", default="high", choices=["low", "medium", "high", "critical"])
    parser.add_argument("--description", default="container cpu usage > 95%% for 5m")
    parser.add_argument("--repeat", type=int, default=1, help="投递次数（默认 1）")
    parser.add_argument("--interval", type=float, default=1.0, help="投递间隔秒")
    args = parser.parse_args()

    if not args.secret:
        print("[alert] 缺少签名密钥：传 --secret 或设置 WEBHOOK_ALERTS_SECRET")
        return 1

    url = f"{args.base_url.rstrip('/')}/api/v1/webhooks/alerts/{args.connector_id}"
    delivery_base = args.delivery_id or f"sim-{uuid.uuid4().hex[:12]}"

    for i in range(args.repeat):
        delivery_id = f"{delivery_base}-{i}" if args.new_delivery else delivery_base
        body = json.dumps(
            {
                "delivery_id": delivery_id,
                "fingerprint": args.fingerprint,
                "title": args.title,
                "severity": args.severity,
                "asset_id": args.asset_id,
                "description": args.description,
                "payload": {"alertname": "CPUSaturation", "source": "simulate_alert.py", "seq": i},
            },
            separators=(",", ":"),
        ).encode("utf-8")
        signature = hmac.new(args.secret.encode(), body, hashlib.sha256).hexdigest()
        headers = {
            "Content-Type": "application/json",
            "X-Signature": signature,
            "X-Timestamp": str(int(time.time())),
        }
        status, envelope = _post(url, body, headers)
        if status == 200:
            data = envelope["data"]
            action = "新建" if data["incident_created"] else "关联至既有"
            print(
                f"[alert] #{i} HTTP 200 delivery={delivery_id} alert={data['alert_id']} "
                f"{action} incident={data['incident_id']}"
            )
        else:
            error = envelope.get("error", {})
            print(
                f"[alert] #{i} HTTP {status} delivery={delivery_id} "
                f"code={error.get('code')} message={error.get('message')} "
                f"details={error.get('details')}"
            )
        if i + 1 < args.repeat:
            time.sleep(args.interval)
    return 0


if __name__ == "__main__":
    sys.exit(main())
