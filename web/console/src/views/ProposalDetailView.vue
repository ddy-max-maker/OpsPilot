<script setup lang="ts">
// Proposal 详情：dry-run / 四眼审批 / 执行 / Verification（危险操作全部二次确认）
import { computed, onMounted, ref } from "vue";
import { useRoute } from "vue-router";
import { proposalsApi } from "../api";
import type { DryRunResult, ProposalDetail } from "../types";
import StateBlock from "../components/StateBlock.vue";
import StatusBadge from "../components/StatusBadge.vue";
import { toast, errorMessage } from "../toast";
import { auth } from "../auth";

const route = useRoute();
const proposalId = route.params.id as string;

const loading = ref(true);
const error = ref<string | null>(null);
const detail = ref<ProposalDetail | null>(null);
const busy = ref("");
const comment = ref("");
const dryRun = ref<DryRunResult | null>(null);
const pollTimer = ref<ReturnType<typeof setInterval> | null>(null);

const me = computed(() => auth.state.me);
const p = computed(() => detail.value?.proposal);
// 四眼：approver/admin 且非发起人（服务端最终裁决）
const canDecide = computed(() =>
  auth.hasRole("approver", "admin") && !!p.value && p.value.created_by_user !== me.value?.id);
const canExecute = computed(() =>
  auth.hasRole("operator", "approver", "admin") &&
  !!p.value && ["approved"].includes(p.value.status));

async function load() {
  loading.value = true;
  error.value = null;
  try {
    detail.value = await proposalsApi.get(proposalId);
  } catch (e) {
    error.value = errorMessage(e);
  } finally {
    loading.value = false;
  }
}

async function act(action: "dry-run" | "approve" | "reject" | "execute") {
  if (!p.value) return;
  const labels: Record<string, string> = {
    "dry-run": `对 ${p.value.target_asset_id} 执行只读 dry-run 前置检查`,
    approve: `批准该方案（目标环境 ${p.value.environment}，资产 ${p.value.target_asset_id}，风险 ${p.value.risk_level}）`,
    reject: "拒绝该方案",
    execute: `在 ${p.value.environment} 环境对 ${p.value.target_asset_id} 执行恢复动作（有副作用！）`,
  };
  const danger = action === "execute" || action === "approve";
  const msg = danger
    ? `⚠ 危险操作二次确认\n\n${labels[action]}\n\n确认继续？`
    : `确认${labels[action]}？`;
  if (!confirm(msg)) return;

  busy.value = action;
  try {
    if (action === "dry-run") {
      const dr = await proposalsApi.dryRun(proposalId);
      dryRun.value = dr;
      toast(dr.passed ? "dry-run 全部通过" : "dry-run 存在未通过项", !dr.passed);
    } else if (action === "approve") {
      await proposalsApi.approve(proposalId, comment.value || undefined);
      toast("已批准");
    } else if (action === "reject") {
      await proposalsApi.reject(proposalId, comment.value || undefined);
      toast("已拒绝");
    } else if (action === "execute") {
      await proposalsApi.execute(proposalId);
      toast("执行已受理（经 Outbox 异步执行）");
      startPolling();
    }
    comment.value = "";
    await load();
  } catch (e) {
    toast(errorMessage(e), true);
  } finally {
    busy.value = "";
  }
}

// 执行/验证为异步，轮询详情直至离开 executing
function startPolling() {
  stopPolling();
  pollTimer.value = setInterval(async () => {
    try {
      const d = await proposalsApi.get(proposalId);
      detail.value = d;
      const s = d.proposal.status;
      if (!["executing"].includes(s)) stopPolling();
    } catch { /* 忽略 */ }
  }, 3000);
}
function stopPolling() {
  if (pollTimer.value) {
    clearInterval(pollTimer.value);
    pollTimer.value = null;
  }
}

onMounted(() => {
  void load();
  // 若已在执行中，自动开始轮询
  void proposalsApi.get(proposalId).then((d) => {
    if (["executing", "approved"].includes(d.proposal.status)) startPolling();
  }).catch(() => undefined);
});
</script>

