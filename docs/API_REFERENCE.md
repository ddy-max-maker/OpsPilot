# API 参考（API_REFERENCE）

> 本文件由运行中的服务导出 `openapi.json` 后核对生成（49 条路径 / 56 个操作 / 73 个 schema），
> 与代码同源。交互式文档：`http://localhost:8000/docs`（Swagger UI）、`/redoc`。
> 契约的冻结版本见 [CONTRACTS.md](CONTRACTS.md)，本文件面向「怎么调」。

---

## 1. 通用约定

### 1.1 统一响应包裹

所有业务响应（含错误）都是同一层壳：

```json
{ "request_id": "req_...", "data": { ... }, "error": null }
```

失败时 `data=null`：

```json
{
  "request_id": "req_...",
  "data": null,
  "error": { "code": "NOT_FOUND", "message": "resource not found", "details": null }
}
```

`request_id` 由服务端生成，贯穿日志与 `audit_logs`，报障时提供它比提供时间点更有用。

### 1.2 认证

```http
POST /api/v1/auth/login
Content-Type: application/json

{ "username": "e2e_operator", "password": "E2ePassw0rd!2026" }
```

返回 `data.access_token`，之后所有请求带 `Authorization: Bearer <token>`。
Access token 有效期 `ACCESS_TOKEN_EXPIRE_MINUTES`（默认 30 分钟），首版无 Refresh Token。

两条与认证相关的硬约束：

- 首登（`must_change_password=true`）只能调 `POST /auth/change-password`，其它业务接口一律
  `403 PASSWORD_CHANGE_REQUIRED`；
- 示例中 `e2e_*` 账号由 `scripts/seed_e2e_users.py` 创建，仅用于演示。

### 1.3 RBAC 角色

| 角色 | 能力 |
| --- | --- |
| `viewer` | 只读（事件/run/证据/报告/知识/审计） |
| `operator` | viewer + 建事件、确认、发起诊断、建提案、执行已审批提案 |
| `approver` | viewer + 审批/驳回提案（**不能审批自己发起的**） |
| `admin` | 全部 + 资产/集成/Runbook/知识发布、复核、评估 |

四眼规则在服务端强制：`proposal.created_by == current_user.id` 时审批返回
`403 FORBIDDEN`，与前端无关。

### 1.4 幂等键

重要 POST 必须带 `Idempotency-Key`（缺失 → `400 IDEMPOTENCY_KEY_REQUIRED`）：

- `POST /incidents`
- `POST /incidents/{id}/investigations`
- `POST /proposals`

同 key + 同请求体 → 返回首次结果（不重复创建）；同 key + 不同请求体 →
`409 IDEMPOTENCY_KEY_MISMATCH`。保留时长 `IDEMPOTENCY_TTL_HOURS`（默认 24h）。

### 1.5 分页

列表接口统一 `limit`（默认 50，上限 200）与 `cursor`，响应形如：

```json
{ "data": { "items": [], "next_cursor": "..." } }
```

个别端点字段名不同（见 §2 备注），例如知识文档列表用 `documents`、知识检索用 `hits`。
**这是历史实现的真实差异，调用前请以 `/docs` 的 schema 为准。**

### 1.6 错误码

| code | HTTP | 含义 |
| --- | --- | --- |
| `VALIDATION_ERROR` | 400 | 参数/schema 校验失败 |
| `IDEMPOTENCY_KEY_REQUIRED` | 400 | 缺 `Idempotency-Key` |
| `UNAUTHENTICATED` | 401 | 未登录 / token 无效或过期 |
| `FORBIDDEN` | 403 | 角色不足，或四眼自审 |
| `PASSWORD_CHANGE_REQUIRED` | 403 | 首登未改密 |
| `NOT_FOUND` | 404 | 资源不存在（**越权也返回 404，不暴露存在性**） |
| `CONFLICT_STATE` | 409 | 状态机不允许该操作（如对已终态 run 再取消） |
| `IDEMPOTENCY_KEY_MISMATCH` | 409 | 同 key 不同体 |
| `DUPLICATE_ALERT` | 409 | webhook 告警 delivery 重复 |
| `INTERNAL` | 500 | 服务端错误 |

`FORBIDDEN_ASSET_SCOPE` 已在 `app/core/errors.py` 定义但当前未被任何路径使用——保留给后续
「跨资产越权」细分场景，本版资产范围校验统一走 `NOT_FOUND`（不暴露存在性）。

---

## 2. 端点清单

### 2.1 认证 `auth`

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/v1/auth/login` | 用户名密码换 token |
| POST | `/api/v1/auth/change-password` | 改密（首登强制） |
| GET | `/api/v1/auth/me` | 当前用户与角色 |

### 2.2 健康检查 `health`

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/v1/health/live` | 进程存活（不查依赖） |
| GET | `/api/v1/health/ready` | 就绪（DB 等依赖可用才 200） |

