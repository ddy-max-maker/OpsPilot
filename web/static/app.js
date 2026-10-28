/* OpsPilot Next 最小前端（D1）：真实调用后端 API，无任何假数据。
 *
 * 页面流：登录（含首登改密）→ 资产管理 → 故障事件列表 → 创建事件 → 查看详情 → 确认故障。
 */
"use strict";

const API = "/api/v1";
const TOKEN_KEY = "opspilot_token";

const SEVERITIES = ["low", "medium", "high", "critical"];
const INCIDENT_STATUSES = [
  "open", "acknowledged", "investigating", "mitigating",
  "monitoring", "needs_human", "resolved", "closed",
];
const ENVIRONMENTS = ["sandbox", "staging", "production"];

let token = localStorage.getItem(TOKEN_KEY) || null;
let me = null;

// ---- 基础工具 ----

const $app = document.getElementById("app");
const $nav = document.getElementById("nav");
const $toast = document.getElementById("toast");

function esc(value) {
  const div = document.createElement("div");
  div.textContent = value === null || value === undefined ? "" : String(value);
  return div.innerHTML;
}

let toastTimer = null;
function toast(message, isError) {
  $toast.textContent = message;
  $toast.className = "toast" + (isError ? " error" : "");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => $toast.classList.add("hidden"), 3500);
}

function fmtTime(iso) {
  if (!iso) return "-";
  return new Date(iso).toLocaleString("zh-CN", { hour12: false });
}

async function api(path, options) {
  const opts = options || {};
  const headers = { "Content-Type": "application/json" };
  if (token) headers["Authorization"] = "Bearer " + token;
  if (opts.headers) Object.assign(headers, opts.headers);
  const resp = await fetch(API + path, {
    method: opts.method || "GET",
    headers,
    body: opts.body === undefined ? undefined : JSON.stringify(opts.body),
  });
  let envelope = null;
  try { envelope = await resp.json(); } catch (e) { /* 非 JSON 响应 */ }
  if (resp.status === 401 && token) {
    doLogout();
    throw { code: "UNAUTHENTICATED", message: "登录已过期，请重新登录" };
  }
  if (!resp.ok) {
    throw (envelope && envelope.error) || { code: "HTTP_" + resp.status, message: "请求失败（HTTP " + resp.status + "）" };
  }
  return envelope ? envelope.data : null;
}

function showError(err) {
  const message = (err && err.message) || String(err);
  toast(message, true);
}

// ---- 认证 ----

function doLogout() {
  token = null;
  me = null;
  localStorage.removeItem(TOKEN_KEY);
  location.hash = "#/login";
}

async function loadMe() {
  me = await api("/auth/me");
  document.getElementById("nav-user-name").textContent = me.display_name || me.username;
  document.getElementById("nav-user-roles").textContent = me.roles.join(", ");
  $nav.classList.remove("hidden");
}

async function handleLoginSubmit(event) {
  event.preventDefault();
  const form = event.target;
  const submitBtn = form.querySelector("button[type=submit]");
  submitBtn.disabled = true;
  try {
    const data = await api("/auth/login", {
      method: "POST",
      body: {
        username: form.username.value.trim(),
        password: form.password.value,
      },
    });
    token = data.access_token;
    localStorage.setItem(TOKEN_KEY, token);
    await loadMe();
    if (me.must_change_password) {
      renderChangePassword(data.username);
      return;
    }
    toast("登录成功");
    location.hash = "#/assets";
  } catch (err) {
    showError(err);
  } finally {
    submitBtn.disabled = false;
  }
}

function renderChangePassword(username) {
  $app.innerHTML = `
    <div class="login-wrap panel">
      <h1>修改初始密码</h1>
      <p class="login-title">账号 ${esc(username)} 首次登录，必须修改密码后才能继续。</p>
      <form id="change-pw-form">
        <div style="margin-bottom:12px">
          <label>当前密码</label>
          <input name="old_password" type="password" required />
        </div>
        <div style="margin-bottom:16px">
          <label>新密码（至少 8 位）</label>
          <input name="new_password" type="password" minlength="8" required />
        </div>
        <button class="btn" type="submit" style="width:100%">修改密码并继续</button>
        <div class="error-text hidden" id="change-pw-error"></div>
      </form>
    </div>`;
  document.getElementById("change-pw-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const form = event.target;
    const errBox = document.getElementById("change-pw-error");
    errBox.classList.add("hidden");
    try {
      await api("/auth/change-password", {
        method: "POST",
        body: { old_password: form.old_password.value, new_password: form.new_password.value },
      });
      toast("密码已修改");
      const fresh = await api("/auth/login", {
        method: "POST",
        body: { username: me.username, password: form.new_password.value },
      });
      token = fresh.access_token;
      localStorage.setItem(TOKEN_KEY, token);
      await loadMe();
      location.hash = "#/assets";
    } catch (err) {
      errBox.textContent = err.message || "修改失败";
      errBox.classList.remove("hidden");
    }
  });
}

