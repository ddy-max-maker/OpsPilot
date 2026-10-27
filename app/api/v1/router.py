"""/api/v1 路由聚合。

D1-A 仅注册：auth / assets / health。
Incident 与 Webhook 接口属于 D1-B（CONTRACTS §5 已预留）。
"""

from __future__ import annotations

from fastapi import APIRouter

from app.api.v1 import assets, auth, health

api_router = APIRouter()
api_router.include_router(auth.router)
api_router.include_router(assets.router)
api_router.include_router(health.router)