`ready` 是 `bootstrap_demo.sh` 的就绪判据；容器 `HEALTHCHECK` 也用它。

### 2.3 资产 `assets`

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/v1/assets` | 列表（`limit` ≤ 200） |
| POST | `/api/v1/assets` | 登记资产（admin） |
| PATCH | `/api/v1/assets/{asset_id}` | 更新 |

`endpoints.container_name` 是**容器身份的唯一权威**：登记了就按它解析，没登记才回退
`instance.container_prefix + asset.name`（D7 修复，见 FAILURE_POSTMORTEMS §3）。

### 2.4 事件 `incidents`

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/v1/incidents` | 列表 |
| POST | `/api/v1/incidents` | 建事件（**需幂等键**） |
| GET | `/api/v1/incidents/{incident_id}` | 详情 |
| POST | `/api/v1/incidents/{incident_id}/acknowledge` | 确认（open → acknowledged） |

### 2.5 诊断运行 `runs`

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/v1/incidents/{incident_id}/investigations` | 发起诊断（**需幂等键**，202 返回 `run_id`） |
| GET | `/api/v1/runs/{run_id}` | 状态权威（含 `usage`、`model_config_snapshot`） |
| POST | `/api/v1/runs/{run_id}/cancel` | 取消（仅非终态） |
| GET | `/api/v1/runs/{run_id}/events` | 事件流：默认 JSON 分页；`Accept: text/event-stream` → SSE |
| GET | `/api/v1/runs/{run_id}/evidence` | 证据列表（`items`） |
| GET | `/api/v1/runs/{run_id}/report` | 结构化报告（结论 + 假设 + 引用） |

**SSE 细节**（`app/domains/runs/sse.py`）：`id` 字段等于事件 `seq`；客户端断线重连带
`Last-Event-ID: <seq>` 即从下一条重放；空闲发心跳注释行保活。
`Last-Event-ID` 非整数 → `400 VALIDATION_ERROR`。**GET `/runs/{id}` 永远是状态权威**，
SSE 只是加速通道，不能替代轮询兜底。

### 2.6 知识 `knowledge`

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/v1/knowledge/documents` | 文档列表（字段名 `documents`） |
| POST | `/api/v1/knowledge/documents` | 建文档 + v1 草稿（operator / admin） |
| GET | `/api/v1/knowledge/documents/{document_id}` | 详情（含版本） |
| POST | `/api/v1/knowledge/documents/{document_id}/versions` | 追加草稿版本（operator / admin） |
| POST | `/api/v1/knowledge/versions/{version_id}/review` | 复核（admin；reviewer ≠ author） |
| POST | `/api/v1/knowledge/versions/{version_id}/publish` | 发布（admin；建向量索引，旧版本 superseded） |
| POST | `/api/v1/knowledge/versions/{version_id}/retire` | 退役（admin；退出检索范围） |
| GET | `/api/v1/knowledge/search` | 检索（认证用户，按资产范围前置过滤）：返回 `hits` + `route_ranks` + `latency_ms` |
| POST | `/api/v1/knowledge/from-report/{run_id}` | 把诊断报告转成知识草稿（人工发起，不自动发布、不自我强化） |

检索请求示例：

```http
GET /api/v1/knowledge/search?query=Redis%20连接池耗尽&limit=5
```

```json
{ "data": { "mode": "hybrid", "latency_ms": 46,
  "hits": [ { "title": "复盘：Redis 连接池耗尽导致业务超时（S1 类）",
              "version_no": 1, "ranks": { "text": 1, "vector": 1 } } ] } }
```

`ranks.text=null` 表示全文路没召回、仅向量路召回——这正是 hybrid 相对 `text-only` 的增量。

### 2.7 Runbook

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/v1/runbooks` | 列表（含各版本状态） |
| POST | `/api/v1/runbooks` | 创建（附 v1 草稿，**草稿不可执行**） |
| GET | `/api/v1/runbooks/{runbook_id}` | 详情 |
| POST | `/api/v1/runbooks/{runbook_id}/versions` | 追加草稿版本 |
| POST | `/api/v1/runbooks/versions/{version_id}/publish` | 发布（旧 published 版本退役） |
| POST | `/api/v1/runbooks/versions/{version_id}/retire` | 退役 |

`risk_level` 只能是 `r0` / `r1`，且必须与 `allowlist_action_keys` 中动作的注册风险级一致
（`restart_container@v1` 是 `r1`），否则 dry-run 的 `action_registered` 检查失败。
`allowed_environments` 硬排除 `production`。

### 2.8 提案与执行 `proposals`

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/v1/proposals` | 列表 |
| POST | `/api/v1/proposals` | 建提案（**需幂等键**） |
| GET | `/api/v1/proposals/{proposal_id}` | 详情（含 `executions`、`verifications`） |
| POST | `/api/v1/proposals/{proposal_id}/dry-run` | 只读前置检查（无副作用） |
| POST | `/api/v1/proposals/{proposal_id}/approve` | 审批（approver ≠ 发起人） |
| POST | `/api/v1/proposals/{proposal_id}/reject` | 驳回 |
| POST | `/api/v1/proposals/{proposal_id}/execute` | 入队执行 |

