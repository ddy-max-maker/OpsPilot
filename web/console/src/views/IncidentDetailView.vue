<script setup lang="ts">
// 事件详情 + 诊断（工作台域 → 诊断详情域入口）
import { onMounted, ref } from "vue";
import { useRoute } from "vue-router";
import { incidentsApi, runsApi } from "../api";
import type { Incident } from "../types";
import StateBlock from "../components/StateBlock.vue";
import StatusBadge from "../components/StatusBadge.vue";
import RunPanel from "../components/RunPanel.vue";
import { toast, errorMessage } from "../toast";
import { auth } from "../auth";

const route = useRoute();
const incidentId = route.params.id as string;

const loading = ref(true);
const error = ref<string | null>(null);
const incident = ref<Incident | null>(null);
const runId = ref<string | null>(null);
const starting = ref(false);

const canDiagnose = auth.hasRole("operator", "approver", "admin");

async function load() {
  loading.value = true;
  error.value = null;
  try {
    incident.value = await incidentsApi.get(incidentId);
    // run 发现：URL ?run= 优先（可分享/跨会话恢复）；其次本机启动记录
    // （后端无 runs 列表端点，契约冻结不加新端点）
    const fromQuery = typeof route.query.run === "string" ? route.query.run : "";
    const fromLocal = localStorage.getItem(`opspilot_run_${incidentId}`);
    runId.value = fromQuery || fromLocal;
    if (fromQuery && fromQuery !== fromLocal) {
      localStorage.setItem(`opspilot_run_${incidentId}`, fromQuery);
    }
  } catch (e) {
    error.value = errorMessage(e);
  } finally {
    loading.value = false;
  }
}

async function startDiagnosis() {
  if (!confirm(`确认为事件 ${incident.value?.title} 启动 Agent 诊断？（只读取证，不执行变更）`)) return;
  starting.value = true;
  try {
    const r = await runsApi.start(incidentId);
    runId.value = r.run_id;
    localStorage.setItem(`opspilot_run_${incidentId}`, r.run_id);
    toast(`诊断已受理：${r.run_id}`);
  } catch (e) {
    toast(errorMessage(e), true);
  } finally {
    starting.value = false;
  }
}

onMounted(load);
</script>

<template>
  <StateBlock :loading="loading" :error="error" @retry="load">
    <div class="panel" v-if="incident">
      <div class="toolbar">
        <h3 style="margin:0;flex:1">{{ incident.title }}</h3>
        <StatusBadge :value="incident.environment" />
        <StatusBadge :value="incident.severity" />
        <StatusBadge :value="incident.status" />
        <button v-if="canDiagnose" class="btn btn-primary" :disabled="starting" @click="startDiagnosis">
          {{ starting ? "启动中…" : "启动 Agent 诊断" }}
        </button>
      </div>
      <div class="detail-rows">
        <span class="k">事件 ID</span><span class="mono">{{ incident.id }}</span>
        <span class="k">描述</span><span>{{ incident.description ?? "—" }}</span>
        <span class="k">主资产</span><span class="mono">{{ incident.primary_asset_id }}</span>
        <span class="k">来源</span><span>{{ incident.source }}<template v-if="incident.alert_id">（告警 {{ incident.alert_id }}）</template></span>
        <span class="k">创建时间</span><span class="mono muted">{{ incident.created_at?.slice(0,19).replace("T"," ") }}</span>
      </div>
    </div>

    <template v-if="runId">
      <RunPanel :run-id="runId" :key="runId" />
    </template>
    <div v-else class="panel">
      <div class="state">
        这里查不到本事件的历史诊断：控制台没有「事件 → run」的反查接口（契约冻结，未新增端点），
        因此无法区分「确实还没诊断过」与「诊断过但本机没有记录」。
        <template v-if="canDiagnose">
          若已知 run 标识，请到「诊断详情」粘贴查看；点击右上「启动 Agent 诊断」会新建一次只读取证。
        </template>
        <template v-else>若已知 run 标识，请到「诊断详情」粘贴查看。</template>
      </div>
    </div>
  </StateBlock>
</template>