function renderLogin() {
  $nav.classList.add("hidden");
  $app.innerHTML = `
    <div class="login-wrap panel">
      <h1>OpsPilot Next</h1>
      <p class="login-title">AIOps 故障处置平台 · D1 控制台</p>
      <form id="login-form">
        <div style="margin-bottom:12px">
          <label>用户名</label>
          <input name="username" autocomplete="username" required />
        </div>
        <div style="margin-bottom:16px">
          <label>密码</label>
          <input name="password" type="password" autocomplete="current-password" required />
        </div>
        <button class="btn" type="submit" style="width:100%">登录</button>
      </form>
    </div>`;
  document.getElementById("login-form").addEventListener("submit", handleLoginSubmit);
}

// ---- 资产管理 ----

function envBadge(environment) {
  const cls = environment === "production" ? "red" : environment === "staging" ? "orange" : "green";
  return `<span class="badge ${cls}">${esc(environment)}</span>`;
}

function renderAssets(items) {
  if (!items.length) {
    return `<div class="empty">暂无资产，请先登记受监控资产。</div>`;
  }
  return `
    <table>
      <thead><tr>
        <th>ID</th><th>名称</th><th>环境</th><th>类型</th><th>重要度</th><th>状态</th><th>负责人</th><th>创建时间</th>
      </tr></thead>
      <tbody>
        ${items.map((a) => `
          <tr>
            <td class="mono">${esc(a.id)}</td>
            <td>${esc(a.name)}</td>
            <td>${envBadge(a.environment)}</td>
            <td>${esc(a.asset_type)}</td>
            <td>${esc(a.criticality)}</td>
            <td>${esc(a.status)}</td>
            <td>${esc(a.owner || "-")}</td>
            <td>${fmtTime(a.created_at)}</td>
          </tr>`).join("")}
      </tbody>
    </table>`;
}

async function loadAssetsView() {
  $app.innerHTML = `<div class="empty">加载中…</div>`;
  let assets;
  try {
    assets = await api("/assets?limit=100");
  } catch (err) {
    $app.innerHTML = `<div class="panel"><div class="empty">加载失败：${esc(err.message)}</div></div>`;
    return;
  }
  const isAdmin = me.roles.includes("admin");
  const typeOptions = ["host", "container", "database", "service", "network", "other"]
    .map((t) => `<option value="${t}">${t}</option>`).join("");
  $app.innerHTML = `
    <div class="page-head">
      <h1>资产管理</h1>
      ${isAdmin ? '<button id="toggle-asset-form" class="btn">新建资产</button>' : ""}
    </div>
    <div id="asset-form-panel" class="panel hidden">
      <form id="asset-form">
        <div class="form-grid">
          <div><label>名称 *</label><input name="name" required maxlength="200" /></div>
          <div><label>环境 *</label><select name="environment">${ENVIRONMENTS.map((e) => `<option value="${e}">${e}</option>`).join("")}</select></div>
          <div><label>类型 *</label><select name="asset_type">${typeOptions}</select></div>
          <div><label>重要度</label><select name="criticality"><option value="low">low</option><option value="medium" selected>medium</option><option value="high">high</option><option value="critical">critical</option></select></div>
          <div><label>负责人</label><input name="owner" maxlength="200" /></div>
          <div><label>健康检查 URL（可选）</label><input name="health_url" placeholder="http://…" /></div>
        </div>
        <div style="margin-top:14px">
          <button class="btn" type="submit">创建</button>
        </div>
      </form>
    </div>
    <div class="panel">${renderAssets(assets.items)}</div>`;
  if (isAdmin) {
    document.getElementById("toggle-asset-form").addEventListener("click", () => {
      document.getElementById("asset-form-panel").classList.toggle("hidden");
    });
    document.getElementById("asset-form").addEventListener("submit", async (event) => {
      event.preventDefault();
      const form = event.target;
      const body = {
        name: form.name.value.trim(),
        environment: form.environment.value,
        asset_type: form.asset_type.value,
        criticality: form.criticality.value,
      };
      if (form.owner.value.trim()) body.owner = form.owner.value.trim();
      if (form.health_url.value.trim()) body.endpoints = { health_url: form.health_url.value.trim() };
      try {
        await api("/assets", { method: "POST", body });
        toast("资产已创建");
        loadAssetsView();
      } catch (err) {
        showError(err);
      }
    });
  }
}