dry-run 返回 7 项检查：

```json
{ "data": { "checks": [
  { "name": "runbook_version_published", "passed": true, "detail": null },
  { "name": "asset_active", "passed": true, "detail": null },
  { "name": "environment_match", "passed": true, "detail": "asset_env=sandbox" },
  { "name": "action_registered", "passed": true, "detail": "risk=r1" },
  { "name": "parameters_valid", "passed": true, "detail": null },
  { "name": "container_binding", "passed": true, "detail": "opspilot-demo-redis" },
  { "name": "not_expired", "passed": true, "detail": "expires_at=..." }
] } }
```

执行结果分级：`succeeded` / `failed` / **`pre_check_failed`**（门禁拒绝）/
`unknown`（超时或连接中断，**不自动重试**，转人工）。
默认配置（`ACTION_EXECUTION_ENABLED=false`）下真实写操作一律在 `pre_check_failed` 被拒，
`result_detail.reason` 记录原因，外部效果确定为零。

### 2.9 集成与能力 `integrations`

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/v1/integrations` | 集成实例列表 |
| POST | `/api/v1/integrations` | 建实例（凭据经 `password_env` 间接引用，不入库明文） |
| POST | `/api/v1/integrations/{instance_id}/test` | 连通性测试 |
| POST | `/api/v1/integrations/{instance_id}/enable` | 启用 |
| POST | `/api/v1/integrations/{instance_id}/disable` | 停用 |
| GET | `/api/v1/capabilities` | 能力注册中心列表 |
| GET | `/api/v1/capabilities/{capability_key}` | 能力详情（参数 schema、只读声明） |
| POST | `/api/v1/capabilities/{capability_key}/tests` | 能力自测 |
| GET | `/api/v1/assets/{asset_id}/observability-bindings` | 观测绑定 |
| PUT | `/api/v1/assets/{asset_id}/observability-bindings` | 建/更新绑定 |
| DELETE | `/api/v1/assets/{asset_id}/observability-bindings/{binding_kind}` | 删除绑定 |

### 2.10 Webhook 告警 `webhooks`

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/v1/webhooks/alerts/{connector_id}` | 接第三方告警（HMAC-SHA256 签名 + 时间戳防重放） |

签名与重复投递语义见 [SECURITY_ACCEPTANCE.md](SECURITY_ACCEPTANCE.md) §3。

### 2.11 评估 `evaluations`

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/v1/evaluations/suites` | 套件列表（4 套件 39 例；含 `dataset_version` / `dataset_digest`）；认证用户可读 |
| POST | `/api/v1/evaluations/runs` | 触发一次评估（202；`wait=true` 时同步执行）；approver / admin |
| GET | `/api/v1/evaluations/runs/{run_id}` | 评估进度、逐案例结果、汇总指标与强门禁 |

评估 API 与 `scripts/eval_fake_report.py` / `eval_real_model.py` 同源但独立：
前者在服务内跑（便于控制台查看），后者给 CI 与离线回归用。

### 2.12 审计 `audit`

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/v1/audit-logs` | 审计流水（谁、何时、对什么、做了什么、结果） |

工具调用、审批、发布、执行、鉴权失败都会落审计。审计写入与业务写入同事务，避免「操作成功但没痕迹」。

---

## 3. 一次完整调用的最小序列

```bash
API=http://127.0.0.1:8000/api/v1
T=$(curl -s --noproxy '*' -X POST $API/auth/login -H 'Content-Type: application/json' \
  -d '{"username":"e2e_operator","password":"E2ePassw0rd!2026"}' | python -c 'import sys,json;print(json.load(sys.stdin)["data"]["access_token"])')

INC=$(curl -s --noproxy '*' -X POST $API/incidents -H "Authorization: Bearer $T" \
  -H 'Content-Type: application/json' -H "Idempotency-Key: demo-$(date +%s)" \
  -d '{"title":"Redis 不可达","environment":"sandbox","primary_asset_id":"as_demo_redis","severity":"high"}' \
  | python -c 'import sys,json;print(json.load(sys.stdin)["data"]["id"])')

curl -s --noproxy '*' -X POST $API/incidents/$INC/acknowledge -H "Authorization: Bearer $T"
curl -s --noproxy '*' -X POST $API/incidents/$INC/investigations -H "Authorization: Bearer $T" \
  -H 'Content-Type: application/json' -H "Idempotency-Key: demo-run-$(date +%s)" -d '{}'
```

`--noproxy '*'` 不是可选项：宿主机若配了 `HTTP_PROXY` 且 `NO_PROXY` 不含 127.0.0.1，
请求会被代理接管并返回 502（见 FAILURE_POSTMORTEMS §9）。
懒人版：`python scripts/demo_routes.py a` 已把这些步骤写全。
