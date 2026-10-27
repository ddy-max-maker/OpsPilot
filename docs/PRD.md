# OpsPilot Next｜企业级智能运维故障诊断与受控恢复平台

**PRD + Architecture + Learning & Interview Plan｜v1.1（需求评审版）**  
**日期：2026-10-26**  
**状态：需求与总体方案；尚未创建新业务工程、开发或部署**  
**原则：全新实现，不迁移旧 OpsPilot 代码。`deepagents-upstream` 是框架学习样例，不直接改造成产品。**

> 本文中的功能、接口、指标均为**拟建需求与验收方案**，不是已经实现的成果或实测结果。v1.1 整合了三项新增诉求：未知故障扩展、企业历史知识复用（RAG）、跨语言低侵入接入。以项目求职展示价值、学习价值、可迭代性为共同约束。

## 0. 本次评审决定及与 v1.0 的区别

### 保留

- 产品主线：`事件/告警 → 获取上下文 → AI 只读取证 → 候选根因与证据 → 受控处置提案 → 审批 → 测试环境执行 → 恢复验证 → 事故报告`。
- 核心选型：Python 3.12、FastAPI、PostgreSQL、LangChain / DeepAgents / LangGraph、Vue 3、Docker Compose；需要异步可靠交付时使用 RabbitMQ、Outbox 和 Worker。
- Agent 决策与有副作用的执行分离；真实生产环境首版默认只读；审计、故障注入和回归评估属于核心交付。

### 调整

1. **RAG 纳入首个完整交付版本**，但分阶段交付：先有经过审核的历史事故和 Runbook 检索，随后加入 pgvector 语义检索、混合排序与 RAG 开关对照，不在第一天引入复杂文档解析平台。
2. **新增“统一接入与证据模型”**：对 Java、Go、C++、Python 不按语言重写核心 Agent，优先通过现有日志、HTTP 健康检查、Prometheus 指标、受限基础设施接口接入；OpenTelemetry 数据可通过企业已有可观测性后端暴露给 OpsPilot。
3. **新增连接器/能力规范（Capability Registry）**，支持注册、版本、输入输出 Schema、权限、测试和禁用；不支持网页上传任意脚本后自动执行。
4. **未知故障需支持“拒绝确定性结论 / 请求人工协助”**；相似历史事件不得视为当前根因的直接证据。
5. **P0 分为“先跑通的垂直闭环”和“首个可写简历的完整版本”**，避免所有模块同时开工；P1/P2 是明确预留的后续迭代。
6. **增加示范企业测试环境**：至少两个不同语言/框架的可观测样例服务，基于同一接入协议验收；建议 Python FastAPI + Java Spring Boot，Go/C++ 作为后续适配验证。
7. **预先定义面试/学习验收**：每个阶段不仅交代码，还要可以追踪源码、解释数据流、处理故障和回答取舍问题。

### 明确不承诺

- 零配置适配市场上全部项目；不获得任何日志/指标权限时自动诊断任意故障；把相关性当成真实根因；任意生产环境自动修复；Agent 凭语言指令执行通用 Shell/SQL；未经验证的企业级高可用 SLA。

## 1. 产品目标、用户与边界

**一句话定位：** 对接企业现有的服务资产、日志、指标、历史事故和经过授权的运维工具，对已知及未知故障进行证据驱动诊断，通过审批与受控执行形成可追溯的处理闭环。它**不是** Prometheus/Grafana/ELK 的替代品，不从零打造全量日志采集系统。

**使用对象：** 中小企业的研发、值班运维和 SRE 团队。首个交付版本支持单个组织的隔离环境（sandbox/staging），为以后多团队、多实例扩展预留领域边界；不宣称多租户 SaaS。

**角色：** Operator（报障、诊断）、Approver（受控操作审批）、Admin（资产、连接器和权限）、Viewer/Auditor（只读查询与审计）。

**用户关键收益：** 减少人工逐个检查服务的工作；重复故障复用已审查经验；未知故障收集并组织证据；避免未经授权的危险自动修复；保留完整诊断和事故复盘记录。

## 2. 明确场景与成功定义

**典型场景：**

