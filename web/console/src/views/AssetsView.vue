<script setup lang="ts">
// 资产与环境（工作台域）：列表 + admin 登记
import { onMounted, ref } from "vue";
import { assetsApi } from "../api";
import type { Asset } from "../types";
import StateBlock from "../components/StateBlock.vue";
import StatusBadge from "../components/StatusBadge.vue";
import { toast, errorMessage } from "../toast";
import { auth } from "../auth";

const loading = ref(true);
const error = ref<string | null>(null);
const assets = ref<Asset[]>([]);
const isAdmin = auth.isAdmin();

const showCreate = ref(false);
const form = ref({
  name: "", environment: "sandbox", asset_type: "service", criticality: "medium",
  owner: "", container_name: "",
});
const busy = ref(false);

async function load() {
  loading.value = true;
  error.value = null;
  try {
    const page = await assetsApi.list({ limit: 200 });
    assets.value = page.items;
  } catch (e) {
    error.value = errorMessage(e);
  } finally {
    loading.value = false;
  }
}

async function create() {
  if (!form.value.name) {
    toast("请填写资产名", true);
    return;
  }
  busy.value = true;
  try {
    await assetsApi.create({
      name: form.value.name,
      environment: form.value.environment,
      asset_type: form.value.asset_type,
      criticality: form.value.criticality,
      owner: form.value.owner || undefined,
      endpoints: form.value.container_name ? { container_name: form.value.container_name } : undefined,
    });
    toast("资产已登记");
    showCreate.value = false;
    await load();
  } catch (e) {
    toast(errorMessage(e), true);
  } finally {
    busy.value = false;
  }
}

onMounted(load);
</script>

<template>
  <div class="panel">
    <div class="toolbar">
      <h3 style="margin:0;flex:1">资产与环境</h3>
      <button v-if="isAdmin" class="btn btn-primary" @click="showCreate = !showCreate">+ 登记资产</button>
    </div>

    <div v-if="showCreate && isAdmin" class="panel" style="background:#fafbfc">
      <div class="form-grid">
        <label class="f">名称 *</label><input v-model="form.name" placeholder="如 eval-orders" />
        <label class="f">环境 *</label>
        <select v-model="form.environment">
          <option v-for="e in ['sandbox','staging','production']" :key="e" :value="e">{{ e }}</option>
        </select>
        <label class="f">类型 *</label>
        <select v-model="form.asset_type">
          <option v-for="t in ['service','container','database','cache','gateway','host']" :key="t" :value="t">{{ t }}</option>
        </select>
        <label class="f">关键度</label>
        <select v-model="form.criticality">
          <option v-for="c in ['low','medium','high']" :key="c" :value="c">{{ c }}</option>
        </select>
        <label class="f">负责人</label><input v-model="form.owner" />
        <label class="f">容器名</label><input v-model="form.container_name" placeholder="docker 容器名（可选）" />
      </div>
      <button class="btn btn-primary" style="margin-top:10px" :disabled="busy" @click="create">
        {{ busy ? "提交中…" : "登记" }}
      </button>
    </div>

    <StateBlock :loading="loading" :error="error" :empty="!assets.length" empty-text="暂无资产" @retry="load">
      <table class="tbl">
        <thead><tr><th>名称</th><th>环境</th><th>类型</th><th>关键度</th><th>状态</th><th>负责人</th><th>标签</th></tr></thead>
        <tbody>
          <tr v-for="a in assets" :key="a.id">
            <td>{{ a.name }}<div class="mono muted">{{ a.id }}</div></td>
            <td><StatusBadge :value="a.environment" /></td>
            <td>{{ a.asset_type }}</td>
            <td><StatusBadge :value="a.criticality" /></td>
            <td><StatusBadge :value="a.status" /></td>
            <td class="muted">{{ a.owner ?? "—" }}</td>
            <td><span v-for="t in a.tags" :key="t" class="badge b-gray" style="margin-right:4px">{{ t }}</span></td>
          </tr>
        </tbody>
      </table>
    </StateBlock>
  </div>
</template>
