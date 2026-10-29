// API 模块：按后端资源组织（路径与 app/api/v1 一致）
import { http } from "./http";
import type {
  Approval, Asset, AuditEvent, Capability, DryRunResult, Environment,
  EvaluationRun, Evidence, Execution, Incident, Integration, KnowledgeDocument, KnowledgeHit,
  MeData, Page, Proposal, ProposalDetail, Report, Run, RunEvent, Runbook, SuiteSummary,
  TokenData, Verification,
} from "../types";

// ---------- auth ----------
export const authApi = {
  login: (username: string, password: string) =>
    http.post<TokenData>("/api/v1/auth/login", { body: { username, password } }),
  me: () => http.get<MeData>("/api/v1/auth/me"),
  changePassword: (old_password: string, new_password: string) =>
    http.post<{ status: string; access_token: string }>("/api/v1/auth/change-password", {
      body: { old_password, new_password },
    }),
};

// ---------- assets ----------
export interface AssetQuery extends Record<string, unknown> {
  environment?: string; asset_type?: string; status?: string; limit?: number; cursor?: string;
}
export const assetsApi = {
  list: (query?: AssetQuery) => http.get<Page<Asset>>("/api/v1/assets", { query }),
  create: (body: Record<string, unknown>) => http.post<Asset>("/api/v1/assets", { body }),
  update: (id: string, body: Record<string, unknown>) =>
    http.patch<Asset>(`/api/v1/assets/${id}`, { body }),
  bindings: (assetId: string) =>
    http.get<{ items: Array<{ id: string; asset_id: string; instance_id: string; binding_kind: string; config: Record<string, unknown> | null }> }>(
      `/api/v1/assets/${assetId}/observability-bindings`),
  upsertBinding: (assetId: string, body: Record<string, unknown>) =>
    http.put<Record<string, string>>(`/api/v1/assets/${assetId}/observability-bindings`, { body }),
  deleteBinding: (assetId: string, kind: string) =>
    http.delete<{ deleted: boolean }>(`/api/v1/assets/${assetId}/observability-bindings/${kind}`),
};

// ---------- incidents ----------
export interface IncidentQuery extends Record<string, unknown> {
  status?: string; severity?: string; environment?: string; primary_asset_id?: string;
  limit?: number; cursor?: string;
}
export const incidentsApi = {
  list: (query?: IncidentQuery) => http.get<Page<Incident>>("/api/v1/incidents", { query }),
  get: (id: string) => http.get<Incident>(`/api/v1/incidents/${id}`),
  create: (body: Record<string, unknown>) =>
    http.post<Incident>("/api/v1/incidents", { body, idempotency: true }),
  acknowledge: (id: string) =>
    http.post<Incident>(`/api/v1/incidents/${id}/acknowledge`),
};

// ---------- runs / diagnostics ----------
export const runsApi = {
  start: (incidentId: string, body?: Record<string, unknown>) =>
    http.post<{ run_id: string; status: string; incident_id: string }>(
      `/api/v1/incidents/${incidentId}/investigations`, { body: body ?? {}, idempotency: true }),
  get: (runId: string) => http.get<Run>(`/api/v1/runs/${runId}`),
  events: (runId: string, afterSeq?: number, limit?: number) =>
    http.get<{ items: RunEvent[]; next_after_seq: number | null; has_more: boolean }>(
      `/api/v1/runs/${runId}/events`, { query: { after_seq: afterSeq, limit } }),
  cancel: (runId: string) => http.post<Run>(`/api/v1/runs/${runId}/cancel`),
  evidence: (runId: string, query?: { source_type?: string; status?: string; limit?: number }) =>
    http.get<Page<Evidence>>(`/api/v1/runs/${runId}/evidence`, { query }),
  report: (runId: string) => http.get<Report>(`/api/v1/runs/${runId}/report`),
};

// ---------- knowledge ----------
export const knowledgeApi = {
  documents: () =>
    http.get<{ documents: KnowledgeDocument[] }>("/api/v1/knowledge/documents"),
  document: (id: string) => http.get<KnowledgeDocument>(`/api/v1/knowledge/documents/${id}`),
  create: (body: Record<string, unknown>) =>
    http.post<{ document_id: string; version_id: string; status: string }>(
      "/api/v1/knowledge/documents", { body }),
  addVersion: (docId: string, body: Record<string, unknown>) =>
    http.post<{ version_id: string; version_no: number }>(
      `/api/v1/knowledge/documents/${docId}/versions`, { body }),
  review: (versionId: string) =>
    http.post<{ version_id: string; status: string; reviewed_by: string }>(
      `/api/v1/knowledge/versions/${versionId}/review`),
  publish: (versionId: string) =>
    http.post<{ version_id: string; status: string; published_at: string; rag_mode: string }>(
      `/api/v1/knowledge/versions/${versionId}/publish`),
  retire: (versionId: string) =>
    http.post<{ version_id: string; status: string }>(
      `/api/v1/knowledge/versions/${versionId}/retire`),
  search: (query: string, environment?: string, limit?: number) =>
    http.get<{ mode: string; latency_ms: number; hits: KnowledgeHit[]; route_ranks?: unknown }>(
      "/api/v1/knowledge/search", { query: { query, environment, limit } }),
  fromReport: (runId: string) =>
    http.post<{ document_id: string; version_id: string }>(
      `/api/v1/knowledge/from-report/${runId}`),
};

