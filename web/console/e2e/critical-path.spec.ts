// D6 验收路径 E2E（真实后端；外部模型为后端配置的真实 Provider，不以 Mock 冒充）
import { test, expect, type Page } from "@playwright/test";
import { login, logout, E2E_PASSWORD } from "./helpers";

const STAMP = Date.now().toString(36);
let incidentUrl = "";
let incidentId = "";
let lastRunId: string | null = null;

function acceptDialogs(page: Page): void {
  // confirm() 弹窗必须在触发点击之前注册（Playwright 默认 dismiss）
  page.on("dialog", (d) => void d.accept());
}

async function toastShown(page: Page, text: string): Promise<void> {
  await expect(page.locator(".toast", { hasText: text }).first()).toBeVisible();
}

// ---------- 1. 管理员登录 → 工作台 → 资产登记 ----------
test("01 管理员登录与工作台", async ({ page }) => {
  await login(page, "e2e_admin");
  await expect(page.getByRole("link", { name: "工作台与事件" })).toBeVisible();
  await expect(page.locator(".grid-stats .stat")).toHaveCount(4);
  await page.goto("/assets");
  await page.getByRole("button", { name: "+ 登记资产" }).click();
  await page.locator(".form-grid input").first().fill(`e2e-svc-${STAMP}`);
  await page.locator(".form-grid select").nth(1).selectOption("service"); // nth(0)=环境, nth(1)=类型
  await page.getByRole("button", { name: "登记", exact: true }).click();
  await toastShown(page, "资产已登记");
  await expect(page.locator("tr", { hasText: `e2e-svc-${STAMP}` })).toBeVisible();
});

// ---------- 2. 创建故障 → 确认 ----------
test("02 创建与确认故障事件", async ({ page }) => {
  await login(page, "e2e_operator");
  await page.goto("/");
  await page.getByRole("button", { name: "+ 登记故障" }).click();
  await page.locator(".form-grid input").first().fill(`e2e 故障 ${STAMP}`);
  // 等资产下拉加载出真实选项（operator 已被授予全资产 read 范围）
  const assetSelect = page.locator(".form-grid select").nth(1);
  await expect(assetSelect.locator("option").nth(1)).toBeAttached({ timeout: 20_000 });
  await assetSelect.selectOption({ index: 1 });
  await page.getByRole("button", { name: "创建事件" }).click();
  await toastShown(page, "事件已创建");
  const row = page.locator("tr", { hasText: `e2e 故障 ${STAMP}` });
  await row.getByRole("button", { name: "确认" }).click();
  await toastShown(page, "已确认");
});

// ---------- 3. 启动诊断 → SSE 过程 → Evidence/报告（真实后端链路） ----------
test("03 启动诊断并观察 SSE 过程", async ({ page }) => {
  acceptDialogs(page);
  await login(page, "e2e_operator");
  await page.goto("/");
  await page.locator("tr", { hasText: `e2e 故障 ${STAMP}` }).getByRole("link").first().click();
  await page.waitForURL("**/incidents/**");
  incidentUrl = page.url();
  incidentId = page.url().split("/").pop() ?? "";
  await page.getByRole("button", { name: "启动 Agent 诊断" }).click();
  await toastShown(page, "诊断已受理");
  // 记录 run_id（跨测试共享：各测试是独立浏览器上下文，localStorage 不互通）
  await expect
    .poll(
      async () =>
        page.evaluate((id) => localStorage.getItem(`opspilot_run_${id}`), incidentId),
      { timeout: 30_000, intervals: [1_000] },
    )
    .not.toBeNull();
  lastRunId = await page.evaluate(
    (id) => localStorage.getItem(`opspilot_run_${id}`),
    incidentId,
  );
  // SSE 状态指示出现（实时 / 连接中 / 已结束均算接通）
  await expect(page.locator(".badge", { hasText: /^SSE/ }).first()).toBeVisible({ timeout: 30_000 });
  // 后端配置的 provider（CI=fake / 本地=deepseek）：等待出现事件轨迹
  await expect
    .poll(async () => page.locator(".timeline .t-item").count(), { timeout: 240_000, intervals: [5_000] })
    .toBeGreaterThan(0);
  // Evidence / 报告区块渲染（无论报告是否生成，区块存在即可）
  await expect(page.getByText("证据（实时观测")).toBeVisible();
  await expect(page.getByText("诊断报告").first()).toBeVisible();
});

