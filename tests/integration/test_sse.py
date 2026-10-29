"""SSE 实时事件流集成测试（D6）：

覆盖契约要点：
- JSON 向后兼容：默认/显式 application/json 仍为 JSON 分页；
- 显式 Accept: text/event-stream 才进入 SSE；
- 帧 id = run_events.seq；全量重放与 Last-Event-ID 续传（不丢、不重复）；
- 终态 run 正确发送 end 帧并关闭；运行中 run 持续推送新事件；
- 心跳为 ": ping" 注释行，不产生伪造诊断事件；
- 未登录 401、越权 404 伪装、非法 Last-Event-ID 400。

测试工具说明：httpx 的 ASGITransport 会全量缓冲响应体（body_parts 聚合后才
构造 Response），无法观测运行中流的增量推送；因此「运行中 run」场景用原生
ASGI 消息驱动（_RawStream）直接消费 http.response.body 消息。
"""

from __future__ import annotations

import asyncio
import contextlib
import json

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.enums import RunEventType, RunStatus
from app.models import Asset, Incident, InvestigationRun, RunEvent, User
from tests.conftest import api_login, auth_headers

EVENTS_URL = "/api/v1/runs/{run_id}/events"


# ---- 种子辅助 ----


async def _seed_run(
    db_session_factory: async_sessionmaker[AsyncSession],
    *,
    run_id: str,
    status: str = "running",
    event_seqs: tuple[int, ...] = (1, 2),
) -> None:
    """直连测试库播种用户 + 资产 + 事件 + run（Admin 范围全量可读）。"""
    async with db_session_factory() as session:
        session.add(
            User(
                id="usr_sse_creator",
                username="sse_creator",
                email="sse_creator@test.local",
                password_hash="!",
                display_name="SSE Creator",
                is_active=False,
                must_change_password=False,
            )
        )
        await session.flush()
        asset = Asset(
            id="as_sse_demo",
            name="sse-demo-redis",
            environment="sandbox",
            asset_type="cache",
            status="active",
            criticality="medium",
            endpoints={},
            owner=None,
            tags=[],
        )
        session.add(asset)
        await session.flush()
        session.add(
            Incident(
                id="inc_sse",
                title="SSE 演示事件",
                environment="sandbox",
                primary_asset_id=asset.id,
                source="manual",
                severity="medium",
                status="investigating",
                created_by="usr_sse_creator",
            )
        )
        await session.flush()
        session.add(
            InvestigationRun(
                id=run_id,
                incident_id="inc_sse",
                status=status,
                triggered_by="usr_sse_creator",
                model_config_json={"provider": "fake"},
                budgets={},
                usage={},
                authorization_snapshot={"is_admin": True, "all_assets": True},
                langgraph_thread_id=run_id,
            )
        )
        for seq in event_seqs:
            session.add(
                RunEvent(
                    run_id=run_id,
                    seq=seq,
                    type=RunEventType.TOOL_CALLED if seq % 2 else RunEventType.EVIDENCE_ADDED,
                    payload={"seq": seq},
                )
            )
        await session.commit()


async def _admin_headers(client: httpx.AsyncClient) -> dict[str, str]:
    login = await api_login(client, "admin", "AdminBoot123!")
    assert login.status_code == 200
    token = login.json()["data"]["access_token"]
    change = await client.post(
        "/api/v1/auth/change-password",
        json={"old_password": "AdminBoot123!", "new_password": "AdminSse123!"},
        headers=auth_headers(token),
    )
    assert change.status_code == 200
    return auth_headers(change.json()["data"]["access_token"])


@pytest.fixture
async def admin(client: httpx.AsyncClient) -> dict[str, str]:
    return await _admin_headers(client)


# ---- SSE 帧解析辅助 ----


class _StreamCollector:
    """后台任务经 httpx 读 SSE 流（适用于会自然结束的流），逐行推入队列。"""

    def __init__(self) -> None:
        self.queue: asyncio.Queue[tuple[str, str]] = asyncio.Queue()
        self.task: asyncio.Task[None] | None = None

    async def cancel(self) -> None:
        if self.task is not None:
            self.task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.task


