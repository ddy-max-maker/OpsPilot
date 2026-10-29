// E2E 共享：账号与登录助手（真实后端，账号由种子脚本/SQL 预建于开发库）
import { type Page, expect } from "@playwright/test";

export const E2E_PASSWORD = "E2ePassw0rd!2026";

export async function login(page: Page, username: string): Promise<void> {
  await page.goto("/login");
  const inputs = page.locator("input");
  await inputs.nth(0).fill(username);
  await inputs.nth(1).fill(E2E_PASSWORD);
  await page.getByRole("button", { name: "登录" }).click();
  await expect(page.locator(".userbox")).toContainText(username, { timeout: 15_000 });
}

export async function logout(page: Page): Promise<void> {
  await page.getByRole("button", { name: "退出" }).click();
  await page.waitForURL("**/login");
}
