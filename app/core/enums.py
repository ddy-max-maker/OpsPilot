"""受控枚举（StrEnum）。

存储约定（CONTRACTS §2 / DATA_MODEL §1.4）：状态枚举以小写蛇形 TEXT 落库，
由 PostgreSQL CHECK 约束兜底；本模块同时是 Pydantic 校验的合法值来源。
"""

from __future__ import annotations

from enum import StrEnum


class RoleCode(StrEnum):
    OPERATOR = "operator"
    APPROVER = "approver"
    ADMIN = "admin"
    VIEWER = "viewer"


class Environment(StrEnum):
    SANDBOX = "sandbox"
    STAGING = "staging"
    PRODUCTION = "production"


class AssetType(StrEnum):
    SERVICE = "service"
    CONTAINER = "container"
    DATABASE = "database"
    CACHE = "cache"
    GATEWAY = "gateway"
    HOST = "host"


class AssetStatus(StrEnum):
    ACTIVE = "active"
    RETIRED = "retired"


class Criticality(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class AssetEdgeRelation(StrEnum):
    DEPENDS_ON = "depends_on"
    RUNS_ON = "runs_on"
    SERVES = "serves"


class PolicyPermission(StrEnum):
    READ = "read"
    INVESTIGATE = "investigate"
    APPROVE = "approve"
    EXECUTE = "execute"


class Severity(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class AlertStatus(StrEnum):
    RECEIVED = "received"
    MAPPED = "mapped"
    IGNORED = "ignored"


class IncidentStatus(StrEnum):
    OPEN = "open"
    ACKNOWLEDGED = "acknowledged"
    INVESTIGATING = "investigating"
    MITIGATING = "mitigating"
    MONITORING = "monitoring"
    RESOLVED = "resolved"
    CLOSED = "closed"
    NEEDS_HUMAN = "needs_human"


class IncidentSource(StrEnum):
    MANUAL = "manual"
    ALERT = "alert"


class ActorType(StrEnum):
    USER = "user"
    AGENT = "agent"
    SYSTEM = "system"
    ACTION_SERVICE = "action_service"


class IdempotencyState(StrEnum):
    RECEIVED = "received"
    COMPLETED = "completed"


class OutboxStatus(StrEnum):
    PENDING = "pending"
    DISPATCHED = "dispatched"
    FAILED = "failed"
