# D6 交付验收报告（12 项）

> 生成时间：2026-10-29 · 分支 `main` · 远端 `ddy-max-maker/OpsPilot`
> 范围：D6 剩余交付（控制台 / SSE / 评估审计 / 安全复核 / 浏览器 E2E / CI / 交付报告）。
> 本阶段**不进入 D7**，完成后停止等待验收。

---

## 1. Git 状态

| 项 | 值 |
|---|---|
| 分支 / 远端 | `main` → `https://github.com/ddy-max-maker/OpsPilot` |
| D6 提交链 | `bd7cd0b` → `a7c94e4`（评估 39/39）→ `3aca4dd`（D6 说明）→ `7a83626`（控制台）→ `887df8a`（门禁接线+holdout）→ `3ef2493`（E2E+脚本+CI）→ `d3d38e5`（CI 修复）→ `d5334dc`（tags 归一）→ `24ede3d`（资产 endpoints 根因修复）→ 本报告 |
| 远程 CI | CI `40831046827` success；d6 `40831046861` success |
| 工作区 | 干净（提交后 `git status` 无输出） |
| 本地/远端 | 一致（push 无拒绝，`origin/main` 与本地同步） |

D6 期间的调试过程、12 处评估修复根因记录见 `docs/D6-DELIVERY.md`。

---

## 2. 控制台五点导航域（无 Mock）

13 个视图 + 4 个组件，全部调用真实后端 API（`web/console`，26 个 `.ts/.vue` 源文件）：

| 导航域 | 路由 | 视图 | 关键能力 |
|---|---|---|---|
| 工作台与事件 | `/` `/incidents/:id` `/assets` | DashboardView / IncidentDetailView / AssetsView | 统计卡、事件登记/确认/关闭、资产登记、发起诊断 |
| 诊断详情 | `/diagnostics` | DiagnosticsView + RunPanel | run 列表、SSE 实时轨迹、证据/假设/报告分区、历史 run 恢复 |
| 知识中心 | `/knowledge` `/runbooks` | KnowledgeView / RunbooksView | 文档创建/复核/发布（四眼）、检索（模式标记）、Runbook 版本与 allowlist |
| 审批与恢复 | `/approvals` `/approvals/:id` | ApprovalsView / ProposalDetailView | 方案创建、dry-run、批准/驳回、执行、验证轮询、状态机徽章 |
| 平台与质量 | `/platform` `/evaluations` `/audit` | PlatformView / EvaluationsView / AuditView | 连接器与能力清单、评估套件执行、审计事件（RBAC 受限） |
| 账号 | `/login` `/change-password` | LoginView / ChangePasswordView | JWT 登录、首登改密 |

前后端契约按枚举逐一对应（`src/types.ts`），`Envelope` 统一解包，401 自动跳登录，写操作携带幂等键。

---

## 3. 构建与类型检查

| 检查 | 结果 |
|---|---|
| `npx vue-tsc --noEmit` | 通过（`strict` + `verbatimModuleSyntax`，无 `any` 逃逸点新增） |
| `npx vite build` | 通过，`dist` 221 KB；入口 `index-*.js` 107.40 kB（gzip 41.63 kB），其余按路由分包 |
| CI `frontend` job | success（`npm ci` → `vue-tsc` → `vite build`） |

---

## 4. SSE 前端接入（`src/sse/runStream.ts`）

- **鉴权**：`fetch` + `ReadableStream` 手工解析 SSE，`Authorization: Bearer` 走请求头；JWT **绝不进 URL**（规避日志/Referer 泄漏）。
- **事件模型**：消费 `run_event`，忽略 `: ping` 心跳注释行；终止帧 `end` 或 `run_finished` 事件即停止重连。
- **断线重连**：携带 `Last-Event-ID` 续传，指数退避，上限 12 次；本地实测刷新/重连后无重复轨迹。
- **去重**：按事件 `seq` 去重，重放重叠区间不产生重复条目。
- **刷新恢复**：先按 run_id 用 JSON 分页补历史事件，再以最后 `seq` 作为 `Last-Event-ID` 续流，页面刷新后轨迹从数据库重建（E2E 用例 07 覆盖）。
- **终态**：run 进入终态后停止轮询与重连，避免空转。

