<script setup lang="ts">
// 全局 toast 渲染（逻辑在 src/toast.ts）
import { toastState } from "../toast";
</script>

<template>
  <div v-for="t in toastState.list" :key="t.id" class="toast" :class="{ err: t.err }">{{ t.text }}</div>
</template>
