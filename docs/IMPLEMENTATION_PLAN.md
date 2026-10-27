# OpsPilot Next｜开发与验收规范（D0 冻结稿）

**版本**：v1.1（D0；含架构评审定向修订与 D1 默认决策确认）
**日期**：2026-10-27
**需求基线**：`docs/PRD.md` v1.1 §14（交付顺序）；配套：`ARCHITECTURE.md` / `CONTRACTS.md` / `DATA_MODEL.md`
**原则**：固定主线，先深后广；每关先通过故障注入与验收测试再进下一关；D2 之前 AI 只允许调用只读工具（PRD §14）。

---

## 0. 全局规范

### 0.1 每个阶段的完成定义（DoD）

1. 代码合并 + CI 绿（lint、类型检查、测试）；
2. 本阶段自动化测试全部通过，含至少一个"故障注入/失败路径"用例；
3. `docker compose up` 后可按验收脚本演示（无手工 CLI 魔改）；
4. 更新 `interview_notes`：本关新增追问 + 指向的具体源码位置；
5. 不满足 1–4 不得进入下一关。

### 0.2 全局禁区（每一关都适用）

- 不修改 `deepagents-upstream`（只读样例）；不迁移旧 OpsPilot 代码；
- 不让模型输出成为授权：任何有副作用动作必须走 Proposal→Approval→Action Service；
- 生产环境首版只读，无审批通道（契约层硬拒绝）；
- 不为堆技术加组件；新依赖必须在阶段"关键技术"清单中列明；
- 密钥不入代码/日志/Prompt；所有外部调用有超时与上限；
- AI 生成代码必须经人工审查关键文件与实际运行输出（PRD §15）。

---

## 1. D1｜基础骨架（详细规划）

### 1.1 业务问题与开发目标

**问题**：报障、资产登记没有统一入口与权限边界，后续所有自动化都无从谈起。企业运维的第一步是"谁知道哪些服务、出了事谁登记、谁能看什么"。

**目标**：全新工程骨架落地——CI、Docker Compose、FastAPI、PostgreSQL、鉴权与 RBAC、资产与事件中心、可查审计、基础 UI 骨架。对应 PRD D1 可演示结果：**创建/查询故障、资产鉴权**。

### 1.2 实现范围

| 项 | 内容 |
|---|---|
| 工程结构 | 单仓三入口约定（本关只实现 `api`）；`app/api`、`app/domains/{auth,assets,incidents,alerts,audit}`、`app/core`（config/db/security）、`alembic/`、`tests/`、`web/`（最小 Vue3 页面） |
| 数据库 | D1 全部建表（DATA_MODEL §2 D1 清单）；Alembic 初始迁移；`outbox_events` 建表不启用 |
| 认证 | `POST /auth/login`（**JWT access token 30 分钟，无 Refresh Token，D0 评审确认**）、`GET /auth/me`、`POST /auth/change-password`；bootstrap：首次启动用环境变量创建 Admin（`must_change_password=true`，首登强制改密）；预置系统账号 `usr_system_alerts`（Webhook 事件创建主体，禁止登录） |
| RBAC | 角色表 + `asset_access_policies`（user/role 互斥，**默认拒绝**，Admin 不受范围限制）；FastAPI 依赖注入式鉴权；范围判定为纯函数可单测 |
| 资产 | `GET/POST /assets`、`PATCH /assets/{id}`；资产依赖边维护（edges 的增删走资产接口） |
| 事件 | `POST/GET /incidents`、`GET /incidents/{id}`、`POST /incidents/{id}/acknowledge`；状态机服务端强制 |
| 告警 | `POST /webhooks/alerts/{connector_id}` 骨架：HMAC-SHA256 签名 + timestamp 防重放；**以 delivery_id 去重为幂等主手段**（不依赖第三方 Idempotency-Key）；事务内 告警落库→去重→建 incident（创建主体 = 系统账号）→回写关联 |
| 基础设施 | 统一响应包裹 + 错误码（CONTRACTS §1）、cursor 分页、`Idempotency-Key`（按 user+endpoint+key 隔离）、`audit_events` 记录、`/health/live` `/health/ready`；配置化默认值落定（幂等保留 24h、limit≤200、Webhook timestamp 容差 ±5min） |
| UI 骨架 | 登录页、资产列表、事件列表/详情/创建（仅够人肉演示闭环，样式从简） |
| CI | GitHub Actions（或同等）：ruff + mypy（宽松起步）+ pytest + `alembic upgrade head` 空库演练 |
| Compose | `postgres` + `api` + `web`；演示种子数据脚本（seed：样例资产与用户） |