---

## 5. 浏览器 E2E（Playwright + Chromium，真实后端）

`web/console/e2e/critical-path.spec.ts`，7 用例顺序执行（有状态依赖），`workers: 1`：

| # | 用例 | 覆盖点 |
|---|---|---|
| 01 | 管理员登录与工作台 | 登录、四统计卡、资产登记落库 |
| 02 | 创建与确认故障事件 | 事件 CRUD、状态推进 |
| 03 | 启动诊断并观察 SSE | run 启动、轨迹流式渲染、证据/报告区块 |
| 04 | 知识中心创建与发布检索 | 知识创建、operator 无发布入口（RBAC UI 边界）、admin 复核+发布、检索模式可见 |
| 05 | 审批与恢复全链路 | 建 r1 Runbook → 发布 → 方案引用 → dry-run → **他账号**四眼批准 → 执行 → 状态离开 `executing` |
| 06 | RBAC 权限边界 | viewer 无写入口、operator 访问审计得 403 |
| 07 | 刷新后状态从数据库恢复 | `?run=` 恢复 + `reload()` 后轨迹重建 |

本地（compose 栈 + 真实 Postgres/RabbitMQ）：**7 passed（41.4s）**。

远程 GitHub Actions（d6 `e2e` job，ubuntu-latest + pgvector + RabbitMQ + 显式开启的 fake provider + `ACTION_EXECUTION_ENABLED=false`）：**7 passed（22.0s）**，run `40831046861`。

CI 首轮暴露两处真实缺陷，均已修复并复测：

1. 测试 05 失败于串联门禁——Runbook 风险级需与 `restart_container@v1` 注册规格一致（r1）、方案须引用本次发布的版本、目标资产必须登记 `endpoints.container_name`（D6 加固的“资产登记为唯一权威”）。修复：用例选择 r1、按名称选中新版本、目标改用已登记的 `demo-redis`。
2. CI 首跑 01/02/03 失败于**资产列表 500**（`INTERNAL`）。用全新库 + 本地 API 复现后拿到 traceback：`AssetEndpoints` 是 `extra="forbid"`（仅允许 `health_url/metrics_ref/logs_ref/container_name/health_check`），而 E2E 种子资产多写了 `host`/`port` → 响应校验失败导致**整个列表** 500（本地 demo 数据由 API 写入，未暴露）。修复：种子改用 `container_name` + `health_check`；另对 `AssetResponse.tags` 补 `NULL → []` 归一（同类隐患：非 API 写入的行 `tags` 为 NULL 同样会打挂列表），对外契约不变。

---

## 6. 确定性评估（fake provider，CI 门禁）

数据集 `d6-eval-v2`，`dataset_digest=4f5ca554e6e4ecde`，39 案例 / 4 套件：

| 套件 | 通过 | 内容 |
|---|---|---|
| `fault-core` | 12/12 | Redis 拒绝/超时、容器 OOM/缺失、Java 5xx、MySQL 拒绝、未知退出码（预算 0）等 |
| `security-gates` | 13/13 | 未审批执行、同人审批、非审批角色、production 拒绝、草稿 Runbook、消息目标篡改、伪造提案、未登记容器、过期审批（9 个负向 + 4 个正向） |
| `rag-ablation` | 6/6 | 文本/混合检索、误导知识抵抗、CJK 无空格查询 |
| `reliability-recovery` | 8/8 | 双执行单 Outbox、执行-验证间崩溃、验证窗口翻转、stale fence 拒绝 |
| **合计** | **39/39** | 报告 `docs/eval/fake_ci_report.json`，CI `eval-report` job success |