async def _open_stream(
    client: httpx.AsyncClient, url: str, headers: dict[str, str]
) -> tuple[_StreamCollector, int, str]:
    """打开 SSE 流并在后台读行；返回收集器、HTTP 状态码与 Content-Type。"""
    collector = _StreamCollector()

    async def _reader() -> None:
        try:
            async with client.stream("GET", url, headers=headers) as resp:
                collector.queue.put_nowait(("status", str(resp.status_code)))
                collector.queue.put_nowait(("ctype", resp.headers.get("content-type", "")))
                async for line in resp.aiter_lines():
                    collector.queue.put_nowait(("line", line))
        except Exception as exc:  # 流被取消/关闭产生的传输异常不影响断言
            collector.queue.put_nowait(("error", repr(exc)))
        finally:
            collector.queue.put_nowait(("done", ""))

    collector.task = asyncio.create_task(_reader())
    kind, value = await asyncio.wait_for(collector.queue.get(), timeout=5.0)
    assert kind == "status", f"expected status first, got {(kind, value)}"
    kind2, ctype = await asyncio.wait_for(collector.queue.get(), timeout=5.0)
    assert kind2 == "ctype"
    return collector, int(value), ctype


async def _read_frame(collector: _StreamCollector) -> list[str] | None:
    """读取一个完整 SSE 帧（空行分隔）；流结束返回 None。"""
    lines: list[str] = []
    while True:
        kind, value = await asyncio.wait_for(collector.queue.get(), timeout=5.0)
        if kind in ("done", "error"):
            return None
        if kind != "line":  # pragma: no cover - 防御
            continue
        if value == "":
            if lines:
                return lines
            continue
        lines.append(value)


def _frame_field(frame: list[str], prefix: str) -> str | None:
    for line in frame:
        if line.startswith(prefix):
            return line[len(prefix) :]
    return None


class _RawStream:
    """直接驱动 ASGI app 的流式读取器（绕过 httpx ASGITransport 的全量缓冲）。"""

    def __init__(
        self, task: asyncio.Task[None], queue: asyncio.Queue[dict[str, object]], status: int
    ) -> None:
        self.task = task
        self.queue = queue
        self.status = status
        self._text = ""
        self._closed = False

    async def readline(self, timeout: float = 5.0) -> str | None:
        """读取一行（不含换行）；流结束返回 None。"""
        while "\n" not in self._text:
            if self._closed:
                return None
            try:
                msg = await asyncio.wait_for(self.queue.get(), timeout)
            except TimeoutError:
                return None
            if msg["type"] == "http.response.body":
                self._text += str(msg.get("body", b"") or b"", "utf-8", errors="replace")
                if not msg.get("more_body", False):
                    self._closed = True
            elif msg["type"] == "http.disconnect":
                self._closed = True
        line, self._text = self._text.split("\n", 1)
        return line.rstrip("\r")

    async def close(self) -> None:
        self.task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await self.task


async def _open_raw_stream(app: object, path: str, headers: dict[str, str]) -> _RawStream:
    """以原生 ASGI 协议发起请求，逐消息消费响应体（用于运行中流的增量观测）。"""
    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "root_path": "",
        "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()],
        "client": ("testclient", 123),
        "server": ("testserver", 80),
    }
    messages: asyncio.Queue[dict[str, object]] = asyncio.Queue()

    async def receive() -> dict[str, object]:
        await asyncio.sleep(3600)  # 请求体已完：挂起直至被取消

    async def send(message: dict[str, object]) -> None:
        await messages.put(message)

    task = asyncio.create_task(app(scope, receive, send))  # type: ignore[operator]
    first = await asyncio.wait_for(messages.get(), timeout=5.0)
    assert first["type"] == "http.response.start", first
    stream = _RawStream(task, messages, int(first["status"]))  # type: ignore[arg-type]
    return stream


