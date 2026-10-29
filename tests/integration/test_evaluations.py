"""评估体系集成测试（D6，PRD §13 / DATA_MODEL §3.9）：

- 数据集播种：≥30 案例、开发/保留集划分、套件 kind 完整；
- 完整评估执行：真实诊断管线（Fake Model + 确定性 stub 故障注入）、
  结果持久化、按场景查询与汇总指标；
- 三强门禁：越权执行成功 = 0、重复副作用 = 0、恢复验证误报 = 0
  （DB 状态 + 审计 + 执行记录交叉核验的执行器产出）。

诚实性约束固化：Fake Model 结果的 tokens/estimated_cost 必须为 unavailable。
"""

from __future__ import annotations

import httpx
import pytest

from tests.conftest import (
    ADMIN_BOOT_PASSWORD,
    ADMIN_BOOT_USERNAME,
    api_login,
    auth_headers,
    create_test_user,
)

pytestmark = pytest.mark.integration


@pytest.fixture
async def ready(client: httpx.AsyncClient) -> dict[str, str]:
    """已就绪 Admin 请求头（复用改密换发新 token 的流程）。"""
    login = await api_login(client, ADMIN_BOOT_USERNAME, ADMIN_BOOT_PASSWORD)
    assert login.status_code == 200
    token = login.json()["data"]["access_token"]
    change = await client.post(
        "/api/v1/auth/change-password",
        json={"old_password": ADMIN_BOOT_PASSWORD, "new_password": "AdminEval123!"},
        headers=auth_headers(token),
    )
    assert change.status_code == 200
    return auth_headers(change.json()["data"]["access_token"])


async def test_suites_endpoint_seeds_dataset(
    client: httpx.AsyncClient, ready: dict[str, str]
) -> None:
    resp = await client.get("/api/v1/evaluations/suites", headers=ready)
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["dataset_version"] == "d6-eval-v1"
    assert len(data["dataset_digest"]) == 16
    # ≥30 案例；开发集 + 保留集 = 全集；保留集固定非空
    stats = data["seed_stats"]
    assert stats["cases_total"] >= 30
    assert stats["holdout"] >= 5
    assert stats["dev"] + stats["holdout"] == stats["cases_total"]
    kinds = {s["kind"] for s in data["items"]}
    assert kinds == {"fault", "security", "rag_ablation", "reliability"}
    names = {s["name"] for s in data["items"]}
    assert {"fault-core", "rag-ablation", "security-gates", "reliability-recovery"} <= names
    # 幂等：二次访问不重复播种
    resp2 = await client.get("/api/v1/evaluations/suites", headers=ready)
    assert resp2.status_code == 200
    assert resp2.json()["data"]["seed_stats"] == stats


async def test_run_creation_requires_elevated_role(
    client: httpx.AsyncClient, ready: dict[str, str], db_session_factory
) -> None:
    """发起评估运行需要 approver/admin 角色（viewer 只读 → 403）。"""
    await create_test_user(
        db_session_factory,
        username="eval_viewer",
        password="EvalViewer123!",
        role_codes=["viewer"],
    )
    login = await api_login(client, "eval_viewer", "EvalViewer123!")
    assert login.status_code == 200
    viewer_headers = auth_headers(login.json()["data"]["access_token"])
    resp = await client.post(
        "/api/v1/evaluations/runs",
        json={"suite_id": "evs_fault_core", "wait": False},
        headers=viewer_headers,
    )
    assert resp.status_code == 403


async def _create_run(
    client: httpx.AsyncClient, headers: dict[str, str], suite_id: str
) -> str:
    resp = await client.post(
        "/api/v1/evaluations/runs",
        json={"suite_id": suite_id, "wait": True, "note": "integration-test"},
        headers=headers,
    )
    assert resp.status_code == 202, resp.text
    run_id = resp.json()["data"]["run_id"]
    assert resp.json()["data"]["status"] == "completed"
    return run_id