命令：`uv run python scripts/eval_fake_report.py --out docs/eval/fake_ci_report.json`。

---

## 7. 真实模型评估（DeepSeek，诚实结果）

`deepseek-chat`，7 个代表案例，产物 `docs/eval/real_deepseek_20261029_044040.json`：

| 案例 | 通过 | top1 | 假设数 | 证据数 | 知识命中 |
|---|---|---|---|---|---|
| fault_redis_down_refuse | ✗ | ✗ | 0 | 9 | 0 |
| fault_container_oom_exit | ✗ | ✗ | 3 | 8 | 0 |
| fault_java_stack_503 | ✗ | ✗ | 0 | 7 | 0 |
| fault_mysql_connect_refuse | ✗ | ✗ | 0 | 8 | 0 |
| fault_redis_down_timeout | ✗ | ✗ | 3 | 10 | 0 |
| fault_unknown_exit_budget0 | ✓ | ✗ | 0 | 0 | 0 |
| rag_hybrid_misleading_resist | ✗ | ✗ | 4 | 7 | 1 |

**1/7（14%）**。三类失败模式（未改写任何期望值换取分数）：

1. **报告 JSON 解析失败**（4/7 无任何假设产出）：模型以 Markdown 包裹报告，结构化解析落空——工程问题是解析健壮性，不是期望值问题。
2. **假设全部 `insufficient_data`**（3/7 有假设但未定根因）：模型给出候选描述却不给结论，`top1_hit` 无法命中。
3. **真实知识误用**（1/7 命中被禁止的误导知识）。

token 统计为 `unavailable`（provider 未回传 usage），已在产物中如实标注；Fake 与真实评估严格分离，真实评估不进 CI、不读取私人 Key。

---

## 8. Holdout 审计

- v1 holdout 12 例中，`fault_redis_down_timeout`、`fault_cross_service_trace` 在调试期被暴露 → **降级为普通案例并显式记录**（`_HOLDOUT_EXPOSED_V1`），不再计入 holdout 指标。
- v2 重新冻结：保留 10 例（`_HOLDOUT_KEPT_V1`）+ 新增 4 例（`_HOLDOUT_ADDED_V2`：容器 OOM 退出、MySQL 拒绝、Java 5xx、stale fence）= **14 例**。
- 自 `7a83626` 起冻结，`dataset_digest` 固定；**为分数修改 holdout 期望值 = 0 次**。
- `seed_dataset` 增加 holdout 标记幂等同步（`holdout_resynced` 统计），保证数据集重播种后标记不回退。

---

## 9. 三项安全强门禁复核

| 门禁 | 机制 | 证据 |
|---|---|---|
| **越权执行 = 0** | 四眼原则（`decided_by ≠ created_by_user`）+ 角色校验 + 环境硬排除 production + 仅 published Runbook + 动作级 allowlist + 参数 schema + 容器命名空间/资产登记一致性 | `sec_unapproved_execute_rejected`、`sec_same_user_approval_rejected`、`sec_non_approver_role_rejected`、`sec_production_environment_rejected`、`sec_draft_runbook_not_referenceable`、`sec_tampered_message_target`、`sec_forged_proposal_dropped`、`sec_unregistered_container_rejected`、`sec_expired_approval_rejected`（9 负向）+ 4 正向，13/13 |
| **重复副作用 = 0** | Outbox 单次投递 + `action_idempotency` 唯一键 + 执行 fence/幂等令牌 | `rel_double_execute_single_outbox`、`rel_stale_fence_rejected`，套件 8/8 |
| **恢复误报 = 0** | 执行-验证分离、验证观察窗口、窗口内翻转不误判、崩溃恢复不重发 | `rel_crash_between_execute_and_verify`、`rel_verification_window_flip` |
| **执行前五要素复核** | dry-run 只读前置检查 + 审批/执行时点重新校验（目标容器、资产、环境、参数、Runbook 版本） | `sec_tampered_message_target`（消息参数不信任）、`sec_unregistered_container_rejected` |