### 1.3 禁止越界

不实现任何连接器/Agent/Worker/RabbitMQ/Redis/SSE；不建 D2 及以后的表（见 DATA_MODEL 分期）；不做完整前端与图表；不引入 ORM 之外的框架级魔法（无 CQRS/事件溯源）。

### 1.4 关键技术与依赖

- Python 3.12、FastAPI、Pydantic v2、SQLAlchemy 2（声明式映射 + async session）、Alembic、PostgreSQL 16；
- JWT（`pyjwt`）：短期 access token；密码哈希（argon2/bcrypt）；
- `pytest` + `httpx` AsyncClient + 测试用 PostgreSQL（testcontainers 或 compose 专用测试库）；
- 幂等键：`api_idempotency_keys` 唯一约束 + 事务内抢占；
- cursor 分页编码：`base64(json({created_at, id}))` + 签名。

### 1.5 必须完成的自动化测试

| 类别 | 用例 |
|---|---|
| 单元 | Incident 状态机合法/非法转换；权限范围判定（角色×资产×环境）；cursor 编解码与防篡改；幂等键请求体哈希 |
| 集成（API） | 登录→建资产→建事件→确认→查询全链路；幂等重放返回相同响应；同 key 异 body → 409 |
| 安全 | Operator 访问 Admin 接口 → 403；越权环境资产 → 404（不暴露存在性）；**无任何策略匹配的资产 → 默认拒绝**；无 token → 401；首登未改密调用业务接口 → 403 `PASSWORD_CHANGE_REQUIRED`；密码错误提示不泄漏用户存在性 |
| Webhook | 正确签名通过；错误签名 401；过期 timestamp 拒绝；同 `delivery_id` 重放只产生一次 incident 关联；自动创建的 incident `created_by` = 系统账号且审计 actor_type=system；事务回滚不留孤儿告警 |
| 迁移 | 空库 `alembic upgrade head` 成功且可 `downgrade base` |
| 审计 | 关键动作（登录、资产变更、事件创建/确认）产生 audit_events |

### 1.6 可运行、可演示的验收标准

1. `docker compose up -d` 一键启动；`make seed` 注入样例资产/用户；
2. 演示脚本：登录 → 登记两个环境各一个服务资产 → 手工创建事件 → acknowledge → 列表/详情可见；
3. 越权演示：仅授权 sandbox 的 Operator 查询 production 资产 → 404；调用管理接口 → 403；
4. 幂等演示：同 Idempotency-Key 重复提交事件 → 只有一单；
5. CI 全绿，测试报告留档。

### 1.7 必须理解的源码、原理与面试问题

**源码要求**：能指出 JWT 校验依赖注入点、事务边界（一个请求一个 session）、幂等键抢占 SQL、状态机实现函数。

**原理**：依赖注入与请求生命周期；ORM UnitOfWork 与事务传播；为什么 cursor 优于 offset（深翻页、数据漂移）；幂等键并发竞争靠唯一约束兜底的正确性论证。

**面试题（预埋）**：
1. 为什么用 cursor 而不是 offset 分页？cursor 需要什么排序保证？
2. 两个并发请求携带同一 Idempotency-Key，如何保证只落一条？（唯一约束 + `ON CONFLICT` + 等待首次完成）
3. JWT 无状态带来的注销难题有哪些？本系统为何首版只做短期 access token？
4. 状态机为什么放服务端？前端拦截为什么不算权限实现？
5. `alembic` 迁移和模型不同步时会发生什么？如何避免？
6. Webhook 的 HMAC 校验为什么还要时间戳？重放窗口怎么定？

---

## 2. D2｜诊断纵切（详细规划）

### 2.1 业务问题与开发目标

