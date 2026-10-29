"""诊断运行 API（CONTRACTS §5 Runs 端点，D2 交付）：

- POST /incidents/{incident_id}/investigations  创建 run（202，预算+快照+幂等）
- GET  /runs/{run_id}                           状态+usage+stop_reason
- GET  /runs/{run_id}/events                    事件流（JSON 分页；text/event-stream → SSE，D6）
- POST /runs/{run_id}/cancel                    合作式取消
- GET  /runs/{run_id}/evidence                  证据列表
- GET  /runs/{run_id}/report                    诊断报告（最新版本快照）
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, Query, Request, status
from fastapi.responses import JSONResponse, StreamingResponse

from app.api.deps import (
    CurrentUser,
    InvestigateScopeDep,
    ReadScopeDep,
    SessionDep,
    SettingsDep,
    require_roles,
)
from app.core.db import get_session_factory
from app.core.enums import EvidenceSourceType, EvidenceStatus
from app.core.errors import idempotency_key_required, validation_error
from app.core.responses import Envelope, ok
from app.domains.idempotency.service import (
    endpoint_tag,
    hash_request_body,
    run_idempotent,
)
from app.domains.runs import service as run_service
from app.domains.runs import sse
from app.domains.runs.schemas import (
    EvidenceListData,
    InvestigationCreateRequest,
    ReportResponse,
    RunCreatedData,
    RunEventListData,
    RunResponse,
)

router = APIRouter(tags=["runs"])

RunActorDep = Annotated[CurrentUser, Depends(require_roles("operator", "approver", "admin"))]


@router.post(
    "/incidents/{incident_id}/investigations",
    response_model=Envelope[RunCreatedData],
    status_code=status.HTTP_202_ACCEPTED,
)
async def create_investigation(
    incident_id: str,
    request: Request,
    session: SessionDep,
    settings: SettingsDep,
    scope: InvestigateScopeDep,
    actor: RunActorDep,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> JSONResponse:
    """创建诊断 run（202 异步受理；Idempotency-Key 幂等，CONTRACTS §1.3）。"""
    if not idempotency_key:
        raise idempotency_key_required()
    raw = await request.body()
    request_hash = hash_request_body(raw)

    async def execute() -> tuple[int, dict[str, Any]]:
        body = (
            InvestigationCreateRequest.model_validate_json(raw)
            if raw.strip()
            else InvestigationCreateRequest()
        )
        data = await run_service.create_run(
            session, settings, incident_id, scope, actor.user.id, body, commit=False
        )
        return status.HTTP_202_ACCEPTED, ok(data).model_dump(mode="json")

    status_code, resp_body, replayed = await run_idempotent(
        session,
        settings,
        user_id=actor.user.id,
        endpoint=endpoints_tag_create(incident_id),
        key=idempotency_key,
        request_hash=request_hash,
        execute=execute,
    )
    return JSONResponse(
        status_code=status_code,
        content=resp_body,
        headers={"Idempotency-Replayed": "true"} if replayed else None,
    )


def endpoints_tag_create(incident_id: str) -> str:
    return endpoint_tag("POST", f"/api/v1/incidents/{incident_id}/investigations")


@router.get("/runs/{run_id}", response_model=Envelope[RunResponse])
async def get_run(
    run_id: str,
    session: SessionDep,
    scope: ReadScopeDep,
) -> Envelope[RunResponse]:
    """run 状态 + usage + stop_reason（read 范围；越权 404 伪装）。"""
    data = await run_service.get_run_detail(session, run_id, scope)
    return ok(data)


@router.get("/runs/{run_id}/events", response_model=Envelope[RunEventListData])
async def list_run_events(
    run_id: str,
    request: Request,
    session: SessionDep,
    settings: SettingsDep,
    scope: ReadScopeDep,
    after_seq: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=200)] = 100,
    last_event_id: Annotated[str | None, Header(alias="Last-Event-ID")] = None,
) -> Envelope[RunEventListData] | StreamingResponse:
    """事件流：默认 JSON 分页（D2 契约不变）；显式 Accept: text/event-stream → SSE（D6）。

    SSE 语义见 app/domains/runs/sse.py：id=seq、Last-Event-ID 重放、心跳注释、
    终态 end 帧关闭；订阅前完成登录态与资产读取权限校验（越权 404 伪装）。
    """
    if "text/event-stream" in request.headers.get("accept", "").lower():
        start_seq = after_seq
        if last_event_id is not None:
            try:
                start_seq = int(last_event_id)
            except ValueError as exc:
                raise validation_error("Last-Event-ID must be a non-negative integer") from exc
            if start_seq < 0:
                raise validation_error("Last-Event-ID must be a non-negative integer")
        # 流开始前完成权限/存在性校验：越权与缺失以标准 JSON 错误返回（而非流内帧）
        await run_service.get_run_detail(session, run_id, scope)
        generator = sse.stream_run_events(
            get_session_factory(), settings, run_id, last_seq=start_seq
        )
        return StreamingResponse(
            generator,
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )
    data = await run_service.list_run_events(
        session, settings, run_id, scope, after_seq=after_seq, limit=limit
    )
    return ok(data)


@router.post("/runs/{run_id}/cancel", response_model=Envelope[RunResponse])
async def cancel_run(
    run_id: str,
    session: SessionDep,
    scope: InvestigateScopeDep,
    actor: RunActorDep,
) -> Envelope[RunResponse]:
    """合作式取消（queued 直接终态；running 置取消标志，Worker 收敛）。"""
    data = await run_service.cancel_run(session, run_id, scope, actor.user.id)
    return ok(data)


@router.get("/runs/{run_id}/evidence", response_model=Envelope[EvidenceListData])
async def list_run_evidence(
    run_id: str,
    session: SessionDep,
    settings: SettingsDep,
    scope: ReadScopeDep,
    source_type: Annotated[EvidenceSourceType | None, Query()] = None,
    evidence_status: Annotated[EvidenceStatus | None, Query(alias="status")] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 100,
) -> Envelope[EvidenceListData]:
    """证据列表（按 source_type/status 过滤）。"""
    data = await run_service.list_run_evidence(
        session,
        settings,
        run_id,
        scope,
        source_type=source_type,
        status=evidence_status,
        limit=limit,
        cursor=None,
    )
    return ok(data)


@router.get("/runs/{run_id}/report", response_model=Envelope[ReportResponse])
async def get_run_report(
    run_id: str,
    session: SessionDep,
    scope: ReadScopeDep,
) -> Envelope[ReportResponse]:
    """诊断报告（diagnostic_reports 最新版本快照，ADR-13）。"""
    data = await run_service.get_run_report(session, run_id, scope)
    return ok(data)