async def _read_raw_frame(stream: _RawStream) -> list[str] | None:
    """按空行分隔读取一个 SSE 帧；流结束返回 None。"""
    lines: list[str] = []
    while True:
        line = await stream.readline()
        if line is None:
            return lines or None
        if line == "":
            if lines:
                return lines
            continue
        lines.append(line)


# ---- JSON 向后兼容 ----


async def test_default_accept_returns_json_pagination(
    client: httpx.AsyncClient, admin: dict[str, str], db_session_factory
) -> None:
    await _seed_run(db_session_factory, run_id="run_sse1", status="completed")
    resp = await client.get(EVENTS_URL.format(run_id="run_sse1"), headers=admin)
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/json")
    body = resp.json()
    assert body["error"] is None
    assert [i["seq"] for i in body["data"]["items"]] == [1, 2]
    assert body["data"]["has_more"] is False


async def test_json_accept_header_still_json(
    client: httpx.AsyncClient, admin: dict[str, str], db_session_factory
) -> None:
    await _seed_run(db_session_factory, run_id="run_sse2", status="completed")
    resp = await client.get(
        EVENTS_URL.format(run_id="run_sse2"),
        headers={**admin, "Accept": "application/json"},
    )
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/json")


# ---- SSE 基本流：重放 + 终态关闭 ----


async def test_sse_replays_all_events_and_closes_on_terminal_run(
    client: httpx.AsyncClient, admin: dict[str, str], db_session_factory
) -> None:
    await _seed_run(db_session_factory, run_id="run_sse3", status="completed")
    collector, status_code, ctype = await _open_stream(
        client,
        EVENTS_URL.format(run_id="run_sse3"),
        headers={**admin, "Accept": "text/event-stream"},
    )
    try:
        assert status_code == 200
        assert "text/event-stream" in ctype
        # retry 提示帧
        retry_frame = await _read_frame(collector)
        assert retry_frame == ["retry: 3000"]
        # 事件帧：id=seq、event=run_event、data JSON
        seqs: list[int] = []
        while True:
            frame = await _read_frame(collector)
            assert frame is not None, "stream closed before end frame"
            event = _frame_field(frame, "event: ")
            if event == "end":
                assert _frame_field(frame, "id: ") is None  # end 帧无 id（非业务事件）
                data = json.loads(_frame_field(frame, "data: ") or "{}")
                assert data["reason"] == "run_finished"
                assert data["status"] == RunStatus.COMPLETED.value
                break
            assert event == "run_event"
            seqs.append(int(_frame_field(frame, "id: ")))
        assert seqs == [1, 2]
        # 流应在 end 帧后自然关闭
        assert await _read_frame(collector) is None
    finally:
        await collector.cancel()


async def test_sse_last_event_id_resumes_without_loss_or_duplication(
    client: httpx.AsyncClient, admin: dict[str, str], db_session_factory
) -> None:
    """Last-Event-ID=2 重连：只收到 seq>2，无丢失、无重复。"""
    await _seed_run(db_session_factory, run_id="run_sse4", status="completed", event_seqs=(1, 2, 3))
    collector, status_code, _ctype = await _open_stream(
        client,
        EVENTS_URL.format(run_id="run_sse4"),
        headers={**admin, "Accept": "text/event-stream", "Last-Event-ID": "2"},
    )
    try:
        assert status_code == 200
        assert await _read_frame(collector) == ["retry: 3000"]
        frame1 = await _read_frame(collector)
        assert frame1 is not None
        assert _frame_field(frame1, "id: ") == "3"
        end = await _read_frame(collector)
        assert end is not None and "event: end" in end
        assert await _read_frame(collector) is None  # 严格关闭
    finally:
        await collector.cancel()


# ---- 运行中 run：实时推送 + 终态收敛（原生 ASGI 驱动）----