// ---- 故障事件 ----

function statusBadge(status) {
  const map = {
    open: "red", acknowledged: "orange", investigating: "blue", mitigating: "blue",
    monitoring: "yellow", needs_human: "red", resolved: "green", closed: "gray",
  };
  return `<span class="badge ${map[status] || "muted"}">${esc(status)}</span>`;
}

function renderIncidents(items, filters) {
  if (!items.length) {
    return `<div class="empty">没有匹配的故障事件。</div>`;
  }
  return `
    <table>
      <thead><tr>
        <th>ID</th><th>标题</th><th>状态</th><th>严重度</th><th>环境</th><th>资产</th><th>来源</th><th>创建时间</th>
      </tr></thead>
      <tbody>
        ${items.map((i) => `
          <tr class="clickable" data-id="${esc(i.id)}">
            <td class="mono">${esc(i.id.slice(0, 14))}…</td>
            <td>${esc(i.title)}</td>
            <td>${statusBadge(i.status)}</td>
            <td><span class="badge ${i.severity === "critical" || i.severity === "high" ? "red" : "muted"}">${esc(i.severity)}</span></td>
            <td>${envBadge(i.environment)}</td>
            <td class="mono">${esc(i.primary_asset_id.slice(0, 12))}…</td>
            <td>${esc(i.source)}</td>
            <td>${fmtTime(i.created_at)}</td>
          </tr>`).join("")}
      </tbody>
    </table>`;
}

async function loadIncidentsView() {
  $app.innerHTML = `<div class="empty">加载中…</div>`;
  let assets = { items: [] };
  let incidents;
  const statusOptions = [`<option value="">全部状态</option>`]
    .concat(INCIDENT_STATUSES.map((s) => `<option value="${s}">${s}</option>`)).join("");
  const severityOptions = [`<option value="">全部严重度</option>`]
    .concat(SEVERITIES.map((s) => `<option value="${s}">${s}</option>`)).join("");
  const envOptions = [`<option value="">全部环境</option>`]
    .concat(ENVIRONMENTS.map((e) => `<option value="${e}">${e}</option>`)).join("");
  try {
    incidents = await api("/incidents?limit=100");
    try { assets = await api("/assets?limit=100"); } catch (e) { /* 范围内无资产也可建事件时再提示 */ }
  } catch (err) {
    $app.innerHTML = `<div class="panel"><div class="empty">加载失败：${esc(err.message)}</div></div>`;
    return;
  }
  const assetOptions = assets.items
    .map((a) => `<option value="${esc(a.id)}">${esc(a.name)}（${esc(a.environment)}）</option>`)
    .join("");
  $app.innerHTML = `
    <div class="page-head">
      <h1>故障事件</h1>
      <button id="toggle-incident-form" class="btn">新建故障</button>
    </div>
    <div id="incident-form-panel" class="panel hidden">
      <form id="incident-form">
        <div class="form-grid">
          <div class="full"><label>标题 *</label><input name="title" required maxlength="200" /></div>
          <div class="full"><label>描述</label><textarea name="description" maxlength="5000"></textarea></div>
          <div><label>环境 *</label><select name="environment">${ENVIRONMENTS.map((e) => `<option value="${e}">${e}</option>`).join("")}</select></div>
          <div><label>主资产 *</label><select name="primary_asset_id" required>${assetOptions || "<option value=''>（暂无可选资产）</option>"}</select></div>
          <div><label>严重度</label><select name="severity">${SEVERITIES.map((s) => `<option value="${s}" ${s === "medium" ? "selected" : ""}>${s}</option>`).join("")}</select></div>
          <div><label>症状指纹（可选）</label><input name="fingerprint" maxlength="200" /></div>
        </div>
        <div style="margin-top:14px">
          <button class="btn" type="submit">创建（自动携带幂等键）</button>
        </div>
      </form>
    </div>
    <div class="panel">
      <div class="filters">
        <select id="f-status">${statusOptions}</select>
        <select id="f-severity">${severityOptions}</select>
        <select id="f-env">${envOptions}</select>
        <input id="f-asset" placeholder="按资产 ID 过滤" />
        <button id="f-apply" class="btn ghost">筛选</button>
      </div>
      <div id="incident-table">${renderIncidents(incidents.items)}</div>
    </div>`;

  document.getElementById("toggle-incident-form").addEventListener("click", () => {
    document.getElementById("incident-form-panel").classList.toggle("hidden");
  });

  document.getElementById("f-apply").addEventListener("click", async () => {
    const params = new URLSearchParams({ limit: "100" });
    const status = document.getElementById("f-status").value;
    const severity = document.getElementById("f-severity").value;
    const env = document.getElementById("f-env").value;
    const asset = document.getElementById("f-asset").value.trim();
    if (status) params.set("status", status);
    if (severity) params.set("severity", severity);
    if (env) params.set("environment", env);
    if (asset) params.set("primary_asset_id", asset);
    try {
      const data = await api("/incidents?" + params.toString());
      document.getElementById("incident-table").innerHTML = renderIncidents(data.items);
    } catch (err) {
      showError(err);
    }
  });

  document.getElementById("incident-table").addEventListener("click", (event) => {
    const row = event.target.closest("tr[data-id]");
    if (row) location.hash = "#/incidents/" + row.dataset.id;
  });

  document.getElementById("incident-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const form = event.target;
    const body = {
      title: form.title.value.trim(),
      environment: form.environment.value,
      primary_asset_id: form.primary_asset_id.value,
      severity: form.severity.value,
    };
    if (form.description.value.trim()) body.description = form.description.value.trim();
    if (form.fingerprint.value.trim()) body.fingerprint = form.fingerprint.value.trim();
    try {
      const data = await api("/incidents", {
        method: "POST",
        body,
        headers: { "Idempotency-Key": crypto.randomUUID() },
      });
      toast("故障事件已创建：" + data.id);
      location.hash = "#/incidents/" + data.id;
    } catch (err) {
      showError(err);
    }
  });
}

