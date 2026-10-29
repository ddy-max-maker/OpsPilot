import { defineConfig, devices } from "@playwright/test";

// E2E：真实后端（127.0.0.1:8000 经 vite 代理），不使用 Mock
// 前置：docker compose 栈运行中（api/worker/ops-action/postgres/rabbitmq/demo 容器）
export default defineConfig({
  testDir: "./e2e",
  timeout: 300_000, // 诊断链路可能分钟级
  expect: { timeout: 15_000 },
  fullyParallel: false, // 顺序执行：验收路径有状态依赖
  workers: 1,
  reporter: [["list"], ["html", { open: "never" }]],
  use: {
    baseURL: "http://localhost:5173",
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
    actionTimeout: 15_000,
  },
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"] } }],
  webServer: {
    command: "npm run dev",
    url: "http://localhost:5173",
    reuseExistingServer: true,
    timeout: 60_000,
  },
});
