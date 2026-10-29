# D6 安全验收：执行请求防篡改、Docker 权限隔离与恢复可靠性

状态：D6 交付（2026-10）。本文档是 PRD §13 三项强门禁中与执行安全相关项的
风险声明、隔离方式与验证证据登记处。

---

## 1. 执行请求防篡改（消息 ≠ 权威来源）

### 风险

D5 及更早版本中，`app/action/consumer.py` 的 `_recheck_authorization` 仅比较
消息携带的 `parameter_hash` 与 Proposal 保存的哈希，随后执行器直接采用
**消息中的 `parameters`**。攻击者若能向 RabbitMQ 投递伪造消息（或篡改队列中
的消息体），即可在不触碰数据库的情况下改变实际执行参数、action_key 甚至
目标指向——审批快照形同虚设。

### 修复（D6 已落地）

核心原则：**RabbitMQ 消息仅作为"某 execution 需要执行"的通知，不是任何
执行语义字段的权威来源。**

`handle_execute_requested`（[consumer.py](../opspilot-next/app/action/consumer.py)）现按以下顺序收敛：

1. 消息中的 `proposal_id` 必须与该 `execution_id` 在数据库中的
   `ActionExecution.proposal_id` 精确一致，否则丢弃（零副作用）。
2. `_load_authorized_context` 从数据库全链导出执行上下文：
   `ActionExecution → Proposal → Approval → RunbookVersion → Asset`；
3. 权威取值全部来自数据库：action_key、parameters、目标资产、环境；
4. 以规范化参数重算 `parameter_hash`，与 Proposal.parameter_hash 与
   Approval 快照三方比对（含 canonical JSON 逐一比对）；
5. 目标容器身份以**资产登记**（`Asset.endpoints.container_name`）为唯一权威：
   `resolve_container_name` 只接受与登记一致的参数容器名；
   **未登记容器身份的资产不能仅凭消息中的容器名进入执行**；
6. 执行前重新校验：环境 ∈ {sandbox, staging}、审批未过期、四眼原则、
   RunbookVersion 处于 published、ACTION_ALLOWLIST + parameters_schema 校验；
7. 执行调用使用 DB 导出的 `context["action_key"]` / `context["parameters"]`，
   验证阶段同样以 context 为准。

因此：篡改消息中任意字段（parameters、parameter_hash、action_key、
target_asset_id、environment、proposal_id）都无法改变最终执行目标——要么
按数据库权威值执行，要么被丢弃。

### 验证证据（自动化测试）

[tests/integration/test_d6_security.py](../opspilot-next/tests/integration/test_d6_security.py)：

| 测试 | 篡改向量 | 断言 |
| --- | --- | --- |
| `test_tampered_payload_parameters_never_change_target` | 替换消息 parameters + 重签哈希 | 仍按 DB 容器执行 |
| `test_tampered_payload_action_key_ignored` | 替换消息 action_key | 忽略，按 DB action_key 执行 |
| `test_forged_proposal_link_dropped` | 伪造 proposal_id 关联 | 丢弃，execution 保持 pending，副作用 0 |
| `test_asset_without_registered_container_cannot_execute` | 消息携带合法容器名但资产未登记 | skipped，不执行 |
| `test_db_parameter_tampering_detected_by_hash_recompute` | 直接改 DB parameters | 哈希重算比对失败，拒绝执行 |
| `test_execution_disabled_by_default_zero_side_effects` | 默认配置直接投递合法消息 | pre_check_failed（demo-only 门禁） |

全部防篡改测试断言**外部副作用次数为 0**（FakeDockerClient 调用计数）。

---

## 2. Docker 权限隔离（重要声明）

### 风险

受控恢复（`restart_container`）需要访问 Docker Engine。若 ops-action 直接
挂载宿主机 `docker.sock`，等价于把宿主机 root 级 Docker 管理权交给该容器——
容器名 allowlist 只是**应用层字符串匹配**，不是基础设施级权限隔离：
.allowlist 无法阻止容器内代码直接与 Docker API 交互执行任意容器操作
（启停/删除任意容器、挂载宿主文件系统、创建特权容器等）。

### 当前隔离方式（诚实声明）

- **默认关闭真实执行**：`ACTION_EXECUTION_ENABLED` 默认 `false`
  （[config.py](../opspilot-next/app/core/config.py)）。未显式开启时，任何
  执行请求都会在执行器入口被拒绝（`pre_check_failed`，reason 含
  "demo-only guard"），**不会触碰任何 Docker Daemon**。
