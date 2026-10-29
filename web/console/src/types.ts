// 后端契约类型（字段与 app/core/enums.py、app/core/responses.py 等逐一对应）

// ---------- 枚举（精确字符串值） ----------
export type RoleCode = "operator" | "approver" | "admin" | "viewer";
export type Environment = "sandbox" | "staging" | "production";
export type AssetType = "service" | "container" | "database" | "cache" | "gateway" | "host";
export type AssetStatus = "active" | "retired";
export type Criticality = "low" | "medium" | "high";
export type Severity = "low" | "medium" | "high" | "critical";
export type IncidentStatus =
  | "open" | "acknowledged" | "investigating" | "mitigating"
  | "monitoring" | "resolved" | "closed" | "needs_human";
export type IncidentSource = "manual" | "alert";

export type RunStatus =
  | "created" | "queued" | "running" | "completed"
  | "waiting_approval" | "needs_human" | "failed" | "canceled";
export type RunStopReason =
  | "completed" | "budget_exhausted" | "no_new_evidence" | "unknown_exit" | "canceled" | "error";
export type RunEventType =
  | "run_started" | "node_entered" | "node_exited" | "tool_called" | "tool_returned"
  | "evidence_added" | "hypothesis_added" | "budget_warning" | "error" | "run_finished";
export const RUN_TERMINAL_STATUSES: RunStatus[] = ["completed", "needs_human", "failed", "canceled"];

export type EvidenceSourceType = "metric" | "log" | "trace" | "health" | "infra" | "knowledge";
export type EvidenceStatus = "ok" | "error" | "timeout" | "unavailable";
export type HypothesisStatus = "candidate" | "supported" | "refuted" | "insufficient_data";
export type Confidence = "low" | "medium" | "high";
export type ReportStatus = "final" | "amended";

export type ProposalStatus =
  | "draft" | "pending_approval" | "approved" | "rejected" | "expired"
  | "executing" | "executed" | "execution_failed" | "execution_unknown"
  | "verified" | "verify_failed" | "verify_unknown" | "canceled";
export type ActionExecutionStatus =
  | "pending" | "pre_check_failed" | "running" | "succeeded" | "failed" | "unknown" | "skipped";
export type VerificationStatus = "in_progress" | "passed" | "failed" | "unknown";

export type KnowledgeDocType = "runbook" | "postmortem" | "guide";
export type KnowledgeSensitivity = "internal" | "restricted";
export type KnowledgeVersionStatus = "draft" | "reviewed" | "published" | "superseded" | "retired";
export type RunbookRiskLevel = "r0" | "r1";

export type RagMode = "off" | "text-only" | "hybrid";
export type ActorType = "user" | "agent" | "system" | "action_service";

// ---------- 响应包装 ----------
export interface ErrorBody {
  code: string;
  message: string;
  details?: Record<string, unknown> | null;
}
export interface Envelope<T> {
  request_id: string;
  data: T | null;
  error: ErrorBody | null;
}

// ---------- 认证 ----------
export interface TokenData {
  access_token: string;
  token_type: string;
  expires_in: number;
  user_id: string;
  username: string;
  must_change_password: boolean;
}
export interface MeData {
  id: string;
  username: string;
  email: string;
  display_name: string | null;
  is_active: boolean;
  must_change_password: boolean;
  roles: RoleCode[];
  asset_scope: {
    is_admin: boolean;
    all_assets: boolean;
    environments: string[];
    asset_ids: string[];
  };
}

// ---------- 资产 ----------
export interface Asset {
  id: string;
  name: string;
  environment: Environment;
  asset_type: AssetType;
  status: AssetStatus;
  criticality: Criticality;
  endpoints: {
    health_url?: string | null;
    metrics_ref?: string | null;
    logs_ref?: string | null;
    container_name?: string | null;
    health_check?: Record<string, unknown> | null;
  };
  owner: string | null;
  tags: string[];
  created_at: string;
  updated_at: string;
}
export interface Page<T> {
  items: T[];
  next_cursor: string | null;
  has_more: boolean;
}