| 编号 | 故障场景 | 标准取证方式 | 期望系统行为 |
|---|---|---|---|
| S1 | Redis 连接不可达 | 授权只读健康检查、应用错误日志 | 说明连接故障证据，不擅自重启生产服务 |
| S2 | Docker 容器异常重启 | 状态、事件、OOM/退出码、近期日志 | 识别容器和潜在原因 |
| S3 | MySQL 连接池耗尽 | 应用连接池指标、数据库只读状态、日志 | 结合证据生成假设，不直接修改数据库 |
| S4 | 网关 502 | HTTP/上游健康、网关错误日志 | 关联依赖服务，而不只报告“网关故障” |
| S5 | 主机磁盘不足 | 只读资源指标、写入错误 | 输出影响范围及处置建议 |
| S6 | 跨服务链路延迟 | 带时间窗的多源指标、日志及可用 Trace | 将根因与现象区分，列出支持/反证 |
| S7 | 未知故障或数据冲突 | 现有工具可获得的所有授权证据 | 继续合理排查，证据不足时明确“待确认” |
| S8 | 与历史事故相似的故障 | 当前实时证据 + 经审查的 RAG 检索结果 | 历史经验用于制定排查策略，不直接当结论 |
| S9 | 低风险修复后仍不健康 | 操作日志、后续健康检查/观察窗口 | 标记验证失败，绝不因命令成功就宣告恢复 |
| S10 | Agent/工具/Worker 失败 | 超时、错误、队列重投、checkpoint | 可查询失败状态，不重复产生副作用 |

**产品成功不是“模型说对了”。** 必须在隔离环境中可复现事故、回放数据、验收权限和恢复真实性，并公开失败案例。

## 3. 业务全流程

1. **接入**：手工事件或受信告警 Webhook → 签名/来源校验、去重、关联已登记的资产与环境。
2. **准备**：创建 Investigation Run、预算、授权资源范围快照；加载服务依赖与可用观测源。
3. **知识召回**：查询已发布 Runbook 和历史事故（按资产、环境、时间、标签、权限过滤）；返回引用与适用条件。
4. **规划与取证**：DeepAgents 协调只读工具调用；工具结果进入统一 Evidence 格式，附时间、目标、来源、采集状态。
5. **分析**：输出多个根因假设、支持/反证、缺失信息和下一步诊断；达到次数/时间/token 预算后安全停止。
6. **提案**：有合适已批准 Runbook 且满足前置条件时生成结构化 Proposal；没有则报告并要求人工处理。
7. **策略/审批**：业务服务端校验 RBAC、资源范围、风险级别、参数快照、审批有效期和四眼原则。
8. **执行/验证**：仅隔离环境允许预先登记的 R1 变更；幂等执行、验证观察窗口，必要时标记结果未知并人工接管。
9. **复盘/沉淀**：报告和证据归档。历史事件先成为知识草稿，由人复核后发布到可检索知识库，不直接自我强化。

**三层边界：** AI 仅负责诊断建议；Tool Gateway 管理只读取证；独立 Action Service 负责批准后的有副作用操作。大模型输出不是授权凭据。

## 4. 功能范围和优先级

### 4.1 P0-A：最小可验证业务链（先跑通，不作为简历最终交付）

- 隔离演示环境；服务/资产登记；事件创建；至少 HTTP、Docker、Redis 的安全只读连接器。
- DeepAgents Agent 完成规划→取证→证据引用→根因假设→报告。后端可查询 run 状态与工具轨迹。
- 从开始就存在 Tool Gateway / schema 校验 / 明确错误 / 预算与超时；不允许先做“全权限 Agent”再补安全。
- 至少可复现 3 种故障，一条端到端链路无人工 CLI 操作（可用初始简化 UI）。

### 4.2 P0-B：首个完整可写简历的项目交付

