# 面试追问准备（25 题）

> 原则：每个回答都能回到**本仓库的具体文件与函数**，不讲教科书定义。
> 「坑」那一栏是真被问倒过、或者自己踩过的地方——写出来比藏起来更有用。
> 路径均相对仓库根目录。

---

## A. Agent 执行核心

### 1. DeepAgents、LangGraph、LangChain 各自在项目里干什么？为什么不全用 LangChain？

- **LangChain** 只做最底层的东西：模型客户端（`ChatOpenAI` 兼容层）、消息类型、工具 schema。
- **LangGraph** 提供图执行与状态持久化（`PostgresSaver`，独立 `langgraph` schema，`thread_id = run_id`）。
- **DeepAgents** 提供「主 Agent」这套上层范式：规划→调工具→观察→再规划的内置循环，以及
  `execute`、通用 subagent 这类内置能力——这些我用 `HarnessProfile` 裁掉了，因为我的工具
  必须全部经 Tool Gateway，不能让它自己去跑 shell。
- 不全用 LangChain 的原因：手写 ReAct 图等于重复实现 DeepAgents 已有的循环，且预算/取消/
  停止条件都要自己塞进图里，错一次就变成死循环或烧钱。

**源码**：`app/domains/agent/runtime.py`（`_register_harness_profile_for`、`build_agent`）、
`docs/ADR-D2-EXECUTION-CORE.md`。

**坑**：DeepAgents 版本升级会改 harness 的注册方式；我把注册收敛到一个函数里，
就是为了升级时只改一处。

---

### 2. AgentRuntime 怎么驱动图执行？预算怎么做到「Prompt 绕不过去」？

- 用 `astream(stream_mode="updates")` **逐 super-step 步进**，而不是 `ainvoke` 一把梭。
- 每个 step 之间检查：取消标记（DB）、deadline、迭代数、tool_calls、tokens。任一项越界就
  中断图并写 `stop_reason`，run 转 `needs_human` 或 `failed`。
- 预算值来自 `run.budgets`（创建 run 时落库），不是 prompt 里的软约束——模型改不了数据库。

**源码**：`app/domains/agent/runtime.py::execute_run`、`_budget_from_run`、`_accumulate_tokens`。

**坑**：token 计数要从 `usage_metadata` 累加，不同 provider 字段名不一致；`fake` provider 返回 0，
所以评估里真实模型和 fake 的 token 差异是真实差异，不是统计口径差异。

---

### 3. Tool Gateway 是独立服务吗？为什么不做成独立服务？

- 不是。它是 **Worker 进程内的一个库**（`ToolGateway`），所有暴露给 Agent 的工具句柄只能由它生成。
- 好处：Agent 代码物理上拿不到连接器；校验、限流、脱敏、审计都在同一事务上下文里，
  不需要跨网络传授权快照；故障域也不用多一跳。
- 什么时候该升级成独立服务：需要按工具维度独立扩缩容、或需要把连接器凭据与 Worker 彻底
  分离到不同信任域时。这两条现在都不成立。

**源码**：`app/domains/tools/gateway.py`、`manifests.py`、`registry.py`。

**坑**：Gateway 里做「容器名解析」时曾经把资产登记和实例前缀叠加成双前缀，
导致「容器不存在」被当成「探测成功」。现在统一走 `resolve_container_name`（资产登记优先）。

---

### 4. 工具调用的权限是怎么隔离的？只说三条就够。

1. **能力白名单**：只有 `manifest` 里登记过、且被连接的 capability 才能被调用；名字不在表里直接拒。
2. **参数 schema 校验**：`required / pattern / enum / additionalProperties`，容器名必须匹配
   `^opspilot-demo-[a-z0-9][a-z0-9-]*$`。
3. **资产范围**：调用前校验 `asset_id` 在本次 run 的授权快照内；越权即越权观测。

再加一层：**只读**。连接器只实现读接口，Worker 不持有写凭据、不挂载 `docker.sock`。

**源码**：`app/domains/tools/gateway.py`、`app/domains/approvals/allowlist.py`、`roproxy/`。

---

### 5. LangGraph 的 checkpointer 和业务数据库，谁是权威？为什么？

**业务库（PostgreSQL）**。checkpoint 只服务「图执行到哪一步、能恢复」，它回答不了
「这个事件现在是什么状态」。

