# OpsPilot Next

企业级智能运维故障诊断与受控恢复平台（全新实现，不迁移旧 OpsPilot 代码）。

当前仓库状态：**D4 完成**（企业历史故障知识库与 RAG：知识生命周期 + 四眼复核 +
PostgreSQL 全文/pgvector 混合检索 + Agent 知识工具 + RAG 消融实验；
含 D3 安全修复：Jaeger 归属校验、Prometheus 服务标签绑定、执行快照审计）。
D1/D2/D3 能力全部保留；不含 MCP / RabbitMQ / Action Service / 自动修复
（见 docs/IMPLEMENTATION_PLAN.md 的 D5 及后续阶段）。

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

# 3. 故障注入（S1 Redis 不可达 / S2 容器退出 / S4 网关上游故障，实测 504）与恢复
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
（旧 Worker 失效后禁止回写；**业务写入与租约校验同事务原子执行**，接管方在
原 Worker 事务提交前无法认领，消除 TOCTOU 竞态）；
业务状态以 PostgreSQL 为权威，Checkpoint 仅用于图执行恢复。

## D3 跨语言接入与可扩展诊断

### Capability Registry（连接器能力注册中心）

新增迁移 `0003_d3_registry`（`connector_types` / `connector_instances` /
`capability_manifests` / `capability_test_runs` / `asset_observability_bindings`）。
核心设计：

- **Manifest v1 冻结 Schema**：`manifest_version`（仅 `"1"`）、`read_only=true`
  强制、`min_platform` 兼容校验；不兼容 Manifest 在注册时被拒绝（API 返回 rejected 列表）。
- **Connector Type ↔ Instance 分离**：同一 Type 可注册多个实例，各绑不同企业资产
  与连接配置（如两个 Prometheus 实例分别服务不同环境）。
- **按资产解析实例**（修复 D2 按 connector_type 聚合导致同类实例相互覆盖的问题）：
  解析顺序 = 资产绑定（`asset_observability_bindings`）→ 单一 enabled 实例默认 →
  多实例未绑定时拒绝（`INSTANCE_AMBIGUOUS`）。Agent 调用工具时依据授权资产解析
  对应实例，**不串用其他资产的凭据或数据**。
- 连接器插件仅允许代码审查后随镜像部署的内置实现（docker.logs / redis.ping /
  http.probe / prometheus.query / jaeger.traces / mysql.diagnostics），
  不支持上传任意脚本执行。
- 单连接器故障不影响其他连接器（异常隔离为 unavailable Evidence）。

### 可观测性标准化接入

| 连接器 | 数据面 | 受控约束 |
|---|---|---|
| `prometheus.query` | GET /api/v1/query_range | 模板白名单（实例 config `allowed_templates`），`{service}` 由网关注入授权资产值；时间范围 ≤1h、点数 ≤60 |
| `jaeger.traces` | Jaeger HTTP API（service 搜索 + trace 详情） | trace_id 格式校验、limit=5、时间范围受限 |
| `mysql.diagnostics` | 预定义只读查询（SHOW STATUS/VARIABLES + performance_schema 慢查询摘要） | 最小权限只读账号（`opspilot_ro`），密码经 `password_env` 环境变量注入，无通用 SQL 执行器 |

OTel：demo-java / demo-app 经 OTLP（4318）导出到 Jaeger all-in-one，
Prometheus 抓取两个服务的指标端点。目标服务返回的日志/Trace 属性视为
不可信数据（仅作证据，不当指令）。

### Java + Python 跨语言诊断

`demo-java/`：Spring Boot 3.3.5（Java 17）真实服务——健康检查（actuator）、
业务接口（订单）、Micrometer→Prometheus 指标、Micrometer Tracing→OTLP→Jaeger 链路。
故障注入场景：J1 接口延迟、J2 异常响应（错误率）、J3 依赖故障（下游 503）。
两种语言服务均真实运行于 Docker Compose，**同一 AgentRuntime / Tool Gateway /
报告机制诊断**（不复制 Agent 引擎）。能力边界：未接入 JMX/JVM 专用指标时，
JVM 内存/GC/线程池细节不可观测（诊断依赖日志、HTTP 指标与链路数据）。

### 证据质量语义（向后兼容）