// ---- 事件详情 + 确认 ----

async function loadIncidentDetail(incidentId) {
  $app.innerHTML = `<div class="empty">加载中…</div>`;
  let incident;
  try {
    incident = await api("/incidents/" + encodeURIComponent(incidentId));
  } catch (err) {
    $app.innerHTML = `<div class="panel"><div class="empty">加载失败：${esc(err.message)}</div></div>`;
    return;
  }
  const canAcknowledge = incident.status === "open" &&
    (me.roles.includes("operator") || me.roles.includes("approver") || me.roles.includes("admin"));
  const canInvestigate = me.roles.includes("operator") || me.roles.includes("approver") || me.roles.includes("admin");
  const runnable = incident.status !== "open" && incident.status !== "closed" && incident.status !== "resolved";
  const transitions = {
    open: "acknowledged / needs_human",
    acknowledged: "investigating",
    investigating: "mitigating / needs_human",
    mitigating: "monitoring / needs_human",
    monitoring: "resolved / investigating",
    needs_human: "investigating / closed",
    resolved: "closed",
    closed: "（终态）",
  };
  $app.innerHTML = `
    <div class="page-head">
      <h1>故障详情</h1>
      <a href="#/incidents" class="btn ghost">返回列表</a>
    </div>
    <div class="panel">
      <dl class="detail-grid">
        <dt>ID</dt><dd class="mono">${esc(incident.id)}</dd>
        <dt>标题</dt><dd>${esc(incident.title)}</dd>
        <dt>描述</dt><dd style="white-space:pre-wrap">${esc(incident.description || "-")}</dd>
        <dt>状态</dt><dd>${statusBadge(incident.status)}</dd>
        <dt>严重度</dt><dd><span class="badge ${incident.severity === "critical" || incident.severity === "high" ? "red" : "muted"}">${esc(incident.severity)}</span></dd>
        <dt>环境</dt><dd>${envBadge(incident.environment)}</dd>
        <dt>主资产</dt><dd class="mono">${esc(incident.primary_asset_id)}</dd>
        <dt>来源</dt><dd>${esc(incident.source)}${incident.alert_id ? '（告警 <span class="mono">' + esc(incident.alert_id) + "</span>）" : ""}</dd>
        <dt>指纹</dt><dd class="mono">${esc(incident.fingerprint || "-")}</dd>
        <dt>创建人</dt><dd class="mono">${esc(incident.created_by)}</dd>
        <dt>创建时间</dt><dd>${fmtTime(incident.created_at)}</dd>
        <dt>更新时间</dt><dd>${fmtTime(incident.updated_at)}</dd>
        <dt>解决时间</dt><dd>${fmtTime(incident.resolved_at)}</dd>
        <dt>允许的下一步</dt><dd>${esc(transitions[incident.status] || "-")}</dd>
      </dl>
      <div id="ack-area" style="margin-top:18px">
        ${canAcknowledge ? '<button id="ack-btn" class="btn">确认故障（acknowledge）</button>'
          : incident.status === "open"
            ? '<span class="badge muted">当前角色无法确认故障（需要 operator/approver/admin）</span>'
            : ""}
        ${canInvestigate && runnable ? '<button id="start-run-btn" class="btn" style="margin-left:8px">启动智能诊断</button>' : ""}
      </div>
    </div>
    <div id="run-panel"></div>`;
  const ackBtn = document.getElementById("ack-btn");
  if (ackBtn) {
    ackBtn.addEventListener("click", async () => {
      ackBtn.disabled = true;
      try {
        await api(`/incidents/${encodeURIComponent(incidentId)}/acknowledge`, { method: "POST" });
        toast("故障已确认");
        loadIncidentDetail(incidentId);
      } catch (err) {
        showError(err);
        ackBtn.disabled = false;
      }
    });
  }
  const startBtn = document.getElementById("start-run-btn");
  if (startBtn) {
    startBtn.addEventListener("click", async () => {
      startBtn.disabled = true;
      try {
        const data = await api(`/incidents/${encodeURIComponent(incidentId)}/investigations`, {
          method: "POST",
          headers: { "Idempotency-Key": crypto.randomUUID() },
          body: {},
        });
        toast("诊断已受理：" + data.run_id);
        localStorage.setItem("opspilot_run_" + incidentId, data.run_id);
        renderRunPanel(incidentId, data.run_id);
      } catch (err) {
        showError(err);
        startBtn.disabled = false;
      }
    });
    // 页面重开时恢复最近一次 run 面板
    const savedRun = localStorage.getItem("opspilot_run_" + incidentId);
    if (savedRun) {
      renderRunPanel(incidentId, savedRun);
    }
  }
}

