# OpsPilot Next

企业级智能运维故障诊断与受控恢复平台（全新实现，不迁移旧 OpsPilot 代码）。

当前仓库状态：**D1-B 完成**（在 D1-A 基础上新增：故障事件管理、告警 Webhook 接入、
API 幂等、最小 Web 控制台、GitHub CI）。不含 AI Agent / Worker / RAG / RabbitMQ /
Action Service（见 docs/IMPLEMENTATION_PLAN.md 的 D2 及后续阶段）。

## 技术栈

- Python 3.12 + FastAPI + Pydantic v2 + SQLAlchemy 2 (async) + Alembic
- PostgreSQL 16（真实外键/唯一约束/索引，迁移管理，禁用 create_all）
- uv 管理依赖与虚拟环境；Ruff / mypy / pytest 保证工程质量
- JWT Access Token（30 分钟，无 Refresh Token）+ argon2 密码哈希
- Docker Compose：一键启动 PostgreSQL 与 API

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

Web 控制台：<http://localhost:8080>（登录 → 资产管理 → 故障事件 → 创建/确认 → 审计日志）。

登录（首次登录强制改密）：

```bash
curl -X POST http://localhost:8000/api/v1/auth/login \
  -H "Content-Type: application/json" \
  -d '{"username": "admin", "password": "<.env 中引导密码>"}'
# 用返回的 access token 调用 POST /api/v1/auth/change-password 修改密码
```

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
│   ├── models/               # SQLAlchemy 2 模型（D1 全部 11 张表）
│   ├── domains/
│   │   ├── auth/             # 登录/改密/Admin 引导（repository + service + schemas）
│   │   ├── access/           # RBAC 与资产范围判定（纯函数 scope.py）
│   │   ├── assets/           # 资产管理（repository + service + schemas）
│   │   ├── incidents/        # 故障事件（冻结状态机 state_machine.py + CRUD/acknowledge）
│   │   ├── alerts/           # 告警 Webhook 接入（HMAC 验签/去重/建单映射）
│   │   ├── idempotency/      # API 幂等（(user, endpoint, key) 隔离、事务内抢占）
│   │   └── audit/            # 审计事件写入与查询
│   └── api/
│       ├── deps.py           # Session/当前用户/RBAC/范围依赖注入
│       └── v1/               # /api/v1 路由（auth / assets / incidents / webhooks / audit / health）
├── entrypoints/              # 单仓三入口约定：api（D1）/ worker（D2 预留）/ action（D5 预留）
├── web/                      # 最小 Web 控制台（nginx:1.27-alpine + 原生 JS，真实调 API）
├── scripts/                  # seed.py（演示资产）、simulate_alert.py（签名告警模拟）
├── .github/workflows/ci.yml  # CI：Ruff + mypy + pytest（真实 PG）+ Alembic 空库演练
├── alembic/                  # 迁移脚本（async env.py，D1 初始迁移）
├── tests/                    # 单元测试 + 真实 PostgreSQL 集成测试
├── docker-compose.yml        # postgres + api + web
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