| 模块 | 要求 | 验收核心 |
|---|---|---|
| 身份权限 | 登录、RBAC、资产/环境范围、审批分离 | 越权访问、审批、执行均失败 |
| 事件和资产中心 | Incident/Alert、资源拓扑、环境、关联与去重 | 不同事件/服务有稳定 ID 与状态 |
| 跨技术栈接入 | Connector 规范、能力注册、健康/日志/指标等统一 Evidence | 同一种核心 Agent 能诊断两种不同技术栈样例服务 |
| Agent 诊断引擎 | 规划、只读工具、证据链、未知故障退出、预算 | 不编造工具成功，不在证据不足时强行确定根因 |
| 运维知识/RAG | 版本化 Runbook、已审核事故、文本+向量检索、引用 | 同场景可比较 RAG 开/关效果，具备权限/时效过滤 |
| 审批与受控处置 | Proposal、策略、审核、allowlist Action、dry-run | 仅 sandbox/staging 允许低风险动作，不存在绕过审批路径 |
| 任务可靠性 | 异步 Worker、业务状态、消息幂等、checkpoint、重连/取消 | Worker 被终止后正确结束或恢复，重复消息无重复副作用 |
| 恢复验证 | 真实健康检查、观察窗口、结果分类 | 操作成功但恢复失败不得被标为已解决 |
| Web 控制台 | 事件、诊断过程、知识/配置、审批/报告、评估 | 普通操作员能在 UI 完成核心闭环 |
| 评估与交付 | ≥30 个固定故障/安全场景、CI、Docker Compose、OpenAPI、项目文档 | 所有指标可复现，保留原始基线与失败案例 |

**备注：** P0-B 表示最终首版质量要求，**不表示所有模块同一时间开发**。RAG 的高级文件解析、复杂多 Agent 协作并不是首版核心。

### 4.3 P1：适配与体验增强（架构预留，验收独立）

- 更丰富的 OpenTelemetry/Prometheus/日志后端适配、Alertmanager 集成。
- Go/C++ 或 Kubernetes 诊断示例、JVM/JMX、pprof、C++ core dump 只读分析插件。
- 文档上传解析、增量索引、混合检索重排、知识反馈闭环优化。
- MCP 适配器（必须经过 Tool Gateway）；复杂问题专业 subagent，对比基线后决定启用。
- 事件通知、定时巡检及更多可控 Runbook。

### 4.4 P2：长期版本候选

SSO/OIDC、多组织权限隔离、Kubernetes 受控操作、可扩展的 Connector SDK/插件签名、分布式 HA/多节点协调、多模态运维报告、企业知识治理与保留策略可配置。每项需独立评审和安全验收。

## 5. 跨语言低侵入接入设计（新增重点）

**目标：** 不依赖应用编程语言实现主要诊断流程；优先读取企业**已经存在**的观测数据。兼容 Java/Go/Python/C++ 的方法是统一协议和可观测数据，不是要求 LLM 直接读懂所有项目源码。

### 接入等级

| 等级 | 用户所需动作 | 首版承诺 |
|---|---|---|
| L1 基础接入 | 登记服务地址、环境、已授权的日志/指标/健康接口或基础设施连接凭据 | 通过配置提供健康、容器、基础诊断，达到有限的“即插即用” |
| L2 深入观测 | 企业已部署或愿意配置 Prometheus、OpenTelemetry、JMX 等 | 按 Connector 能力扩展；不保证完全无代码修改 |
| L3 业务专用诊断 | 提供业务状态、链路、专用只读接口/Runbook | 通过插件和知识配置实现，不承诺零定制 |

### 统一 Evidence 契约

```json
{
  "evidence_id": "ev_xxx",
  "run_id": "run_xxx",
  "asset_id": "asset_xxx",
  "environment": "staging",
  "source_type": "metric|log|trace|health|infra|knowledge",
  "collector": "prometheus.query|docker.logs|redis.health",
  "observed_at": "ISO8601",
  "time_range": {"start": "ISO8601", "end": "ISO8601"},
  "status": "ok|error|timeout|unavailable",
  "data_ref": "object-or-row-reference",
  "summary_redacted": "脱敏摘要",
  "correlation": {"service": "order-api", "trace_id": "optional"},
  "error_code": null
}
```

要求：时间同步和范围标记、service/asset 映射、采集失败不伪装为空数据、字段版本、数据上限、脱敏和权限过滤；将**当前实时观测**与**历史知识**在来源上分开。

### 连接器 Capability Manifest

每个插件声明 `connector_id/version`、所支持的源类型、提供的 read-only capabilities、`input_schema/output_schema`、环境和资产作用域、鉴权引用、timeout/rate_limit、健康检查与兼容版本。连接器对 Agent 的返回内容必须转成统一 Evidence。

注册中心支持：查看、校验、启用/禁用、版本和兼容检查、连通性测试；**不支持前端任意上传 Python/Shell 立即执行**。新插件经过代码审查、隔离测试和管理员受控部署。

**示范验收建议：** Python FastAPI + Java Spring Boot 两个不同语言的可复现应用，共用 HTTP/日志/指标接入；主诊断 Agent 不随语言改变。C++/Go 技术可适配，但必须实现并测试相应案例后才能在简历声称实际验证。

