// 全局 toast 状态（供任意模块导入使用）
import { reactive } from "vue";

interface Toast { id: number; text: string; err: boolean }
export const toastState = reactive<{ list: Toast[] }>({ list: [] });
let seq = 0;

export function toast(text: string, err = false): void {
  const id = ++seq;
  toastState.list.push({ id, text, err });
  setTimeout(() => {
    const i = toastState.list.findIndex((t) => t.id === id);
    if (i >= 0) toastState.list.splice(i, 1);
  }, 4000);
}

export function errorMessage(e: unknown): string {
  const anyE = e as { code?: string; message?: string; status?: number };
  if (anyE?.code === "FORBIDDEN") return "权限不足（403）";
  if (anyE?.code === "NOT_FOUND") return "资源不存在或无权访问（404）";
  if (anyE?.status === 401) return "登录已过期，请重新登录（401）";
  return anyE?.message ?? String(e);
}