`classify_evidence` 四分类：`HEALTHY`（ok）/ `FAULT_OBSERVED`（成功采集到目标
错误响应，如 HTTP_504——**有效故障观测，但不足以证明具体上游根因**）/
`INDETERMINATE`（超时、连接错误等不可判读）/ `COLLECTION_FAILED`（连接器失败）。
`supported` 根因须 ≥1 条 `root_cause_capable`（HEALTHY 或 FAULT_OBSERVED）证据；
仅有采集失败/不可判读证据时降级为 unknown（不编造根因）。

### D3 新增 API

| 方法与路径 | 说明 |
|---|---|
| `GET /api/v1/integrations` | 连接器实例列表（含最近测试结果） |
| `POST /api/v1/integrations` | 注册实例（拒绝未知 Type 与内联密钥） |
| `POST /api/v1/integrations/{id}/test` | 连通性测试（记录 `capability_test_runs`，Admin） |
| `POST /api/v1/integrations/{id}/enable` / `disable` | 启用/禁用实例（Admin） |
| `GET /api/v1/capabilities` | 能力发现（内置 6 Manifest） |
| `POST /api/v1/capabilities/{key}/tests` | 触发能力测试（Admin） |
| `GET/PUT/DELETE /api/v1/assets/{id}/observability-bindings` | 资产↔实例绑定管理 |

Web 控制台新增"连接器"页：查看能力/实例、配置与绑定资产、测试连通性、
查看最近一次测试结果。

### D3 演示流程

```bash
docker compose up --build -d          # 全部服务（含 demo-java / prometheus / jaeger / demo-mysql）
uv run python scripts/seed_demo.py    # 6 类型 / 6 实例 / 6 资产 / 5 绑定（幂等）

powershell -File scripts/demo/faults.ps1 inject J2     # Java 异常响应（错误率）
powershell -File scripts/demo/faults.ps1 inject J3     # Java 依赖故障（下游 503）
powershell -File scripts/demo/faults.ps1 inject M1     # MySQL 慢查询负载
powershell -File scripts/demo/faults.ps1 recover M1
```

## D4 企业历史故障知识库与 RAG

### 架构与红线

- **知识生命周期**（CONTRACTS §2.9）：`draft → reviewed → published → superseded/retired`，
  发布必须经人工复核（**四眼：reviewed_by ≠ created_by，服务端强制**）；
  发布/退役/版本变更写审计；未发布/过期/退役知识对 Agent 不可见。
- **检索红线**：权限、状态、有效期、资产范围、环境过滤**全部发生在 SQL 排序之前**
  （`applies_to.asset_ids` 与 Run 授权快照按资产粒度求交；不做"全局 Top-K 后 Python 剔除"）。
- **知识不是证据**：`source_type=knowledge` 的检索结果即使 status=ok 也**永不参与
  supported 根因支撑**（`classify_evidence(source_type="knowledge") → KNOWLEDGE 质量，
  `root_cause_capable=False`）；历史相似性只引导排查方向，supported 根因必须由本
  Run 实时业务观测支撑（runtime 服务端强制降级 + Prompt 双保险）。
- **混合检索**（仅 PostgreSQL，无独立向量库）：text-only = simple 全文 + 逐词 ILIKE；
  hybrid = 文本路 + pgvector 余弦距离路，RRF（k=60）融合；每路得分/最终排名/命中
  版本写 `retrieval_events`。小规模知识集采用过滤后精确向量排序（召回正确性优先，
  HNSW 索引按契约建立）。
- **Embedding 版本化**：`knowledge_versions` 记录 embedding_model/model_version/
  vector_dimension/chunking_version；检索强制过滤 model_version——换模型 = 新增
  版本向量行，不覆盖历史索引。

### Embedding Provider

| Provider | 模型 | 维度 | 用途 |
|---|---|---|---|
| `local_e5`（默认） | intfloat/multilingual-e5-small（query/passage 前缀） | 384 | 真实语义检索（需 `uv sync --group embedding`） |
| `fake` | 确定性哈希向量 | 384 | CI（不联网、无 API Key） |

### D4 API 与 Web

| 方法与路径 | 说明 |
|---|---|
| `GET/POST /knowledge/documents`、`GET /knowledge/documents/{id}` | 文档与版本管理 |
| `POST /knowledge/documents/{id}/versions` | 追加草稿版本 |
| `POST /knowledge/versions/{id}/review` | 人工复核（四眼校验） |
| `POST /knowledge/versions/{id}/publish` / `retire` | 发布（构建向量索引）/ 退役 |
| `GET /knowledge/search` | 权限前置过滤 + text/hybrid 检索（返回引用与得分） |
| `POST /knowledge/from-report/{run_id}` | 诊断报告 → 知识草稿（人工触发，不自动发布） |
| `GET/POST /runbooks`、`POST /runbooks/{id}/versions` | Runbook 管理（草稿不可执行） |
| `POST /runbooks/versions/{id}/publish` / `retire` | 发布 / 退役 |

Agent 工具（Tool Gateway，受预算/限流/审计约束）：`retrieve_incidents`（历史事故
复盘/guide）、`search_runbooks`（已发布 Runbook 引用）。每次检索写 `retrieval_events`
（模式/过滤/命中/每路排名/耗时/used_by_agent）；知识内容按不可信文本处理
（脱敏 + Prompt Injection 模式标记）。

Web 控制台新增"知识库"页：检索、创建草稿、复核/发布/退役、Runbook 查看。

### D4 演示与消融实验

```bash
uv run python scripts/demo/seed_knowledge.py     # 8 场景知识集（幂等）

