<script setup lang="ts">
// 知识中心：检索 + 文档/版本生命周期（创建/追加/复核/发布/退役）
import { onMounted, ref } from "vue";
import { knowledgeApi } from "../api";
import type { KnowledgeDocument, KnowledgeHit, KnowledgeVersion } from "../types";
import StateBlock from "../components/StateBlock.vue";
import StatusBadge from "../components/StatusBadge.vue";
import { toast, errorMessage } from "../toast";
import { auth } from "../auth";

const loading = ref(true);
const error = ref<string | null>(null);
const docs = ref<KnowledgeDocument[]>([]);
const isAdminOrOperator = auth.hasRole("admin", "operator");
const isAdmin = auth.hasRole("admin");

// 检索
const query = ref("");
const searchEnv = ref("");
const searching = ref(false);
const searchMode = ref("");
const searchLatency = ref(0);
const hits = ref<KnowledgeHit[]>([]);
const searchError = ref<string | null>(null);

// 创建
const showCreate = ref(false);
const cForm = ref({ doc_type: "runbook", title: "", content: "", sensitivity: "internal", tags: "" });
const busyCreate = ref(false);

const busyVersion = ref(new Set<string>()); // version 操作去抖

async function load() {
  loading.value = true;
  error.value = null;
  try {
    const r = await knowledgeApi.documents();
    docs.value = r.documents;
  } catch (e) {
    error.value = errorMessage(e);
  } finally {
    loading.value = false;
  }
}

async function search() {
  if (!query.value.trim()) return;
  searching.value = true;
  searchError.value = null;
  try {
    const r = await knowledgeApi.search(query.value.trim(), searchEnv.value || undefined, 10);
    hits.value = r.hits;
    searchMode.value = r.mode;
    searchLatency.value = r.latency_ms;
  } catch (e) {
    searchError.value = errorMessage(e);
  } finally {
    searching.value = false;
  }
}

async function createDoc() {
  if (!cForm.value.title || !cForm.value.content) {
    toast("请填写标题与内容", true);
    return;
  }
  busyCreate.value = true;
  try {
    await knowledgeApi.create({
      doc_type: cForm.value.doc_type,
      title: cForm.value.title,
      content: cForm.value.content,
      sensitivity: cForm.value.sensitivity,
      tags: cForm.value.tags ? cForm.value.tags.split(",").map((t) => t.trim()).filter(Boolean) : undefined,
    });
    toast("知识文档已创建（草稿版本）");
    showCreate.value = false;
    await load();
  } catch (e) {
    toast(errorMessage(e), true);
  } finally {
    busyCreate.value = false;
  }
}

async function versionAction(v: KnowledgeVersion, action: "review" | "publish" | "retire") {
  const label = { review: "复核", publish: "发布", retire: "退役" }[action];
  if (!confirm(`确认${label}版本 ${v.document_id} v${v.version_no}？`)) return;
  busyVersion.value.add(v.id);
  try {
    await (action === "review" ? knowledgeApi.review(v.id)
      : action === "publish" ? knowledgeApi.publish(v.id)
      : knowledgeApi.retire(v.id));
    toast(`已${label}`);
    await load();
  } catch (e) {
    toast(errorMessage(e), true);
  } finally {
    busyVersion.value.delete(v.id);
  }
}

onMounted(load);
</script>