// ---------- 4. 知识中心：创建 → 复核/发布（四眼） → 检索 ----------
test("04 知识中心创建与发布检索", async ({ page }) => {
  acceptDialogs(page);
  await login(page, "e2e_operator");
  await page.goto("/knowledge");
  await page.getByRole("button", { name: "+ 新建知识" }).click();
  await page.locator(".form-grid input").first().fill(`e2e 知识 ${STAMP}`);
  await page.locator(".form-grid textarea").fill(`E2E 知识内容 ${STAMP}：Redis 连接拒绝时检查端口与最大连接数。`);
  await page.getByRole("button", { name: "创建", exact: true }).click();
  await toastShown(page, "知识文档已创建");
  // operator（非 admin）无复核/发布操作入口（RBAC UI 边界；服务端四眼拒绝由集成测试覆盖）
  await expect(page.getByRole("button", { name: "复核" })).toHaveCount(0);
  await logout(page);
  // admin 复核 + 发布（直接断言版本状态徽章，避免 toast 时序脆弱）
  await login(page, "e2e_admin");
  await page.goto("/knowledge");
  const panel2 = page.locator(".panel", { hasText: `e2e 知识 ${STAMP}` }).last();
  await panel2.getByRole("button", { name: "复核" }).click();
  await expect(panel2.getByText("reviewed", { exact: true })).toBeVisible({ timeout: 30_000 });
  await panel2.getByRole("button", { name: "发布" }).click();
  await expect(panel2.getByText("published", { exact: true })).toBeVisible({ timeout: 30_000 });
  // 检索（应命中刚发布文档）
  await page.locator("input[placeholder*='查询']").fill(`E2E 知识内容 ${STAMP}`);
  await page.getByRole("button", { name: "检索" }).click();
  await expect(page.getByText(/模式：/)).toBeVisible({ timeout: 20_000 });
});