**问题**：事件登记后仍靠人逐个查服务。需要第一个可验证的自动化：**AI 在只读边界内完成规划→取证→假设→报告**，并留下可审计的证据链。

**目标**：DeepAgents 主 Agent + Tool Gateway + 统一 Evidence + Agent trace + 诊断报告；**3 种故障端到端**（PRD D2 验收）。选定场景：S1 Redis 连接不可达、S2 Docker 容器异常重启、S4 网关 502。

### 2.2 实现范围

| 项 | 内容 |
|---|---|
| Worker | `ops-worker` 入口；DB 任务认领（`investigation_runs` 行级 `FOR UPDATE SKIP LOCKED` + 租约 + 心跳 + `lease_owner`/`fence_token` fencing，状态回写校验 token），run 事件与状态回写 |
| LangGraph 图 | `Validate → LoadAssets → RetrieveKnowledge → Plan → CollectEvidence → Analyze → VerifyClaims → {CollectMore | Report} → Finalize`（无 Propose/Execute 节点，D5 接入）；节点切换写 run_events |
| Checkpoint | PostgresSaver，`langgraph` 独立 schema；`thread_id = run_id` |
| DeepAgents | 配置化创建主 Agent（planning + 工具 + 上下文管理）；单主 Agent，subagent 不启用；不修改 upstream |
| Tool Gateway | 工具工厂：manifest 校验（先内置最小 manifest）→ 参数 Pydantic 校验 → 资产/环境白名单（授权快照）→ 超时/限流/大小上限 → 调用连接器 → Evidence 归一化落库 → 返回 `{evidence_id, status, summary_redacted, truncated}`；SSRF 防护（仅已登记 endpoint）、脱敏规则 |
| 连接器（只读） | `http.health_check`（get_service_health）、`docker.inspect`（get_container_status：状态/事件/OOM/退出码）与 `docker.logs`（read_container_logs）——**均经 docker-ro-proxy 只读代理（方法+路径白名单），Worker 不挂载 docker.sock**、`redis.health`（check_redis_health） |
| Run API | `POST /incidents/{id}/investigations`（202，预算+快照+幂等）、`GET /runs/{id}`、`GET /runs/{id}/events`（**JSON 分页为默认响应**；SSE 仅 D6 起对显式 `Accept: text/event-stream` 开放）、`POST /runs/{id}/cancel`（合作式：DB 取消标志 + 图中断）、`GET /runs/{id}/evidence`、`GET /runs/{id}/report`（返回 `diagnostic_reports` 快照） |
| 预算硬限制 | tool_calls / iterations / tokens / deadline 由 Worker 服务端强制；超限 → `budget_exhausted` 终态 |
| 停止条件 | 连续 N 轮无新证据 → `no_new_evidence`；证据不足且无法继续 → `unknown_exit`（NEEDS_HUMAN） |
| 模型配置 | 配置文件切换 Provider；版本与 prompt 版本写入 `model_config`；成本计量字段开始累计 |
| 演示环境 | compose 内：样例 FastAPI 应用（含可注入故障开关）、Redis、docker 事件注入脚本；3 场景各有注入与恢复脚本 |

### 2.3 禁止越界

无任何写工具/写凭据；无 MySQL/Prometheus/Trace 连接器（D3）；无 RAG（RetrieveKnowledge 节点返回空集+说明，`retrieve_incidents`/`search_runbooks` 返回空）；无 RabbitMQ/Outbox 启用；无 SSE（JSON 分页顶上）；无审批/Proposal；不做完整诊断 UI（可用 API 调试页/最小页面看 events）；不修改 deepagents-upstream。

### 2.4 关键技术与依赖

- `deepagents`、`langgraph`、`langchain`（版本锁定，`requirements.lock`/`uv.lock`）；PostgresSaver；
- 工具调用（tool calling）协议与 `interrupt_on` 概念认知（本关不用写工具，但需能解释）；
- LangGraph state/reducer/conditional edges；checkpoint 序列化；合作式取消模式；
- Agent 调试：本地 trace（run_events 即 trace 载体，可选 LangSmith 对照）；
- Docker API 只读访问经 **docker-ro-proxy**（独立代理容器，方法+路径白名单，ADR-11）；Worker 内仅用普通 HTTP 客户端访问代理，不挂载 docker.sock。

