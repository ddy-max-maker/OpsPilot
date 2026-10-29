"""评估 API（D6，PRD §13 / DATA_MODEL §3.9）：

- GET  /evaluations/suites      套件列表（含数据集版本/摘要与开发/保留集统计）
- POST /evaluations/runs        创建评估运行（202；wait=true 时同步执行）
- GET  /evaluations/runs/{id}   运行详情（可复现锚点 + 逐案例结果 + 汇总指标 + 强门禁）

权限：管理员/审批人可读与执行（评估属于平台质量面）；只读角色可读。
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, status

from app.api.deps import ReadScopeDep, SessionDep, SettingsDep, require_roles
from app.core.responses import Envelope, ok
from app.domains.evaluations import service as eval_service
from app.domains.evaluations.schemas import (
    RunCreatedData,
    RunCreateRequest,
    SuiteListData,
)

router = APIRouter(tags=["evaluations"])

_EvalAdminDep = Annotated[Any, Depends(require_roles("approver", "admin"))]


@router.get("/evaluations/suites", response_model=Envelope[SuiteListData])
async def list_suites(
    session: SessionDep,
    _scope: ReadScopeDep,
) -> Envelope[SuiteListData]:
    """评估套件列表（幂等播种数据集；返回开发集/保留集规模与数据集摘要）。"""
    items, stats = await eval_service.list_suites(session)
    return ok(
        SuiteListData(
            items=items,
            dataset_version=eval_service.cases_mod.DATASET_VERSION,
            dataset_digest=eval_service.cases_mod.dataset_digest(),
            seed_stats=stats,
        )
    )


@router.post(
    "/evaluations/runs",
    response_model=Envelope[RunCreatedData],
    status_code=status.HTTP_202_ACCEPTED,
)
async def create_run(
    body: RunCreateRequest,
    session: SessionDep,
    settings: SettingsDep,
    _actor: _EvalAdminDep,
) -> Envelope[RunCreatedData]:
    """创建评估运行（202 异步受理；wait=true 供 CI/CLI 同步等待）。"""
    from app.core.db import get_session_factory  # noqa: PLC0415

    run = await eval_service.create_run(
        session,
        get_session_factory(),
        settings,
        suite_id=body.suite_id,
        note=body.note,
        wait=body.wait,
    )
    return ok(RunCreatedData(run_id=run.id, suite_id=run.suite_id, status=run.status))


@router.get("/evaluations/runs/{run_id}", response_model=Envelope[dict])
async def get_run(
    run_id: str,
    session: SessionDep,
    _scope: ReadScopeDep,
) -> Envelope[dict]:
    """评估运行详情（config 锚点 + 逐案例结果 + 汇总指标 + 三强门禁计数）。"""
    data = await eval_service.run_payload(session, run_id)
    return ok(data)
