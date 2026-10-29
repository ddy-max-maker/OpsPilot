<script setup lang="ts">
// 平台域：评估套件、运行与指标
import { computed, onMounted, ref } from "vue";
import { evaluationsApi } from "../api";
import type { EvaluationRun, SuiteSummary } from "../types";
import StateBlock from "../components/StateBlock.vue";
import StatusBadge from "../components/StatusBadge.vue";
import { toast, errorMessage } from "../toast";
import { auth } from "../auth";

const loading = ref(true);
const error = ref<string | null>(null);
const suites = ref<SuiteSummary[]>([]);
const datasetInfo = ref<{ dataset_version: string; dataset_digest: string }>({ dataset_version: "", dataset_digest: "" });
const runs = ref<Record<string, EvaluationRun>>({});
const activeRunId = ref("");
const starting = ref("");
const canRun = auth.hasRole("approver", "admin");

const activeRun = computed(() => (activeRunId.value ? runs.value[activeRunId.value] : null));

async function load() {
  loading.value = true;
  error.value = null;
  try {
    const r = await evaluationsApi.suites();
    suites.value = r.items;
    datasetInfo.value = { dataset_version: r.dataset_version, dataset_digest: r.dataset_digest };
  } catch (e) {
    error.value = errorMessage(e);
  } finally {
    loading.value = false;
  }
}

async function startSuite(s: SuiteSummary) {
  if (!confirm(`运行评估套件「${s.name}」（${s.case_count} 案例，含 ${s.holdout_count} 保留集）？`)) return;
  starting.value = s.id;
  try {
    const r = await evaluationsApi.createRun(s.id, true); // wait 同步跑完（fake 模型分钟级）
    toast("评估完成，加载结果…");
    activeRunId.value = r.run_id;
    await pollRun(r.run_id);
  } catch (e) {
    toast(errorMessage(e), true);
  } finally {
    starting.value = "";
  }
}

async function pollRun(runId: string) {
  for (let i = 0; i < 120; i++) {
    const r = await evaluationsApi.getRun(runId);
    runs.value = { ...runs.value, [runId]: r };
    if (r.status === "completed" || r.status === "failed") return;
    await new Promise((res) => setTimeout(res, 2000));
  }
}

const pct = (v: number) => `${Math.round(v * 100)}%`;

onMounted(load);
</script>

<template>
  <div class="panel">
    <div class="toolbar">
      <h3 style="margin:0;flex:1">评估套件（确定性 fake 模型）</h3>
      <span class="mono muted" style="font-size:11px">
        数据集 {{ datasetInfo.dataset_version }} · {{ datasetInfo.dataset_digest?.slice(0, 12) }}
      </span>
      <button class="btn" @click="load">刷新</button>
    </div>
    <StateBlock :loading="loading" :error="error" :empty="!suites.length" empty-text="暂无套件" @retry="load">
      <table class="tbl">
        <thead><tr><th>套件</th><th>类别</th><th>案例数</th><th>保留集</th><th>操作</th></tr></thead>
        <tbody>
          <tr v-for="s in suites" :key="s.id">
            <td><strong>{{ s.name }}</strong><div class="muted" style="font-size:12px">{{ s.description }}</div></td>
            <td><StatusBadge :value="s.kind" /></td>
            <td>{{ s.case_count }}</td>
            <td>{{ s.holdout_count }}</td>
            <td>
              <button v-if="canRun" class="btn btn-sm btn-primary" :disabled="starting === s.id" @click="startSuite(s)">
                {{ starting === s.id ? "运行中…" : "运行" }}
              </button>
              <span v-else class="muted" style="font-size:12px">需 approver/admin</span>
            </td>
          </tr>
        </tbody>
      </table>
    </StateBlock>
  </div>

  <div class="panel" v-if="activeRun">
    <h3>评估结果 <span class="mono muted">{{ activeRun.id }}</span> <StatusBadge :value="activeRun.status" /></h3>
    <div class="grid-stats" style="grid-template-columns:repeat(6,1fr)">
      <div class="stat"><div class="num">{{ pct(activeRun.summary.top1_hit_rate) }}</div><div class="lbl">Top-1</div></div>
      <div class="stat"><div class="num">{{ pct(activeRun.summary.top3_hit_rate) }}</div><div class="lbl">Top-3</div></div>
      <div class="stat"><div class="num">{{ activeRun.summary.passed }}/{{ activeRun.summary.cases_total }}</div><div class="lbl">通过案例</div></div>
      <div class="stat"><div class="num">{{ activeRun.summary.p50_latency_ms }}<small>/{{ activeRun.summary.p95_latency_ms }}ms</small></div><div class="lbl">P50/P95 延迟</div></div>
      <div class="stat"><div class="num">{{ activeRun.summary.avg_tool_calls.toFixed(1) }}</div><div class="lbl">平均工具调用</div></div>
      <div class="stat">
        <div class="num" :style="{ color: activeRun.summary.gates_passed ? 'var(--ok)' : 'var(--danger)' }">
          {{ activeRun.summary.gates_passed ? "✓" : "✗" }}
        </div>
        <div class="lbl">强门禁（越权{{ activeRun.summary.gate_unauthorized_success }}/重复{{ activeRun.summary.gate_repeated_side_effects }}/误报{{ activeRun.summary.gate_verification_misreport }}）</div>
      </div>
    </div>

    <table class="tbl">
      <thead><tr><th>案例</th><th>保留集</th><th>结果</th><th>Top-1</th><th>证据/知识</th><th>延迟</th><th>明细</th></tr></thead>
      <tbody>
        <tr v-for="c in activeRun.results" :key="c.case_id">
          <td>{{ c.title }}<div class="mono muted" style="font-size:11px">{{ c.case_key }}</div></td>
          <td>{{ c.holdout ? "🔒" : "" }}</td>
          <td><StatusBadge :value="c.passed ? 'passed' : 'failed'" /></td>
          <td>{{ c.metrics.top1_hit === true ? "✓" : c.metrics.top1_hit === false ? "✗" : "—" }}</td>
          <td class="mono muted" style="font-size:11px">{{ c.metrics.evidence_total }} / {{ c.metrics.knowledge_hits }}</td>
          <td class="mono muted" style="font-size:11px">{{ c.metrics.latency_ms ?? "—" }}ms</td>
          <td class="mono" style="font-size:11px;max-width:260px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">
            {{ JSON.stringify(c.detail) }}
          </td>
        </tr>
      </tbody>
    </table>
  </div>
  <div class="panel" v-else>
    <div class="state">选择套件并点击「运行」查看结果（含失败案例明细与原始基线）</div>
  </div>
</template>
