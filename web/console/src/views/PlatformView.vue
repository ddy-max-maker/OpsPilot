<script setup lang="ts">
// 平台域：Connector 实例 + Capability + 资产绑定
import { onMounted, ref } from "vue";
import { assetsApi, assetsApi as _a, integrationsApi } from "../api";
import type { Asset, Capability, IntegrationType } from "../types";
import StateBlock from "../components/StateBlock.vue";
import StatusBadge from "../components/StatusBadge.vue";
import { toast, errorMessage } from "../toast";
import { auth } from "../auth";

const loading = ref(true);
const error = ref<string | null>(null);
const integrations = ref<Array<{ id: string; name: string; connector_type: string; status: string }>>([]);
const types = ref<IntegrationType[]>([]);
const capabilities = ref<Capability[]>([]);
const assets = ref<Asset[]>([]);
const isAdmin = auth.hasRole("admin");

const showCreate = ref(false);
const cForm = ref({ connector_type: "", name: "" });
const busy = ref("");

const bindAsset = ref("");
const bindInstance = ref("");
const bindKind = ref("metrics");

async function load() {
  loading.value = true;
  error.value = null;
  try {
    const jobs: Array<Promise<void>> = [
      integrationsApi.capabilities().then((r) => { capabilities.value = r.capabilities ?? []; }),
      assetsApi.list({ limit: 200 }).then((r) => { assets.value = r.items ?? []; }),
    ];
    if (isAdmin) {
      jobs.push(
        integrationsApi.list().then((r) => {
          // 后端返回 {types, instances, tests}（契约见 app/api/v1/integrations.py）；
          // 早期前端按 {items} 解析，导致 instances 为 undefined、整页渲染失败
          integrations.value = r.instances ?? [];
          types.value = r.types ?? [];
          if (!cForm.value.connector_type && (r.types ?? []).length) {
            cForm.value.connector_type = r.types[0].code;
          }
        }),
      );
    }
    await Promise.all(jobs);
  } catch (e) {
    error.value = errorMessage(e);
  } finally {
    loading.value = false;
  }
}

async function createIntegration() {
  if (!cForm.value.name) {
    toast("请填写名称", true);
    return;
  }
  busy.value = "create";
  try {
    await integrationsApi.create({ connector_type: cForm.value.connector_type, name: cForm.value.name });
    toast("连接器实例已创建");
    showCreate.value = false;
    await load();
  } catch (e) {
    toast(errorMessage(e), true);
  } finally {
    busy.value = "";
  }
}

async function testIntegration(id: string) {
  busy.value = `test-${id}`;
  try {
    const r = await integrationsApi.test(id);
    toast(`连通性测试：${JSON.stringify(r).slice(0, 160)}`);
  } catch (e) {
    toast(errorMessage(e), true);
  } finally {
    busy.value = "";
  }
}

async function toggle(inst: { id: string; status: string }) {
  busy.value = `toggle-${inst.id}`;
  try {
    await (inst.status === "enabled" ? integrationsApi.disable(inst.id) : integrationsApi.enable(inst.id));
    await load();
  } catch (e) {
    toast(errorMessage(e), true);
  } finally {
    busy.value = "";
  }
}

async function upsertBinding() {
  if (!bindAsset.value || !bindInstance.value) {
    toast("请选择资产与实例", true);
    return;
  }
  busy.value = "bind";
  try {
    await assetsApi.upsertBinding(bindAsset.value, { instance_id: bindInstance.value, binding_kind: bindKind.value });
    toast("绑定已保存");
  } catch (e) {
    toast(errorMessage(e), true);
  } finally {
    busy.value = "";
  }
}

async function testCapability(key: string) {
  busy.value = `cap-${key}`;
  try {
    const r = await integrationsApi.testCapability(key, bindAsset.value || undefined);
    toast(`能力测试：${JSON.stringify(r).slice(0, 160)}`);
  } catch (e) {
    toast(errorMessage(e), true);
  } finally {
    busy.value = "";
  }
}

onMounted(load);
</script>

