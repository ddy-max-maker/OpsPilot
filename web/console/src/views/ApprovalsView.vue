<script setup lang="ts">
// 审批与恢复域：Proposal 列表
import { onMounted, ref } from "vue";
import { proposalsApi } from "../api";
import type { Proposal } from "../types";
import StateBlock from "../components/StateBlock.vue";
import StatusBadge from "../components/StatusBadge.vue";
import { errorMessage } from "../toast";

const loading = ref(true);
const error = ref<string | null>(null);
const proposals = ref<Proposal[]>([]);
const fStatus = ref("");

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
      <button class="btn" @click="load">刷新</button>
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
            <td class="mono muted">{{ p.created_by_user?.slice(0,14) ?? p.created_by.slice(0,14) }}</td>
            <td class="mono muted">{{ p.created_at?.slice(0,19).replace("T"," ") }}</td>
            <td><router-link :to="`/approvals/${p.id}`" class="btn btn-sm">详情</router-link></td>
          </tr>
        </tbody>
      </table>
    </StateBlock>
  </div>
</template>