// ---------- 事件 ----------
export interface Incident {
  id: string;
  title: string;
  description: string | null;
  environment: Environment;
  primary_asset_id: string;
  source: IncidentSource;
  alert_id: string | null;
  fingerprint: string | null;
  severity: Severity;
  status: IncidentStatus;
  created_by: string;
  resolved_at: string | null;
  created_at: string;
  updated_at: string;
}

// ---------- Run / 证据 / 报告 ----------
export interface Run {
  id: string;
  incident_id: string;
  status: RunStatus;
  stop_reason: RunStopReason | null;
  triggered_by: string;
  model_config_snapshot: Record<string, unknown>;
  budgets: Record<string, unknown>;
  usage: Record<string, unknown>;
  langgraph_thread_id: string | null;
  cancel_requested: boolean;
  started_at: string | null;
  finished_at: string | null;
  created_at: string;
  updated_at: string;
}
export interface RunEvent {
  id: number;
  run_id: string;
  seq: number;
  type: RunEventType;
  payload: Record<string, unknown> | null;
  created_at: string;
}
export interface Evidence {
  id: string;
  run_id: string;
  asset_id: string | null;
  environment: Environment | null;
  source_type: EvidenceSourceType;
  collector: string;
  observed_at: string | null;
  time_range: Record<string, unknown> | null;
  status: EvidenceStatus;
  data_ref: Record<string, unknown>;
  summary_redacted: string | null;
  correlation: Record<string, unknown> | null;
  error_code: string | null;
  truncated: boolean;
  schema_version: string;
  created_at: string;
}
export interface Hypothesis {
  id: string;
  rank: number;
  statement: string;
  status: HypothesisStatus;
  supporting_evidence_ids: string[];
  contradicting_evidence_ids: string[];
  missing_information: string[] | null;
  confidence: Confidence;
  created_at: string;
}
export interface Report {
  id: string;
  run_id: string;
  version_no: number;
  status: ReportStatus;
  conclusion: { summary?: string; key_findings?: Array<Record<string, unknown>> } | null;
  hypotheses: Hypothesis[];
  evidence_refs: string[];
  unknown_note: { is_unknown: boolean; reason?: string; missing_information?: string[] } | null;
  generator: { model?: string; model_version?: string; prompt_version?: string } | null;
  generated_at: string;
  created_at: string;
}

// ---------- 知识 ----------
export interface KnowledgeVersion {
  id: string;
  document_id: string;
  version_no: number;
  status: KnowledgeVersionStatus;
  change_note: string | null;
  created_by: string;
  reviewed_by: string | null;
  published_at: string | null;
  valid_from: string | null;
  valid_until: string | null;
  sensitivity: KnowledgeSensitivity;
  applies_to: Record<string, unknown> | null;
  embedding_model: string | null;
  vector_dimension: number | null;
  created_at: string;
}
export interface KnowledgeDocument {
  id: string;
  doc_type: KnowledgeDocType;
  title: string;
  source_incident_id: string | null;
  owner: string;
  tags: string[];
  versions: KnowledgeVersion[];
}
export interface KnowledgeHit {
  version_id: string;
  document_id: string;
  doc_type: KnowledgeDocType;
  title: string;
  version_no: number;
  published_at: string | null;
  content: string;
  scores: Record<string, unknown>;
  final_rank: number | null;
  rrf_score: number | null;
}

// ---------- Runbook ----------
export interface RunbookVersion {
  id: string;
  runbook_id: string;
  version_no: number;
  status: "draft" | "published" | "retired";
  steps: Record<string, unknown>[];
  preconditions: Record<string, unknown> | null;
  parameters_schema: Record<string, unknown> | null;
  allowed_environments: string[];
  allowlist_action_keys: string[];
  risk_level: RunbookRiskLevel;
  published_at: string | null;
}
export interface Runbook {
  id: string;
  name: string;
  description: string | null;
  risk_level: RunbookRiskLevel;
  status: "draft" | "published" | "retired";
  versions: RunbookVersion[];
}