<template>
  <div class="panel">
    <h3>知识检索（RAG：权限过滤 → 全文/向量混合）</h3>
    <div class="toolbar">
      <input v-model="query" placeholder="查询，如：redis 连接拒绝处置" style="flex:1" @keyup.enter="search" />
      <select v-model="searchEnv" style="width:130px">
        <option value="">不限环境</option>
        <option v-for="e in ['sandbox','staging','production']" :key="e" :value="e">{{ e }}</option>
      </select>
      <button class="btn btn-primary" :disabled="searching || !query.trim()" @click="search">
        {{ searching ? "检索中…" : "检索" }}
      </button>
    </div>
    <div v-if="searchError" class="state error" style="padding:10px">{{ searchError }}</div>
    <template v-else-if="searchMode">
      <div class="muted" style="margin-bottom:8px;font-size:12px">
        模式：<strong>{{ searchMode }}</strong>
        <template v-if="searchMode !== 'off'">　延迟 {{ searchLatency }}ms　命中 {{ hits.length }}</template>
        <template v-else>　（rag_mode=off，知识检索未启用）</template>
      </div>
      <table class="tbl" v-if="hits.length">
        <thead><tr><th>文档</th><th>类型</th><th>版本</th><th>内容摘录</th></tr></thead>
        <tbody>
          <tr v-for="h in hits" :key="h.version_id">
            <td>{{ h.title }}<div class="mono muted">{{ h.document_id }}</div></td>
            <td><StatusBadge :value="h.doc_type" /></td>
            <td class="mono">v{{ h.version_no }}</td>
            <td class="mono" style="font-size:11px">{{ h.content.slice(0, 200) }}</td>
          </tr>
        </tbody>
      </table>
      <div v-else-if="searchMode !== 'off'" class="state">无命中结果</div>
    </template>
  </div>

  <div class="panel">
    <div class="toolbar">
      <h3 style="margin:0;flex:1">知识文档（事故复盘 / Runbook / 指南）</h3>
      <button v-if="isAdminOrOperator" class="btn btn-primary" @click="showCreate = !showCreate">+ 新建知识</button>
    </div>

    <div v-if="showCreate && isAdminOrOperator" class="panel" style="background:#fafbfc">
      <div class="form-grid">
        <label class="f">类型 *</label>
        <select v-model="cForm.doc_type">
          <option v-for="t in ['runbook','postmortem','guide']" :key="t" :value="t">{{ t }}</option>
        </select>
        <label class="f">标题 *</label><input v-model="cForm.title" />
        <label class="f">敏感级</label>
        <select v-model="cForm.sensitivity">
          <option value="internal">internal</option>
          <option value="restricted">restricted</option>
        </select>
        <label class="f">标签</label><input v-model="cForm.tags" placeholder="逗号分隔" />
        <label class="f">内容 *</label><textarea v-model="cForm.content" style="min-height:120px" />
      </div>
      <button class="btn btn-primary" style="margin-top:10px" :disabled="busyCreate" @click="createDoc">
        {{ busyCreate ? "创建中…" : "创建" }}
      </button>
    </div>

    <StateBlock :loading="loading" :error="error" :empty="!docs.length" empty-text="暂无知识文档" @retry="load">
      <div v-for="d in docs" :key="d.id" class="panel" style="margin-bottom:10px;background:#fafbfc">
        <div class="toolbar" style="margin-bottom:8px">
          <strong>{{ d.title }}</strong>
          <StatusBadge :value="d.doc_type" />
          <span class="mono muted">{{ d.id }}</span>
          <span v-for="t in d.tags" :key="t" class="badge b-gray">{{ t }}</span>
        </div>
        <table class="tbl">
          <thead><tr><th>版本</th><th>状态</th><th>变更说明</th><th>创建/复核人</th><th>发布时间</th><th v-if="isAdmin">操作</th></tr></thead>
          <tbody>
            <tr v-for="v in d.versions" :key="v.id">
              <td class="mono">v{{ v.version_no }}</td>
              <td><StatusBadge :value="v.status" /></td>
              <td class="muted">{{ v.change_note ?? "—" }}</td>
              <td class="mono muted">{{ v.created_by.slice(0,12) }}<template v-if="v.reviewed_by"> / {{ v.reviewed_by.slice(0,12) }}</template></td>
              <td class="mono muted">{{ v.published_at?.slice(0,19).replace("T"," ") ?? "—" }}</td>
              <td v-if="isAdmin">
                <button v-if="v.status === 'draft'" class="btn btn-sm" :disabled="busyVersion.has(v.id)" @click="versionAction(v, 'review')">复核</button>
                <button v-if="v.status === 'reviewed'" class="btn btn-sm btn-primary" :disabled="busyVersion.has(v.id)" @click="versionAction(v, 'publish')">发布</button>
                <button v-if="v.status === 'published'" class="btn btn-sm btn-danger" :disabled="busyVersion.has(v.id)" @click="versionAction(v, 'retire')">退役</button>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
    </StateBlock>
  </div>
</template>
