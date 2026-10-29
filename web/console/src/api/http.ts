// 统一 HTTP 客户端：Envelope 解包、JWT、401/403/404/业务错误处理、幂等键
import type { Envelope, ErrorBody } from "../types";

const TOKEN_KEY = "opspilot_token";

export function getToken(): string | null {
  return localStorage.getItem(TOKEN_KEY);
}
export function setToken(token: string | null): void {
  if (token) localStorage.setItem(TOKEN_KEY, token);
  else localStorage.removeItem(TOKEN_KEY);
}

export class ApiError extends Error {
  readonly status: number;
  readonly code: string;
  readonly details: Record<string, unknown> | null;
  readonly requestId: string;
  constructor(status: number, body: ErrorBody, requestId: string) {
    super(body.message || `HTTP ${status}`);
    this.status = status;
    this.code = body.code || "INTERNAL";
    this.details = body.details ?? null;
    this.requestId = requestId;
  }
}

export function isAuthError(e: unknown): e is ApiError {
  return e instanceof ApiError && (e.status === 401 || e.status === 403);
}

type Opts = {
  method?: string;
  body?: unknown;
  query?: Record<string, unknown>;
  headers?: Record<string, string>;
  idempotency?: boolean; // 生成并携带 Idempotency-Key（重试同一次调用复用）
};

function buildUrl(path: string, query?: Opts["query"]): string {
  if (!query) return path;
  const params = new URLSearchParams();
  for (const [k, v] of Object.entries(query)) {
    if (v !== undefined && v !== null && v !== "") params.set(k, String(v));
  }
  const qs = params.toString();
  return qs ? `${path}?${qs}` : path;
}

let onUnauthorized: (() => void) | null = null;
export function setUnauthorizedHandler(fn: () => void): void {
  onUnauthorized = fn;
}

async function request<T>(path: string, opts: Opts = {}): Promise<T> {
  const headers: Record<string, string> = { Accept: "application/json", ...(opts.headers ?? {}) };
  const token = getToken();
  if (token) headers.Authorization = `Bearer ${token}`;
  if (opts.body !== undefined) headers["Content-Type"] = "application/json";
  if (opts.idempotency) headers["Idempotency-Key"] = crypto.randomUUID();

  let resp: Response;
  try {
    resp = await fetch(buildUrl(path, opts.query), {
      method: opts.method ?? "GET",
      headers,
      body: opts.body !== undefined ? JSON.stringify(opts.body) : undefined,
    });
  } catch {
    throw new ApiError(0, { code: "NETWORK", message: "无法连接后端服务" }, "");
  }

  let envelope: Envelope<T> | null = null;
  try {
    envelope = (await resp.json()) as Envelope<T>;
  } catch {
    /* 非 JSON 响应 */
  }

  if (resp.ok && envelope && envelope.error === null) {
    return envelope.data as T;
  }

  const errBody: ErrorBody =
    envelope?.error ?? { code: "INTERNAL", message: `HTTP ${resp.status}` };
  if (resp.status === 401) {
    setToken(null);
    onUnauthorized?.();
  }
  throw new ApiError(resp.status, errBody, envelope?.request_id ?? "");
}

export const http = {
  get: <T>(path: string, opts?: Opts) => request<T>(path, { ...opts, method: "GET" }),
  post: <T>(path: string, opts?: Opts) => request<T>(path, { ...opts, method: "POST" }),
  patch: <T>(path: string, opts?: Opts) => request<T>(path, { ...opts, method: "PATCH" }),
  put: <T>(path: string, opts?: Opts) => request<T>(path, { ...opts, method: "PUT" }),
  delete: <T>(path: string, opts?: Opts) => request<T>(path, { ...opts, method: "DELETE" }),
};