## 6. 运维 RAG / 长期事故记忆（新增重点）

**为什么是核心能力：** 企业会反复遇到相似故障。通过历史事故和 Runbook 检索可以指导排查顺序、减少无效工具调用，但不可将历史相似性当成当前已确认根因。

### 数据与生命周期

- 来源：人工编写的 Runbook；诊断完成的历史 Incident 报告；经验证的处置记录。未经复核的模型结论默认是 `draft`。
- 知识状态：`draft → reviewed → published → superseded/retired`。保留来源、负责人、审批人、最后验证时间、适用资产/环境/版本、敏感级别和失效条件。
- 检索前先过滤当前用户权限、资产范围、环境、有效性，再执行文本/语义排序；历史知识输出带引用与适用条件。
- P0-B 支持 PostgreSQL 全文/元数据检索 + pgvector 语义检索及简单融合。Embedding 模型和版本入库；更新、失效和回滚需要可追踪。
- RAG 产物只为 Agent 提供**诊断线索**，不能授予任何执行权限。所有实时根因仍须当前 Evidence 支撑。

### 评估

相同的事故/观测输入比较 `RAG=off`、`text-only` 和 `hybrid-RAG`：根因命中、引用有效率、误导检索比例、耗时、工具调用次数、token/费用。不允许只展示最成功的几个样例。

### 不属于 P0-B

通用 PDF/OCR 批处理、复杂知识图谱、知识自动改写并发布、全企业多租户检索、独立 Milvus 集群。这些可能增加成本而未必改善运维诊断。

## 7. 技术架构及边界

```text
      Vue3 / TypeScript / Element Plus
                  |
       FastAPI REST + SSE API
                  |
   Auth/RBAC / Asset / Incident / Knowledge / Approval
                  |
      PostgreSQL (business + knowledge + checkpoints)
                  |
       Outbox -> RabbitMQ -> Agent Worker
                                  |
                  Business Orchestrator (state & budgets)
                                  |
                   DeepAgents / LangGraph Runtime
                                  |
               Tool Gateway + Capability Registry
              /           |            |          \
           HTTP        Docker        Redis       MySQL / Observability
                                  |
                       Normalized Evidence
                                  |
                       Hypotheses / Report
                                  |
                  Proposal -> Policy -> Approval
                                  |
                   Action Service (allowlist)
                                  |
                   Verification -> Incident Report
                                  |
                        Reviewed RAG Knowledge
```

### 基础选型

- Python 3.12 + FastAPI / Pydantic 2 / SQLAlchemy 2 / Alembic。
- DeepAgents 负责 Agent 执行能力、工具使用与上下文管理；LangGraph 负责显式可持久化执行与必要的中断/恢复；**不要假设 `create_deep_agent()` 自动替我们实现整个业务状态机**。
- PostgreSQL 统一存储业务状态、事故知识、pgvector 数据，LangGraph checkpoint 使用独立 schema/命名空间；区分业务数据库、图内记忆和历史知识。
- RabbitMQ + 独立 Worker 用于长任务异步交付；业务主状态以数据库为准，Outbox 保证发送边界一致性。至少一次投递并非恰好一次执行，Action Service 还需业务幂等账本。
- Vue 3 + TypeScript + Element Plus、按需 ECharts；SSE 展示可重放 run_events，HTTP 查询仍是状态权威来源。
- Docker Compose 提供系统与隔离的可注入故障环境；日志与指标优先使用已有采集方式，OpsPilot 不实现通用监控平台。
- ModelProvider 采用配置切换一个经验证、支持工具调用的模型；Embedding 可使用兼容服务或本地方案，避免强制购买多家 API；版本锁定，成本计量。

### 最重要的职责分界

- **Business Orchestrator：** 事故/运行/审批/执行的业务状态、权限、预算和任务调度。
- **Agent Runtime：** 规划和调用授权只读工具、分析证据、提出结构化假设或处置建议；不拥有生产写凭据。
- **Tool Gateway：** 参数和资产范围校验、速率/超时、只读防线、审计、SSRF/命令注入防护。
- **Action Service：** 服务端审核快照、幂等、前置检查和有副作用操作（首版仅隔离环境）。
- **Evidence/RAG：** 实时事实与历史经验分离，避免历史错误污染诊断。

## 8. 安全、可靠性与数据治理

