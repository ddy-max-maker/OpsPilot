# OpsPilot Next

企业级智能运维故障诊断与受控恢复平台（全新实现，不迁移旧 OpsPilot 代码）。

当前仓库状态：**D1-A 企业后端基础工程**（认证、RBAC 与资产范围、资产管理、审计、
真实 PostgreSQL 持久化）。不含 AI Agent / Worker / RabbitMQ / Action Service /
Webhook / Incident 业务接口（见 docs/IMPLEMENTATION_PLAN.md 的 D1-B 及后续阶段）。

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

# 3. 启动 PostgreSQL + API（API 容器启动时自动执行 alembic upgrade head）
docker compose up --build -d

# 4. 验证
curl http://localhost:8000/api/v1/health/live
curl http://localhost:8000/api/v1/health/ready
```

登录（首次登录强制改密）：

```bash
curl -X POST http://localhost:8000/api/v1/auth/login \
  -H "Content-Type: application/json" \
  -d '{"username": "admin", "password": "<.env 中引导密码>"}'
# 用返回的 access token 调用 POST /api/v1/auth/change-password 修改密码
```

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
│   │   └── audit/            # 审计事件写入
│   └── api/
│       ├── deps.py           # Session/当前用户/RBAC 依赖注入
│       └── v1/               # /api/v1 路由（auth / assets / health）
├── entrypoints/              # 单仓三入口约定：api（D1）/ worker（D2 预留）/ action（D5 预留）
├── alembic/                  # 迁移脚本（async env.py，D1 初始迁移）
├── tests/                    # 单元测试 + 真实 PostgreSQL 集成测试
├── docker-compose.yml        # postgres + api
└── pyproject.toml            # uv/依赖/Ruff/mypy/pytest 配置
```

## 安全设计要点

- 初始 Admin 仅由环境变量引导创建（`BOOTSTRAP_ADMIN_*`），首登强制改密；
- JWT 无状态短有效期（30 分钟）；密码修改后旧 Token 在剩余有效期内的窗口是已登记技术债务；
- RBAC：`operator/approver/admin/viewer` 四角色；资产管理（创建/修改）仅 Admin；
- 资产范围：`asset_access_policies`（user 级 ∪ role 级），**默认拒绝**；Admin 不受范围限制；
- 越权访问单资源按 404 伪装（不暴露存在性）；列表查询按范围过滤；
- 所有响应统一包裹 `{request_id, data, error}`，错误码为稳定枚举（docs/CONTRACTS.md §1.4）。

## 契约与文档

需求与设计文档位于 `docs/`，以 v1.1 为冻结基线；API 契约的机器可读格式由
`/openapi.json` 提供（FastAPI 自动导出）。
