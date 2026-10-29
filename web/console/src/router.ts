import { createRouter, createWebHistory } from "vue-router";
import { auth } from "./auth";
import { setUnauthorizedHandler } from "./api/http";

const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: "/login", name: "login", component: () => import("./views/LoginView.vue") },
    { path: "/change-password", name: "change-password", component: () => import("./views/ChangePasswordView.vue") },
    { path: "/", name: "dashboard", component: () => import("./views/DashboardView.vue"), meta: { nav: "workbench" } },
    { path: "/incidents/:id", name: "incident", component: () => import("./views/IncidentDetailView.vue"), meta: { nav: "workbench" } },
    { path: "/assets", name: "assets", component: () => import("./views/AssetsView.vue"), meta: { nav: "workbench" } },
    { path: "/diagnostics", name: "diagnostics", component: () => import("./views/DiagnosticsView.vue"), meta: { nav: "diagnostics" } },
    { path: "/knowledge", name: "knowledge", component: () => import("./views/KnowledgeView.vue"), meta: { nav: "knowledge" } },
    { path: "/runbooks", name: "runbooks", component: () => import("./views/RunbooksView.vue"), meta: { nav: "knowledge" } },
    { path: "/approvals", name: "approvals", component: () => import("./views/ApprovalsView.vue"), meta: { nav: "approvals" } },
    { path: "/approvals/:id", name: "approval-detail", component: () => import("./views/ProposalDetailView.vue"), meta: { nav: "approvals" } },
    { path: "/platform", name: "platform", component: () => import("./views/PlatformView.vue"), meta: { nav: "platform" } },
    { path: "/evaluations", name: "evaluations", component: () => import("./views/EvaluationsView.vue"), meta: { nav: "platform" } },
    { path: "/audit", name: "audit", component: () => import("./views/AuditView.vue"), meta: { nav: "platform" } },
    { path: "/:pathMatch(.*)*", redirect: "/" },
  ],
});

// 401 统一跳登录
setUnauthorizedHandler(() => {
  auth.logout();
  if (router.currentRoute.value.name !== "login") {
    void router.push({ name: "login" });
  }
});

router.beforeEach(async (to) => {
  if (to.name === "login") return true;
  if (!auth.isAuthenticated) return { name: "login" };
  if (!auth.state.me) {
    const me = await auth.fetchMe();
    if (!me) return { name: "login" };
    if (me.must_change_password && to.name !== "change-password") {
      return { name: "change-password" };
    }
  }
  return true;
});

export default router;
