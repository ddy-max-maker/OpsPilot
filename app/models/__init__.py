"""SQLAlchemy 2 声明式模型（持久化层，跨领域共享）。

表结构严格对齐 docs/DATA_MODEL.md v1.1 §3（D1 + D2 清单）；
枚举列使用 TEXT + CHECK 约束（DATA_MODEL §1.4 实现取舍）。
"""

from app.models.alert import Alert
from app.models.asset import Asset, AssetEdge
from app.models.audit import AuditEvent
from app.models.base import Base
from app.models.connector import ConnectorInstance, ConnectorType, SecretRef
from app.models.evidence import DiagnosticReport, Evidence, Hypothesis
from app.models.incident import Incident
from app.models.outbox import ApiIdempotencyKey, OutboxEvent
from app.models.registry import AssetObservabilityBinding, CapabilityManifest, CapabilityTestRun
from app.models.run import InvestigationRun, RunEvent
from app.models.user import SYSTEM_USER_ID, AssetAccessPolicy, Role, User, UserRole

__all__ = [
    "SYSTEM_USER_ID",
    "Alert",
    "ApiIdempotencyKey",
    "Asset",
    "AssetAccessPolicy",
    "AssetEdge",
    "AssetObservabilityBinding",
    "AuditEvent",
    "Base",
    "CapabilityManifest",
    "CapabilityTestRun",
    "ConnectorInstance",
    "ConnectorType",
    "DiagnosticReport",
    "Evidence",
    "Hypothesis",
    "Incident",
    "InvestigationRun",
    "OutboxEvent",
    "Role",
    "RunEvent",
    "SecretRef",
    "User",
    "UserRole",
]
