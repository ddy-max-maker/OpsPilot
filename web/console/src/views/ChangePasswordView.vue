<script setup lang="ts">
import { ref } from "vue";
import { useRouter } from "vue-router";
import { auth } from "../auth";
import { toast, errorMessage } from "../toast";

const router = useRouter();
const oldPassword = ref("");
const newPassword = ref("");
const confirm2 = ref("");
const busy = ref(false);

async function submit() {
  if (newPassword.value !== confirm2.value) {
    toast("两次输入的新密码不一致", true);
    return;
  }
  busy.value = true;
  try {
    await auth.changePassword(oldPassword.value, newPassword.value);
    toast("密码已修改");
    void router.push("/");
  } catch (e) {
    toast(errorMessage(e), true);
  } finally {
    busy.value = false;
  }
}
</script>

<template>
  <div class="panel" style="width: 420px">
    <h3>首次登录，请修改密码</h3>
    <p class="muted" style="margin-top:0">安全策略要求首次登录后立即修改初始密码；修改后当前会话令牌将自动更换。</p>
    <div class="form-grid" style="grid-template-columns: 100px 1fr">
      <label class="f">当前密码</label>
      <input v-model="oldPassword" type="password" autocomplete="current-password" />
      <label class="f">新密码</label>
      <input v-model="newPassword" type="password" autocomplete="new-password" />
      <label class="f">确认新密码</label>
      <input v-model="confirm2" type="password" autocomplete="new-password" />
    </div>
    <button class="btn btn-primary" style="width:100%;margin-top:14px"
      :disabled="busy || !oldPassword || !newPassword" @click="submit">
      {{ busy ? "提交中…" : "修改密码并继续" }}
    </button>
  </div>
</template>