// ---------- Proposal / 执行 / 验证 ----------
export interface Proposal {
  id: string;
  run_id: string;
  incident_id: string;
  runbook_version_id: string;
  target_asset_id: string;
  environment: Environment;
  parameters: Record<string, unknown>;
  parameter_hash: string;
  risk_level: string;
  preconditions_result: Record<string, unknown> | null;
  status: ProposalStatus;
  created_by: string;
  created_by_user: string | null;
  expires_at: string | null;
  created_at: string;
  updated_at: string;
}
export interface Approval {
  id: string;
  proposal_id: string;
  decision: "approved" | "rejected";
  decided_by: string;
  proposal_snapshot: Record<string, unknown> | null;
  expires_at: string | null;
  comment: string | null;
  decided_at: string;
}
export interface Execution {
  id: string;
  proposal_id: string;
  action_key: string;
  idempotency_key: string;
  status: ActionExecutionStatus;
  executor: string | null;
  external_ref: string | null;
  result_detail: Record<string, unknown> | null;
  started_at: string | null;
  finished_at: string | null;
  created_at: string;
}
export interface Verification {
  id: string;
  execution_id: string;
  status: VerificationStatus;
  checks: Array<{ name?: string; asset_id?: string; expected?: unknown; observed?: unknown; pass?: boolean }> | null;
  observation_window: { start?: string; end?: string; sample_count?: number } | null;
  concluded_at: string | null;
  created_at: string;
}
export interface ProposalDetail {
  proposal: Proposal;
  approvals: Approval[];
  executions: Execution[];
  verifications: Verification[];
}
export interface DryRunResult {
  proposal_id: string;
  passed: boolean;
  checks: Array<{ name: string; passed: boolean; detail: string | null }>;
  preconditions_result: Record<string, unknown> | null;
}

// ---------- 集成 / 能力 ----------
export interface Integration {
  id: string;
  name: string;
  connector_type: string;
  status: "enabled" | "disabled";
}
export interface Capability {
  capability_id: string;
  connector_type: string;
  version: string;
  source_types: string[];
  read_only: boolean;
  scopes: string[];
  rate_limit: string | null;
  timeout_ms: number | null;
  enabled: boolean;
  compatibility: Record<string, unknown>;
}
export interface Binding {
  id: string;
  asset_id: string;
  instance_id: string;
  binding_kind: "metrics" | "logs" | "traces" | "health";
  config: Record<string, unknown> | null;
}

// ---------- 评估 ----------
export interface SuiteSummary {
  id: string;
  name: string;
  kind: string;
  description: string | null;
  case_count: number;
  holdout_count: number;
}
export interface CaseResultView {
  case_id: string;
  case_key: string;
  title: string;
  holdout: boolean;
  passed: boolean;
  metrics: Record<string, number | string | boolean>;
  detail: Record<string, unknown> | null;
}
export interface EvaluationRun {
  id: string;
  suite_id: string;
  suite_name: string;
  status: "pending" | "running" | "completed" | "failed";
  config: Record<string, unknown>;
  started_at: string | null;
  finished_at: string | null;
  error: string | null;
  summary: {
    cases_total: number;
    passed: number;
    failed: number;
    top1_hit_rate: number;
    top3_hit_rate: number;
    unknown_exit_rate: number;
    knowledge_misuse_total: number;
    wrong_reference_total: number;
    p50_latency_ms: number;
    p95_latency_ms: number;
    avg_tool_calls: number;
    gate_unauthorized_success: number;
    gate_repeated_side_effects: number;
    gate_verification_misreport: number;
    gates_passed: boolean;
  };
  results: CaseResultView[];
}

// ---------- 审计 ----------
export interface AuditEvent {
  id: number;
  actor_type: ActorType;
  actor_id: string;
  action: string;
  resource_type: string | null;
  resource_id: string | null;
  environment: string | null;
  detail: Record<string, unknown> | null;
  request_id: string;
  created_at: string;
}
