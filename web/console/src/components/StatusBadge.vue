<script setup lang="ts">
// 状态徽章：统一状态 → 颜色映射
const props = defineProps<{ value: string; map?: Record<string, string> }>();
const DEFAULT_MAP: Record<string, string> = {
  // 成功/健康类
  completed: "b-green", succeeded: "b-green", passed: "b-green", verified: "b-green",
  published: "b-green", approved: "b-green", active: "b-green", ok: "b-green",
  enabled: "b-green", resolved: "b-green", closed: "b-gray", supported: "b-green",
  // 进行中类
  running: "b-blue", executing: "b-blue", investigating: "b-blue", in_progress: "b-blue",
  acknowledged: "b-blue", queued: "b-blue", pending: "b-blue", pending_approval: "b-amber",
  waiting_approval: "b-amber", draft: "b-gray", reviewed: "b-blue", candidate: "b-blue",
  monitoring: "b-blue", created: "b-gray",
  // 警示类
  failed: "b-red", rejected: "b-red", refuted: "b-red", error: "b-red", retired: "b-gray",
  superseded: "b-gray", verify_failed: "b-red", execution_failed: "b-red",
  // 中性/未知类
  unknown: "b-amber", execution_unknown: "b-amber", verify_unknown: "b-amber",
  canceled: "b-gray", canceled_: "b-gray", skipped: "b-gray", needs_human: "b-amber",
  insufficient_data: "b-amber", budget_exhausted: "b-amber", no_new_evidence: "b-amber",
  unknown_exit: "b-amber", mitigating: "b-purple", open: "b-amber", expired: "b-gray",
  pre_check_failed: "b-red", timeout: "b-amber", unavailable: "b-red", final: "b-blue",
  amended: "b-purple", low: "b-gray", medium: "b-blue", high: "b-amber", critical: "b-red",
  sandbox: "b-green", staging: "b-amber", production: "b-red",
  metric: "b-blue", log: "b-purple", trace: "b-amber", health: "b-green", infra: "b-gray",
  knowledge: "b-purple",
};
const cls = (props.map ?? DEFAULT_MAP)[props.value] ?? "b-gray";
</script>

<template>
  <span class="badge" :class="cls">{{ value }}</span>
</template>