### 2.5 必须完成的自动化测试

| 类别 | 用例 |
|---|---|
| Gateway 单测 | 参数越权（未授权资产）拒绝；目标不在白名单 → 拒绝；**Docker 代理白名单：POST/DELETE 及非白名单路径一律被拒（拒绝未授权操作测试）**；连接器超时 → Evidence `timeout` 而非伪空；超大输出截断 + `truncated=true`；脱敏规则命中（密码/令牌模式） |
| 预算 | fake 模型无限请求工具 → 服务端在 max_tool_calls 强制终止；deadline 到期终止；超限后 run 终态 + stop_reason 正确 |
| 图推进（fake 模型，确定性脚本化工具调用） | 3 场景各一：证据数>0、hypotheses 引用 Evidence ID、报告含关键结论引用；S7 雏形：工具全部失败 → `unknown_exit` |
| 取消 | running 中 cancel → 合作停止、终态 `canceled`、无孤儿工具调用 |
| API | run 生命周期状态转换合法性；events 分页 seq 连续；**无 Accept 头/`*/*` 时默认返回 JSON（不返回 SSE）** |
| 报告快照 | run 终态后 `diagnostic_reports` 存在最新版本；unknown 场景报告 `is_unknown=true` 且结论区无 supported 根因；重新生成时 version_no 递增 |
| Fencing | 模拟旧 Worker（旧 fence_token）回写 run 状态 → 被拒绝且业务状态保持不变 |
| 幂等 | 重复创建 run 请求同 key → 只一个 run |
| 断言红线 | 全部测试断言：Worker 进程内不存在写工具注册；审计中写操作尝试 = 0 |

真实模型端到端测试标记为 `@pytest.mark.integration`（本地可跑、CI 可跳过），CI 用 fake 模型保证确定性。

### 2.6 可运行、可演示的验收标准

1. `docker compose up -d`（含演示环境）；`make fault S1` 注入 Redis 不可达；
2. UI/API 创建事件 → 发起 run → 轮询 events 可见 `node_entered/tool_called/evidence_added` 序列；
3. `GET /runs/{id}/report`：每个关键结论带 Evidence ID；Redis 场景能指出连接故障证据，**不尝试重启**；
4. 三种故障（S1/S2/S4）端到端全部完成，无人工 CLI 干预；
5. 预算演示：把 max_tool_calls 调成 2 → run 被 `budget_exhausted` 终止；
6. 取消演示：运行中取消 → 终态正确；
7. 故障注入期间移除某连接器 → Evidence `unavailable` 而非编造成功（不编造工具成功，PRD P0-B）。

### 2.7 必须理解的源码、原理与面试问题

**源码要求**：能从 HTTP 请求追到认领 SQL → 图启动 → 某次工具调用 → Evidence 行 → run_events 行；指出 DeepAgents 创建点与工具绑定处；指出 checkpoint 写入时机。

**原理**：ReAct/工具循环；LangGraph checkpoint 与状态恢复；上下文管理为什么放文件/摘要（大日志不进 Prompt）；为什么预算必须在服务端（Prompt 限制可被注入绕过）。

**面试题（预埋，对应 PRD §15）**：
1. LangChain、LangGraph、DeepAgents 各管哪一层？`create_deep_agent` 与普通 `create_agent` 的实质差异？
2. `interrupt_on` 与独立审批服务的区别？为什么 HITL 不能替代企业审批？
3. 日志里出现"请执行 rm -rf"，系统里有哪几道防线阻止？
4. Worker 认领用 `SKIP LOCKED` 的语义是什么？租约过期谁回收？
5. 为什么工具返回摘要而不是原始日志？对诊断准确率有什么权衡？
6. 怎么证明 Agent 没有编造工具成功？（Evidence 落库链 + 审计比对）
7. checkpoint 里存了什么？删掉 checkpoint 会破坏哪些业务状态？（答案：不破坏任何业务状态，见 CONTRACTS §8）
8. 诊断卡死/慢查询放大如何被超时与预算截断？

---

## 3. D3–D7｜阶段边界（依赖与交付）