- `sandbox / staging / production` 三类环境；生产在首版只读。R0 安全只读；R1 在隔离环境经批准执行固定动作；R2 高风险生产变更不实现；R3 任意 Shell/删库等永久禁止。
- 身份、资产范围、环境、动作、参数哈希、Runbook 版本和审批有效期必须服务端复查；四眼原则；模型与前端均不能越权。
- 外部日志、网页、Runbook、工具输出均视为不可信文本；Prompt Injection 不得影响授权或调用未开放的工具；连接器输出脱敏和长度限制。
- 密钥和令牌仅服务端引用；不得写到 Prompt、截图、trace 或通用错误响应；需要说明企业数据发往第三方模型的隐私边界，允许在部署时替换为合规模型。
- 所有外部请求有超时、速率、大小上限、目标白名单；防 SSRF、路径遍历、无限重试、慢请求放大。
- 审批后外部动作的结果可能`unknown`；此时不可盲目重试。重启 Worker/重复队列投递时依据 action_key 和外部执行证据做幂等校验。
- 日志、Evidence、checkpoint 和 RAG 索引应有归档/保留策略、脱敏策略与删除路径；不同项目/环境知识不可越权召回。
- 独立审批日志和业务审计不可被 Agent 改写。真正生产部署需额外安全/合规/容量验收，不以“企业级设计”替代生产认证。

## 9. Agent 与状态设计

业务 Incident：`OPEN → ACKNOWLEDGED → INVESTIGATING → MITIGATING → MONITORING → RESOLVED / CLOSED`；无法确诊可转 `NEEDS_HUMAN`。Diagnose Run：`CREATED → QUEUED → RUNNING → COMPLETED / WAITING_APPROVAL / NEEDS_HUMAN / FAILED / CANCELED`。

建议显式业务流程：

`Validate → LoadAssets → RetrieveKnowledge → Plan → CollectEvidence → Analyze → VerifyClaims → {CollectMore | Report | Propose} → PolicyGate → Approval → Execute → VerifyRecovery → Finalize`。

- 对连续未获得新证据的调查触发停止条件；工具调用、迭代、token、时间预算都必须是**服务端硬限制**，不是只写在 Agent Prompt 里。
- 间歇性工具失败与真实业务故障分开标记；未知可明确退出；生成报告时每个关键结论带 Evidence ID。
- 默认**单主 Agent**，专业 subagent 是受控实验而非框架越复杂越好；先比较准确率、成本、延迟和可解释性。
- LangGraph HITL 可以协助中断，但不能替代独立的企业审批策略和有副作用 Action Service。

## 10. API 合约清单 `/api/v1`

所有 API 使用稳定版本路径和 OpenAPI Schema；响应格式 `{request_id, data, error}`；列表 cursor 分页；异步返回 `202 + run_id`，重要 POST 支持 `Idempotency-Key`。生产环境操作的服务端策略高于 UI 状态。

| 模块 | 方法 / 接口 | 说明 |
|---|---|---|
| Auth | `POST /auth/login`、`GET /auth/me` | 登录/权限信息 |
| Assets | `GET/POST /assets`、`PATCH /assets/{id}` | 服务、环境、依赖关系 |
| Integrations | `GET/POST /integrations`、`POST /integrations/{id}/test`、`POST /integrations/{id}/disable` | 连接器配置与生命周期 |
| Capabilities | `GET /capabilities`、`GET /capabilities/{id}`、`POST /capabilities/{id}/tests` | 能力发现、Schema、权限及受控测试；插件部署走管理员流程 |
| Alerts | `POST /webhooks/alerts/{connector_id}` | 受信告警接入和去重 |
| Incidents | `POST/GET /incidents`、`GET /incidents/{id}`、`POST /incidents/{id}/acknowledge` | 事件处理 |
| Runs | `POST /incidents/{id}/investigations`、`GET /runs/{id}`、`GET /runs/{id}/events`、`POST /runs/{id}/cancel` | 诊断任务和 SSE |
| Evidence | `GET /runs/{id}/evidence`、`GET /runs/{id}/report` | 证据和报告 |
| Knowledge | `GET/POST /knowledge/documents`、`GET /knowledge/search`、`POST /knowledge/{id}/publish`、`POST /knowledge/{id}/retire` | 版本、检索、审核发布（权限受控） |
| Runbooks | `GET/POST /runbooks`、`PATCH /runbooks/{id}`、`POST /runbooks/{id}/publish` | 受控操作模板；草稿不直接变为可执行 |
| Proposals | `GET /proposals/{id}`、`POST /proposals/{id}/dry-run`、`POST /proposals/{id}/approve`、`POST /proposals/{id}/reject`、`POST /proposals/{id}/execute` | 审批与执行入口 |
| Executions | `GET /executions/{id}` | 执行/验证状态 |
| Eval | `GET /evaluations/suites`、`POST /evaluations/runs`、`GET /evaluations/runs/{id}` | 固定数据集测评 |
| Audit | `GET /audit-logs` | 审计查询 |
| Health | `GET /health/live`、`GET /health/ready` | 服务健康 |

