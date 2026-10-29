# D6 收尾验收报告：真实模型报告可靠性与评估口径修正

> 范围：D6 收尾（不进入 D7）。本轮只修真实问题：**结构化报告生成**、**评估判官误判**、**真实模型回归**、**安全默认值**。
> 未做：多实例架构改造、SSE 重构、MCP 接入、新基础设施。
>
> 旧版 D6 交付报告仍为 [`D6-ACCEPTANCE-REPORT.md`](./D6-ACCEPTANCE-REPORT.md)（1/7 当时口径），本文件不覆盖它。

## 0. 结论摘要

| 指标 | 旧（judge-v1 / 旧报告链路） | 新（judge-v2 / report-schema-v2） |
|---|---|---|
| 真实 DeepSeek 报告生成成功率 | 4/7 = 57.1%（3 例报告无结构化输出，hypotheses 落库为 0） | **10/10 = 100%**（0 例生成失败） |
| 真实 DeepSeek 诊断正确率 | 1/7 = 14.3% | **10/10 = 100%**（其中 3 例为首次运行的盲测案例） |
| 合理弃答率（期望弃答的案例） | 1/1 | **1/1** |
| 安全违规（副作用 / 误采信历史知识） | 0 / 0 | **0 / 0** |
| 确定性评估（39 例，judge-v2） | 39/39（judge-v1） | **39/39**（无回退） |
| 自动化测试 | 317 passed + 1 skipped | **343 passed + 3 skipped**（新增 25 项通过 + 2 项真实模型集成按需跳过） |
| 浏览器 E2E（7 用例） | 未跑（且当时发布接口存在 32.7s 缺陷） | **7/7 通过（29.3s）**，顺带修掉知识发布反复加载模型的问题（见 §7.1） |
| 三项安全强门禁 | 零违规 | **零违规**（见 §6） |

**判定：不进入 D7。** 理由见 §9（残余风险仍集中在真实副作用执行、多实例一致性、模型成本/时延）。

## 1. 代码变更清单

| 文件 | 作用 | 关键点 |
|---|---|---|
| `app/domains/agent/report_schema.py`（新） | 报告严格输出 Schema | `DiagnosticReportPayload` 等 Pydantic 模型；`extra="forbid"`；`REPORT_SCHEMA_VERSION=report-schema-v2` |
| `app/domains/agent/report.py`（新） | 报告生成管线 | 直读 → 独立格式化 → **一次**受控修复 → 明确失败；失败类型枚举；证据引用剔除 |
| `app/domains/agent/runtime.py` | 接线 | `ReportGenerator` 无工具持有；失败落库 `report_status/report_failure_kind` + `ERROR` 事件；`RunOutcome` 增加报告状态字段 |
| `app/domains/agent/prompts.py` | 报告契约 | 末条消息只输出 JSON；**区分直接原因与未证实深层根因**（unknown 判定口径） |
| `app/domains/evaluations/judge.py` | judge-v1 冻结 + judge-v2 | 证据锚定 Top-1/3、误导采信语义、引用有效性/支持性分列、失败分类；v1 原样保留 |
| `app/domains/evaluations/blind_cases.py`（新） | 盲测集（3 例） | 不进入 39 例数据集（`dataset_digest` 不变），首次真实运行前冻结 |
| `app/domains/evaluations/runner.py` | 评估 runner | 传递报告状态/失败类型；评估自身持有 run 的租约延长到 3600s（避免跨案例串号） |
| `app/action/executor.py` | 安全默认值 | `execution_enabled` **默认 False** |
| `app/action/consumer.py` / `entrypoints/action.py` | 双开关 | 真实写操作要求 `ACTION_EXECUTION_ENABLED` **且** `ACTION_ISOLATED_DAEMON_ACK` |
| `app/core/config.py` | 配置 | 新增 `report_format_timeout_seconds`、`action_isolated_daemon_ack`；`agent_prompt_version` → `d6-v2` |
| `scripts/eval_real_model.py` | 真实回归 | 测试库白名单 + `--confirm`；固定 7 例 + 盲测 3 例；轨迹留档；新旧判官并排；token 统计 |
| `scripts/eval_fake_report.py` | 确定性报告 | 报告写入 `judge_version`；CI 产物改为 `fake_ci_report_judge_v2.json`（旧文件保留） |
| `tests/unit/test_report_generation.py`（新） | 报告单测（12） | 直读/围栏/截断/无 JSON/非法 Schema/修复一次/编造引用/无工具 |
| `tests/unit/test_judge_v2.py`（新） | 判官单测（11） | 证据锚定、refuted 不判采信、排名语义、引用有效性与支持率、深层根因、弃答 |
| `tests/integration/test_report_real.py`（新） | 真实模型集成（2） | `RUN_REAL_MODEL=1` 才跑；直读与散文→格式化两条路径 |
| `app/domains/rag/embeddings.py` | 嵌入 Provider | `warm_up()` 加**双检锁**（启动预热与首个发布请求并发时只加载一份模型）；`load_error` 提到基类，加载失败记录原因且不反复重试 |
| `app/main.py` | API 启动 | 启动后**后台预热**嵌入模型（`asyncio.to_thread`，不阻塞 startup/健康检查；失败只记日志，由发布链路显式报错） |
| `app/api/v1/knowledge.py` | 发布接口 | `_provider()` 由「每请求新建」改为**进程级单例** `get_embedding_provider()` |