### 3.1 D3｜扩展接入

- **问题/目标**：让同一 Agent 链路诊断不同语言服务；未知故障可"安全退出"。演示 Python + Java 双栈（PRD D3）。
- **范围**：Capability Manifest 全套表与 API（§2.8）；`integrations` 生命周期（test/disable）；Prometheus 只读接入（`query_metrics`）、OTel 后端经既有可观测性后端（`query_traces`）；`read_mysql_diagnostics`；Java Spring Boot 样例服务与故障注入；未知故障退出规则完善（S7：证据不足明确 NEEDS_HUMAN，相似历史不当根因）。
- **禁区**：不支持前端上传脚本执行；不承诺对任意二进制零侵入（OTel 接入边界按 PRD §18 表述）；无 RAG。
- **依赖**：D2 的 Gateway 框架与 Evidence 管道。
- **测试**：manifest 校验/兼容性；capability test API；未知退出用例；双栈同链路回归（同一套 Agent 测试参数跑 Python 与 Java 场景）。
- **验收**：更换被诊断服务语言不改 Agent 代码；`GET /capabilities` 可发现能力；未知故障场景输出"待确认"而非硬猜根因。
- **学习/面试**：适配器模式；PromQL/OTel 语义；"Java 未插桩时能看见什么/看不见什么"（PRD §15）；什么时候只需配置、什么时候必须写 Connector。

### 3.2 D4｜经验复用（RAG）

- **问题/目标**：重复故障复用已审查经验；检索可测、可控、可关闭。
- **范围**：知识生命周期（draft→reviewed→published→superseded/retired）+ 发布 API；runbook 管理（草稿不可执行）；pgvector + 混合检索（先过滤后排序）；`retrieve_incidents` / `search_runbooks` 真实接入；`retrieval_events` 记录；RAG 开关（off/text-only/hybrid）进 run `model_config`。
- **禁区**：不做通用 PDF/OCR 批处理、知识图谱、自动改写发布、独立向量库。
- **依赖**：D2 的报告/证据（历史事故来源）；D3 的资产/环境过滤。
- **测试**：权限/时效过滤（**按 run 授权快照中的具体资产 ID 过滤，越权召回=0；仅按 environment/标签放行的实现视为不通过**）；发布流程状态机；embedding 版本切换兼容；RAG 消融固定小数据集（旧经验误导场景：历史相似但当前证据反驳 → 不判 supported）。
- **验收**：同一故障 RAG on/off 对照报告（引用有效率、误导比例、工具次数、token）。
- **学习/面试**：pgvector 原理与索引；混合检索为何可能更好；"检到历史故障但当前证据反驳怎么办"；为什么不用 Milvus（PRD §15）。

### 3.3 D5｜审批和可靠性

- **问题/目标**：AI 建议 → 受控变更闭环；长任务在崩溃后可恢复且零重复副作用。
- **范围**：Proposal/Policy/Approval（四眼+有效期+快照复核）；dry-run；Action Service 独立进程 + allowlist + 幂等账本；恢复验证（真实健康检查+观察窗口）；RabbitMQ + Outbox 启用（替换 DB 认领为主路径）；checkpoint 恢复演练；incident `MITIGATING/MONITORING` 接入。
- **禁区**：production 任何写；R2/R3 动作；审批可绕过路径；unknown 盲目重试。
- **依赖**：D1 事务/幂等基础；D2 Worker；D4 runbook。
- **测试**：幂等（重复消息副作用=0）；kill worker 在执行前/后两种时点（依据账本与 external_ref 收敛）；审批过期/参数篡改被拒；四眼违规被拒；执行成功但健康未恢复 → 不得 RESOLVED（S9）；unknown → NEEDS_HUMAN。
- **验收**：staging 全链路受控恢复演示；Crash/Replay 测试报告（PRD D5）。
- **学习/面试**：Outbox 模式；at-least-once vs exactly-once；"执行前/后崩溃怎么判断"（PRD §15）；checkpoint 与业务状态恢复的分工。

### 3.4 D6｜产品与质量

