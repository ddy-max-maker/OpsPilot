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


# ---- D2：诊断运行（CONTRACTS §2.4–§2.7 / §2.16）----


class RunStatus(StrEnum):
    CREATED = "created"
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    WAITING_APPROVAL = "waiting_approval"
    NEEDS_HUMAN = "needs_human"
    FAILED = "failed"
    CANCELED = "canceled"


RUN_TERMINAL_STATUSES = frozenset(
    {RunStatus.COMPLETED, RunStatus.NEEDS_HUMAN, RunStatus.FAILED, RunStatus.CANCELED}
)


class RunStopReason(StrEnum):
    COMPLETED = "completed"
    BUDGET_EXHAUSTED = "budget_exhausted"
    NO_NEW_EVIDENCE = "no_new_evidence"
    UNKNOWN_EXIT = "unknown_exit"
    CANCELED = "canceled"
    ERROR = "error"


class RunEventType(StrEnum):
    RUN_STARTED = "run_started"
    NODE_ENTERED = "node_entered"
    NODE_EXITED = "node_exited"
    TOOL_CALLED = "tool_called"
    TOOL_RETURNED = "tool_returned"
    EVIDENCE_ADDED = "evidence_added"
    HYPOTHESIS_ADDED = "hypothesis_added"
    BUDGET_WARNING = "budget_warning"
    ERROR = "error"
    RUN_FINISHED = "run_finished"


class EvidenceSourceType(StrEnum):
    METRIC = "metric"
    LOG = "log"
    TRACE = "trace"
    HEALTH = "health"
    INFRA = "infra"
    KNOWLEDGE = "knowledge"


class EvidenceStatus(StrEnum):
    OK = "ok"
    ERROR = "error"
    TIMEOUT = "timeout"
    UNAVAILABLE = "unavailable"


class HypothesisStatus(StrEnum):
    CANDIDATE = "candidate"
    SUPPORTED = "supported"
    REFUTED = "refuted"
    INSUFFICIENT_DATA = "insufficient_data"


class Confidence(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class ReportStatus(StrEnum):
    FINAL = "final"
    AMENDED = "amended"


class ConnectorInstanceStatus(StrEnum):
    ENABLED = "enabled"
    DISABLED = "disabled"


class AgentProvider(StrEnum):
    """模型 Provider（IMPLEMENTATION_PLAN §4#6：部署期锁定一个支持工具调用的模型）。"""

    FAKE = "fake"
    ANTHROPIC = "anthropic"
    GOOGLE = "google"
