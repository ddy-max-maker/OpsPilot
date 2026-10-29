<script setup lang="ts">
// 异步区块统一状态：加载 / 空态 / 失败（含重试）/ 正常
defineProps<{
  loading: boolean;
  error?: string | null;
  empty?: boolean;
  emptyText?: string;
}>();
const emit = defineEmits<{ retry: [] }>();
</script>

<template>
  <div v-if="loading" class="state"><span class="spinner"></span> 加载中…</div>
  <div v-else-if="error" class="state error">
    <div>{{ error }}</div>
    <button class="btn" style="margin-top:10px" @click="emit('retry')">重试</button>
  </div>
  <div v-else-if="empty" class="state">{{ emptyText ?? "暂无数据" }}</div>
  <slot v-else />
</template>
