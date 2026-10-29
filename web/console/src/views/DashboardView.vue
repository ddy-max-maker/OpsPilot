<script setup lang="ts">
// 工作台与事件：故障看板（统计）+ 事件列表（筛选/创建/确认）
import { computed, onMounted, ref } from "vue";
import { incidentsApi, assetsApi } from "../api";
import type { Incident, Asset } from "../types";
import StateBlock from "../components/StateBlock.vue";
import StatusBadge from "../components/StatusBadge.vue";
import { toast, errorMessage } from "../toast";
import { auth } from "../auth";

const loading = ref(true);
const error = ref<string | null>(null);
const incidents = ref<Incident[]>([]);
const assets = ref<Asset[]>([]);

const fStatus = ref("");
const fSeverity = ref("");
const fEnv = ref("");
const canCreate = auth.hasRole("operator", "approver", "admin");

const showCreate = ref(false);
const cForm = ref({ title: "", description: "", environment: "sandbox", primary_asset_id: "", severity: "medium" });
const busyCreate = ref(false);
const acking = ref(new Set<string>());

const stats = computed(() => {
  const by = (s: string) => incidents.value.filter((i) => i.status === s).length;
  return [
    { lbl: "Open", num: by("open") },
    { lbl: "处理中", num: by("investigating") + by("mitigating") },
    { lbl: "待人工 / 观察", num: by("needs_human") + by("monitoring") },
    { lbl: "已解决/关闭", num: by("resolved") + by("closed") },
  ];
});

async function load() {
  loading.value = true;
  error.value = null;
  try {
    const [inc, ast] = await Promise.all([
      incidentsApi.list({
        status: fStatus.value || undefined,
        severity: fSeverity.value || undefined,
        environment: fEnv.value || undefined,
        limit: 50,
      }),
      assetsApi.list({ limit: 200 }),
    ]);
    incidents.value = inc.items;
    assets.value = ast.items;
  } catch (e) {
    error.value = errorMessage(e);
  } finally {
    loading.value = false;
  }
}

const assetName = (id: string) => assets.value.find((a) => a.id === id)?.name ?? id;

async function createIncident() {
  if (!cForm.value.title || !cForm.value.primary_asset_id) {
    toast("请填写标题与主资产", true);
    return;
  }
  busyCreate.value = true;
  try {
    await incidentsApi.create({
      title: cForm.value.title,
      description: cForm.value.description || undefined,
      environment: cForm.value.environment,
      primary_asset_id: cForm.value.primary_asset_id,
      severity: cForm.value.severity,
    });
    toast("事件已创建");
    showCreate.value = false;
    cForm.value = { title: "", description: "", environment: "sandbox", primary_asset_id: "", severity: "medium" };
    await load();
  } catch (e) {
    toast(errorMessage(e), true);
  } finally {
    busyCreate.value = false;
  }
}

async function acknowledge(inc: Incident) {
  acking.value.add(inc.id);
  try {
    await incidentsApi.acknowledge(inc.id);
    toast("已确认");
    await load();
  } catch (e) {
    toast(errorMessage(e), true);
  } finally {
    acking.value.delete(inc.id);
  }
}

onMounted(load);
</script>

<template>
  <div class="grid-stats">
    <div v-for="s in stats" :key="s.lbl" class="stat">
      <div class="num">{{ s.num }}</div>
      <div class="lbl">{{ s.lbl }}</div>
    </div>
  </div>

  <div class="panel">
    <div class="toolbar">
      <h3 style="margin:0;flex:1">故障事件</h3>
      <select v-model="fStatus" @change="load">
        <option value="">全部状态</option>
        <option v-for="s in ['open','acknowledged','investigating','mitigating','monitoring','resolved','closed','needs_human']" :key="s" :value="s">{{ s }}</option>
      </select>
      <select v-model="fSeverity" @change="load">
        <option value="">全部严重度</option>
        <option v-for="s in ['low','medium','high','critical']" :key="s" :value="s">{{ s }}</option>
      </select>
      <select v-model="fEnv" @change="load">
        <option value="">全部环境</option>
        <option v-for="e in ['sandbox','staging','production']" :key="e" :value="e">{{ e }}</option>
      </select>
      <button v-if="canCreate" class="btn btn-primary" @click="showCreate = !showCreate">+ 登记故障</button>
    </div>

    <div v-if="showCreate && canCreate" class="panel" style="background:#fafbfc">
      <div class="form-grid">
        <label class="f">标题 *</label>
        <input v-model="cForm.title" placeholder="如：订单服务 5xx 激增" />
        <label class="f">环境 *</label>
        <select v-model="cForm.environment">
          <option v-for="e in ['sandbox','staging','production']" :key="e" :value="e">{{ e }}</option>
        </select>
        <label class="f">主资产 *</label>
        <select v-model="cForm.primary_asset_id">
          <option value="">— 选择资产 —</option>
          <option v-for="a in assets" :key="a.id" :value="a.id">{{ a.name }}（{{ a.environment }}）</option>
        </select>
        <label class="f">严重度</label>
        <select v-model="cForm.severity">
          <option v-for="s in ['low','medium','high','critical']" :key="s" :value="s">{{ s }}</option>
        </select>
        <label class="f">描述</label>
        <textarea v-model="cForm.description" />
      </div>
      <button class="btn btn-primary" style="margin-top:10px" :disabled="busyCreate" @click="createIncident">
        {{ busyCreate ? "创建中…" : "创建事件" }}
      </button>
    </div>

    <StateBlock :loading="loading" :error="error" :empty="!incidents.length" empty-text="暂无事件" @retry="load">
      <table class="tbl">
        <thead><tr>
          <th>标题</th><th>环境</th><th>主资产</th><th>严重度</th><th>状态</th><th>来源</th><th>创建时间</th><th></th>
        </tr></thead>
        <tbody>
          <tr v-for="i in incidents" :key="i.id">
            <td><router-link :to="`/incidents/${i.id}`" style="color:var(--primary)">{{ i.title }}</router-link>
              <div class="mono muted">{{ i.id }}</div></td>
            <td><StatusBadge :value="i.environment" /></td>
            <td>{{ assetName(i.primary_asset_id) }}</td>
            <td><StatusBadge :value="i.severity" /></td>
            <td><StatusBadge :value="i.status" /></td>
            <td class="muted">{{ i.source }}</td>
            <td class="muted mono">{{ i.created_at?.slice(0, 19).replace("T", " ") }}</td>
            <td>
              <button v-if="canCreate && i.status === 'open'" class="btn btn-sm" :disabled="acking.has(i.id)"
                @click="acknowledge(i)">确认</button>
            </td>
          </tr>
        </tbody>
      </table>
    </StateBlock>
  </div>
</template>
