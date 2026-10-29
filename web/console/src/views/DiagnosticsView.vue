<script setup lang="ts">
// 诊断详情域：按 run 标识查看（支持粘贴 run_id）+ 快捷入口说明
import { ref } from "vue";
import RunPanel from "../components/RunPanel.vue";

const runId = ref("");
const activeRun = ref("");

function open() {
  if (runId.value.trim()) activeRun.value = runId.value.trim();
}
</script>

<template>
  <div class="panel">
    <h3>诊断运行查看</h3>
    <p class="muted" style="margin-top:0">
      诊断运行从「工作台与事件」→ 事件详情页发起；此处可按 run 标识直接查看任意运行的轨迹、证据与报告。
    </p>
    <div class="toolbar">
      <input v-model="runId" placeholder="run_id，如 run_xxx" style="width:340px" @keyup.enter="open" />
      <button class="btn btn-primary" :disabled="!runId.trim()" @click="open">查看</button>
    </div>
  </div>
  <RunPanel v-if="activeRun" :run-id="activeRun" :key="activeRun" />
</template>