<template>
  <div class="panel" v-if="isAdmin">
    <div class="toolbar">
      <h3 style="margin:0;flex:1">Connector 实例（接入管理）</h3>
      <button class="btn btn-primary" @click="showCreate = !showCreate">+ 新建实例</button>
    </div>
    <div v-if="showCreate" class="panel" style="background:#fafbfc">
      <div class="form-grid">
        <label class="f">类型 *</label>
        <select v-model="cForm.connector_type">
          <option v-for="t in types" :key="t.code" :value="t.code">
            {{ t.code }} — {{ t.display_name }}
          </option>
        </select>
        <label class="f">名称 *</label><input v-model="cForm.name" />
      </div>
      <button class="btn btn-primary" style="margin-top:10px" :disabled="busy === 'create'" @click="createIntegration">创建</button>
    </div>
    <StateBlock :loading="loading" :error="error" :empty="!integrations.length" empty-text="暂无连接器实例" @retry="load">
      <table class="tbl">
        <thead><tr><th>名称</th><th>类型</th><th>状态</th><th>操作</th></tr></thead>
        <tbody>
          <tr v-for="i in integrations" :key="i.id">
            <td>{{ i.name }}<div class="mono muted">{{ i.id }}</div></td>
            <td class="mono">{{ i.connector_type }}</td>
            <td><StatusBadge :value="i.status" /></td>
            <td>
              <button class="btn btn-sm" :disabled="busy === `test-${i.id}`" @click="testIntegration(i.id)">测试</button>
              <button class="btn btn-sm" :disabled="busy === `toggle-${i.id}`" @click="toggle(i)">
                {{ i.status === "enabled" ? "停用" : "启用" }}
              </button>
            </td>
          </tr>
        </tbody>
      </table>
    </StateBlock>

    <div class="panel" style="background:#fafbfc;margin-top:12px">
      <h3>资产可观测性绑定</h3>
      <div class="toolbar">
        <select v-model="bindAsset" style="width:220px">
          <option value="">— 资产 —</option>
          <option v-for="a in assets" :key="a.id" :value="a.id">{{ a.name }}（{{ a.environment }}）</option>
        </select>
        <select v-model="bindInstance" style="width:220px">
          <option value="">— 连接器实例 —</option>
          <option v-for="i in integrations" :key="i.id" :value="i.id">{{ i.name }}</option>
        </select>
        <select v-model="bindKind" style="width:120px">
          <option v-for="k in ['metrics','logs','traces','health']" :key="k" :value="k">{{ k }}</option>
        </select>
        <button class="btn btn-primary" :disabled="busy === 'bind'" @click="upsertBinding">保存绑定</button>
      </div>
    </div>
  </div>

  <div class="panel">
    <h3>Capability Manifest（只读工具能力，source_type 与配额）</h3>
    <StateBlock :loading="loading" :error="error" :empty="!capabilities.length" empty-text="暂无能力" @retry="load">
      <table class="tbl">
        <thead><tr><th>能力</th><th>连接器</th><th>数据源</th><th>只读</th><th>限流</th><th>超时</th><th>操作</th></tr></thead>
        <tbody>
          <tr v-for="c in capabilities" :key="c.capability_key">
            <td class="mono">{{ c.capability_key }}</td>
            <td class="mono">{{ c.collector }}</td>
            <td><span class="badge b-gray">{{ c.source_type }}</span></td>
            <td><StatusBadge :value="c.read_only ? 'passed' : 'failed'" /></td>
            <td class="mono muted">
              {{ c.rate_limit?.max_calls_per_run ?? "—" }}/run ·
              {{ c.rate_limit?.per_second ?? "—" }}/s
            </td>
            <td class="mono muted">{{ c.timeout_ms ?? "—" }}ms</td>
            <td>
              <button v-if="isAdmin" class="btn btn-sm" :disabled="busy === `cap-${c.capability_key}`" @click="testCapability(c.capability_key)">测试</button>
            </td>
          </tr>
        </tbody>
      </table>
    </StateBlock>
  </div>
</template>