具体表现：前端的状态、SSE 的事件、审计，全部读 `investigation_runs` / `run_events`；
`GET /runs/{id}` 是状态权威，SSE 只是加速通道。

**源码**：`app/domains/runs/service.py`、`app/domains/runs/sse.py`、
`docs/ARCHITECTURE.md` §5.3。

**坑**：如果让 checkpoint 当权威，一旦图执行完但进程在写业务表前崩了，
checkpoint 说"完成"、业务表说"running"，两边永远对不上。

---

## B. 证据与根因

### 6. 什么是 Evidence？为什么假设只能引用 Evidence？

- Evidence 是一次工具调用的**结构化落库**：`source_type`、`collector`、`status`、
  `observed_at`、`summary_redacted`、`data_ref`（脱敏后的原始载荷 + 注入标记）。
- 报告里的 hypothesis 通过 `supporting_evidence_ids` 挂证据；评估器的 judge-v2 就是校验
  「第一名假设是否引用了带对应 `error_code` 的真实证据」。
- 为什么必须这样：没有证据锚定，「根因」就是模型的文风；有了锚定，错也错得可追溯。

**源码**：`app/domains/tools/gateway.py`（`_persist_*_evidence`）、
`app/domains/agent/report.py`、`app/domains/evaluations/judge.py`。

**坑**：`status=unavailable` 也必须带 `error_code`。早期把采集失败写成「ok + 空结果」，
等于把「看不见」伪装成「没问题」。

---

### 7. 深层根因不确定时怎么办？为什么模型不会整单弃答？

分层归因：直接原因有证据 → `supported`；更深的层 → `insufficient_data`；
被证据排除的 → `refuted`。只有**完全没有证据**时才整单转未知（如工具预算为 0）。

提示词里写死了这条口径，D6 之前模型经常「深层不确定 → 整份报告 unknown」，
评估把它分类为 `premature_abstention`。

**源码**：`app/domains/agent/prompts.py`、`report_schema.py`、
`docs/EVALUATION_REPORT.md` §2.4（10 例人工复核，可以看到这个行为稳定复现）。

---

### 8. pgvector 的 Hybrid RAG 具体怎么融合？为什么用 RRF 而不是加权分数？

- 权限/环境/时效 **前置过滤**（先在 SQL 层过滤，再召回），然后文本路 + 向量路各取 Top-K，
  用 RRF 融合：`score = Σ 1/(k + rank)`，`k=60`。
- 用 RRF 的原因：两路分数量纲不同（ts_rank 与余弦距离），加权要调参且不稳定；
  RRF 只用排名，跨查询不需要重调。
- 每条命中都带 `ranks.text / ranks.vector`，所以你能看见「这条是向量路捞回来的」。

**源码**：`app/domains/rag/retrieval.py::_rrf_fuse`、`app/domains/rag/chunking.py`。

**坑**：CJK 无空格查询要做标点分词，否则全文路几乎必然空召回。

---

### 9. 知识的权限和生命周期怎么保证「Agent 不会看到不该看的、也不会看到没审过的」？

- 状态机：`draft → reviewed → published → superseded/retired`；**只有 published 进检索范围**。
- 四眼：`reviewer ≠ author`，服务端强制；发布时旧版本自动 superseded。
- 检索前置过滤：`applies_to.asset_ids` / `environments` + 当前用户资产范围，先过滤再召回。
- 发布时建向量索引，索引带 `embedding_model` / `vector_dimension` / `chunking_version`，
  换模型就知道哪些向量过期了。

**源码**：`app/domains/knowledge/service.py`、`app/api/v1/knowledge.py`。

**坑**：`publish_version` 要开独立会话建索引，所以播种数据必须先 commit，否则索引建在看不见的行上。

---

### 10. Prompt Injection 怎么处理？为什么不是「检测到就拦掉」？

- 用 8 条中英正则检测「忽略上述指令 / 你现在是 / 授予管理员权限 / system prompt:」这类模式，
  命中后写入证据的 `data_ref.injection_flags`，**内容照原样（脱敏后）返回**。
- 不拦的原因：拦了会给攻击者一个「诱导误拦」的拒绝服务面；清洗则会篡改证据。
- 真正的兜底不在检测，在**架构**：模型就算被说服，写操作也必须走提案 + 四眼 + allowlist + 门禁，
  模型没有直接动手的通路。