- **推荐隔离形态**：仅当存在专用隔离演示环境（独立 Docker Daemon 或隔离
  VM，`ACTION_DOCKER_SOCKET` 指向该 daemon）时才允许开启。该 daemon 中只
  运行演示用容器（命名前缀 `opspilot-demo-`），OpsPilot 自身与其他业务
  容器运行在日常宿主机上，物理分离。
- **保留既有只读面**：`docker-ro-proxy`（只读诊断连接器）保持独立，不受
  执行链路影响。
- **禁止项**：不允许 ops-action 挂载用户日常宿主机的 `docker.sock` 后仅靠
  更严格的容器名前缀匹配宣称"已消除高权限风险"——该说法不成立，本仓库
  不做此声明。

### 现阶段能力边界

| 能力 | 状态 |
| --- | --- |
| 应用层执行门禁（审批/哈希/allowlist/登记容器） | 已实现并有测试覆盖 |
| 基础设施级权限隔离（独立 daemon/VM） | 仅在部署方提供隔离演示环境时成立；仓库默认配置下真实执行关闭（demo-only） |
| 生产环境真实动作 | 不支持、不允许（环境白名单仅 sandbox/staging） |

---

## 3. 执行成功但验证未完成的恢复（崩溃场景 E）

风险：动作已成功落库（execution=succeeded、proposal=executed）但验证未开始
时进程崩溃，重启后若直接跳过，Incident 永远停在"已执行未验证"。

处理（[convergence.py](../opspilot-next/app/action/convergence.py)）：

- `resume_pending_verifications` 启动与周期扫描：识别
  `execution=succeeded ∧ proposal=executed` 且无结论性 VerificationResult 的
  记录，**只重发只读验证**（health check），绝不重发有副作用的动作；
- 验证记录幂等：`verification_results.execution_id` UNIQUE 约束
  （迁移 0006 `uq_verification_execution`）+ `verify_execution` 入口的结论性
  记录检查，多消费者并发不会重复写入；
- 动作本身 UNKNOWN 时维持既有规则：不盲目重试，等待幂等账本收敛判定。

验证证据：

- `test_crash_between_execution_and_verification_converges`：动作成功落库后
  （模拟"验证前崩溃"）调用恢复 → 验证收敛为 verified，且 Docker restart
  次数为 1（无重复副作用）；
- `test_verify_execution_idempotent_on_reentry`：重复进入验证只产生一条
  记录。

## 4. 恢复验证窗口完整取样

风险：验证服务若在"早期连续 N 次健康"后提前宣布恢复成功，会把"先健康后
再次失败"的抖动误报为成功。

处理（[verification.py](../opspilot-next/app/action/verification.py)）：

- 完整观察窗口模式：窗口期内**持续取样**，仅"出现失败/不可观测"才提前
  结束；不存在"连续通过即提前退出"路径；
- 判定：通过次数 ≥ `verification_min_passing_probes` 且探测次数足够。

验证证据：

- `test_window_full_sampling_healthy_then_failing`：前 2 次健康、第 3 次失败
  → 最终 failed（不误报）；
- `test_window_samples_entire_duration_when_healthy`：全程健康 → 窗口跑满、
  探测次数符合预期。

## 5. 其他安全修复（D6）

- **Runbook 重名**：`create_runbook` 先查重，冲突返回稳定 409
  （`test_duplicate_runbook_name_conflict`），不再 500；
- **改密后旧 JWT 立即失效**：用户表新增 `token_jti`，改密时绑定新 token 的
  jti；`get_current_user` 校验 token jti 与服务端记录一致，旧 token 即刻
  401。改密响应携带重新签发的新 token（客户端无感轮换）
  （`test_old_jwt_invalid_after_password_change`）；
- **审计与错误格式**：审计 detail 仅存业务字段（用户名、ID、状态流转），
  不落参数明文与凭据；API 错误统一 `Envelope{error:{code,message}}` 格式，
  越权一律 404 伪装，不泄漏资源存在性。

## 6. 三项强门禁与本文件的关系

| 门禁 | 交叉核验位置 |
| --- | --- |
| 越权/未审批执行成功次数 = 0 | 评估执行器安全场景断言 + `test_d6_security.py` + 审计事件 `action.execution.*` 交叉比对 |
| 重复消息重复外部副作用 = 0 | 幂等账本（`action_idempotency`）+ FakeDockerClient 调用计数 + 隔离演示环境执行记录 |
| 恢复验证失败误报成功 = 0 | 验证窗口完整取样测试 + evaluation reliability 场景断言 |
