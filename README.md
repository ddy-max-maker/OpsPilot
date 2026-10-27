# OpsPilot Next

企业级智能运维故障诊断与受控恢复平台（全新实现，不迁移旧 OpsPilot 代码）。

当前仓库状态：**D2 完成**（DeepAgents 智能故障诊断：AgentRuntime + Tool Gateway +
ops-worker + 真实故障演示环境 + 诊断报告）。D1-A/D1-B 能力全部保留；
不含 RAG / RabbitMQ / MCP / Action Service / 自动修复（见 docs/IMPLEMENTATION_PLAN.md 的 D3 及后续阶段）。

## 技术栈

- Python 3.12 + FastAPI + Pydantic v2 + SQLAlchemy 2 (async) + Alembic
- PostgreSQL 16（真实外键/唯一约束/索引，迁移管理，禁用 create_all）
- **DeepAgents 0.7.32 + LangGraph 1.2.18 + LangChain 1.4.7**（D2 智能诊断执行核心，ADR-D2）
- PostgreSQL Checkpointer（`langgraph` 独立 schema，thread_id = run_id）
- uv 管理依赖与虚拟环境；Ruff / mypy / pytest 保证工程质量
- JWT Access Token（30 分钟，无 Refresh Token）+ argon2 密码哈希
- Docker Compose：一键启动 PostgreSQL、API、Web、ops-worker、docker-ro-proxy 与演示环境

## 快速开始

```bash
# 1. 安装依赖（自动创建 .venv，Python 3.12）
uv sync

# 2. 准备环境变量
copy .env.example .env      # Windows
# 编辑 .env：务必修改 JWT_SECRET 与 BOOTSTRAP_ADMIN_PASSWORD

# 3. 启动 PostgreSQL + API + Web 控制台（API 容器启动时自动执行 alembic upgrade head）
docker compose up --build -d

# 4. 验证
curl http://localhost:8000/api/v1/health/live
curl http://localhost:8000/api/v1/health/ready
```

Web 控制台：<http://localhost:8080>（登录 → 资产管理 → 故障事件 → 创建/确认 → 启动诊断 → 审计日志）。

登录（首次登录强制改密）：

```bash
curl -X POST http://localhost:8000/api/v1/auth/login \
  -H "Content-Type: application/json" \
  -d '{"username": "admin", "password": "<.env 中引导密码>"}'
# 用返回的 access token 调用 POST /api/v1/auth/change-password 修改密码
```

## D2 智能诊断（DeepAgents）

架构决策见 `docs/ADR-D2-EXECUTION-CORE.md`：**单一执行核心** = DeepAgents 内置
规划/工具/取证循环（不手写重复 ReAct 图）；业务侧 AgentRuntime 通过
`astream(stream_mode="updates")` 步进驱动，在 super-step 间强制执行
取消 / deadline / 迭代 / Token 预算（服务端硬限制，Prompt 无法绕过）。
框架内部消息对象不外泄领域层；DeepAgents 内置 `execute` 工具与通用 subagent
已通过 HarnessProfile 裁剪，文件操作隔离于 graph state 虚拟 FS。

```bash
# 1. 启动全部服务（含 worker 与只读 Docker 代理）
docker compose up --build -d
docker compose --profile demo up -d        # 演示故障环境（demo-app / demo-redis / demo-gateway）

# 2. 写入连接器/资产/拓扑（幂等）
uv run python scripts/seed_demo.py

# 3. 故障注入（S1 Redis 不可达 / S2 容器退出 / S4 网关 502）与恢复
powershell -File scripts/demo/faults.ps1 inject S1
powershell -File scripts/demo/faults.ps1 status S1
powershell -File scripts/demo/faults.ps1 recover S1

# 4. 端到端诊断：建事件 → 确认 → 创建 Run → 轮询 → 证据/报告
uv run python scripts/run_scenario.py S1
```

模型 Provider 经环境变量配置（`AGENT_PROVIDER`：fake | anthropic | google，
API Key 通过 `AGENT_API_KEY_ENV` 间接引用，密钥不入代码）。`fake` 为确定性
脚本模型（测试/演示）；真实模型验收需提供凭据后单独执行（不进 CI）。

### D2 关键接口