// ---- 诊断运行面板（轮询状态 + 事件 + 证据 + 报告）----

const RUN_TERMINAL = new Set(["completed", "needs_human", "failed", "canceled"]);
let runPollTimer = null;

function renderRunPanel(incidentId, runId) {
  const panel = document.getElementById("run-panel");
  if (!panel) return;
  if (runPollTimer) clearInterval(runPollTimer);
  panel.innerHTML = `
    <div class="panel">
      <div class="page-head" style="margin-bottom:8px">
        <h1 style="font-size:16px">智能诊断 <span class="mono">${esc(runId.slice(0, 14))}…</span></h1>
        <button id="close-run-btn" class="btn ghost">收起</button>
      </div>
      <div id="run-status">加载中…</div>
      <div style="margin:10px 0">
        <button id="show-events-btn" class="btn ghost">工具调用过程</button>
        <button id="show-evidence-btn" class="btn ghost">证据</button>
        <button id="show-report-btn" class="btn ghost">诊断报告</button>
        <button id="cancel-run-btn" class="btn danger">取消</button>
      </div>
      <div id="run-detail"></div>
    </div>`;
  document.getElementById("close-run-btn").addEventListener("click", () => {
    if (runPollTimer) clearInterval(runPollTimer);
    panel.innerHTML = "";
  });
  document.getElementById("show-events-btn").addEventListener("click", () => showRunEvents(runId));
  document.getElementById("show-evidence-btn").addEventListener("click", () => showRunEvidence(runId));
  document.getElementById("show-report-btn").addEventListener("click", () => showRunReport(runId));
  document.getElementById("cancel-run-btn").addEventListener("click", async () => {
    try {
      await api(`/runs/${encodeURIComponent(runId)}/cancel`, { method: "POST" });
      toast("已请求取消");
      pollRunStatus(incidentId, runId);
    } catch (err) {
      showError(err);
    }
  });
  pollRunStatus(incidentId, runId);
}