本阶段修复的真实缺陷：`ActionExecutor` 构造时未传入 `execution_enabled`/`docker`，配置默认 `false` 但**实际执行恒为开启**——已接线（`app/action/consumer.py`），并补 `docker-compose.yml` 的 `ACTION_EXECUTION_ENABLED` / `ACTION_DOCKER_SOCKET`。

---

## 10. 残余风险（不掩盖）

1. **容器执行面仅为演示范畴**：Docker 隔离依赖 `opspilot-demo-*` 命名空间前缀与资产登记，**不是**通用容器编排的安全边界；compose 中 `ops-action` 挂载宿主 docker socket，属 demo 设计。
2. **CI 不验证真实副作用**：`ACTION_EXECUTION_ENABLED=false`，E2E 只验证到 `pre_check_failed` 的状态机正确性，真实重启动作仅在本地 compose 栈验过。
3. **fake provider 开关默认值**：`.env.example` 为本地演示写 `true`，生产必须 `false`；缺失凭据时直接报错而非静默降级。
4. **CJK 检索**：无标点长句依赖标点分词，召回下降（`rag-ablation` 已覆盖有标点场景）。
5. **真实模型报告解析脆弱**：4/7 案例因 Markdown 包裹导致结构化解析失败，需 JSON 模式/重试策略（D7 候选）。
6. **Windows 事件循环**：需 `WindowsSelectorEventLoopPolicy`（psycopg 异步不兼容 Proactor），Linux 部署无此问题，脚本已内置。
7. **token/cost 统计缺失**：真实评估产物标注 `unavailable`。

---

## 11. 静态检查与 CI

| 检查 | 本地 | 远程 |
|---|---|---|
| `ruff check .` | All checks passed | CI lint job `success` |
| `ruff format --check .` | 197 files already formatted | `success`（此前失败，已规范化 18 文件） |
| `mypy app` | Success: 126 source files | `success` |
| `pytest -q`（真实 Postgres + RabbitMQ） | 317 passed, 1 skipped（296s） | CI test job `success`（含空库 `alembic upgrade head` + schema 对象核对） |
| 前端 `vue-tsc` + `vite build` | 通过 | d6 `frontend` `success` |
| 评估报告 39/39 | 通过 | d6 `eval-report` `success` |
| Playwright E2E | 7/7（41.4s） | d6 `e2e` `success`（7/7，22.0s） |

远程运行记录：CI `40831046827`（Lint & Type Check / Tests 全 success）、d6 `40831046861`（eval-report / frontend / e2e 全 success）。
CI 工作流：`.github/workflows/ci.yml`（后端主门禁）+ `.github/workflows/d6.yml`（评估报告 / 前端构建 / 浏览器 E2E）。

修复前的 4 次远程失败均定位到真实缺陷（不是配置糊弄）：`ruff format` 未规范化、`Settings` 必填密钥缺失、fake provider 开关未显式设置、种子资产 `endpoints` 越界字段导致资产列表 500。

---

## 12. D7 开工判定

**判定：不满足开工条件，不进入 D7。**

说明：D6 七项任务中 1–6 已完成并具备本地 + 远程可复现证据，第 7 项即本报告。阻塞 D7 的不是流程项，而是**质量阈值**：

- 真实模型成功率 1/7（14%），报告结构化解析是首要工程缺口；
- 真实副作用执行未在 CI 验证（仅本地 compose）；
- SSE 断线续传、长时任务恢复仅单机验证，缺并发/压测与多实例一致性证据；
- 11 项残余风险中第 1、2、5 项属 D7 级设计问题（执行面隔离、真实执行门禁、模型输出契约）。

建议 D7 的三个切入点（不含实施）：①模型输出契约与解析重试；②执行面从 demo 命名空间走向可配置隔离；③多实例 SSE 与 Outbox 并发一致性压测。
