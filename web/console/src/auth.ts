// 认证状态（reactive 单例，无 pinia）
import { computed, reactive } from "vue";
import { authApi } from "./api";
import { getToken, setToken } from "./api/http";
import type { MeData, RoleCode } from "./types";

interface AuthState {
  me: MeData | null;
  loading: boolean;
}

const state = reactive<AuthState>({ me: null, loading: false });

export const auth = {
  state,
  get token(): string | null {
    return getToken();
  },
  get isAuthenticated(): boolean {
    return !!getToken();
  },
  roles: computed<RoleCode[]>(() => state.me?.roles ?? []),
  hasRole(...roles: RoleCode[]): boolean {
    if (!state.me) return false;
    return roles.some((r) => state.me!.roles.includes(r));
  },
  isAdmin(): boolean {
    return !!state.me?.asset_scope?.is_admin;
  },

  async login(username: string, password: string): Promise<{ mustChangePassword: boolean }> {
    const token = await authApi.login(username, password);
    setToken(token.access_token);
    await this.fetchMe();
    return { mustChangePassword: token.must_change_password };
  },

  async fetchMe(): Promise<MeData | null> {
    state.loading = true;
    try {
      state.me = await authApi.me();
      return state.me;
    } catch {
      state.me = null;
      return null;
    } finally {
      state.loading = false;
    }
  },

  async changePassword(oldPassword: string, newPassword: string): Promise<void> {
    const r = await authApi.changePassword(oldPassword, newPassword);
    setToken(r.access_token); // 旧 token 即刻作废
    await this.fetchMe();
  },

  logout(): void {
    setToken(null);
    state.me = null;
  },
};
