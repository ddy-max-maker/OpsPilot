<script setup lang="ts">
// 审批与恢复域：Proposal 列表 + 新建方案
import { onMounted, ref } from "vue";
import { proposalsApi, runbooksApi, assetsApi } from "../api";
import type { Proposal, Runbook, Asset } from "../types";
import StateBlock from "../components/StateBlock.vue";
import StatusBadge from "../components/StatusBadge.vue";
import { toast, errorMessage } from "../toast";
import { auth } from "../auth";

const loading = ref(true);
const error = ref<string | null>(null);
const proposals = ref<Proposal[]>([]);
const fStatus = ref("");
const canCreate = auth.hasRole("operator", "approver", "admin");

// 新建方案
const showCreate = ref(false);
const runbooks = ref<Runbook[]>([]);
const assets = ref<Asset[]>([]);
const publishedVersions = ref<Array<{ id: string; label: string }>>([]);
const cForm = ref({ run_id: "", runbook_version_id: "", target_asset_id: "", environment: "sandbox", parameters: "{}" });
const busyCreate = ref(false);

async function load() {
  loading.value = true;
  error.value = null;
  try {
    const r = await proposalsApi.list({ status: fStatus.value || undefined, limit: 200 });
    proposals.value = r.proposals;
  } catch (e) {
    error.value = errorMessage(e);
  } finally {
    loading.value = false;
  }
}

async function openCreate() {
  showCreate.value = !showCreate.value;
  if (showCreate.value && !runbooks.value.length) {
    try {
      const [rb, ast] = await Promise.all([runbooksApi.list(), assetsApi.list({ limit: 200 })]);
      runbooks.value = rb.runbooks;
      assets.value = ast.items;
      publishedVersions.value = rb.runbooks.flatMap((b) =>
        b.versions
          .filter((v) => v.status === "published")
          .map((v) => ({ id: v.id, label: `${b.name} v${v.version_no}（${v.risk_level}）` })),
      );
    } catch (e) {
      toast(errorMessage(e), true);
    }
  }
}

async function createProposal() {
  let params: unknown;
  try {
    params = JSON.parse(cForm.value.parameters || "{}");
  } catch {
    toast("parameters 不是合法 JSON", true);
    return;
  }
  busyCreate.value = true;
  try {
    await proposalsApi.create({
      run_id: cForm.value.run_id,
      runbook_version_id: cForm.value.runbook_version_id,
      target_asset_id: cForm.value.target_asset_id,
      environment: cForm.value.environment,
      parameters: params,
    });
    toast("方案已创建，待审批");
    showCreate.value = false;
    await load();
  } catch (e) {
    toast(errorMessage(e), true);
  } finally {
    busyCreate.value = false;
  }
}

onMounted(load);
</script>

<template>
  <div class="panel">
    <div class="toolbar">
      <h3 style="margin:0;flex:1">恢复方案（Proposal）</h3>
      <select v-model="fStatus" @change="load">
        <option value="">全部状态</option>
        <option v-for="s in ['draft','pending_approval','approved','rejected','expired','executing','executed','execution_failed','execution_unknown','verified','verify_failed','verify_unknown','canceled']"
          :key="s" :value="s">{{ s }}</option>
      </select>
      <button v-if="canCreate" class="btn btn-primary" @click="openCreate">+ 新建方案</button>
      <button class="btn" @click="load">刷新</button>
    </div>

    <div v-if="showCreate && canCreate" class="panel" style="background:#fafbfc">
      <p class="muted" style="margin-top:0;font-size:13px">
        方案只能引用已发布（published）的 Runbook 版本；目标环境不含 production（服务端强制拒绝）。
      </p>
      <div class="form-grid">
        <label class="f">诊断 Run *</label>
        <input v-model="cForm.run_id" placeholder="run_id（事件详情页发起诊断后获得）" />
        <label class="f">Runbook 版本 *</label>
        <select v-model="cForm.runbook_version_id">
          <option value="">— 选择已发布版本 —</option>
          <option v-for="v in publishedVersions" :key="v.id" :value="v.id">{{ v.label }}</option>
        </select>
        <label class="f">目标资产 *</label>
        <select v-model="cForm.target_asset_id">
          <option value="">— 选择资产 —</option>
          <option v-for="a in assets" :key="a.id" :value="a.id">{{ a.name }}（{{ a.environment }}）</option>
        </select>
        <label class="f">目标环境 *</label>
        <select v-model="cForm.environment">
          <option value="sandbox">sandbox</option>
          <option value="staging">staging</option>
        </select>
        <label class="f">参数 JSON</label>
        <textarea v-model="cForm.parameters" class="mono" />
      </div>
      <button class="btn btn-primary" style="margin-top:10px" :disabled="busyCreate" @click="createProposal">
        {{ busyCreate ? "创建中…" : "创建方案" }}
      </button>
    </div>

    <StateBlock :loading="loading" :error="error" :empty="!proposals.length" empty-text="暂无恢复方案" @retry="load">
      <table class="tbl">
        <thead><tr>
          <th>方案</th><th>环境</th><th>风险</th><th>状态</th><th>发起人</th><th>创建时间</th><th></th>
        </tr></thead>
        <tbody>
          <tr v-for="p in proposals" :key="p.id">
            <td><router-link :to="`/approvals/${p.id}`" style="color:var(--primary)" class="mono">{{ p.id }}</router-link>
              <div class="muted" style="font-size:12px">run: {{ p.run_id?.slice(0,16) }}</div></td>
            <td><StatusBadge :value="p.environment" /></td>
            <td><StatusBadge :value="p.risk_level" /></td>
            <td><StatusBadge :value="p.status" /></td>
            <td class="mono muted" :title="p.created_by_user ?? p.created_by">{{ p.created_by_user ?? p.created_by }}</td>
            <td class="mono muted">{{ p.created_at?.slice(0,19).replace("T"," ") }}</td>
            <td><router-link :to="`/approvals/${p.id}`" class="btn btn-sm">详情</router-link></td>
          </tr>
        </tbody>
      </table>
    </StateBlock>
  </div>
</template>