**源码**：`app/domains/tools/redaction.py::detect_injection`、
`app/domains/tools/gateway.py`（知识检索结果的 `note` 字段）。

**坑**：别说「已经防住了」。这是模式匹配，无关键词/编码混淆的注入可能绕过——
这条写在了 `docs/SECURITY_ACCEPTANCE.md` §12 的边界表里。

---

## C. 可靠性

### 11. Worker 怎么保证一个 run 不会被两个进程同时跑？

`FOR UPDATE SKIP LOCKED` 原子认领 + 租约 + `fence_token` 单调递增：

- 认领时 `fence_token = COALESCE(fence_token,0)+1` 并写入 `lease_owner` / `lease_expires_at`；
- 所有回写都带 `WHERE id = :id AND fence_token = :token`，旧 Worker 复活后写不进任何终态；
- 心跳续租；租约过期后其他 Worker 可以接管，token 变成 +1，老进程静默退出。

**源码**：`app/worker/claim.py`（`claim_next_run` / `finish_run` / `heartbeat`）、
`app/domains/agent/runtime.py`（`lease_fence` 判断）。

**坑**：「至少一次投递」意味着同一任务可能被投两次；幂等不能只放在消息层，
必须放在**状态机 + 认领状态**上。

---

### 12. RabbitMQ 和数据库怎么保证不丢消息？

**Transactional Outbox**：业务状态变更与 outbox 行在**同一个事务**里写，
提交后由 dispatcher 读 outbox 投递到 MQ，成功后标记已发。

效果：不会出现「状态改了但消息没发」，也不会出现「消息发了但状态没改」。
重复投递由下游的认领状态与幂等键吸收。

**源码**：`app/action/outbox.py`（`publish_event` / `dispatch_batch` / `declare_topology`）、
`app/domains/runs/service.py`（同事务写 outbox）。

---

### 13. 项目里有几层幂等？分别防什么？

| 层 | 机制 | 防什么 |
| --- | --- | --- |
| API | `Idempotency-Key`，`(user, endpoint, key)` 唯一，同 key 同体返回首次结果，同 key 异体 409 | 客户端重试/双击导致的重复建单 |
| 消息 | 认领状态 + 手动 ACK | 至少一次投递导致的重复消费 |
| 动作 | `action_idempotency` 账本（按 `action_key`） | 重复执行同一个外部副作用 |

**源码**：`app/domains/idempotency/`、`app/worker/claim.py`、`app/action/executor.py`。

**坑**：动作账本能承诺的是「**零重复提交**」，不是「恰好一次外部效果」——
`docker restart` 没有原生幂等键，所以 allowlist 里 `restart_container@v1` 标的是 `idempotent: false`。

---

### 14. Proposal / Approval / Action Service / 验证，这条链路的关键设计是什么？

- Proposal：策略校验（RBAC/范围/风险级/环境/参数）→ `pending_approval`，落 `parameter_hash` 快照。
- Approval：四眼（approver ≠ creator）+ 有效期 + 备注。
- Execute：校验审批未过期、参数哈希一致 → Outbox → MQ。
- Action：**消息只是通知**，执行上下文全从数据库导（见第 15 题）→ 前置检查 → allowlist 执行 →
  幂等账本 → **恢复验证**。
- 验证：真实健康检查 + 完整观察窗口取样，通过次数够才 `verified`。

**关键点**：三段状态各自独立（approval / execution / verification），
前一段成功不能掩盖后一段失败。「命令成功 ≠ 服务恢复」是 PRD 的硬要求。

**源码**：`app/domains/approvals/service.py`、`app/action/consumer.py`、
`app/action/executor.py`、`app/action/verification.py`、`app/action/convergence.py`。

---

### 15. 如果有人往队列里投一条伪造消息，能不能改执行目标？

不能。`handle_execute_requested` 的顺序是：

1. 消息里的 `proposal_id` 必须与该 `execution_id` 在库中的 `proposal_id` 完全一致，否则丢弃；
2. 从 `ActionExecution → Proposal → Approval → RunbookVersion → Asset` 全链导出上下文；
3. action_key / parameters / 目标资产 / 环境 **全部取库里的值**；
4. 重算规范化的 `parameter_hash`，与 Proposal 和 Approval 快照三方比对；
5. 容器身份以资产登记为唯一权威，未登记容器身份的资产不能仅凭消息执行。