<template>
  <StateBlock :loading="loading" :error="error" @retry="load">
    <div class="panel" v-if="p">
      <div class="toolbar">
        <h3 style="margin:0;flex:1">恢复方案 <span class="mono muted">{{ p.id }}</span></h3>
        <StatusBadge :value="p.environment" />
        <StatusBadge :value="p.risk_level" />
        <StatusBadge :value="p.status" />
        <button class="btn btn-sm" @click="load">刷新</button>
      </div>

      <div class="panel" style="background:#fff7ed;border-color:#fdba74">
        <strong>⚠ 危险操作公示</strong>
        <div class="detail-rows" style="margin-top:8px">
          <span class="k">目标环境</span><span><StatusBadge :value="p.environment" />（production 一律服务端拒绝）</span>
          <span class="k">目标资产</span><span class="mono">{{ p.target_asset_id }}</span>
          <span class="k">风险等级</span><span>{{ p.risk_level }}</span>
          <span class="k">Runbook 版本</span><span class="mono">{{ p.runbook_version_id }}</span>
          <span class="k">参数</span><pre class="evidence" style="margin:0">{{ JSON.stringify(p.parameters, null, 2) }}</pre>
          <span class="k">参数哈希</span><span class="mono muted" style="font-size:11px">{{ p.parameter_hash }}</span>
          <span class="k">审批有效期</span><span class="mono muted">{{ p.expires_at?.slice(0,19).replace("T"," ") ?? "—" }}</span>
        </div>
      </div>

      <div class="toolbar" v-if="canDecide || canExecute || auth.hasRole('operator','approver','admin')">
        <button class="btn" :disabled="busy !== ''" @click="act('dry-run')">
          {{ busy === "dry-run" ? "检查中…" : "Dry-run 前置检查" }}
        </button>
        <button v-if="canDecide && p.status === 'pending_approval'" class="btn btn-primary"
          :disabled="busy !== ''" @click="act('approve')">批准（四眼）</button>
        <button v-if="canDecide && p.status === 'pending_approval'" class="btn btn-danger"
          :disabled="busy !== ''" @click="act('reject')">拒绝</button>
        <button v-if="canExecute" class="btn btn-danger" :disabled="busy !== ''" @click="act('execute')">
          {{ busy === "execute" ? "执行中…" : "执行恢复（sandbox/staging）" }}
        </button>
        <input v-model="comment" placeholder="审批/拒绝备注（可选）" style="width:260px" />
      </div>

      <div class="panel" v-if="dryRun" style="background:#fafbfc">
        <h3>Dry-run 结果：{{ dryRun.passed ? "✅ 通过" : "❌ 存在未通过项" }}</h3>
        <table class="tbl">
          <tbody>
            <tr v-for="(c, i) in dryRun.checks" :key="i">
              <td>{{ c.name }}</td>
              <td><StatusBadge :value="c.passed ? 'passed' : 'failed'" /></td>
              <td class="muted">{{ c.detail ?? "" }}</td>
            </tr>
          </tbody>
        </table>
      </div>
    </div>

    <div class="grid-2" v-if="detail">
      <div class="panel">
        <h3>审批记录（四眼原则：批准人 ≠ 发起人）</h3>
        <table class="tbl" v-if="detail.approvals.length">
          <thead><tr><th>决定</th><th>批准人</th><th>备注</th><th>时间</th></tr></thead>
          <tbody>
            <tr v-for="a in detail.approvals" :key="a.id">
              <td><StatusBadge :value="a.decision === 'approved' ? 'approved' : 'rejected'" /></td>
              <td class="mono muted" :title="a.decided_by">{{ a.decided_by }}</td>
              <td class="muted">{{ a.comment ?? "—" }}</td>
              <td class="mono muted">{{ a.decided_at?.slice(0,19).replace("T"," ") }}</td>
            </tr>
          </tbody>
        </table>
        <div v-else class="state">暂无审批</div>
      </div>

      <div class="panel">
        <h3>执行与验证</h3>
        <table class="tbl" v-if="detail.executions.length">
          <thead><tr><th>动作</th><th>状态</th><th>结果</th><th>时间</th></tr></thead>
          <tbody>
            <tr v-for="e in detail.executions" :key="e.id">
              <td class="mono">{{ e.action_key }}</td>
              <td><StatusBadge :value="e.status" /></td>
              <td class="mono" style="font-size:11px">{{ JSON.stringify(e.result_detail)?.slice(0,140) }}</td>
              <td class="mono muted" style="font-size:11px;white-space:nowrap">{{ e.started_at?.slice(0,19).replace("T"," ") ?? "—" }}</td>
            </tr>
          </tbody>
        </table>
        <div v-else class="state">尚未执行</div>

        <table class="tbl" v-if="detail.verifications.length" style="margin-top:10px">
          <thead><tr><th>验证</th><th>状态</th><th>检查项</th><th>观察窗口</th></tr></thead>
          <tbody>
            <tr v-for="v in detail.verifications" :key="v.id">
              <td class="mono">{{ v.id.slice(0,12) }}</td>
              <td><StatusBadge :value="v.status" /></td>
              <td>
                <div v-for="(c, i) in v.checks ?? []" :key="i" style="font-size:12px">
                  {{ c.name }}: 期望 {{ JSON.stringify(c.expected) }} / 实测 {{ JSON.stringify(c.observed) }}
                  <StatusBadge :value="c.pass ? 'passed' : 'failed'" />
                </div>
              </td>
              <td class="mono muted" style="font-size:11px">
                {{ v.observation_window ? `${v.observation_window.sample_count ?? 0} 样本` : "—" }}
              </td>
            </tr>
          </tbody>
        </table>
        <div v-else class="state">尚未验证</div>
      </div>
    </div>
  </StateBlock>
</template>