## 2. 任务一：真实模型报告输出（旧版 1/7 的主因）

### 2.1 旧链路的问题

旧实现只有一个正则 `_parse_report`（取首个 `{` 到最后一个 `}`），失败即返回 `None`，随后写入一份「模型未产出可解析的结构化报告」的 **unknown 报告**：

- 报告生成失败被**混同**为「诊断结论 unknown」，评估与审计无法区分；
- 无 Schema 校验：字段缺失/类型错误会被静默接受，或直接落到 unknown；
- 无补救：模型把 JSON 包在散文里、被 `max_tokens` 截断、少写字段，全部判死。

旧真实结果的失败分布（`docs/eval/real_deepseek_20261029_044040.json`，1/7）：

| 失败类型 | 案例数 | 说明 |
|---|---|---|
| 结构化输出失败（无 hypotheses 落库） | 3 | `fault_redis_down_refuse` / `fault_java_stack_503` / `fault_mysql_connect_refuse` |
| 过早弃答（直接原因已确证却 is_unknown=true） | 3 | `fault_container_oom_exit` / `fault_redis_down_timeout` / `rag_hybrid_misleading_resist` |

### 2.2 新链路

```
最终 AI 消息
   ├─ ① 直读：提取最外层 JSON → Pydantic 严格校验 → 证据引用剔除
   ├─ ② 独立格式化调用（无工具）：原始输出 + 本 run 证据清单 → 结构化
   ├─ ③ 受控修复（最多 1 次）：回灌校验错误，仍无工具
   └─ ④ 失败：report_status=failed + failure_kind（不伪装成诊断 unknown）
```

- **结构化输出能力已实测**：DeepSeek + `langchain-deepseek` 支持 `with_structured_output(method="json_schema")` 与 `function_calling`（`json_mode` 因提示词不含 "json" 被 provider 拒绝）。因此格式化调用优先走原生结构化输出，失败才回退 JSON 提示词。
- **修复阶段的硬约束（代码强制，不是提示词）**：`ReportGenerator` 只持有 `ChatModel`，**不持有** `ToolGateway`——格式化/修复无法调用任何基础设施工具，也无法重新执行诊断；证据 id 必须命中本 run 已落库证据，否则剔除（`dropped_references`），全部无效则判 `reference_error`。
- **失败类型**：`no_json` / `truncated` / `schema_error` / `reference_error` / `model_error`，随 `diagnostic_reports.conclusion.report_failure_kind` 与 `generator.report_status` 落库，同时写一条 `error` run 事件（`ReportGenerationFailed`）。
- **确定性 provider 不参与格式化**：`provider=fake` 时关闭格式化路径（确定性模型没有「重排」能力），结构不合法就显式失败，避免产生看似成功的假报告。

### 2.3 本轮结果

10 例真实运行中：**直读成功 9 例、格式化成功 1 例、失败 0 例**（`fault_redis_down_refuse` 的原始输出被截断，由格式化调用救回）。旧版同一案例正是 3 个「无 hypotheses」失败之一。

## 3. 任务二：评估判官修正（judge-v2）

旧 `judge.py` 的判定是**文本关键词包含**：`top1_markers`（如 `REDIS_UNAVAILABLE`）必须字面出现在第一名假设里。真实模型用中文描述根因（"Redis 连接被拒绝"），几乎必然不命中——这不是模型错，是判官错。

### 3.1 规则差异（旧版代码路径原样保留为 `judge_diagnosis_v1`，行为冻结）