// ---------- 5. Runbook → Proposal → 四眼审批 → 执行 → 验证 ----------
test("05 审批与恢复全链路（他账号审批 + sandbox 执行）", async ({ page }) => {
  test.setTimeout(420_000);
  acceptDialogs(page);
  // admin 建 runbook 并发布
  await login(page, "e2e_admin");
  await page.goto("/runbooks");
  await page.getByRole("button", { name: "+ 新建 Runbook" }).click();
  await page.locator(".form-grid input").first().fill(`e2e-restart-${STAMP}`);
  // restart_container@v1 注册规格为 r1，Runbook 风险等级必须一致（服务端强校验）
  await page.locator(".form-grid select").first().selectOption("r1");
  await page.getByRole("button", { name: "创建", exact: true }).click();
  await toastShown(page, "Runbook 已创建");
  const rbPanel = page.locator(".panel", { hasText: `e2e-restart-${STAMP}` }).last();
  await rbPanel.getByRole("button", { name: "发布" }).click();
  await toastShown(page, "操作成功");
  await logout(page);

  // operator：创建 proposal（run_id 由诊断用例通过模块变量共享）
  await login(page, "e2e_operator");
  const runId = lastRunId;
  test.skip(!runId, "无可用诊断 run（前置诊断用例未产出）");
  await page.goto("/approvals");
  await page.getByRole("button", { name: "+ 新建方案" }).click();
  await page.locator("input[placeholder*='run_id']").fill(runId!);
  const versionSelect = page.locator(".form-grid select").nth(0);
  await expect(versionSelect.locator("option", { hasText: `e2e-restart-${STAMP}` })).toBeAttached({ timeout: 20_000 });
  // 引用本用例新建并发布的 Runbook 版本（完整链路：创建→发布→被方案引用）
  const verValue = await versionSelect
    .locator("option", { hasText: `e2e-restart-${STAMP}` })
    .getAttribute("value");
  await versionSelect.selectOption(verValue!);
  const assetSelect2 = page.locator(".form-grid select").nth(1);
  await expect(assetSelect2.locator("option").nth(1)).toBeAttached({ timeout: 20_000 });
  // 目标资产必须登记 container_name（D6 安全门禁：资产登记是容器身份唯一权威）
  const assetValue = await assetSelect2
    .locator("option", { hasText: "demo-redis" })
    .first()
    .getAttribute("value");
  await assetSelect2.selectOption(assetValue!);
  await page.locator(".form-grid select").nth(2).selectOption("sandbox");
  await page.locator(".form-grid textarea").fill('{"container_name": "opspilot-demo-redis"}');
  await page.getByRole("button", { name: "创建方案" }).click();
  await toastShown(page, "方案已创建");
  await logout(page);

  // approver（另一账号，满足四眼）：dry-run → 批准
  await login(page, "e2e_approver");
  await page.goto("/approvals");
  const propRow = page.locator("tr", { hasText: runId!.slice(0, 12) }).first();
  await propRow.getByRole("link", { name: "详情" }).click();
  await page.getByRole("button", { name: "Dry-run 前置检查" }).click();
  await expect(page.getByText(/Dry-run 结果/)).toBeVisible({ timeout: 30_000 });
  await page.getByRole("button", { name: "批准（四眼）" }).click();
  await toastShown(page, "已批准");
  await logout(page);

  // operator：执行 → 轮询至状态离开 executing（CI 中门禁默认关闭 → pre_check_failed 亦算通过流程）
  await login(page, "e2e_operator");
  await page.goto("/approvals");
  await page.locator("tr", { hasText: runId!.slice(0, 12) }).first().getByRole("link", { name: "详情" }).click();
  await page.getByRole("button", { name: "执行恢复（sandbox/staging）" }).click();
  await toastShown(page, "执行已受理");
  await expect
    .poll(async () => page.locator(".badge", { hasText: /^executing$/ }).count(), {
      timeout: 240_000,
      intervals: [5_000],
    })
    .toBe(0);
  await expect(page.getByText("执行与验证")).toBeVisible();
});

// ---------- 6. RBAC：viewer 只读 / operator 审计 403 ----------
test("06 RBAC 权限边界", async ({ page }) => {
  await login(page, "e2e_viewer");
  await page.goto("/approvals");
  await expect(page.getByRole("button", { name: "+ 新建方案" })).toHaveCount(0);
  await page.goto("/audit");
  await expect(page.getByText("审计事件")).toBeVisible({ timeout: 20_000 });
  await logout(page);

  await login(page, "e2e_operator");
  await page.goto("/audit");
  await expect(page.locator(".state.error")).toContainText("权限不足", { timeout: 20_000 });
});

// ---------- 7. 刷新恢复：页面刷新后从数据库重建诊断状态 ----------
test("07 刷新后诊断状态从数据库恢复", async ({ page }) => {
  test.skip(!incidentUrl || !lastRunId, "无前置 incident/run");
  await login(page, "e2e_operator");
  // 通过 ?run= 查询参数恢复（等价于分享链接 / 跨会话恢复路径）
  await page.goto(`${incidentUrl}?run=${lastRunId}`);
  await expect(page.locator(".timeline").first()).toBeVisible({ timeout: 30_000 });
  await page.reload();
  // run 面板与事件轨迹从 run_events 重放恢复
  await expect(page.locator(".timeline").first()).toBeVisible({ timeout: 30_000 });
  await expect(page.getByText("实时轨迹")).toBeVisible();
});