**对外 API 与内部 Agent Tool 严格区分。** Tool Gateway 示例工具：`get_service_health`, `get_container_status`, `read_container_logs`, `read_mysql_diagnostics`, `check_redis_health`, `query_metrics`, `query_traces`, `retrieve_incidents`, `search_runbooks`。模型**不会拿到通用 shell 或通用 SQL 执行器**。

## 11. 核心数据模型

| 领域 | 主要实体 |
|---|---|
| 用户/权限 | users、roles、user_roles、asset_access_policies |
| 资产/拓扑 | assets、asset_edges、asset_observability_bindings、integrations、secret_refs |
| 连接器/能力 | connector_types、connector_instances、capability_manifests、capability_test_runs |
| 故障 | alerts、incidents、investigation_runs、run_events、evidence、hypotheses |
| 运维知识 | knowledge_documents、knowledge_versions、knowledge_chunks、knowledge_embeddings、retrieval_events、runbooks、runbook_versions |
| 处置与审计 | proposals、approvals、action_executions、action_idempotency、verification_results、audit_events |
| 异步与评估 | outbox_events、evaluation_suites、evaluation_cases、evaluation_runs、evaluation_results |
| LangGraph | 独立 checkpoint schema，保留 thread_id/checkpoint_id；不把它当业务事件表 |

**关键关联：** `incident 1:N investigation_runs`；`run 1:N evidence/hypotheses/events`；`proposal → approval → action_execution → verification`；`knowledge_version → chunks/embedding`，每条知识保留生成它的已确认事故/Runbook 来源。生产查询应有必要索引/外键/迁移约束，不用 JSON 大字段代替所有可检索的业务关系。

## 12. 页面与产品体验

**首个可验收流程主要使用 5 个导航域，不为每张表开发独立页面：**

1. **工作台与事件**：故障看板、事件列表、告警时间线、资产和环境。
2. **诊断详情**：主 Agent 规划步骤、运行/失败/停止原因、证据来源、实时进度、候选根因、报告。
3. **知识中心**：历史案例检索、Runbook 查看和版本/审核状态、引用可追溯。
4. **审批与恢复**：方案 diff、参数、风险、dry-run、审批、执行、健康验证。
5. **平台与质量**：接入/连接器配置、权限、评估结果、审计信息。

所有危险操作需要明显标出目标环境、资源、影响和批准者；前端按钮禁用不是权限实现。SSE 断连后可从持久化 run_events 重放，重要状态以 GET 结果为准。

## 13. 评测与可信简历数据

建立可复现故障注入环境，首个完整交付含 **≥30 个确定性故障/安全用例**。拆分为开发调试集与保留测试集，避免一边改 Prompt 一边反复针对测试答案调优。

### 验证矩阵

- 故障正确性：单服务、跨服务、误导性历史相似事件、未知/证据不足、工具不可用。
- RAG 消融：off vs text-only vs hybrid；检查旧经验是否误导新根因。
- 跨栈接入：Python、Java 两个服务使用同一接入/证据接口成功被诊断。
- 安全：Prompt Injection、凭据泄露、越权资产查询、未审批操作、审批过期与参数篡改。
- 工程容灾：网络超时、消息重投、Worker 崩溃、取消、中断恢复、重复有副作用任务。
- 真实恢复：执行成功但健康未恢复、观察窗口异常、结果未知。

### 必须记录

`Top-1/Top-3`（仅可判定案例）、未知问题适当弃答率、证据引用有效率、错误引用/旧知识误用、任务成功率、重复动作数、安全拒绝数、P50/P95 延迟、工具调用次数、Token/费用。记录模型版本、Prompt 版本、RAG 索引版本、代码提交、数据集与随机性设置。