所以篡改 messages 里任何字段，要么按库值执行，要么被丢弃，副作用都是 0。

**源码**：`app/action/consumer.py`、`tests/integration/test_d6_security.py`（6 个篡改向量）。

---

### 16. 恢复验证为什么不能「连续 N 次健康就算恢复」？

因为会误报：先健康、再失败，早期退出就会把「抖动」记成「恢复」。
D6 改成**完整观察窗口持续取样**，只有出现失败/不可观测才提前结束；
判定要求通过次数 ≥ `verification_min_passing_probes` 且探测次数足够。

**源码**：`app/action/verification.py::_run_observation_window`、
`tests/integration/test_d6_security.py`（`test_window_full_sampling_healthy_then_failing`）。

**坑**：「动作成功落库但验证前崩溃」是独立故障场景，靠 `convergence.resume_pending_verifications`
**只重发只读验证**，绝不重发有副作用的动作。

---

## D. 跨语言与前端

### 17. 跨语言诊断是怎么做的？Java 服务和 Python 服务走的是同一套吗？

是同一套。差异只在**连接器**，不在 Agent：

- 能力注册中心（capability manifest）声明每个能力的 `source_type` / `scopes` / 限流 / 超时；
- Java 演示服务暴露健康端点、业务端点、Micrometer 指标、OTel trace；
- Agent 调的是 `get_service_health` / `query_metrics` / `query_traces`，不关心对面是 Java 还是 Python；
- 证据里的 `source_type` 会区分（health / metric / trace / infra / log），方便评估按维度分析。

**源码**：`app/domains/tools/manifests.py`、`demo-java/`、`app/domains/integrations/`。

---

### 18. SSE 是怎么实现的？断线了怎么办？

- `GET /runs/{id}/events` 默认返回 JSON 分页；带 `Accept: text/event-stream` 时切成 SSE。
- 每个事件 `id = seq`；客户端重连带 `Last-Event-ID: <seq>`，服务端从 `seq+1` 重放。
- 空闲发心跳注释行保活；`Last-Event-ID` 非整数 → 400。
- **事件先落 `run_events` 表再推**，所以推送只是加速，断线重放永远拿得全。

**源码**：`app/domains/runs/sse.py`、`app/api/v1/runs.py`、`web/console/src/sse/`。

**坑**：SSE 不能当状态权威。前端 `RunPanel` 收到终止事件后仍然会再 GET 一次 run，
就是为了避免「少推一个终态事件」导致 UI 卡在 running。

---

### 19. 前端为什么选 Vue 3 + TS + Vite，而不是更省事的方案？

- 五个导航域、几十个接口，需要类型约束：`src/types.ts` 是接口契约的前端镜像，
  `vue-tsc` 在 CI 里跑，字段改名会直接编译失败。
- SSE 需要精细控制（重连、Last-Event-ID、终止后补拉），用封装好的 `src/sse/` 比框架黑盒可控。
- 构建产物交给 nginx 托管 + `/api` 反代，镜像多阶段构建，运行时不带 node。

**源码**：`web/console/src/api/index.ts`、`src/sse/`、`web/Dockerfile`、`web/nginx.conf`。

**坑（真实教训）**：`/integrations` 的响应是 `{types, instances, tests}`，前端早期按 `{items}`
解析 → `undefined.length` → **整页空白**。类型写对了，但类型是手写的「我以为」，不是从 openapi 生成的。
现在的护栏是 E2E `08 导航域渲染冒烟`。

---

## E. 评估与验证

### 20. 评估里的 judge-v1 到 v2 改了什么？为什么要改？

v1 是**字面包含**：期望的 `top1_markers`（如 `REDIS_UNAVAILABLE`）必须出现在第一名假设文本里。
真实模型用中文写「Redis 连接被拒绝」→ 永远不命中 → 把说对的判成全错（7 例只过 1 例）。

v2 改成**证据锚定**：第一名假设必须引用**带对应 error_code 的真实证据**；
新增引用有效性、证据支持率、误导采信、深层根因未证实、弃答正确性等维度。

**没有放宽**：缺证据的结论仍不通过，`unknown` 期望仍严格比对，副作用仍必须为 0。
同一批轨迹并排：v1 2/10、v2 10/10，8 例是 v1 误判（`judge_v1_false_negatives` 有名单）。