# worker 以对应模式 + DeepSeek 启动后（shell 覆盖 RAG_MODE / AGENT_PROVIDER 等）：
powershell -File scripts/demo/faults.ps1 inject S1
uv run python scripts/rag_experiment.py --mode hybrid --scenario S1 --password '...'
powershell -File scripts/demo/faults.ps1 recover S1
# 对照：--mode off / text-only / hybrid × S1、M1（核心固定场景）；J2/S2 单模式补充
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
│   ├── models/               # SQLAlchemy 2 模型（D1 11 张 + D2 诊断域 7 张 + D3 注册中心 5 张）
│   ├── domains/
│   │   ├── auth/             # 登录/改密/Admin 引导（repository + service + schemas）
│   │   ├── access/           # RBAC 与资产范围判定（纯函数 scope.py）
│   │   ├── assets/           # 资产管理（repository + service + schemas）
│   │   ├── incidents/        # 故障事件（冻结状态机 state_machine.py + CRUD/acknowledge）
│   │   ├── alerts/           # 告警 Webhook 接入（HMAC 验签/去重/建单映射）
│   │   ├── idempotency/      # API 幂等（(user, endpoint, key) 隔离、事务内抢占）
│   │   ├── audit/            # 审计事件写入与查询
│   │   ├── runs/             # D2：InvestigationRun 创建/取消/查询（DB 为业务权威）
│   │   ├── tools/            # D2/D3：Tool Gateway + manifest + registry（按资产解析实例）+ 连接器 + 脱敏
│   │   ├── agent/            # D2：AgentRuntime/Provider/Fake 模型/Prompts（ADR-D2）
│   │   ├── integrations/     # D3：连接器实例注册/测试/启停 + 能力发现服务
│   │   ├── knowledge/        # D4：知识生命周期（四眼复核/发布/退役/报告转草稿）
│   │   └── rag/              # D4：Embedding Provider + 分块 + 混合检索（RRF）
│   ├── worker/               # D2：DB 认领（SKIP LOCKED）+ 租约心跳 + fence_token fencing
│   └── api/
│       ├── deps.py           # Session/当前用户/RBAC/范围依赖注入
│       └── v1/               # /api/v1 路由（auth / assets / incidents / webhooks / audit / runs / integrations / capabilities / health）
├── entrypoints/              # 单仓三入口：api / worker（D2 ops-worker）/ action（D5 预留）
├── roproxy/                  # D2：docker-ro-proxy（Docker Engine API 方法+路径白名单，只读）
├── demo/                     # D2/D3：演示故障环境（demo-app / gateway.conf / crasher.py / prometheus.yml / mysql-init / mysql_load.sh）
├── demo-java/                # D3：Java Spring Boot 演示服务（健康/业务/指标/OTel 链路 + J1/J2/J3 故障注入）
├── web/                      # 最小 Web 控制台（nginx:1.27-alpine + 原生 JS，真实调 API；含诊断与连接器管理入口）
├── scripts/                  # seed.py / simulate_alert.py / seed_demo.py / run_scenario.py / demo/faults.ps1
├── .github/workflows/ci.yml  # CI：Ruff + mypy + pytest（真实 PG）+ Alembic 空库演练（含 D3/D4 表校验）
├── alembic/                  # 迁移脚本（async env.py；0001 初始 + 0002 D2 + 0003 D3 + 0004 D4 知识库/pgvector）
├── tests/                    # 单元测试 + 真实 PostgreSQL 集成测试（含 worker/gateway/roproxy/integrations/迁移演练）
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