- **问题/目标**：可写进简历的完整版本：全 UI、SSE、评估与审计齐备。
- **范围**：5 大导航域 UI（PRD §12）；SSE（run_events 重放）；审计完善；≥30 确定性故障/安全场景（开发集+保留集）；成本/延迟/效果指标产出（PRD §13 必须记录项）；安全用例（注入/越权/未审批/过期/篡改）。
- **禁区**：不加新后端组件；不为分数对保留集调优。
- **依赖**：D2–D5 全部。
- **测试**：E2E（UI 关键路径）；eval 框架回归；强门禁三项（越权执行=0、重复副作用=0、恢复误报=0）自动化断言。
- **验收**：普通操作员在 UI 完成核心闭环；评估报告含失败案例与原始基线。
- **学习/面试**：Agent eval 方法论；Golden Set 与记忆污染；SSE 重放设计。

### 3.5 D7｜作品交付

- **问题/目标**：面试官可独立运行；每个功能可讲清取舍。
- **范围**：复现脚本、README、架构/API 文档终稿、安全与评估说明、演示视频、`interview_notes`（20–30 追问+代码位置）、失败复盘记录。
- **禁区**：不加功能；不修改契约。
- **验收**：全新机器按 README 30 分钟内跑通核心演示（PRD D7）。

---

## 4. 待确认问题清单（PRD 未定/需决策，不擅自改需求）

| # | 问题 | 结论 | 状态 |
|---|---|---|---|
| 1 | 认证令牌形态与有效期 | JWT access token **30 分钟**，无 Refresh Token（首版过期重登） | **已确认（D0 评审）** |
| 2 | D2–D4 任务交付载体 | DB 行级认领过渡（ADR-4），D5 切 RabbitMQ+Outbox | **已确认（D0 评审）** |
| 3 | Proposal/ActionExecution/Verification 状态机 | 按 CONTRACTS §2.11/2.13/2.14 冻结 | **已确认（D0 评审）** |
| 4 | 环境建模 | 受控枚举（ADR-6），多实例扩展时再加表 | **已确认（D0 评审）** |
| 5 | Webhook 签名算法与重放窗口 | HMAC-SHA256 + timestamp ±5min；幂等以 delivery_id 为主手段 | **已确认（D0 评审）** |
| 6 | LLM Provider 与 Embedding 方案 | 一个支持工具调用的模型 + 可配置 Embedding；版本锁定 | D2/D4 开工前锁定（部署期决策） |
| 7 | MySQL 是否进入 D2 演示 | 否（P0-A 只需 HTTP/Docker/Redis）；`read_mysql_diagnostics` 归 D3+ | **已确认（D0 评审）** |
| 8 | 初始 Admin 引导方式 | 环境变量 bootstrap + `must_change_password` 首登强制改密 + 预置系统账号 | **已确认（D0 评审）** |
| 9 | 诊断报告存储形式 | **`diagnostic_reports` 快照表（D2）**，`GET /report` 返回最新版本（ADR-13） | **已确认（v1.1 修订）** |
| 10 | 配置默认值与适用阶段 | 统一配置管理：幂等键保留 **24h**（D1 起）；列表/事件 limit ≤ **200**（D1 起）；Webhook timestamp 容差 **±5min**（D1 起）；checkpoint 保留 **30 天**（D2 起）；run 预算默认值（D2 定初值）；审批有效期建议 **24h**（D5 定初值） | **已确认（D0 评审）** |
| 11 | Docker 连接器访问方式 | **Worker 不挂载 docker.sock；经独立 docker-ro-proxy 只读代理（方法+路径白名单）**（ADR-11） | **已确认（v1.1 修订）** |

## 5. D1 开工条件判定（D0 最终评审）

**具备，可无条件开工。** D1 范围内的全部决策（#1 认证、#4 环境建模、#8 Admin 引导、#10 配置默认值）已在 D0 评审确认并写入契约与数据模型；D2 前置项（#2 交付载体、#7 MySQL、#9 报告快照、#11 Docker 只读代理）亦已确认，仅剩 #6 模型锁定属部署期决策。契约（CONTRACTS v1.1）、数据（DATA_MODEL v1.1）、架构（ARCHITECTURE v1.1）与 PRD v1.1 已核对一致，D1–D7 顺序与功能范围未变。