**源码**：`app/domains/evaluations/judge.py`、`tests/unit/test_judge_v2.py`、
`docs/EVALUATION_REPORT.md` §2.6。

---

### 21. 「10/10」到底能说明什么？不能说明什么？

能说明：在这 10 个固定案例上，模型能正确归因、报告能稳定产出结构化结果、
合理弃答正确、安全违规为 0。

不能说明：泛化能力。原因是样本量 10、单模型、单次运行、
7 例在开发过程中被反复查看、基础设施是确定性 stub、判官是规则系统。

所以对外一律叫「**固定案例规则通过率 10/10**」，不叫准确率。
这句话写在 `docs/EVALUATION_REPORT.md` §0 的命名纪律里，是刻意定的。

---

### 22. 盲测集是怎么用的？为什么必须「先写定再跑」？

`blind-v1` 三例（gateway 504 / OOM exit 137 / cache 连接拒绝）在首次真实运行前写定并提交，
**不参与任何调参**，也不进 39 例数据集（`dataset_digest` 保持不变）。

意义：证明前面的修复不是对着测试集过拟合——这三个案例在写定的时候，
模型还没跑过；跑出来 3/3 通过，才有说服力。

**源码**：`app/domains/evaluations/blind_cases.py`、`docs/eval/real_deepseek_judgev2_*.json`
（`blind_dataset_version` / `blind_case_keys`）。

---

### 23. 成本怎么算的？为什么报告里写 `unavailable`？

Token 是 provider `usage_metadata` 回来的真实值：10 例 in 205,188 / out 20,711。
**成本没有拿到**（provider 不返回计价，我也没去查单价换算），所以写 `unavailable`。

理由：宁可缺一个数，也不编一个看起来专业的数。要算成本，需要固定单价快照 + 计价日期，
这件事我写在「适用边界」里而不是拍脑袋填。

**源码**：`scripts/eval_real_model.py`、`docs/eval/real_deepseek_judgev2_20261029_074708.json`。

---

## F. 交付与工程

### 24. 一个新人拿到仓库，多久能跑起来？你怎么验证这件事？

- 一条命令：`bash scripts/bootstrap_demo.sh --with-demo`（Windows 有等价 PowerShell）。
  起栈 → 等 `/health/ready` → 容器内跑 seed → 打印控制台地址与演示账号。
- 我验证的方式不是「在我机器上能跑」，而是 **`git clone` 到一个空目录**重跑一遍。
  这一步暴露了三个真缺陷：控制台镜像是 D1 的旧页面、`demo-java` 从 D3 起就编译不过、
  容器名双前缀让证据链失真。

实测：克隆 2.4s、bootstrap 32.5s、首个诊断 9.4s。

**源码**：`scripts/bootstrap_demo.sh`、`docs/DEPLOYMENT_GUIDE.md`、
`docs/FAILURE_POSTMORTEMS.md`（§1–§3 就是这三次）。

---

### 25. 这个项目你最不放心的地方是什么？

1. **真实写操作默认关闭**，而且宿主 `docker.sock` 不是隔离环境——所以 `verified` 全链路只有
   集成测试覆盖，没有生产级证据。我不会说「已经安全了」。
2. **评估是固定案例 + 规则判官**，泛化能力没有证据。
3. **审计表可写不可验**（同库同权限、无哈希链），严格意义上不满足「不可篡改」。
4. **前端类型是手写镜像**，不是从 openapi 生成；`/integrations` 那次白屏就是代价。

这四条都写进了文档的「适用边界」表。留白的部分说清楚，比全都说「已完成」更可信。

---

## 附：被追问时可以直接打开的文件

| 追问方向 | 打开这个 |
| --- | --- |
| Agent 怎么跑、预算怎么掐 | `app/domains/agent/runtime.py` |
| 工具怎么被管住 | `app/domains/tools/gateway.py` + `manifests.py` |
| 认领/租约/fencing | `app/worker/claim.py` |
| 消息不可信 | `app/action/consumer.py` |
| 验证不误报 | `app/action/verification.py` |
| 判官口径 | `app/domains/evaluations/judge.py` |
| 检索融合 | `app/domains/rag/retrieval.py` |
| 安全边界 | `docs/SECURITY_ACCEPTANCE.md` §12 |
| 踩过的坑 | `docs/FAILURE_POSTMORTEMS.md` |
