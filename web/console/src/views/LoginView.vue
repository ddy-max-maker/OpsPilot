<script setup lang="ts">
import { ref } from "vue";
import { useRouter } from "vue-router";
import { auth } from "../auth";

const router = useRouter();
const username = ref("");
const password = ref("");
const busy = ref(false);
const error = ref<string | null>(null);

async function submit() {
  error.value = null;
  busy.value = true;
  try {
    const r = await auth.login(username.value, password.value);
    void router.push(r.mustChangePassword ? "/change-password" : "/");
  } catch (e) {
    const anyE = e as { status?: number; message?: string };
    error.value = anyE?.status === 401 ? "用户名或密码错误" : anyE?.message ?? "登录失败";
  } finally {
    busy.value = false;
  }
}
</script>

<template>
  <div class="panel" style="width: 380px">
    <h3 style="text-align:center">OpsPilot Console 登录</h3>
    <div class="form-grid" style="grid-template-columns: 80px 1fr">
      <label class="f">用户名</label>
      <input v-model="username" @keyup.enter="submit" autocomplete="username" />
      <label class="f">密码</label>
      <input v-model="password" type="password" @keyup.enter="submit" autocomplete="current-password" />
    </div>
    <div v-if="error" class="state error" style="padding:10px">{{ error }}</div>
    <button class="btn btn-primary" style="width:100%;margin-top:14px" :disabled="busy || !username || !password" @click="submit">
      {{ busy ? "登录中…" : "登录" }}
    </button>
  </div>
</template>