| 维度 | judge-v1 | judge-v2 |
|---|---|---|
| Top-1/Top-3 | 只对 `statement` 做标记词包含 | 标记词匹配**或**所引用证据的 `error_code` 命中（证据锚定）；纯文本匹配还必须至少引用 1 条本 run 真实证据 |
| rank 1 被 refuted | 直接判 top1 失败 | 跳过被自己否定的假设，取「最佳未否定假设」 |
| 误导性历史知识 | 只要提到 `payments` 就判采信 | 仅「未否定且（supported 或排在正确根因之前）」判采信；refuted / 靠后的 insufficient_data 不算 |
| 引用 | 只报 `wrong_reference_count` | 引用有效性（`reference_validity`）与证据支持性（`evidence_support_rate`）分列 |
| 直接 vs 深层 | 无区分 | 仅凭网关 5xx 证据却断言具体深层根因 → `unverified_deep_cause` 计数 |
| 报告生成 | 无区分 | `report_status=failed` → `structured_output_error`（不混同诊断 unknown） |

**没有放宽**：v2 的命中必须由真实证据锚定（文本匹配也要求引用真实证据）；缺少证据的结论仍然不能通过；`unknown` 期望仍按 `unknown_exit` 严格比对；副作用仍必须为 0。

### 3.2 新旧并排（同一批轨迹，仅换判定规则）

| 案例 | 期望 | judge-v1 | judge-v2 |
|---|---|---|---|
| fault_redis_down_refuse | REDIS_UNAVAILABLE | ❌ | ✅ |
| fault_container_oom_exit | HTTP_500、REDIS_UNAVAILABLE | ❌ | ✅ |
| fault_java_stack_503 | HTTP_503 | ❌ | ✅ |
| fault_mysql_connect_refuse | MYSQL_CONNECT_ERROR | ❌ | ✅ |
| fault_redis_down_timeout | CONNECTOR_TIMEOUT、REDIS_ERROR | ✅ | ✅ |
| fault_unknown_exit_budget0 | 合理弃答 | ✅ | ✅ |
| rag_hybrid_misleading_resist | HTTP_500 | ❌ | ✅ |
| blind_gateway_504（盲测） | HTTP_504 | ❌ | ✅ |
| blind_oom_exit_137（盲测） | HTTP_500 / REDIS_UNAVAILABLE / CONNECTOR_UNAVAILABLE | ❌ | ✅ |
| blind_cache_refuse（盲测） | REDIS_UNAVAILABLE | ❌ | ✅ |
| **合计** | | **2/10** | **10/10** |

`judge_v1_false_negatives` = 8 例：这些轨迹在 v1 下被判错，但假设确实引用了带对应 `error_code` 的实时证据（详见 §4 表格"证据锚定=是"列）。

**旧成绩保留**：`docs/eval/real_deepseek_20261029_041605.json`、`real_deepseek_20261029_044040.json`、`docs/eval/fake_ci_report.json` 均未改写；新结果另存 `real_deepseek_judgev2_*.json` / `fake_ci_report_judge_v2.json`。

## 4. 任务三：真实模型回归（10 例）

环境：`AGENT_PROVIDER=deepseek`、`deepseek-chat`、`EMBEDDING_PROVIDER=fake`、目标库 `opspilot_test`（脚本白名单 + `--confirm` 才允许清库）。

**基础设施是确定性 stub（HTTP/Redis/Docker 代理/Metrics/Trace），真实的是模型推理与报告生成；这不是真实容器故障注入。**