async function pollRunStatus(incidentId, runId) {
  const box = document.getElementById("run-status");
  if (!box) return;
  try {
    const run = await api("/runs/" + encodeURIComponent(runId));
    const usage = run.usage || {};
    box.innerHTML = `
      <dl class="detail-grid">
        <dt>状态</dt><dd>${statusBadge(run.status)}${run.cancel_requested ? ' <span class="badge orange">取消请求中</span>' : ""}</dd>
        <dt>停止原因</dt><dd>${esc(run.stop_reason || "-")}</dd>
        <dt>用量</dt><dd>工具调用 ${usage.tool_calls_used ?? 0} · 迭代 ${usage.iterations_used ?? 0} · tokens ${usage.tokens_in ?? 0}/${usage.tokens_out ?? 0}</dd>
        <dt>预算</dt><dd class="mono">${esc(JSON.stringify(run.budgets || {}).slice(0, 160))}</dd>
        <dt>模型</dt><dd class="mono">${esc((run.model_config_snapshot || {}).provider || "")}/${esc((run.model_config_snapshot || {}).model_name || "")}（prompt ${(run.model_config_snapshot || {}).prompt_version || ""}）</dd>
      </dl>`;
    if (!RUN_TERMINAL.has(run.status)) {
      runPollTimer = setTimeout(() => pollRunStatus(incidentId, runId), 2000);
    }
  } catch (err) {
    box.innerHTML = `<div class="error-text">状态查询失败：${esc(err.message)}</div>`;
  }
}

const EVENT_TYPE_BADGE = {
  run_started: "blue", tool_called: "orange", tool_returned: "orange",
  evidence_added: "green", hypothesis_added: "blue", budget_warning: "red",
  error: "red", run_finished: "gray",
};

async function showRunEvents(runId) {
  const box = document.getElementById("run-detail");
  box.innerHTML = `<div class="empty">加载中…</div>`;
  try {
    const data = await api(`/runs/${encodeURIComponent(runId)}/events?after_seq=0&limit=200`);
    box.innerHTML = `
      <table>
        <thead><tr><th>seq</th><th>类型</th><th>内容</th><th>时间</th></tr></thead>
        <tbody>
          ${data.items.map((e) => `
            <tr>
              <td>${e.seq}</td>
              <td><span class="badge ${EVENT_TYPE_BADGE[e.type] || "muted"}">${esc(e.type)}</span></td>
              <td class="mono" style="max-width:480px;overflow-wrap:anywhere">${esc(JSON.stringify(e.payload))}</td>
              <td>${fmtTime(e.created_at)}</td>
            </tr>`).join("")}
        </tbody>
      </table>
      ${data.has_more ? '<div class="empty">事件未完，后续批次从命令行/API 查看</div>' : ""}`;
  } catch (err) {
    box.innerHTML = `<div class="error-text">${esc(err.message)}</div>`;
  }
}

async function showRunEvidence(runId) {
  const box = document.getElementById("run-detail");
  box.innerHTML = `<div class="empty">加载中…</div>`;
  try {
    const data = await api(`/runs/${encodeURIComponent(runId)}/evidence?limit=200`);
    if (!data.items.length) {
      box.innerHTML = `<div class="empty">暂无证据</div>`;
      return;
    }
    box.innerHTML = `
      <table>
        <thead><tr><th>Evidence ID</th><th>来源</th><th>状态</th><th>摘要（脱敏）</th><th>时间</th></tr></thead>
        <tbody>
          ${data.items.map((e) => `
            <tr>
              <td class="mono">${esc(e.id.slice(0, 14))}…</td>
              <td>${esc(e.collector)}</td>
              <td><span class="badge ${e.status === "ok" ? "green" : "red"}">${esc(e.status)}</span>${e.truncated ? ' <span class="badge muted">截断</span>' : ""}</td>
              <td style="max-width:420px;overflow-wrap:anywhere">${esc(e.summary_redacted)}</td>
              <td>${fmtTime(e.observed_at)}</td>
            </tr>`).join("")}
        </tbody>
      </table>`;
  } catch (err) {
    box.innerHTML = `<div class="error-text">${esc(err.message)}</div>`;
  }
}

