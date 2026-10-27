# ADR-D2｜诊断执行核心与 DeepAgents 工具面裁剪（D2 实施期决策）

**日期**：2026-10-27　**状态**：已实施　**关联**：ARCHITECTURE.md §3.3/§3.4、PRD §9、IMPLEMENTATION_PLAN §2.2

## 背景

冻结文档要求 LangGraph 显式业务图 `Validate → LoadAssets → RetrieveKnowledge → Plan →
CollectEvidence → Analyze → VerifyClaims → {CollectMore | Report} → Finalize`，
同时要求 DeepAgents 负责"规划 / 工具循环 / 上下文管理 / 假设与报告产出"。
两者存在职责重叠：DeepAgents 0.7.32 的 `create_deep_agent` 内部即是一个带
planning、文件上下文与工具循环的完整 LangGraph agent——在其外再手写一层
同构的 ReAct 节点图会形成**双执行核心**（两套循环、两套状态、两份 trace），
违反用户约束"不要在外部手写另一套重复的 ReAct/Plan/CollectEvidence/Analyze 模型循环"。

## 决策 1｜单一执行核心 = DeepAgents 内置 agent 循环

- `Business Orchestrator`（Worker 进程内）负责：认领、租约、预算硬限制、取消协调、
  run_events/evidence/hypotheses/report 的业务落库、终态裁决——**不实现图节点**。
- `AgentRuntime`（app/domains/agent/runtime.py）是唯一的框架适配层：
  - 以 `create_deep_agent(model, tools, backend, checkpointer)` 创建单主 Agent；
  - 以 `astream(stream_mode="updates")` **步进驱动**：每个 super-step（model+tools）之间
    由业务侧检查 取消标志 / deadline / 迭代与 token 预算——这就是冻结图中
    CollectEvidence↔Analyze↔CollectMore 循环的服务端强制点（ADR-8 落地）；
  - 领域层只消费 `{evidence_id, status, summary_redacted}` 与工具轨迹，
    **LangGraph 消息对象不逃出 runtime.py**。
- run_events 事件类型映射：`run_started / tool_called / tool_returned /
  evidence_added / hypothesis_added / budget_warning / error / run_finished` 由业务代码写；
  `node_entered / node_exited` 仅在出现显式业务节点时产生——D2 单核心下
  **不伪造节点事件**（契约枚举包含该类型，但不要求每种类型都出现）。
- RetrieveKnowledge 节点：D2 禁区无 RAG——该阶段由系统提示词声明"知识检索不可用"，
  不产出 knowledge 证据（IMPLEMENTATION_PLAN §2.3）。

## 决策 2｜DeepAgents 内置工具面裁剪（实测依据，deepagents==0.7.32）

`create_deep_agent` 默认注册：`ls/read_file/write_file/edit_file/delete/glob/grep`（文件）、
`task`（默认 general-purpose 子代理）、`execute`（shell，仅当 backend 实现
SandboxBackendProtocol 才可用）。实测（StateBackend、无 sandbox）：

| 内置工具 | 实测行为 | 处置 |
|---|---|---|
| `execute` | StateBackend 未实现 SandboxBackendProtocol → 工具返回 error，无法执行命令 | **HarnessProfile.excluded_tools 从工具面移除**（纵深防御：不依赖"返回报错"这层） |
| `task`（general-purpose 子代理） | `subagents=[]` 仍回落注册默认 general-purpose | **HarnessProfile.general_purpose_subagent(enabled=False) 移除**（PRD §9：单主 Agent，子代理不启用） |
| `write_file/read_file/...` | 写入/读取的是 **graph state 内虚拟文件系统**（StateBackend），不触 Worker 真实文件系统，更不触目标基础设施 | **保留**——这正是 ADR-7 的上下文管理机制（大证据放状态、摘要进 Prompt）；无真实 FS 后端被挂载，路径遍历无处可去 |
| 业务只读工具 | 仅经 Tool Gateway 生成的 4 个只读 capability | 每次调用过 manifest/范围/限流/脱敏/审计 |

红线断言（测试固化）：Worker 编译图的工具面 = DeepAgents 虚拟 FS 工具 ∪ 4 个只读
Gateway 工具，**不含 execute / task / 任何写凭据工具**；审计中写操作尝试 = 0。

## 决策 3｜Checkpoint（ADR-5 落地细节）

- `langgraph-checkpoint-postgres` 的 saver 不支持 schema 参数——迁移 0002 预建
  `langgraph` schema，Worker 以 psycopg 连接串 `options='-c search_path=langgraph'`
  使 checkpoint 全部落入独立 schema；`thread_id = run_id`（ADR-5）。
- checkpoint 仅服务恢复；业务状态/事件/证据全部以 public schema 业务表为权威（CONTRACTS §8）。

## 后果

- 图推进 trace 以 run_events（tool_called/tool_returned/evidence_added）+ usage 计数呈现，
  满足"可从 HTTP 请求追到认领 SQL → 图启动 → 工具调用 → Evidence 行"的源码要求；
- D3 引入显式 Manifest 注册表时无需改动执行核心（Gateway 工厂已按 manifest 生成）；
- D5 HITL/中断场景若需要 `interrupt_on`，在 AgentRuntime 配置层启用，不改核心选型。