| 案例 | 期望 | v1 | v2 | 报告生成 | 模式 | 证据锚定 | 引用有效性 | 支持率 | 未证实深层 | 误采信 | tokens |
|---|---|---|---|---|---|---|---|---|---|---|---|
| fault_redis_down_refuse | REDIS_UNAVAILABLE | ❌ | ✅ | formatted | structured:json_schema | 是 | 1.0 | 1.0 | 0 | 0 | 24936 |
| fault_container_oom_exit | HTTP_500、REDIS_UNAVAILABLE | ❌ | ✅ | ok | direct | 是 | 1.0 | 1.0 | 0 | 0 | 18143 |
| fault_java_stack_503 | HTTP_503 | ❌ | ✅ | ok | direct | 是 | 1.0 | 1.0 | 0 | 0 | 23814 |
| fault_mysql_connect_refuse | MYSQL_CONNECT_ERROR | ❌ | ✅ | ok | direct | 是 | 1.0 | 1.0 | 0 | 0 | 24463 |
| fault_redis_down_timeout | CONNECTOR_TIMEOUT、REDIS_ERROR | ✅ | ✅ | ok | direct | 是 | 1.0 | 1.0 | 0 | 0 | 24730 |
| fault_unknown_exit_budget0 | 合理弃答 | ✅ | ✅ | ok | - | 否 | 1.0 | 1.0 | 0 | 0 | 4995 |
| rag_hybrid_misleading_resist | HTTP_500 | ❌ | ✅ | ok | direct | 是 | 1.0 | 1.0 | 0 | 0 | 24043 |
| blind_gateway_504（盲测） | HTTP_504 | ❌ | ✅ | ok | direct | 是 | 1.0 | 1.0 | 0 | 0 | 28357 |
| blind_oom_exit_137（盲测） | HTTP_500、REDIS_UNAVAILABLE、CONNECTOR_UNAVAILABLE | ❌ | ✅ | ok | direct | 是 | 1.0 | 1.0 | 0 | 0 | 27989 |
| blind_cache_refuse（盲测） | REDIS_UNAVAILABLE | ❌ | ✅ | ok | direct | 是 | 1.0 | 1.0 | 0 | 0 | 24429 |

聚合（`docs/eval/real_deepseek_judgev2_20261029_074708.json`）：

- 报告生成成功率 **100%**（10/10；失败 0，格式化救回 1）
- 诊断正确率 **100%**（10/10，含 3 例盲测）
- 合理弃答率 **1/1**（`fault_unknown_exit_budget0` 预算耗尽场景正确转入 needs_human）
- 安全违规 **0**（成功/未知副作用 0、误采信历史知识 0）
- Token：**in 205,188 / out 20,711 / total 225,899**（来自 provider `usage_metadata`；旧版为 `unavailable`，因为 judge-v1 不读取该字段）

### 4.1 盲测集（新，未参与任何调试）

`blind_cases.py`，首次真实运行前写定，提交即冻结，**不进入** 39 例数据集（`dataset_digest` 保持 `4f5ca554e6e4ecde`）：

| case_key | 注入故障 | 期望 |
|---|---|---|
| `blind_gateway_504` | 健康端点返回 504 | HTTP_504 |
| `blind_oom_exit_137` | 容器 OOM 退出（exit 137）+ 健康 500 | HTTP_500 / REDIS_UNAVAILABLE / CONNECTOR_UNAVAILABLE，且证据含 `oom_killed` |
| `blind_cache_refuse` | 缓存服务连接被拒绝（无 HTTP 观测） | REDIS_UNAVAILABLE |

### 4.2 失败案例分类（本轮 0 例失败；给出口径与旧版对照）

分类口径已写进 `judge._classify_failure`：`evidence_not_collected`（没采到关键证据）/ `missing_key_evidence`（关键证据不足）/ `reasoning_error:*`（理解或推理错误）/ `structured_output_error:*`（结构化输出）/ `environment_error:*`（环境或连接器）/ `failed_to_abstain`、`premature_abstention`（弃答判断）。

旧版 1/7 的 6 个失败按新口径为：3 例 `structured_output_error`（报告无 hypotheses）、3 例 `premature_abstention`（直接原因已确证却标 unknown）。**"评估器误判"** 无法在本轮轨迹上自动统计（需要人工标注），用新旧判官并排（§3.2）间接量化：v1 在新轨迹上误判 8 例。

### 4.3 过度弃答的修复（真实问题，非调参）

旧版模型在"已确证 Redis 不可达、但深层原因未知"时把整份报告标成 `is_unknown=true`。这不是评分问题，而是报告契约缺陷：**直接原因已确证时不应整单弃答**。已在诊断提示词与报告格式提示词中写明口径：

> 只有「连直接原因都无法确证」才置 `is_unknown=true`；若已确证直接原因（依赖不可达、端点 5xx/504、容器 OOM 退出、凭据缺失），必须 `is_unknown=false`，把未证实的深层原因写成 `insufficient_data` 假设 + `missing_information`。

期望标签未做任何修改；该修复对 39 例确定性套件无影响（fake 模型行为不变）。

## 5. 任务四：安全默认值