| 方法与路径 | 说明 |
|---|---|
| `POST /api/v1/incidents/{id}/investigations` | 创建诊断 Run（幂等键可选；202） |
| `GET /api/v1/investigation-runs/{id}` | Run 状态（status/stop_reason/usage/budgets） |
| `GET /api/v1/investigation-runs/{id}/events` | 诊断事件流（`after_seq` 增量分页轮询） |
| `POST /api/v1/investigation-runs/{id}/cancel` | 取消运行（合作式 + 服务端强制终止） |
| `GET /api/v1/investigation-runs/{id}/evidence` | 证据列表（脱敏 payload + 引用） |
| `GET /api/v1/investigation-runs/{id}/report` | 结构化诊断报告（最新版本快照） |

Worker（`entrypoints/worker.py`）以 PostgreSQL 任务认领运行：
`SELECT FOR UPDATE SKIP LOCKED` + 租约心跳 + `lease_owner`/`fence_token` fencing
（旧 Worker 失效后禁止回写）；业务状态以 PostgreSQL 为权威，Checkpoint 仅用于图执行恢复。

## 演示（D1-B 链路）

```bash
# 1. 写入演示资产（幂等，可重复运行）
uv run python scripts/seed.py --password "<引导密码>" --new-password "<新密码>"

# 2. 手动创建故障事件（POST /incidents，需 Idempotency-Key；见下方接口示例）

# 3. 模拟外部告警（HMAC 签名；同 delivery 重复投递验证去重；--new-delivery 验证映射）
uv run python scripts/simulate_alert.py --asset-id <as_...> --repeat 3 \
  --secret "<.env 中 WEBHOOK_ALERTS_SECRET>"

# 4. 浏览器打开 http://localhost:8080 查看事件并执行确认（acknowledge）
```

## D1-B 关键接口

| 方法与路径 | 说明 |
|---|---|
| `POST /api/v1/incidents` | 创建故障事件；**必需** `Idempotency-Key` 请求头 |
| `GET /api/v1/incidents` | 列表；`status/severity/environment/primary_asset_id` 筛选 + cursor 分页 |
| `GET /api/v1/incidents/{id}` | 详情（范围内可见，越权 404） |
| `POST /api/v1/incidents/{id}/acknowledge` | 故障确认（open → acknowledged） |
| `POST /api/v1/webhooks/alerts/{connector_id}` | 告警接入：`X-Signature`（HMAC-SHA256 of raw body）+ `X-Timestamp`（±300s） |
| `GET /api/v1/audit-logs` | 审计查询（admin/viewer） |

接口示例（创建事件）：

```bash
curl -X POST http://localhost:8000/api/v1/incidents \
  -H "Authorization: Bearer $TOKEN" \
  -H "Idempotency-Key: $(uuidgen)" \
  -H "Content-Type: application/json" \
  -d '{"title": "支付库连接池耗尽", "environment": "sandbox",
       "primary_asset_id": "as_...", "severity": "high"}'
```

状态机（冻结，非法转换 409 CONFLICT_STATE）：`open → acknowledged → investigating →
mitigating → monitoring → resolved → closed`，另有 `needs_human` 升级通道与
`monitoring → investigating` 回退。

## 本地开发（不使用 Docker 运行 API）

```bash
# 只启动 PostgreSQL
docker compose up -d postgres

# 应用数据库迁移
uv run alembic upgrade head

# 启动 API（开发模式）
uv run uvicorn app.main:app --reload --port 8000
```

## 测试

测试使用真实 PostgreSQL（`docker compose up -d postgres` 后执行），
pytest 启动时自动创建独立的 `opspilot_test` 库并执行迁移。

```bash
uv run pytest                    # 全部测试
uv run pytest -v                 # 显示用例明细
```

## 代码质量

```bash
uv run ruff check app entrypoints tests   # lint
uv run ruff format app entrypoints tests   # 格式化
uv run mypy app entrypoints                # 类型检查
```

## 项目结构