async function showRunReport(runId) {
  const box = document.getElementById("run-detail");
  box.innerHTML = `<div class="empty">加载中…</div>`;
  try {
    const r = await api(`/runs/${encodeURIComponent(runId)}/report`);
    box.innerHTML = `
      <dl class="detail-grid">
        <dt>报告版本</dt><dd>v${r.version_no}（${esc(r.status)}）</dd>
        <dt>故障概述</dt><dd style="white-space:pre-wrap">${esc(r.conclusion.summary || "-")}</dd>
        <dt>关键发现</dt><dd>
          ${(r.conclusion.key_findings || []).map((f) => `
            <div style="margin-bottom:6px">• ${esc(f.finding)}
              <span class="mono">[${(f.evidence_ids || []).map((id) => esc(id.slice(0, 12)) + "…").join(", ")}]</span></div>`).join("") || "-"}
        </dd>
        <dt>候选根因</dt><dd>
          ${(r.hypotheses || []).map((h) => `
            <div style="margin-bottom:6px">#${h.rank} <span class="badge ${h.status === "supported" ? "green" : h.status === "refuted" ? "red" : "muted"}">${esc(h.status)}</span>
              ${esc(h.statement)} <span class="mono">[${h.supporting_evidence_ids.map((id) => esc(id.slice(0, 12)) + "…").join(", ")}]</span></div>`).join("") || "-"}
        </dd>
        <dt>未知说明</dt><dd>${r.unknown_note && r.unknown_note.is_unknown
          ? '<span class="badge red">is_unknown</span> ' + esc(r.unknown_note.reason || "")
          : '<span class="badge green">结论有证据支撑</span>'}</dd>
        <dt>人工处置建议</dt><dd>${(r.conclusion.next_manual_steps || []).map((s) => `• ${esc(s)}`).join("<br/>") || "-"}</dd>
        <dt>生成器</dt><dd class="mono">${esc(JSON.stringify(r.generator || {}))}</dd>
      </dl>`;
  } catch (err) {
    box.innerHTML = `<div class="error-text">${esc(err.message)}</div>`;
  }
}

// ---- 审计日志（Admin/Viewer 可见） ----

async function loadAuditView() {
  $app.innerHTML = `<div class="empty">加载中…</div>`;
  let events;
  try {
    events = await api("/audit-logs?limit=50");
  } catch (err) {
    $app.innerHTML = `<div class="panel"><div class="empty">加载失败：${esc(err.message)}</div></div>`;
    return;
  }
  $app.innerHTML = `
    <div class="page-head"><h1>审计日志</h1></div>
    <div class="panel">
      ${events.items.length ? `
      <table>
        <thead><tr><th>ID</th><th>主体</th><th>动作</th><th>资源</th><th>环境</th><th>时间</th></tr></thead>
        <tbody>
          ${events.items.map((e) => `
            <tr>
              <td>${e.id}</td>
              <td>${esc(e.actor_type)} <span class="mono">${esc(e.actor_id.slice(0, 12))}…</span></td>
              <td><code>${esc(e.action)}</code></td>
              <td>${esc(e.resource_type || "-")} <span class="mono">${esc((e.resource_id || "").slice(0, 12))}${e.resource_id ? "…" : ""}</span></td>
              <td>${esc(e.environment || "-")}</td>
              <td>${fmtTime(e.created_at)}</td>
            </tr>`).join("")}
        </tbody>
      </table>` : '<div class="empty">暂无审计事件。</div>'}
    </div>`;
}

// ---- 连接器管理（D3：Integrations / Capabilities / 绑定 / 连通性测试） ----

