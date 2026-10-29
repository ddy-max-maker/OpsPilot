"""真实模型**盲测**案例（D6 收尾，独立于冻结数据集 d6-eval-v2）。

用途：真实 DeepSeek 回归里除 7 个固定场景外，补充 3 个**此前从未参与调试**
（也未出现在任何 fake 评估/逐案例调参过程中）的场景，降低「按已知案例调优」
带来的乐观偏差。

边界（诚实性）：
- 本模块的案例**不进入** `cases._build_cases()`，因此不改变 `dataset_digest`
  与确定性套件的 39 例口径；
- expected 在首次运行真实模型**之前**写定，事后不得修改（提交即冻结）；
- 基础设施输入仍是评估进程内的确定性 stub（HTTP/Redis/Docker 代理），
  真实的是模型推理，不是真实容器故障注入。
"""

from __future__ import annotations

from typing import Any

from app.domains.evaluations.cases import _diagnosis_expected

BLIND_DATASET_VERSION = "blind-v1"  # 提交即冻结：2026-10-29（真实模型首跑之前）


def build_blind_cases() -> list[dict[str, Any]]:
    """返回盲测案例（结构与 cases._build_cases 的条目一致）。"""
    cases: list[dict[str, Any]] = []

    def add(key: str, title: str, scenario: dict[str, Any], expected: dict[str, Any]) -> None:
        cases.append(
            {
                "suite": "blind-v1",
                "case_key": key,
                "title": title,
                "kind": "diagnosis",
                "scenario": scenario,
                "expected": expected,
            }
        )

    # 1) 网关 504：直接原因明确（上游超时），深层根因未证实
    add(
        "blind_gateway_504",
        "盲测：健康端点 504（网关超时）",
        {
            "primary": {"name": "blind-orders-1", "type": "service", "environment": "sandbox"},
            "connectors": {"http": True, "docker": True},
            "http_behaviors": {"/health/blind-orders-1": ("json", 504)},
            "containers": {"blind-orders-1": {"state": "running"}},
            "plan_profile": "default",
            "rag_mode": "off",
        },
        _diagnosis_expected(top1_markers=["HTTP_504"]),
    )

    # 2) 容器 OOM 退出：直接原因需要容器层证据（oom_killed），非仅 HTTP 500
    add(
        "blind_oom_exit_137",
        "盲测：容器 OOM 退出（exit 137）+ 健康端点 500",
        {
            "primary": {"name": "blind-api-2", "type": "service", "environment": "sandbox"},
            "connectors": {"http": True, "docker": True},
            "http_behaviors": {"/health/blind-api-2": ("json", 500)},
            "containers": {
                "blind-api-2": {
                    "state": "exited",
                    "running": False,
                    "oom_killed": True,
                    "exit_code": 137,
                }
            },
            "logs": {"blind-api-2": ["fatal: out of memory", "killed by cgroup memory limit"]},
            "plan_profile": "default",
            "rag_mode": "off",
        },
        _diagnosis_expected(
            top1_markers=["HTTP_500", "REDIS_UNAVAILABLE", "CONNECTOR_UNAVAILABLE"],
            require_evidence_contains=["oom_killed"],
        ),
    )

    # 3) 缓存连接拒绝（无 HTTP 观测）：只能靠 redis 连接器证据
    add(
        "blind_cache_refuse",
        "盲测：缓存服务连接被拒绝（仅连接器证据）",
        {
            "primary": {"name": "blind-cache-3", "type": "cache", "environment": "sandbox"},
            "connectors": {"redis": "ok", "docker": True, "http": False},
            "redis_behavior": "refuse",
            "containers": {"blind-cache-3": {"state": "running"}},
            "plan_profile": "default",
            "rag_mode": "off",
        },
        _diagnosis_expected(top1_markers=["REDIS_UNAVAILABLE"]),
    )

    return cases


BLIND_CASE_KEYS = [c["case_key"] for c in build_blind_cases()]