async def test_sse_pushes_live_events_then_ends_on_terminal(
    app, admin: dict[str, str], db_session_factory
) -> None:
    await _seed_run(db_session_factory, run_id="run_sse5", status="running", event_seqs=(1, 2))
    stream = await _open_raw_stream(
        app,
        EVENTS_URL.format(run_id="run_sse5"),
        {**admin, "Accept": "text/event-stream"},
    )
    try:
        assert stream.status == 200
        assert await _read_raw_frame(stream) == ["retry: 3000"]
        assert _frame_field(await _read_raw_frame(stream) or [], "id: ") == "1"
        assert _frame_field(await _read_raw_frame(stream) or [], "id: ") == "2"

        # 运行期间追加事件并置终态 → 流增量推送新事件后以 end 帧关闭
        async with db_session_factory() as session:
            session.add(
                RunEvent(
                    run_id="run_sse5",
                    seq=3,
                    type=RunEventType.RUN_FINISHED,
                    payload={"seq": 3},
                )
            )
            run = await session.get(InvestigationRun, "run_sse5")
            assert run is not None
            run.status = RunStatus.COMPLETED.value
            await session.commit()

        frame3 = await _read_raw_frame(stream)
        assert frame3 is not None
        assert _frame_field(frame3, "id: ") == "3"
        data = json.loads(_frame_field(frame3, "data: ") or "{}")
        assert data["type"] == RunEventType.RUN_FINISHED.value
        end = await _read_raw_frame(stream)
        assert end is not None and "event: end" in end
        assert await _read_raw_frame(stream) is None  # 严格关闭
    finally:
        await stream.close()


# ---- 心跳：注释行，不产生伪造业务事件 ----


async def test_sse_heartbeat_is_comment_not_fake_event(
    app, admin: dict[str, str], db_session_factory
) -> None:
    """空事件运行中 run：0.3s 心跳窗口内收到 ': ping' 注释，无任何 run_event。"""
    await _seed_run(db_session_factory, run_id="run_sse6", status="running", event_seqs=())
    stream = await _open_raw_stream(
        app,
        EVENTS_URL.format(run_id="run_sse6"),
        {**admin, "Accept": "text/event-stream"},
    )
    try:
        assert stream.status == 200
        assert await _read_raw_frame(stream) == ["retry: 3000"]
        seen_ping = False
        for _ in range(3):
            frame = await _read_raw_frame(stream)
            assert frame is not None, "stream closed unexpectedly"
            if frame[0].startswith(":"):
                seen_ping = True
                assert frame[0] == ": ping"
            else:
                pytest.fail(f"expected heartbeat comment, got {frame}")
        assert seen_ping, "no heartbeat within window"
    finally:
        await stream.close()


# ---- 权限与参数校验 ----


async def test_sse_unauthenticated_rejected(client: httpx.AsyncClient) -> None:
    async with client.stream(
        "GET",
        EVENTS_URL.format(run_id="run_any"),
        headers={"Accept": "text/event-stream"},
    ) as resp:
        assert resp.status_code == 401
        body = await resp.aread()
        assert b"UNAUTHENTICATED" in body


async def test_sse_scope_enforced_disguised_404(
    client: httpx.AsyncClient, viewer_headers: dict[str, str], db_session_factory
) -> None:
    """无资产策略的只读用户订阅 → 404 伪装（不泄漏存在性）。"""
    await _seed_run(db_session_factory, run_id="run_sse7", status="running")
    async with client.stream(
        "GET",
        EVENTS_URL.format(run_id="run_sse7"),
        headers={**viewer_headers, "Accept": "text/event-stream"},
    ) as resp:
        assert resp.status_code == 404


async def test_sse_invalid_last_event_id_returns_400(
    client: httpx.AsyncClient, admin: dict[str, str]
) -> None:
    resp = await client.get(
        EVENTS_URL.format(run_id="run_any"),
        headers={**admin, "Accept": "text/event-stream", "Last-Event-ID": "abc"},
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "VALIDATION_ERROR"


async def test_sse_unknown_run_disguised_404(
    client: httpx.AsyncClient, admin: dict[str, str]
) -> None:
    resp = await client.get(
        EVENTS_URL.format(run_id="run_missing"),
        headers={**admin, "Accept": "text/event-stream"},
    )
    assert resp.status_code == 404