async function loadIntegrationsView() {
  $app.innerHTML = `<div class="empty">加载中…</div>`;
  let data;
  try {
    data = await api("/integrations");
  } catch (err) {
    $app.innerHTML = `<div class="panel"><div class="empty">加载失败（需要 Admin 角色）：${esc(err.message)}</div></div>`;
    return;
  }
  const caps = await api("/capabilities").catch(() => ({ capabilities: [] }));
  $app.innerHTML = `
    <div class="page-head"><h1>连接器与能力（Capability Registry）</h1></div>
    <div class="panel">
      <h2>连接器类型（内置，代码审查后部署）</h2>
      <table>
        <thead><tr><th>代码</th><th>名称</th><th>Manifest 版本</th><th>兼容性</th></tr></thead>
        <tbody>
          ${(data.types || []).map((t) => `
            <tr>
              <td><code>${esc(t.code)}</code></td>
              <td>${esc(t.display_name)}</td>
              <td>${esc(t.manifest_version || "-")}</td>
              <td><span class="mono">${esc(JSON.stringify(t.compatibility || {})).slice(0, 60)}</span></td>
            </tr>`).join("")}
        </tbody>
      </table>
    </div>
    <div class="panel">
      <h2>连接器实例（Integrations）</h2>
      <table id="instance-table">
        <thead><tr><th>名称</th><th>类型</th><th>状态</th><th>配置</th><th>操作</th></tr></thead>
        <tbody>
          ${(data.instances || []).map((i) => `
            <tr data-id="${esc(i.id)}">
              <td>${esc(i.name)}</td>
              <td><code>${esc(i.connector_type)}</code></td>
              <td><span class="badge ${i.status === "enabled" ? "" : "muted"}">${esc(i.status)}</span></td>
              <td><span class="mono">${esc(JSON.stringify(i.config || {})).slice(0, 80)}…</span></td>
              <td>
                <button class="btn ghost" data-act="test">测试</button>
                ${i.status === "enabled"
                  ? '<button class="btn ghost" data-act="disable">禁用</button>'
                  : '<button class="btn ghost" data-act="enable">启用</button>'}
              </td>
            </tr>`).join("")}
        </tbody>
      </table>
      <div id="instance-test-result" class="mono"></div>
    </div>
    <div class="panel">
      <h2>最近能力/连通性测试</h2>
      ${(data.tests || []).length ? `
      <table>
        <thead><tr><th>结果</th><th>实例</th><th>探针</th><th>时间</th></tr></thead>
        <tbody>
          ${data.tests.map((t) => `
            <tr>
              <td><span class="badge ${t.result === "passed" ? "" : "muted"}">${esc(t.result)}</span></td>
              <td>${esc((t.detail || {}).instance_name || "-")}</td>
              <td>${esc((t.detail || {}).probe || "-")}</td>
              <td>${fmtTime(t.created_at)}</td>
            </tr>`).join("")}
        </tbody>
      </table>` : '<div class="empty">暂无测试记录。</div>'}
    </div>
    <div class="panel">
      <h2>能力发现（GET /capabilities）</h2>
      <table>
        <thead><tr><th>Capability</th><th>Source</th><th>只读</th><th>超时/限流</th></tr></thead>
        <tbody>
          ${(caps.capabilities || []).map((c) => `
            <tr>
              <td><code>${esc(c.capability_key)}</code></td>
              <td>${esc(c.source_type || "")}</td>
              <td>${c.read_only ? "true" : "false"}</td>
              <td>${esc(String(c.timeout_ms || "-"))}ms / ${esc(String((c.rate_limit || {}).max_calls_per_run || "-"))}/run</td>
            </tr>`).join("")}
        </tbody>
      </table>
    </div>`;

  document.getElementById("instance-table").addEventListener("click", async (event) => {
    const btn = event.target.closest("button[data-act]");
    if (!btn) return;
    const row = btn.closest("tr");
    const id = row.dataset.id;
    const act = btn.dataset.act;
    btn.disabled = true;
    try {
      const res = await api(`/integrations/${id}/${act}`, { method: "POST" });
      if (act === "test") {
        document.getElementById("instance-test-result").textContent =
          `最近一次测试：${res.result} — ${res.probe}`;
      } else {
        toast(`已${act === "enable" ? "启用" : "禁用"}`);
        await loadIntegrationsView();
      }
    } catch (err) {
      toast(err.message, true);
    } finally {
      btn.disabled = false;
    }
  });
}

// ---- 路由 ----

function setActiveNav(key) {
  document.querySelectorAll(".nav-links a").forEach((a) => {
    a.classList.toggle("active", a.dataset.nav === key);
  });
}

async function route() {
  const hash = location.hash || "";
  if (!token) {
    renderLogin();
    return;
  }
  if (!me) {
    try {
      await loadMe();
    } catch (err) {
      return; // loadMe 内部 401 已跳登录
    }
  }
  if (me.must_change_password) {
    renderChangePassword(me.username);
    return;
  }
  if (hash.startsWith("#/incidents/")) {
    setActiveNav("incidents");
    await loadIncidentDetail(decodeURIComponent(hash.slice("#/incidents/".length)));
  } else if (hash === "#/incidents") {
    setActiveNav("incidents");
    await loadIncidentsView();
  } else if (hash === "#/assets") {
    setActiveNav("assets");
    await loadAssetsView();
  } else if (hash === "#/integrations") {
    setActiveNav("integrations");
    await loadIntegrationsView();
  } else if (hash === "#/audit") {
    setActiveNav("audit");
    await loadAuditView();
  } else {
    location.hash = "#/assets";
  }
}

window.addEventListener("hashchange", route);
document.getElementById("logout-btn").addEventListener("click", doLogout);
route();