// ---------- runbooks ----------
export const runbooksApi = {
  list: () => http.get<{ runbooks: Runbook[] }>("/api/v1/runbooks"),
  get: (id: string) => http.get<Runbook>(`/api/v1/runbooks/${id}`),
  create: (body: Record<string, unknown>) =>
    http.post<{ runbook_id: string; version_id: string; status: string }>("/api/v1/runbooks", { body }),
  addVersion: (id: string, body: Record<string, unknown>) =>
    http.post<{ version_id: string; version_no: number }>(`/api/v1/runbooks/${id}/versions`, { body }),
  publish: (versionId: string) =>
    http.post<{ version_id: string; status: string }>(`/api/v1/runbooks/versions/${versionId}/publish`),
  retire: (versionId: string) =>
    http.post<{ version_id: string; status: string }>(`/api/v1/runbooks/versions/${versionId}/retire`),
};

// ---------- proposals ----------
export const proposalsApi = {
  list: (query?: { run_id?: string; incident_id?: string; status?: string; limit?: number }) =>
    http.get<{ proposals: Proposal[] }>("/api/v1/proposals", { query }),
  get: (id: string) => http.get<ProposalDetail>(`/api/v1/proposals/${id}`),
  create: (body: Record<string, unknown>) =>
    http.post<Proposal>("/api/v1/proposals", { body, idempotency: true }),
  dryRun: (id: string) => http.post<DryRunResult>(`/api/v1/proposals/${id}/dry-run`),
  approve: (id: string, comment?: string) =>
    http.post<{ proposal: Proposal; approval: Approval }>(`/api/v1/proposals/${id}/approve`, {
      body: { comment },
    }),
  reject: (id: string, comment?: string) =>
    http.post<{ proposal: Proposal; approval: Approval }>(`/api/v1/proposals/${id}/reject`, {
      body: { comment },
    }),
  execute: (id: string) =>
    http.post<{ proposal: Proposal; execution: Execution }>(`/api/v1/proposals/${id}/execute`),
};

// ---------- integrations / capabilities ----------
export const integrationsApi = {
  list: () => http.get<{ items: Array<Integration & { config: Record<string, unknown> | null; tests?: Record<string, unknown> }> }>("/api/v1/integrations"),
  create: (body: Record<string, unknown>) => http.post<Integration>("/api/v1/integrations", { body }),
  test: (id: string) =>
    http.post<Record<string, unknown>>(`/api/v1/integrations/${id}/test`),
  enable: (id: string) => http.post<Record<string, unknown>>(`/api/v1/integrations/${id}/enable`),
  disable: (id: string) => http.post<Record<string, unknown>>(`/api/v1/integrations/${id}/disable`),
  capabilities: () =>
    http.get<{ capabilities: Capability[]; manifest_version: string }>("/api/v1/capabilities"),
  testCapability: (key: string, assetId?: string) =>
    http.post<Record<string, unknown>>(`/api/v1/capabilities/${key}/tests`, { body: { asset_id: assetId } }),
};

// ---------- evaluations ----------
export const evaluationsApi = {
  suites: () =>
    http.get<{ items: SuiteSummary[]; dataset_version: string; dataset_digest: string; seed_stats: Record<string, unknown> }>(
      "/api/v1/evaluations/suites"),
  createRun: (suiteId: string, wait?: boolean) =>
    http.post<{ run_id: string; suite_id: string; status: string }>("/api/v1/evaluations/runs", {
      body: { suite_id: suiteId, wait: wait ?? false },
    }),
  getRun: (runId: string) => http.get<EvaluationRun>(`/api/v1/evaluations/runs/${runId}`),
};

// ---------- audit ----------
export const auditApi = {
  list: (query?: { actor_type?: string; action?: string; limit?: number; cursor?: string }) =>
    http.get<Page<AuditEvent>>("/api/v1/audit-logs", { query }),
};

// ---------- 便捷：环境校验常量 ----------
export const ENVIRONMENTS: Environment[] = ["sandbox", "staging", "production"];
export type { Verification };
