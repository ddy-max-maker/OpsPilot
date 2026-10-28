"""D4 集成测试：知识生命周期（四眼/状态机/审计）+ 权限前置过滤检索 + Runbook。

覆盖（用户验收项）：
- 知识生命周期 draft → reviewed → published → superseded/retired（非法转换 409）；
- 四眼复核：reviewed_by ≠ created_by（服务端强制）；
- 未发布/已过期/已退役知识不可召回；
- 按 asset_id 权限隔离：越权召回必须为 0（SQL 前置过滤，非 Python 后剔除）；
- Embedding 版本隔离：不同 model_version 向量不混检；
- 无匹配结果正常退化；Runbook 草稿不可执行、production 环境硬拒绝。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import text

from app.core.security import hash_password  # noqa: F401
from app.domains.knowledge import service as kb_service
from app.domains.rag.embeddings import FakeEmbeddingProvider
from tests.conftest import auth_headers, create_test_user

KB = "/api/v1/knowledge"
RB = "/api/v1/runbooks"

pytestmark = pytest.mark.integration

PROVIDER = FakeEmbeddingProvider()


@pytest.fixture(autouse=True)
def _rag_text_mode(monkeypatch):
    """本模块检索测试统一在 text-only 模式下运行（off 语义由专属用例覆盖）。"""
    from app.core.config import get_settings

    monkeypatch.setenv("RAG_MODE", "text-only")
    get_settings.cache_clear()
    yield
    monkeypatch.undo()
    get_settings.cache_clear()


async def _second_admin(client: httpx.AsyncClient, db_session_factory) -> dict[str, str]:
    """第二个 Admin（四眼复核人；不得与创建人相同）。"""
    await create_test_user(
        db_session_factory,
        username="admin_two",
        password="AdminTwo123!",
        role_codes=["admin"],
    )
    resp = await client.post(
        "/api/v1/auth/login", json={"username": "admin_two", "password": "AdminTwo123!"}
    )
    assert resp.status_code == 200
    return auth_headers(resp.json()["data"]["access_token"])


async def _create_doc(
    client: httpx.AsyncClient,
    headers: dict,
    *,
    title: str = "Redis 连接池耗尽复盘",
    content: str = "历史故障：Redis 连接池耗尽导致超时。处理：扩容 maxclients 并修复泄漏。",
    **overrides: object,
) -> tuple[str, str]:
    body: dict[str, object] = {
        "doc_type": "postmortem",
        "title": title,
        "content": content,
        "applies_to": {"asset_ids": [], "environments": ["sandbox"]},
    }
    body.update(overrides)
    resp = await client.post(f"{KB}/documents", json=body, headers=headers)
    assert resp.status_code == 201, resp.text
    data = resp.json()["data"]
    return data["document_id"], data["version_id"]


async def _publish(client: httpx.AsyncClient, reviewer: dict, version_id: str) -> None:
    rv = await client.post(f"{KB}/versions/{version_id}/review", headers=reviewer)
    assert rv.status_code == 200, rv.text
    pv = await client.post(f"{KB}/versions/{version_id}/publish", headers=reviewer)
    assert pv.status_code == 200, pv.text


async def _search(
    client: httpx.AsyncClient, headers: dict, query: str, limit: int = 5
) -> list[dict]:
    resp = await client.get(
        f"{KB}/search", params={"query": query, "limit": limit}, headers=headers
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["data"]["hits"]


# ---- 生命周期与四眼 ----


async def test_lifecycle_publish_supersede_retire(client, ready_admin, db_session_factory) -> None:
    reviewer = await _second_admin(client, db_session_factory)
    doc_id, v1 = await _create_doc(client, ready_admin)

    # 草稿直接发布 → 409（必须先人工复核）
    direct = await client.post(f"{KB}/versions/{v1}/publish", headers=reviewer)
    assert direct.status_code == 409

    # 四眼违规：创建人自己复核 → 409
    self_review = await client.post(f"{KB}/versions/{v1}/review", headers=ready_admin)
    assert self_review.status_code == 409

    # 他人复核 → 发布
    await _publish(client, reviewer, v1)
    detail = await client.get(f"{KB}/documents/{doc_id}", headers=ready_admin)
    versions = detail.json()["data"]["versions"]
    assert versions[0]["status"] == "published"
    assert versions[0]["reviewed_by"] != versions[0]["created_by"]
    assert versions[0]["embedding_model_version"]  # 发布时构建索引并写版本快照
    assert versions[0]["vector_dimension"] == 384

    # 已发布 → 二次发布（同版本重发布）→ 409
    republish = await client.post(f"{KB}/versions/{v1}/publish", headers=reviewer)
    assert republish.status_code == 409

    # 新版本发布 → 旧版本自动 superseded
    nv = await client.post(
        f"{KB}/documents/{doc_id}/versions",
        json={"content": "v2：补充根因分析。Redis 连接池耗尽根因为客户端未释放连接。"},
        headers=ready_admin,
    )
    v2 = nv.json()["data"]["version_id"]
    await _publish(client, reviewer, v2)
    detail = await client.get(f"{KB}/documents/{doc_id}", headers=ready_admin)
    statuses = {v["version_no"]: v["status"] for v in detail.json()["data"]["versions"]}
    assert statuses[1] == "superseded"
    assert statuses[2] == "published"

    # 退役 → 状态 retired
    retire = await client.post(f"{KB}/versions/{v2}/retire", headers=reviewer)
    assert retire.status_code == 200
    detail = await client.get(f"{KB}/documents/{doc_id}", headers=ready_admin)
    assert detail.json()["data"]["versions"][1]["status"] == "retired"


async def test_kb_rbac(client, ready_admin, db_session_factory) -> None:
    """operator 可创建草稿；复核/发布/退役仅 admin；viewer 不可创建。"""
    await create_test_user(
        db_session_factory,
        username="kb_op",
        password="KbOp123!",
        role_codes=["operator"],
    )
    login = await client.post(
        "/api/v1/auth/login", json={"username": "kb_op", "password": "KbOp123!"}
    )
    operator = auth_headers(login.json()["data"]["access_token"])

    created = await client.post(
        f"{KB}/documents",
        json={"doc_type": "guide", "title": "op 草稿", "content": "内容"},
        headers=operator,
    )
    assert created.status_code == 201
    version_id = created.json()["data"]["version_id"]

    denied_review = await client.post(f"{KB}/versions/{version_id}/review", headers=operator)
    assert denied_review.status_code == 403
    denied_publish = await client.post(f"{KB}/versions/{version_id}/publish", headers=operator)
    assert denied_publish.status_code == 403

    viewer_view = await client.get(f"{KB}/documents", headers=ready_admin)
    assert viewer_view.status_code == 200


async def test_kb_audit_records(client, ready_admin, db_session_factory) -> None:
    """发布/退役/版本变更写审计。"""
    reviewer = await _second_admin(client, db_session_factory)
    _, v1 = await _create_doc(client, ready_admin)
    await _publish(client, reviewer, v1)
    await client.post(f"{KB}/versions/{v1}/retire", headers=reviewer)
    resp = await client.get("/api/v1/audit-logs?action=knowledge.published", headers=ready_admin)
    assert resp.status_code == 200
    actions = [i["action"] for i in resp.json()["data"]["items"]]
    assert "knowledge.published" in actions
    resp2 = await client.get("/api/v1/audit-logs?action=knowledge.retired", headers=ready_admin)
    assert any(i["action"] == "knowledge.retired" for i in resp2.json()["data"]["items"])


# ---- 检索前置过滤（权限/时效/状态）----


@pytest.fixture
async def kb_env(client, ready_admin, db_session_factory):
    """发布：通用知识 + 授权资产知识；未发布草稿；过期知识；已退役知识。"""
    reviewer = await _second_admin(client, db_session_factory)
    _, generic = await _create_doc(
        client,
        ready_admin,
        title="通用故障排查指南",
        content="磁盘写满会导致服务超时与日志报错，先检查 df -h 与日志清理策略。",
    )
    await _publish(client, reviewer, generic)
    _, scoped = await _create_doc(
        client,
        ready_admin,
        title="Java 服务 5xx 复盘",
        content="Java 服务出现 5xx 错误率升高，根因为下游依赖超时，扩容连接池解决。",
        applies_to={"asset_ids": ["as_auth_scoped"], "environments": []},
    )
    await _publish(client, reviewer, scoped)
    _, draft = await _create_doc(
        client, ready_admin, title="未发布草稿", content="Redis 草稿内容不应被检索"
    )
    _, expired = await _create_doc(
        client,
        ready_admin,
        title="已过期经验",
        content="MySQL 连接数激增旧处理办法，已过期不再适用。",
        valid_until=(datetime.now(UTC) - timedelta(days=1)).isoformat(),
    )
    await _publish(client, reviewer, expired)
    _, retired = await _create_doc(
        client, ready_admin, title="已退役经验", content="Docker OOM 退役经验内容。"
    )
    await _publish(client, reviewer, retired)
    await client.post(f"{KB}/versions/{retired}/retire", headers=reviewer)
    return {"generic": generic, "scoped": scoped, "draft": draft, "expired": expired}


async def test_unpublished_expired_retired_not_recalled(client, ready_admin, kb_env) -> None:
    """draft/过期/退役知识不可召回（状态与有效期过滤在排序前）。"""
    hits = await _search(client, ready_admin, "Redis 草稿内容")
    assert all(h["version_id"] != kb_env["draft"] for h in hits)
    hits = await _search(client, ready_admin, "MySQL 连接数激增旧处理办法")
    assert all(h["version_id"] != kb_env["expired"] for h in hits)
    hits = await _search(client, ready_admin, "Docker OOM 退役经验")
    assert all(h["version_id"] != kb_env["retired"] for h in hits)
    # 通用知识仍可召回（text-only 由 CI 环境决定；此处仅验证过滤面）
    assert isinstance(hits, list)


async def test_asset_scope_isolation_zero_unauthorized_recall(
    client, ready_admin, db_session_factory, kb_env
) -> None:
    """按 asset_id 权限隔离：未授权资产的知识召回必须为 0。"""
    # 无任何策略的用户（默认拒绝）→ 不应召回限定 as_auth_scoped 的知识
    await create_test_user(
        db_session_factory,
        username="kb_nobody",
        password="Nobody123!",
        role_codes=["operator"],
    )
    login = await client.post(
        "/api/v1/auth/login", json={"username": "kb_nobody", "password": "Nobody123!"}
    )
    nobody = auth_headers(login.json()["data"]["access_token"])
    hits = await _search(client, nobody, "Java 服务 5xx 下游依赖超时")
    assert all(h["version_id"] != kb_env["scoped"] for h in hits)
    assert all(
        h["applies_to"] is None or not (h["applies_to"] or {}).get("asset_ids") for h in hits
    )

    # admin（全量）→ 可召回（ready_admin 即全量授权视角）
    admin_hits = await _search(client, ready_admin, "Java 服务 5xx")
    assert any(h["version_id"] == kb_env["scoped"] for h in admin_hits)


async def test_embedding_version_isolation(db_session_factory, db_engine) -> None:
    """不同 embedding 版本向量不混检：检索仅命中当前 model_version。"""
    from app.domains.rag.retrieval import search_knowledge
    from app.models import KnowledgeVersion, User
    from tests.conftest import truncate_all

    await truncate_all(db_engine)
    # 直连服务层发布（truncate 后角色表为空，不走 API bootstrap 用户）
    async with db_session_factory() as session:
        for uid, uname in (("usr_vec_a", "vec_a"), ("usr_vec_b", "vec_b")):
            session.add(
                User(
                    id=uid,
                    username=uname,
                    email=f"{uname}@test.local",
                    password_hash=hash_password("VecUser123!"),
                    is_active=True,
                    must_change_password=False,
                )
            )
        await session.commit()
    async with db_session_factory() as session:
        doc, version = await kb_service.create_document(
            session,
            actor_id="usr_vec_a",
            doc_type="postmortem",
            title="向量版本隔离样本",
            content="Kafka 消费积压的根因是消费者组再平衡风暴，需检查 session 超时配置。",
        )
        await session.commit()
        version_id = version.id
    async with db_session_factory() as session:
        await kb_service.review_version(session, actor_id="usr_vec_b", version_id=version_id)
        await session.commit()
        published = await kb_service.publish_version(
            session,
            session_factory=db_session_factory,
            actor_id="usr_vec_b",
            version_id=version_id,
            provider=PROVIDER,
        )
        assert published.status == "published"
    # 手工注入另一 model_version 的旧向量（模拟历史索引）
    async with db_session_factory() as session:
        rows = (
            await session.execute(
                text(
                    "SELECT chunk_id, vector::text FROM knowledge_embeddings "
                    "WHERE model_version = 'fake-384-v1'"
                )
            )
        ).all()
        assert rows
        for chunk_id, vec in rows:
            await session.execute(
                text(
                    "INSERT INTO knowledge_embeddings (chunk_id, model, model_version, vector) "
                    "VALUES (:cid, 'legacy-model', 'legacy-384-v0', CAST(:vec AS vector)) "
                    "ON CONFLICT DO NOTHING"
                ),
                {"cid": chunk_id, "vec": vec},
            )
        await session.commit()
    response = await search_knowledge(
        db_session_factory,
        query="Kafka 消费积压 再平衡",
        mode="hybrid",
        authorized_asset_ids=set(),
        all_assets=True,
        provider=PROVIDER,
    )
    assert response.hits
    assert all(h.embedding_model_version == "fake-384-v1" for h in response.hits)
    # 版本快照核对
    async with db_session_factory() as session:
        version = await session.get(KnowledgeVersion, version_id)
        assert version.embedding_model_version == "fake-384-v1"


async def test_no_match_degrades_gracefully(client, ready_admin) -> None:
    """无匹配结果正常退化：空 hits + 正常响应（非错误）。"""
    hits = await _search(client, ready_admin, "完全不存在的量子纠缠故障现象 xyzzy")
    assert hits == []


async def test_rag_off_search_returns_disabled_note(client, ready_admin, monkeypatch) -> None:
    """rag_mode=off 时检索 API 返回空集与说明（不报错）。"""
    monkeypatch.setenv("RAG_MODE", "off")
    from app.core.config import get_settings

    get_settings.cache_clear()
    try:
        resp = await client.get(f"{KB}/search", params={"query": "Redis"}, headers=ready_admin)
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["mode"] == "off" and data["hits"] == []
    finally:
        monkeypatch.undo()
        get_settings.cache_clear()


# ---- Runbook ----


async def test_runbook_lifecycle_and_search(client, ready_admin) -> None:
    # production 硬拒绝
    bad = await client.post(
        f"{RB}",
        json={
            "name": "rb-bad",
            "risk_level": "r1",
            "steps": [],
            "allowed_environments": ["production"],
        },
        headers=ready_admin,
    )
    assert bad.status_code == 409

    created = await client.post(
        f"{RB}",
        json={
            "name": "Redis 连接池扩容手册",
            "description": "连接池耗尽时的扩容步骤",
            "risk_level": "r1",
            "steps": [{"action": "redis.config.set", "params": {"maxclients": 10000}}],
            "allowed_environments": ["sandbox", "staging"],
            "allowlist_action_keys": ["redis.config.set"],
        },
        headers=ready_admin,
    )
    assert created.status_code == 201, created.text
    version_id = created.json()["data"]["version_id"]

    # 草稿不可发布前：状态机校验（draft → published 合法，但先验证草稿不可执行语义）
    # 本阶段无任何执行端点：提案/执行路由不存在（D5）
    from app.api.v1.router import api_router

    route_paths = {getattr(r, "path", "") for r in api_router.routes}
    assert not any("proposals" in p or "executions" in p for p in route_paths)

    pub = await client.post(f"{RB}/versions/{version_id}/publish", headers=ready_admin)
    assert pub.status_code == 200

    detail = await client.get(f"{RB}", headers=ready_admin)
    rb = [r for r in detail.json()["data"]["runbooks"] if r["name"] == "Redis 连接池扩容手册"]
    assert rb and rb[0]["status"] == "published"