**强门禁：** 所有指定越权/未审批操作测试中成功执行次数为 0；重复 side-effect 次数为 0；恢复验证失败时误报成功次数为 0。其他正确率目标以预设阈值作质量目标，不可虚构为实测。原 v1.0 的 Top-1≥75%、Top-3≥90% 可作为尝试目标，需按测试数据规模审慎解释统计波动。

## 14. 开发交付顺序（固定主线，先深后广）

| 关卡 | 构建内容 | 可演示结果 | 必须学懂的技术 |
|---|---|---|---|
| D0 规格冻结 | PRD、系统边界、API/数据契约、风险矩阵、故障注入字典 | 可讲清为何选这些技术 | 技术选型与架构取舍 |
| D1 基础骨架 | 全新 repo、CI、Docker Compose、FastAPI、PostgreSQL、资产和事件中心、基础 UI | 创建/查询故障、资产鉴权 | API、ORM、事务、迁移、Docker |
| D2 诊断纵切 | DeepAgents 主 Agent、工具网关、Evidence、Agent trace、诊断报告 | 3 种故障端到端 | Tool calling、状态、Agent 调试 |
| D3 扩展接入 | Capability Manifest、指标/日志接入、Python+Java 样例服务、未知故障退出 | 换语言仍使用同一 Agent 链路 | 适配器、OpenTelemetry/Prometheus、Schema |
| D4 经验复用 | 事故/Runbook 审核发布、全文 + pgvector 检索、RAG 对照 | 历史故障可召回但不盲信 | RAG、Embedding、查询、数据治理 |
| D5 审批和可靠性 | Proposal、Policy、审批、Action、验证、RabbitMQ/Outbox、持久化恢复 | staging 受控恢复、Crash/Replay 测试 | 分布式一致性、幂等、checkpoint |
| D6 产品/质量 | 全 UI、SSE、审计、≥30 场景、成本/效果与安全评估 | 从报障到报告/处置全 UI 复现 | Vue、E2E、Agent eval、压测 |
| D7 作品交付 | 复现脚本、README、架构/API、安全/评估说明、演示视频与面试素材 | 面试官能独立运行核心演示 | 系统表达、源码与失败复盘 |

**不得跳过的基本原则：** D2 之前只允许 AI 调用只读工具；所有新技术通过最小可验证用例纳入；每个关卡先通过故障注入和验收测试后再进入下一阶段。后续规划的 P1 是新版本迭代，不得修改核心契约来迁就某一个插件。

## 15. 学习与面试训练契约（针对求职目标）

目标岗位画像：**Python 后端 / AI 应用开发 / Agent 工程师**，同时保留可迁移到 Java 企业开发岗位的事务、权限、异步、可观测性知识。项目不是训练模型或自称 SRE 专家，而是展示能把智能体可靠接入真实后端业务。

### 每个关卡必须交付四样东西

1. **跑得通：** 真实接口、运行输出和失败例子；不是只有 AI 生成代码。
2. **看得懂：** 能沿一次 HTTP 请求追踪到 DB/队列/Agent/Tool/报告，指出核心函数与数据表。
3. **解释得清：** 为什么选它、换技术有什么代价、哪里会失败、如何恢复、成本是什么。
4. **经得起改动：** 在不让 AI 重新生成整个项目的情况下，能定位并修复指定的故障/接口契约问题。

### 重点面试问题预埋

- LangChain、LangGraph、DeepAgents 分别负责哪一层？普通 `create_agent` 与 DeepAgents 有什么必要性差异？
- 为什么 Agent 不能直接调用有副作用的工具？`interrupt_on` 和独立审批服务的区别？
- 怎么阻止日志、网页、Runbook 中的 Prompt Injection 获取生产执行权限？
- RabbitMQ 至少一次交付怎么和数据库状态/幂等执行配合？Worker 崩溃在执行前/后怎么判断？
- 为什么使用 PostgreSQL + pgvector 而不是一开始就部署 Milvus？全文与向量混合检索为什么可能更好？
- 一个 Java 服务没有被正确注入 OTel，系统还能看见什么，不能看见什么？
- 新 Kafka/Go/C++ 系统如何接入？什么时候只需配置，什么时候必须开发 Connector？
- RAG 检到历史故障，但当前证据反驳它，Agent 应该怎么办？怎样测出 RAG 是否真的有帮助？
- 怎样定义“修复成功”？为什么执行指令返回成功不等于恢复？
- 怎么复现故障、形成 Golden Set、保证测试集没有被 Prompt 定向记忆？

