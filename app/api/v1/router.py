"""/api/v1 路由聚合。

D1-A：auth / assets / health。
D1-B：incidents / webhooks / audit-logs（CONTRACTS §5）。
D2：runs（investigation API）。
"""

from __future__ import annotations

from fastapi import APIRouter

from app.api.v1 import assets, audit, auth, health, incidents, runs, webhooks

api_router = APIRouter()
api_router.include_router(auth.router)
api_router.include_router(assets.router)
api_router.include_router(health.router)
api_router.include_router(incidents.router)
api_router.include_router(webhooks.router)
api_router.include_router(audit.router)
api_router.include_router(runs.router)