def _assert_gates_zero(detail: dict) -> None:
    summary = detail["summary"]
    assert summary["gate_unauthorized_success"] == 0, detail
    assert summary["gate_repeated_side_effects"] == 0, detail
    assert summary["gate_verification_misreport"] == 0, detail
    assert summary["gates_passed"] is True


def _failed_cases(detail: dict) -> list[str]:
    return [r["case_key"] for r in detail["results"] if not r["passed"]]


async def test_fault_suite_full_run(
    client: httpx.AsyncClient, ready: dict[str, str]
) -> None:
    run_id = await _create_run(client, ready, "evs_fault_core")
    detail = (
        await client.get(f"/api/v1/evaluations/runs/{run_id}", headers=ready)
    ).json()["data"]
    assert detail["status"] == "completed"
    assert detail["suite_name"] == "fault-core"
    assert detail["summary"]["cases_total"] == 12
    # 可复现锚点齐备（PRD §13 必须记录项）
    cfg = detail["config"]
    assert cfg["model_provider"] == "fake"
    assert cfg["git_commit"]
    assert cfg["dataset_version"] == "d6-eval-v1"
    assert cfg["prompt_version"]
    assert "rag_mode" in cfg and "budgets" in cfg
    # 全部案例通过（Fake Model 确定性管线）
    assert _failed_cases(detail) == [], _failed_cases(detail)
    assert detail["summary"]["passed"] == 12
    assert detail["summary"]["top1_hit_rate"] is not None
    # Fake Model 不编造成本/Token
    for r in detail["results"]:
        assert r["metrics"]["tokens"] == "unavailable"
        assert r["metrics"]["estimated_cost"] == "unavailable"
    _assert_gates_zero(detail)


async def test_security_suite_full_run(
    client: httpx.AsyncClient, ready: dict[str, str]
) -> None:
    run_id = await _create_run(client, ready, "evs_security_gates")
    detail = (
        await client.get(f"/api/v1/evaluations/runs/{run_id}", headers=ready)
    ).json()["data"]
    assert detail["status"] == "completed"
    assert detail["summary"]["cases_total"] == 13
    assert _failed_cases(detail) == [], _failed_cases(detail)
    # 强门禁：越权/未审批执行成功次数 = 0（DB + 审计 + 执行记录交叉核验）
    _assert_gates_zero(detail)


async def test_reliability_suite_full_run(
    client: httpx.AsyncClient, ready: dict[str, str]
) -> None:
    run_id = await _create_run(client, ready, "evs_reliability_recovery")
    detail = (
        await client.get(f"/api/v1/evaluations/runs/{run_id}", headers=ready)
    ).json()["data"]
    assert detail["status"] == "completed"
    assert detail["summary"]["cases_total"] == 8
    assert _failed_cases(detail) == [], _failed_cases(detail)
    _assert_gates_zero(detail)


async def test_rag_ablation_suite_full_run(
    client: httpx.AsyncClient, ready: dict[str, str]
) -> None:
    run_id = await _create_run(client, ready, "evs_rag_ablation")
    detail = (
        await client.get(f"/api/v1/evaluations/runs/{run_id}", headers=ready)
    ).json()["data"]
    assert detail["status"] == "completed"
    assert detail["summary"]["cases_total"] == 6
    assert _failed_cases(detail) == [], _failed_cases(detail)
    # 误导性知识零采信
    assert detail["summary"]["knowledge_misuse_total"] == 0
    _assert_gates_zero(detail)


async def test_holdout_flag_persisted(
    client: httpx.AsyncClient, ready: dict[str, str]
) -> None:
    """保留集标记持久化且结果可按 holdout 区分（保留集不得用于针对性调优）。"""
    run_id = await _create_run(client, ready, "evs_fault_core")
    detail = (
        await client.get(f"/api/v1/evaluations/runs/{run_id}", headers=ready)
    ).json()["data"]
    holdout_results = [r for r in detail["results"] if r["holdout"]]
    dev_results = [r for r in detail["results"] if not r["holdout"]]
    assert holdout_results, "holdout cases must exist in results"
    assert dev_results
    assert all(r["passed"] for r in holdout_results)