```
OpsPilot-Next/
├── docs/                     # D0 设计文档（PRD/架构/契约/数据模型/实施计划）
├── app/
│   ├── main.py               # FastAPI 应用工厂 + lifespan（引擎初始化/bootstrap）
│   ├── core/                 # 配置、DB Session、安全（JWT/argon2）、错误、响应包裹、分页
│   ├── models/               # SQLAlchemy 2 模型（D1 11 张 + D2 诊断域 7 张表）
│   ├── domains/
│   │   ├── auth/             # 登录/改密/Admin 引导（repository + service + schemas）
│   │   ├── access/           # RBAC 与资产范围判定（纯函数 scope.py）
│   │   ├── assets/           # 资产管理（repository + service + schemas）
│   │   ├── incidents/        # 故障事件（冻结状态机 state_machine.py + CRUD/acknowledge）
│   │   ├── alerts/           # 告警 Webhook 接入（HMAC 验签/去重/建单映射）
│   │   ├── idempotency/      # API 幂等（(user, endpoint, key) 隔离、事务内抢占）
│   │   ├── audit/            # 审计事件写入与查询
│   │   ├── runs/             # D2：InvestigationRun 创建/取消/查询（DB 为业务权威）
│   │   ├── tools/            # D2：Tool Gateway + manifest + 连接器 + 脱敏
│   │   └── agent/            # D2：AgentRuntime/Provider/Fake 模型/Prompts（ADR-D2）
│   ├── worker/               # D2：DB 认领（SKIP LOCKED）+ 租约心跳 + fence_token fencing
│   └── api/
│       ├── deps.py           # Session/当前用户/RBAC/范围依赖注入
│       └── v1/               # /api/v1 路由（auth / assets / incidents / webhooks / audit / runs / health）
├── entrypoints/              # 单仓三入口：api / worker（D2 ops-worker）/ action（D5 预留）
├── roproxy/                  # D2：docker-ro-proxy（Docker Engine API 方法+路径白名单，只读）
├── demo/                     # D2：演示故障环境（demo-app / gateway.conf / crasher.py）
├── web/                      # 最小 Web 控制台（nginx:1.27-alpine + 原生 JS，真实调 API；含诊断入口）
├── scripts/                  # seed.py / simulate_alert.py / seed_demo.py / run_scenario.py / demo/faults.ps1
├── .github/workflows/ci.yml  # CI：Ruff + mypy + pytest（真实 PG）+ Alembic 空库演练（D2 表校验）
├── alembic/                  # 迁移脚本（async env.py；0001 初始 + 0002 D2 诊断域）
├── tests/                    # 单元测试 + 真实 PostgreSQL 集成测试（含 worker/gateway/roproxy/迁移演练）
├── docker-compose.yml        # postgres + api + web + worker + docker-ro-proxy + demo(profile)
└── pyproject.toml            # uv/依赖/Ruff/mypy/pytest 配置
```

## 安全设计要点

- 初始 Admin 仅由环境变量引导创建（`BOOTSTRAP_ADMIN_*`），首登强制改密；
- JWT 无状态短有效期（30 分钟）；密码修改后旧 Token 在剩余有效期内的窗口是已登记技术债务；
- RBAC：`operator/approver/admin/viewer` 四角色；资产管理（创建/修改）仅 Admin；
- 资产范围：`asset_access_policies`（user 级 ∪ role 级），**默认拒绝**；Admin 不受范围限制；
- 越权访问单资源按 404 伪装（不暴露存在性）；列表查询按范围过滤；
- 所有响应统一包裹 `{request_id, data, error}`，错误码为稳定枚举（docs/CONTRACTS.md §1.4）。

## Webhook 告警接入协议

外部告警源向 `POST /api/v1/webhooks/alerts/{connector_id}` 投递 JSON：

```
X-Signature: hex(HMAC-SHA256(secret, raw_body))
X-Timestamp: unix 秒（服务端接受 ±300s 窗口，见 WEBHOOK_TIMESTAMP_TOLERANCE_SECONDS）
```

请求体字段：`delivery_id`（投递去重主键）、`fingerprint`、`title`、`severity`
（low/medium/high/critical）、`asset_id`（须为已登记资产）、可选 `description`/`payload`。
行为：同 `delivery_id` 重复投递返回 409 DUPLICATE_ALERT（不产生第二个事故）；
同资产+同指纹且存在活跃单时关联既有事件；否则以系统身份 `usr_system_alerts` 自动建单。
任何验签/时间窗失败统一返回 401。

## 契约与文档

需求与设计文档位于 `docs/`，以 v1.1 为冻结基线；API 契约的机器可读格式由
`/openapi.json` 提供（FastAPI 自动导出）。