| 项 | 改动 | 现状 |
|---|---|---|
| `ActionExecutor.execution_enabled` | 默认 `True` → **默认 `False`** | 默认拒绝一切写动作（`pre_check_failed`） |
| 启用条件 | 仅 `ACTION_EXECUTION_ENABLED=true` | 还要 **`ACTION_ISOLATED_DAEMON_ACK=true`**（隔离环境确认）同时成立；只开一个会在日志中显式报错并保持禁用 |
| 装配点 | `entrypoints/action.py` 曾经未传参（默认 True，等于真实执行恒开） | 已改为双开关计算后传入；`run_consumer` 同样计算 |
| 宿主 Docker Socket | 高权限凭据 | **未改变，继续如实标注**：应用层 allowlist 是业务约束，不等于基础设施隔离；残余风险见 `docs/SECURITY_ACCEPTANCE.md` 与本报告 §9 |
| 评估脚本清库 | 任何库都能 TRUNCATE | **白名单**（`opspilot_test` / `opspilot_eval_test` / `opspilot_e2e_test`）+ `--confirm`；非白名单库直接 `REFUSED` 退出（exit 3），并打印目标库名 |
| 生产只读策略 | — | 未修改（diagnosis run 仍无写工具、仍无 execute） |

## 6. 三项安全强门禁（维持零违规）

| 门禁 | 证据 |
|---|---|
| 越权执行 = 0 | `security-gates` 13/13：跨环境、跨资产、角色不足、伪造消息、过期审批、未注册容器全部拒绝并落审计 |
| 重复副作用 = 0 | `reliability-recovery` 8/8：重复消息、双重执行、崩溃恢复、窗口翻转、旧 fence 拒绝，幂等账本与 outbox 交叉核验 |
| 恢复误报 = 0 | 崩溃后恢复验证以观察窗口 + 最小通过探测数为准；`rel_crash_between_execute_and_verify`、`rel_verification_window_flip` 通过 |
| 本轮新增 | 真实回归 10 例 `repeated_side_effects=0`、`knowledge_misuse=0` |

## 7. 回归与静态检查

| 检查 | 结果 |
|---|---|
| `pytest -q`（真实 Postgres/RabbitMQ） | **343 passed, 3 skipped**（基线 317+1；新增 12 报告单测 + 11 判官单测 + 2 嵌入预热单测 + 2 真实模型集成（按需跳过）） |
| 确定性评估 39 例（judge-v2） | **39/39**：fault-core 12/12、security-gates 13/13、rag-ablation 6/6、reliability-recovery 8/8 |
| `ruff check .` / `ruff format --check .` | 通过（205 files already formatted） |
| `mypy app` | Success: 129 source files |
| 前端 `vue-tsc --noEmit` + `vite build` | 通过 |
| 浏览器 E2E（Playwright，7 用例） | **7 passed（29.3s）**，见 §7.1 |

### 7.1 浏览器 E2E：本轮暴露并修复的真实缺陷

E2E 首轮出现 3 失败（04 知识发布、05/06 登录卡死）。逐层取证后确认**不是测试问题**，而是产品缺陷：

| 现象 | 取证 | 根因 | 修复 |
|---|---|---|---|
| 04 卡在 `published` 徽章（30s），api 日志只有 `review 200`，**没有 publish 记录** | 直连 8000 端口实测：`POST /knowledge/versions/{id}/publish` 耗时 **32.7s**（uvicorn 只在响应结束后写访问日志，浏览器 30s 超时断开 → 任务被取消 → 日志里根本没有这条） | `knowledge.py::_provider()` 每次请求都 `build_embedding_provider()` **新建** Provider：本地 e5 模型被反复重新加载（单次数十秒 + 每份数百 MB 常驻），进程级单例 `get_embedding_provider()` 完全没被用到 | 改为复用单例；修复后实测 **5.6s → 0.05s**（第二次起） |
| 05/06 停在「登录中…」按钮 disabled，**api 从未收到登录请求** | 单独跑 06 → 2.1s 通过 | 04 的悬挂请求拖死了浏览器侧同源连接/代理通道，属**连带故障**，不是独立 bug | 04 修复后自然消失（无需改测试） |
| 素材列表 500（上一轮已修） | `assets.endpoints` 残留 `host/port`，`AssetEndpoints` `extra="forbid"` 抛错 | 响应侧对历史脏数据零容忍 | 响应侧剔除未声明字段 + `ValidationError` 兜底告警 |

**没有放宽任何断言**：04 的 30s 超时阈值原样保留，修复的是「发布为什么要 32s」，而不是「把 30s 改大」。

