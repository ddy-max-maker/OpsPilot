<script setup lang="ts">
// 诊断详情：run 状态 + SSE 实时轨迹 + 证据 + 假设 + 报告
import { computed, onBeforeUnmount, onMounted, ref } from "vue";
import { runsApi } from "../api";
import type { Evidence, Run, RunEvent, Hypothesis } from "../types";
import { RunEventStream } from "../sse/runStream";
import StateBlock from "../components/StateBlock.vue";
import StatusBadge from "../components/StatusBadge.vue";
import { toast, errorMessage } from "../toast";
import { auth } from "../auth";

const props = defineProps<{ runId: string }>();

const loading = ref(true);
const error = ref<string | null>(null);
const run = ref<Run | null>(null);
const events = ref<RunEvent[]>([]);
const evidence = ref<Evidence[]>([]);
const report = ref<Awaited<ReturnType<typeof runsApi.report>> | null>(null);
const reportMissing = ref(false);
const sseStatus = ref<"idle" | "connecting" | "live" | "ended" | "error">("idle");
const sseMsg = ref("");

const canCancel = auth.hasRole("operator", "approver", "admin");
const isTerminal = computed(() => !!run.value && RunEventStream.isTerminal(run.value.status));
const stopReason = computed(() => run.value?.stop_reason ?? null);

let stream: RunEventStream | null = null;

// 历史知识证据与实时证据严格分区展示（CONTRACTS §2.6）
const realtimeEvidence = computed(() => evidence.value.filter((e) => e.source_type !== "knowledge"));
const knowledgeEvidence = computed(() => evidence.value.filter((e) => e.source_type === "knowledge"));

const EVENT_LABEL: Record<string, string> = {
  run_started: "启动", node_entered: "进入节点", node_exited: "离开节点",
  tool_called: "调用工具", tool_returned: "工具返回", evidence_added: "新增证据",
  hypothesis_added: "新增假设", budget_warning: "预算警告", error: "错误", run_finished: "运行结束",
};

async function loadAll() {
  loading.value = true;
  error.value = null;
  try {
    run.value = await runsApi.get(props.runId);
    const [evPage, evd] = await Promise.all([
      runsApi.events(props.runId, 0, 200),
      runsApi.evidence(props.runId, { limit: 200 }),
    ]);
    events.value = evPage.items;
    evidence.value = evd.items;
    try {
      report.value = await runsApi.report(props.runId);
      reportMissing.value = false;
    } catch {
      report.value = null;
      reportMissing.value = true;
    }
    startStream();
  } catch (e) {
    error.value = errorMessage(e);
  } finally {
    loading.value = false;
  }
}

function startStream() {
  stream?.stop();
  if (!run.value || isTerminal.value) {
    sseStatus.value = "ended";
    return;
  }
  const lastSeq = events.value.length ? events.value[events.value.length - 1].seq : 0;
  stream = new RunEventStream(props.runId, lastSeq, {
    onEvent(ev) {
      events.value.push(ev);
      if (ev.type === "run_finished") void refreshRun();
      if (ev.type === "evidence_added" || ev.type === "hypothesis_added") void refreshArtifacts();
    },
    onEnd(reason, status) {
      sseStatus.value = "ended";
      sseMsg.value = `流结束：${reason}${status ? `（${status}）` : ""}`;
      void refreshRun();
    },
    onError(message, recoverable) {
      sseStatus.value = recoverable ? "connecting" : "error";
      sseMsg.value = message;
      if (!recoverable) toast(message, true);
    },
  });
  sseStatus.value = "connecting";
  sseMsg.value = "";
  void stream.start().then(() => {
    if (sseStatus.value === "connecting") sseStatus.value = "live";
  });
}

async function refreshRun() {
  if (!run.value) return;
  try {
    run.value = await runsApi.get(props.runId);
  } catch { /* 保持旧值 */ }
}

