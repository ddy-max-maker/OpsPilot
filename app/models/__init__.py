"""SQLAlchemy 2 声明式模型（持久化层，跨领域共享）。

表结构严格对齐 docs/DATA_MODEL.md v1.1 §3（D1 清单）；
枚举列使用 TEXT + CHECK 约束（DATA_MODEL §1.4 实现取舍）。
"""

from app.models.alert import Alert
from app.models.asset import Asset, AssetEdge
from app.models.audit import AuditEvent
from app.models.base import Base
from app.models.incident import Incident
from app.models.outbox import ApiIdempotencyKey, OutboxEvent
from app.models.user import SYSTEM_USER_ID, AssetAccessPolicy, Role, User, UserRole

__all__ = [
    "Alert",
    "ApiIdempotencyKey",
    "SYSTEM_USER_ID",
    "Asset",
    "AssetAccessPolicy",
    "AssetEdge",
    "AuditEvent",
    "Base",
    "Incident",
    "OutboxEvent",
    "Role",
    "User",
    "UserRole",
]