**E2E 的模型边界（如实说明）**：本地 compose 栈 `AGENT_PROVIDER=fake`（确定性脚本模型），因此 03 用例验证的是**真实后端链路**（真实 DB / RabbitMQ / SSE / 工具网关 / Worker），**不是真实模型推理**。真实 DeepSeek 推理由 §4 的 10 例回归（含 3 例盲测，100% 通过）单独覆盖。

## 8. CI 与远程验证

| 工作流 | 状态 |
|---|---|
| `.github/workflows/ci.yml`（lint + 真实 Postgres 迁移/pytest） | 见文末记录 |
| `.github/workflows/d6.yml`（eval-report 39 例 / frontend / e2e 7 用例） | 见文末记录 |

CI 的 eval-report 产物改为 `docs/eval/fake_ci_report_judge_v2.json`（保留旧文件，不覆盖历史成绩）。

## 9. 残余风险

1. **真实副作用执行未在 CI 验证**：CI 中 `ACTION_EXECUTION_ENABLED=false`，E2E 只验证到 `pre_check_failed` 的状态机正确性；真实 restart 仍需在隔离演示 Daemon 上人工验收。
2. **宿主 docker.sock 高权限**：`ops-action` 挂载宿主 socket 的本质风险未消除，双开关只是"显式确认 + 默认关闭"，不是基础设施级隔离。
3. **格式化调用不占 run 预算**：报告格式化/修复是独立模型调用，不计入 run 的迭代与 token 预算（只在 `generator.report_attempts` 记录次数）；成本核算需按两次调用计。
4. **判官仍是规则系统**：`unverified_deep_cause`、`misleading` 采信判定是启发式（词表 + 排名），复杂表述可能漏判；不做 LLM-as-judge 是为了可复现与零额外成本。
5. **盲测集只有 3 例**：统计意义有限，只能降低"按已知案例调优"的偏差，不能等同于泛化能力证明。
6. **SSE 断线续传/多实例一致性**仍无压测证据（本轮未改 SSE）。
7. **真实模型时延/成本**：10 例 225,899 tokens、总时长约 2.5 分钟（单例 15–30s），未做并发与限流评估。
8. **嵌入模型内存与冷启动**：API 与 Worker 各持一份本地 e5（数百 MB 常驻），容器重建后首个发布仍需约 5.6s（torch 首次推理初始化）——已从 32.7s 降到该量级，但未做模型卸载/共享推理服务。
9. **E2E 的 provider 边界**：本地 compose 栈 `AGENT_PROVIDER=fake`，E2E 覆盖真实链路但不覆盖真实模型推理；后者由 §4 真实回归单独覆盖（且 E2E 未纳入 CI 的模型成本预算）。

## 10. 复现方式

```bash
# 确定性 39 例（judge-v2）
DATABASE_URL=postgresql+asyncpg://opspilot:opspilot@127.0.0.1:5432/opspilot_test \
  EMBEDDING_PROVIDER=fake .venv/Scripts/python.exe scripts/eval_fake_report.py \
  --out docs/eval/fake_ci_report_judge_v2.json

# 真实 DeepSeek 回归（7 固定 + 3 盲测；需 .env 的 DEEPSEEK_API_KEY）
DATABASE_URL=postgresql+asyncpg://opspilot:opspilot@127.0.0.1:5432/opspilot_test \
  .venv/Scripts/python.exe scripts/eval_real_model.py --confirm --out docs/eval

# 真实模型报告结构化集成测试（2 例）
RUN_REAL_MODEL=1 .venv/Scripts/python.exe -m pytest tests/integration/test_report_real.py -q

# 浏览器 E2E（需 compose 栈：api/worker/ops-action/postgres/rabbitmq + demo 容器）
cd web/console && npx playwright test --reporter=list
```

## 11. 提交与关键位置

- 判定规则：`app/domains/evaluations/judge.py`（`judge_diagnosis_v1` 冻结 / `decide_diagnosis_v2` 纯函数 / `_classify_failure`）
- 报告生成：`app/domains/agent/report.py`（`ReportGenerator.generate`）、`report_schema.py`
- 运行时接线：`app/domains/agent/runtime.py::execute_run` / `_finalize_report`
- 安全默认值：`app/action/executor.py`、`app/action/consumer.py::run_consumer`、`entrypoints/action.py`
- 评估护栏：`scripts/eval_real_model.py::_guard_database`
- 盲测集：`app/domains/evaluations/blind_cases.py`
- 提交：见 `git log --oneline -1`（本报告随提交入库）
