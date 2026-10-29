<script setup lang="ts">
import { computed } from "vue";
import { useRoute, useRouter } from "vue-router";
import { auth } from "./auth";
import { toastState } from "./toast";
import Toasts from "./components/Toasts.vue";

const route = useRoute();
const router = useRouter();
const activeNav = computed(() => (route.meta.nav as string) ?? "workbench");
const me = computed(() => auth.state.me);

const navs = [
  { key: "workbench", label: "工作台与事件", to: "/" },
  { key: "diagnostics", label: "诊断详情", to: "/diagnostics" },
  { key: "knowledge", label: "知识中心", to: "/knowledge" },
  { key: "approvals", label: "审批与恢复", to: "/approvals" },
  { key: "platform", label: "平台与质量", to: "/platform" },
];

function logout() {
  auth.logout();
  void toastState;
  void router.push("/login");
}
</script>

<template>
  <div v-if="route.name === 'login'" class="shell-full"><router-view /></div>
  <div v-else class="shell">
    <header class="topbar">
      <div class="brand">OpsPilot Console</div>
      <nav class="nav">
        <router-link v-for="n in navs" :key="n.key" :to="n.to"
          class="nav-item" :class="{ active: activeNav === n.key }">{{ n.label }}</router-link>
      </nav>
      <div class="userbox" v-if="me">
        <span class="muted">{{ me.username }}</span>
        <span class="role-tag" v-for="r in me.roles" :key="r">{{ r }}</span>
        <button class="btn btn-ghost" @click="logout">退出</button>
      </div>
    </header>
    <main class="content"><router-view /></main>
    <Toasts />
  </div>
</template>