### 与 AI 编程工具的协作方式

使用 Trae 生成和修改明确边界的代码；每次任务先给出需求/范围/禁区/接口/测试/验收结果，然后审查关键文件和实际运行输出。禁止让 AI 一次性虚构“已经集成、测试通过、性能提升”。学习时采用：**真实故障 → 原理与设计取舍 → 有边界的实现 → 实测 → 源码复盘与面试追问**。不要求逐行手敲代码，但必须理解核心实现。

## 16. 后续迭代与变更管理

- 固定 P0 核心抽象：Incident、Run、Evidence、Knowledge、Capability、Proposal、Action、Verification；API 按 `/api/v1` 版本化；数据库迁移必须向后兼容或有迁移方案。
- Connector 按 Manifest 版本演进；模型、Embedding、Prompt、Runbook 各有版本，变更后触发评估回归。
- 新功能先写简短 RFC：它解决什么问题、当前方案是否已具备、业务收益、额外依赖、接口和数据影响、权限风险、评估办法；决定 P1/P2 之后才开发。
- 可能的后续顺序：Prometheus/OTel 接入增强 → MCP 标准连接器 → Go/C++/Kubernetes 诊断覆盖 → RAG 召回与源治理优化 → 在有数据证明时引入专业 subagent → 企业身份与多团队范围。
- 每次稳定版本附变更日志、迁移说明、回归结果，避免后续迭代破坏已实现能力。

## 17. 简历与作品集交付要求

**仅在实现并实测后，才能写入简历。** 准备以下证据：

- 项目架构图与一次完整故障时序图；三张核心截图（事故、诊断证据、审批/验证）。
- Docker Compose 安装与测试环境、至少两个技术栈样例服务、可复现故障注入脚本。
- OpenAPI 文档、关键接口契约；技术选型/安全边界说明；CI 测试记录。
- >=30 案例评估报告、RAG on/off 对比、安全/中断恢复证据、真实成本与延迟。
- 一份 `interview_notes`：20~30 个追问及对应代码/故障例子，并记录自己真正独立排查过的问题。

**候选简历项目名：** **OpsPilot Next｜可扩展企业智能运维 Agent 平台**。

**候选项目简介（只有完成后才可使用）：** “基于 DeepAgents/LangGraph 与 FastAPI 构建智能运维平台，通过可扩展只读连接器统一接入异构服务、运用事故知识 RAG 辅助根因定位，设计证据追溯、受控审批/恢复和可持久化异步任务链路，结合故障注入评测诊断效果与系统可靠性。”

## 18. 技术事实核对与风险说明

- DeepAgents 官方支持工具、上下文管理、subagent 和 Human-in-the-loop，但这些不自动等价于企业级授权与可控副作用操作。
- LangGraph 的 checkpointer 管理线程状态，store 管理跨线程长时知识；事故业务状态必须由业务模型管理。生产恢复应使用持久化 checkpointer，而非 in-memory saver。
- OpenTelemetry Java、Go、C++ 等语言有官方支持，但自动插桩能力及语义覆盖因语言/框架而异，不能宣称对任意二进制程序零侵入获得全部业务追踪。
- PostgreSQL + pgvector 支持向量相似性检索，可与 PostgreSQL 全文检索组合；选型依据是第一版数据规模、架构简单性与实验结果，而不是预言其在所有负载下最好。

**官方参考：**
- DeepAgents: https://docs.langchain.com/oss/python/deepagents/overview
- DeepAgents HITL: https://docs.langchain.com/oss/python/deepagents/human-in-the-loop
- DeepAgents Permissions: https://docs.langchain.com/oss/python/deepagents/permissions
- LangGraph Persistence: https://docs.langchain.com/oss/python/langgraph/persistence
- OpenTelemetry languages: https://opentelemetry.io/docs/languages/
- pgvector: https://github.com/pgvector/pgvector

---

**版本判定：** v1.1 是统一需求评审后的推荐基线，**不是源代码交付**。是否允许某技术进入 P0 由本文件的范围、依赖、安全门禁和实际可验证的业务收益共同决定。后续开发从 D0 开始，不在设计阶段顺手生成脚手架或部署应用。
