<script setup lang="ts">
// 平台域：审计事件（admin/viewer）
import { onMounted, ref } from "vue";
import { auditApi } from "../api";
import type { AuditEvent } from "../types";
import StateBlock from "../components/StateBlock.vue";
import { errorMessage } from "../toast";

const loading = ref(true);
const error = ref<string | null>(null);
const items = ref<AuditEvent[]>([]);
const nextCursor = ref<string | null>(null);
const hasMore = ref(false);
const fActor = ref("");
const busyMore = ref(false);

async function load(reset = true) {
  if (reset) {
    loading.value = true;
    items.value = [];
    nextCursor.value = null;
  }
  error.value = null;
  try {
    const page = await auditApi.list({
      actor_type: fActor.value || undefined,
      limit: 50,
      cursor: reset ? undefined : (nextCursor.value ?? undefined),
    });
    items.value = reset ? page.items : [...items.value, ...page.items];
    nextCursor.value = page.next_cursor;
    hasMore.value = page.has_more;
  } catch (e) {
    error.value = errorMessage(e);
  } finally {
    loading.value = false;
  }
}

async function more() {
  busyMore.value = true;
  await load(false);
  busyMore.value = false;
}

onMounted(() => load());
</script>

<template>
  <div class="panel">
    <div class="toolbar">
      <h3 style="margin:0;flex:1">审计事件（只追加，不可篡改）</h3>
      <select v-model="fActor" @change="load()">
        <option value="">全部主体</option>
        <option v-for="a in ['user','agent','system','action_service']" :key="a" :value="a">{{ a }}</option>
      </select>
      <button class="btn" @click="load()">刷新</button>
    </div>
    <StateBlock :loading="loading" :error="error" :empty="!items.length" empty-text="暂无审计事件" @retry="() => load()">
      <table class="tbl">
        <thead><tr><th>时间</th><th>主体</th><th>动作</th><th>资源</th><th>环境</th><th>详情</th><th>request_id</th></tr></thead>
        <tbody>
          <tr v-for="e in items" :key="e.id">
            <td class="mono muted" style="font-size:11px;white-space:nowrap">{{ e.created_at?.slice(0,19).replace("T"," ") }}</td>
            <td><span class="badge b-gray">{{ e.actor_type }}</span> <span class="mono muted" style="font-size:11px" :title="e.actor_id">{{ e.actor_id.slice(0,12) }}…</span></td>
            <td class="mono">{{ e.action }}</td>
            <td class="mono muted" style="font-size:11px">{{ e.resource_type ?? "" }} {{ e.resource_id ?? "" }}</td>
            <td>{{ e.environment ?? "—" }}</td>
            <td class="mono" style="font-size:11px;max-width:240px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">
              {{ e.detail ? JSON.stringify(e.detail) : "—" }}
            </td>
            <td class="mono muted" style="font-size:10px" :title="e.request_id">{{ e.request_id.slice(0,10) }}…</td>
          </tr>
        </tbody>
      </table>
      <div style="text-align:center;margin-top:10px" v-if="hasMore">
        <button class="btn" :disabled="busyMore" @click="more">{{ busyMore ? "加载中…" : "加载更多" }}</button>
      </div>
    </StateBlock>
  </div>
</template>
