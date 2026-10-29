<script setup lang="ts">
// Runbook 管理（知识中心域）：列表 + 版本 + admin 发布/退役
import { onMounted, ref } from "vue";
import { runbooksApi } from "../api";
import type { Runbook } from "../types";
import StateBlock from "../components/StateBlock.vue";
import StatusBadge from "../components/StatusBadge.vue";
import { toast, errorMessage } from "../toast";
import { auth } from "../auth";

const loading = ref(true);
const error = ref<string | null>(null);
const runbooks = ref<Runbook[]>([]);
const isAdmin = auth.hasRole("admin");
const busy = ref(new Set<string>());

const showCreate = ref(false);
const cForm = ref({
  name: "", description: "", risk_level: "r0", allowed_environments: "sandbox,staging",
  allowlist_action_keys: "restart_container@v1",
  steps_json: '[\n  {"action_key": "docker.restart_container", "args": {"container": "<container_name>"}}\n]',
});
const busyCreate = ref(false);

async function load() {
  loading.value = true;
  error.value = null;
  try {
    const r = await runbooksApi.list();
    runbooks.value = r.runbooks;
  } catch (e) {
    error.value = errorMessage(e);
  } finally {
    loading.value = false;
  }
}

async function create() {
  if (!cForm.value.name) {
    toast("请填写名称", true);
    return;
  }
  let steps: unknown;
  try {
    steps = JSON.parse(cForm.value.steps_json);
  } catch {
    toast("steps 不是合法 JSON", true);
    return;
  }
  busyCreate.value = true;
  try {
    await runbooksApi.create({
      name: cForm.value.name,
      description: cForm.value.description || undefined,
      risk_level: cForm.value.risk_level,
      steps,
      allowed_environments: cForm.value.allowed_environments
        .split(",").map((s) => s.trim()).filter(Boolean),
      allowlist_action_keys: cForm.value.allowlist_action_keys
        .split(",").map((s) => s.trim()).filter(Boolean),
    });
    toast("Runbook 已创建（v1 草稿）");
    showCreate.value = false;
    await load();
  } catch (e) {
    toast(errorMessage(e), true);
  } finally {
    busyCreate.value = false;
  }
}

async function versionAction(versionId: string, action: "publish" | "retire") {
  if (!confirm(`确认${action === "publish" ? "发布" : "退役"}版本？`)) return;
  busy.value.add(versionId);
  try {
    await (action === "publish" ? runbooksApi.publish(versionId) : runbooksApi.retire(versionId));
    toast("操作成功");
    await load();
  } catch (e) {
    toast(errorMessage(e), true);
  } finally {
    busy.value.delete(versionId);
  }
}

onMounted(load);
</script>

<template>
  <div class="panel">
    <div class="toolbar">
      <h3 style="margin:0;flex:1">Runbook（恢复方案模板，r0/r1 低风险）</h3>
      <button v-if="isAdmin" class="btn btn-primary" @click="showCreate = !showCreate">+ 新建 Runbook</button>
    </div>

    <div v-if="showCreate && isAdmin" class="panel" style="background:#fafbfc">
      <div class="form-grid">
        <label class="f">名称 *</label><input v-model="cForm.name" />
        <label class="f">风险级 *</label>
        <select v-model="cForm.risk_level"><option value="r0">r0</option><option value="r1">r1</option></select>
        <label class="f">描述</label><input v-model="cForm.description" />
        <label class="f">允许环境</label><input v-model="cForm.allowed_environments" placeholder="逗号分隔，production 被服务端硬排除" />
        <label class="f">动作白名单</label><input v-model="cForm.allowlist_action_keys" placeholder="逗号分隔，如 restart_container@v1（空 = 不可执行）" />
        <label class="f">步骤 JSON</label><textarea v-model="cForm.steps_json" class="mono" />
      </div>
      <button class="btn btn-primary" style="margin-top:10px" :disabled="busyCreate" @click="create">
        {{ busyCreate ? "创建中…" : "创建" }}
      </button>
    </div>

    <StateBlock :loading="loading" :error="error" :empty="!runbooks.length" empty-text="暂无 Runbook" @retry="load">
      <div v-for="rb in runbooks" :key="rb.id" class="panel" style="margin-bottom:10px;background:#fafbfc">
        <div class="toolbar" style="margin-bottom:8px">
          <strong>{{ rb.name }}</strong>
          <StatusBadge :value="rb.risk_level" />
          <StatusBadge :value="rb.status" />
          <span class="mono muted">{{ rb.id }}</span>
        </div>
        <p class="muted" style="margin:4px 0;font-size:13px">{{ rb.description ?? "" }}</p>
        <table class="tbl">
          <thead><tr><th>版本</th><th>状态</th><th>步骤</th><th>允许环境</th><th>动作白名单</th><th v-if="isAdmin">操作</th></tr></thead>
          <tbody>
            <tr v-for="v in rb.versions" :key="v.id">
              <td class="mono">v{{ v.version_no }}</td>
              <td><StatusBadge :value="v.status" /></td>
              <td class="mono" style="font-size:11px">{{ JSON.stringify(v.steps).slice(0, 160) }}</td>
              <td><span v-for="e in v.allowed_environments" :key="e" class="badge b-gray" style="margin-right:4px">{{ e }}</span></td>
              <td class="mono muted" style="font-size:11px">{{ v.allowlist_action_keys.join(", ") || "—" }}</td>
              <td v-if="isAdmin">
                <button v-if="v.status === 'draft'" class="btn btn-sm btn-primary" :disabled="busy.has(v.id)" @click="versionAction(v.id, 'publish')">发布</button>
                <button v-if="v.status === 'published'" class="btn btn-sm btn-danger" :disabled="busy.has(v.id)" @click="versionAction(v.id, 'retire')">退役</button>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
    </StateBlock>
  </div>
</template>