async function refreshArtifacts() {
  try {
    const evd = await runsApi.evidence(props.runId, { limit: 200 });
    evidence.value = evd.items;
  } catch { /* 忽略瞬时错误 */ }
}

async function cancelRun() {
  if (!confirm("确认取消该诊断运行？")) return;
  try {
    await runsApi.cancel(props.runId);
    toast("已请求取消");
    await refreshRun();
  } catch (e) {
    toast(errorMessage(e), true);
  }
}

const hypotheses = computed<Hypothesis[]>(() => report.value?.hypotheses ?? []);

onMounted(loadAll);
onBeforeUnmount(() => stream?.stop());
</script>

<template>
  <StateBlock :loading="loading" :error="error" @retry="loadAll">
    <div class="panel" v-if="run">
      <div class="toolbar">
        <h3 style="margin:0;flex:1">诊断运行 <span class="mono muted">{{ run.id }}</span></h3>
        <span v-if="sseStatus === 'live'" class="badge b-green">SSE 实时</span>
        <span v-else-if="sseStatus === 'connecting'" class="badge b-amber">SSE 连接中…</span>
        <span v-else-if="sseStatus === 'error'" class="badge b-red">SSE 错误</span>
        <span v-else class="badge b-gray">SSE 已结束</span>
        <button class="btn btn-sm" @click="startStream" :disabled="isTerminal && sseStatus === 'ended' && !!sseMsg.includes('run_finished')">重连</button>
        <button v-if="canCancel && !isTerminal" class="btn btn-sm btn-danger" @click="cancelRun">取消诊断</button>
      </div>
      <div class="detail-rows">
        <span class="k">状态</span><span><StatusBadge :value="run.status" />
          <template v-if="stopReason">　停止原因：<StatusBadge :value="stopReason" /></template>
          <span v-if="run.cancel_requested" class="badge b-amber">已请求取消</span></span>
        <span class="k">模型</span><span class="mono">{{ JSON.stringify(run.model_config_snapshot) }}</span>
        <span class="k">预算</span><span class="mono muted">{{ JSON.stringify(run.budgets) }}</span>
        <span class="k">用量</span><span class="mono muted">{{ JSON.stringify(run.usage) }}</span>
        <span class="k">时间</span>
        <span class="muted mono">{{ run.started_at?.slice(0,19).replace("T"," ") ?? "—" }} → {{ run.finished_at?.slice(0,19).replace("T"," ") ?? "进行中" }}</span>
      </div>
      <div v-if="sseMsg" class="muted" style="margin-top:8px;font-size:12px">SSE：{{ sseMsg }}</div>
    </div>

    <div class="grid-2">
      <div class="panel">
        <h3>实时轨迹（{{ events.length }} 条事件，含断线重放）</h3>
        <div class="timeline" style="max-height:420px;overflow:auto">
          <div v-for="ev in events" :key="ev.seq" class="t-item">
            <div>
              <span class="badge b-gray">#{{ ev.seq }}</span>
              <strong style="margin-left:6px">{{ EVENT_LABEL[ev.type] ?? ev.type }}</strong>
              <span class="mono muted" style="margin-left:8px;font-size:11px">{{ ev.created_at?.slice(11,19) }}</span>
            </div>
            <div v-if="ev.payload" class="mono muted" style="font-size:11px;word-break:break-all">
              {{ JSON.stringify(ev.payload).slice(0, 220) }}
            </div>
          </div>
          <div v-if="!events.length" class="state">暂无事件</div>
        </div>
      </div>

      <div>
        <div class="panel">
          <h3>证据（实时观测 {{ realtimeEvidence.length }} 条）</h3>
          <table class="tbl">
            <thead><tr><th>来源</th><th>采集器</th><th>状态</th><th>摘要</th></tr></thead>
            <tbody>
              <tr v-for="e in realtimeEvidence" :key="e.id">
                <td><StatusBadge :value="e.source_type" /></td>
                <td class="mono muted">{{ e.collector }}</td>
                <td><StatusBadge :value="e.status" /></td>
                <td class="mono" style="font-size:11px">{{ e.summary_redacted?.slice(0, 120) ?? e.error_code }}</td>
              </tr>
            </tbody>
          </table>
          <div v-if="!realtimeEvidence.length" class="state">暂无实时证据</div>
        </div>

        <div class="panel">
          <h3>历史知识引用（{{ knowledgeEvidence.length }} 条）<span class="muted" style="font-size:12px">— 仅供参照，不作为根因直接证据</span></h3>
          <table class="tbl" v-if="knowledgeEvidence.length">
            <tbody>
              <tr v-for="e in knowledgeEvidence" :key="e.id">
                <td><StatusBadge value="knowledge" /></td>
                <td class="mono" style="font-size:11px">{{ e.summary_redacted?.slice(0, 160) }}</td>
              </tr>
            </tbody>
          </table>
          <div v-else class="state">本次诊断未引用历史知识</div>
        </div>
      </div>
    </div>

    <div class="panel" v-if="report">
      <h3>诊断报告 <span class="mono muted">{{ report.id }} · v{{ report.version_no }} · {{ report.status }}</span></h3>
      <div v-if="report.unknown_note?.is_unknown" class="panel" style="background:#fef3c7;border-color:#f59e0b">
        <strong>⚠ 未知结论（适当弃答）</strong>
        <div>{{ report.unknown_note.reason }}</div>
        <div v-if="report.unknown_note.missing_information?.length" class="muted">
          缺失信息：{{ report.unknown_note.missing_information.join("；") }}
        </div>
      </div>
      <div class="detail-rows">
        <span class="k">结论摘要</span><span>{{ report.conclusion?.summary }}</span>
      </div>
      <div v-if="report.conclusion?.key_findings?.length" style="margin-top:10px">
        <strong style="font-size:13px">关键发现</strong>
        <ul style="margin:6px 0">
          <li v-for="(f, i) in report.conclusion.key_findings" :key="i" class="mono" style="font-size:12px">
            {{ JSON.stringify(f).slice(0, 300) }}
          </li>
        </ul>
      </div>
      <div class="grid-2" style="margin-top:10px">
        <div>
          <strong style="font-size:13px">候选根因（假设）</strong>
          <table class="tbl" v-if="hypotheses.length">
            <thead><tr><th>#</th><th>陈述</th><th>状态</th><th>置信</th><th>支持/反证证据</th></tr></thead>
            <tbody>
              <tr v-for="h in hypotheses" :key="h.id">
                <td>{{ h.rank }}</td>
                <td>{{ h.statement }}</td>
                <td><StatusBadge :value="h.status" /></td>
                <td><StatusBadge :value="h.confidence" /></td>
                <td>
                  <span v-for="id in h.supporting_evidence_ids" :key="id" class="badge b-green mono" style="margin-right:4px;font-size:10px">{{ id.slice(0,10) }}</span>
                  <span v-for="id in h.contradicting_evidence_ids" :key="id" class="badge b-red mono" style="margin-right:4px;font-size:10px">{{ id.slice(0,10) }}</span>
                </td>
              </tr>
            </tbody>
          </table>
          <div v-else class="state">暂无假设</div>
        </div>
        <div>
          <strong style="font-size:13px">证据引用（{{ report.evidence_refs.length }}）</strong>
          <pre class="evidence">{{ report.evidence_refs.join("\n") || "无" }}</pre>
          <div class="muted" style="font-size:12px" v-if="report.generator">
            生成器：{{ report.generator.model }} / prompt {{ report.generator.prompt_version }}
          </div>
        </div>
      </div>
    </div>
    <div class="panel" v-else-if="reportMissing && !loading">
      <h3>诊断报告</h3>
      <div class="state">报告尚未生成（运行结束后由 Report 节点写入快照）</div>
    </div>
  </StateBlock>
</template>
