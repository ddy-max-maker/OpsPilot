// SSE 流式客户端：fetch ReadableStream 解析 text/event-stream
// 认证经 Authorization header 携带（不把 JWT 放 URL）；Last-Event-ID 重放；自动重连；终态停止
import { getToken } from "../api/http";
import { RUN_TERMINAL_STATUSES, type RunEvent, type RunStatus } from "../types";

export interface SseHandlers {
  onEvent: (ev: RunEvent) => void;
  onEnd?: (reason: string, status?: RunStatus) => void;
  onError?: (message: string, recoverable: boolean) => void;
}

interface SseState {
  lastSeq: number; // Last-Event-ID（run_events.seq）
  closed: boolean; // 用户主动停止或终态
  controller: AbortController | null;
  reconnects: number;
}

const MAX_RECONNECTS = 12;

/**
 * 订阅 run 事件流。
 * - 先用 JSON 分页把 afterSeq 之前的存量事件拉齐（页面刷新恢复），
 *   再以 Accept: text/event-stream 打开 SSE，从 lastSeq 增量接收。
 * - 断线以 Last-Event-ID 头自动重连（服务端 retry: 3000ms 指导）。
 * - 收到 end 帧（run_finished / run_gone）或运行进入终态后停止订阅。
 */
export class RunEventStream {
  private state: SseState;
  private readonly runId: string;
  private readonly handlers: SseHandlers;

  constructor(runId: string, startAfterSeq: number, handlers: SseHandlers) {
    this.runId = runId;
    this.handlers = handlers;
    this.state = { lastSeq: startAfterSeq, closed: false, controller: null, reconnects: 0 };
  }

  get lastSeq(): number {
    return this.state.lastSeq;
  }

  stop(): void {
    this.state.closed = true;
    this.state.controller?.abort();
  }

  /** 启动：先补历史，再开流 */
  async start(): Promise<void> {
    // 历史（JSON 分页，不依赖 SSE）：页面刷新后由此恢复诊断状态
    let after = this.state.lastSeq;
    try {
      for (;;) {
        const page = await this.fetchJsonPage(after);
        for (const ev of page.items) this.ingest(ev);
        if (!page.has_more || page.next_after_seq === null) break;
        after = page.next_after_seq;
      }
    } catch (e) {
      this.handlers.onError?.(readableError(e), false);
      return;
    }
    if (this.state.closed) return;
    await this.openStream();
  }

  private async fetchJsonPage(afterSeq: number) {
    const { runsApi } = await import("../api");
    return runsApi.events(this.runId, afterSeq, 200);
  }

  private ingest(ev: RunEvent): void {
    if (ev.seq <= this.state.lastSeq) return; // 去重
    this.state.lastSeq = ev.seq;
    this.handlers.onEvent(ev);
  }

  private async openStream(): Promise<void> {
    const headers: Record<string, string> = {
      Accept: "text/event-stream",
      "Last-Event-ID": String(this.state.lastSeq),
    };
    const token = getToken();
    if (token) headers.Authorization = `Bearer ${token}`;

    const controller = new AbortController();
    this.state.controller = controller;

    let resp: Response;
    try {
      resp = await fetch(`/api/v1/runs/${this.runId}/events`, {
        method: "GET",
        headers,
        signal: controller.signal,
      });
    } catch {
      return this.scheduleReconnect("连接中断");
    }

    if (!resp.ok || !resp.body) {
      // 404/401 等为不可恢复错误，交由上层展示
      this.handlers.onError?.(`SSE 连接失败（HTTP ${resp.status}）`, resp.status >= 500);
      return;
    }

    const reader = resp.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";

    try {
      for (;;) {
        const { done, value } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });

        // SSE 帧以空行分隔
        let idx: number;
        while ((idx = buffer.indexOf("\n\n")) >= 0) {
          const rawFrame = buffer.slice(0, idx);
          buffer = buffer.slice(idx + 2);
          this.handleFrame(rawFrame);
        }
      }
    } catch {
      /* abort 或网络错误 */
    }

    if (!this.state.closed) this.scheduleReconnect("连接断开");
  }

  /** 解析单个 SSE 帧 */
  private handleFrame(raw: string): void {
    let event = "message";
    const dataLines: string[] = [];
    for (const line of raw.split("\n")) {
      if (line.startsWith(":")) continue; // 心跳注释帧 : ping
      if (line.startsWith("event:")) event = line.slice(6).trim();
      else if (line.startsWith("data:")) dataLines.push(line.slice(5).trim());
      // id 帧：重连 Last-Event-ID 由 openStream 依据已摄入 seq 自行设置，此处无需读取
    }
    if (!dataLines.length) return;
    let data: Record<string, unknown>;
    try {
      data = JSON.parse(dataLines.join("\n"));
    } catch {
      return;
    }

    if (event === "end") {
      this.state.closed = true;
      this.state.controller?.abort();
      this.handlers.onEnd?.(String(data.reason ?? ""), data.status as RunStatus | undefined);
      return;
    }
    if (event === "run_event") {
      const ev = data as unknown as RunEvent;
      this.ingest(ev);
      // 终态兜底：run_finished 事件即最后一条
      if (ev.type === "run_finished") {
        this.state.closed = true;
        this.state.controller?.abort();
      }
    }
  }

  private async scheduleReconnect(reason: string): Promise<void> {
    if (this.state.closed) return;
    if (this.state.reconnects >= MAX_RECONNECTS) {
      this.handlers.onError?.(`多次重连失败（${reason}），已停止自动重连`, false);
      return;
    }
    this.state.reconnects += 1;
    this.handlers.onError?.(`${reason}，正在重连（第 ${this.state.reconnects} 次）…`, true);
    await new Promise((r) => setTimeout(r, 3000));
    if (!this.state.closed) await this.openStream();
  }

  /** 运行是否已终态（供调用方决定是否停止订阅） */
  static isTerminal(status: RunStatus): boolean {
    return RUN_TERMINAL_STATUSES.includes(status);
  }
}

function readableError(e: unknown): string {
  return e instanceof Error ? e.message : String(e);
}
