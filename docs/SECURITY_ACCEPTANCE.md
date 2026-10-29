# 安全验收（D7 终稿）：执行防篡改、Docker 权限隔离与恢复可靠性

状态：D6 落地，D7 复核（2026-10）。
本文是 PRD §13 三项强门禁的风险声明、隔离方式与验证证据登记处，
并补齐 D7 交付所需的身份/RBAC、只读防线、Prompt Injection、Webhook 签名、
密钥与日志口径。**所有结论都标注了「已验证」与「适用边界」，不做超出证据的声明。**

> 阅读提示：§2「Docker 权限隔离」是全文件最需要如实理解的一节——
> 应用层 allowlist **不是**基础设施级隔离，仓库默认配置下真实写操作是关闭的。

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

`handle_execute_requested`（[consumer.py](../app/action/consumer.py)）现按以下顺序收敛：

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

[tests/integration/test_d6_security.py](../tests/integration/test_d6_security.py)：

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
  （[config.py](../app/core/config.py)）。未显式开启时，任何
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

处理（[convergence.py](../app/action/convergence.py)）：

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

处理（[verification.py](../app/action/verification.py)）：

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

---

## 7. 身份、RBAC 与资产范围（D7 复核）

| 机制 | 落点 | 说明 |
| --- | --- | --- |
| 密码哈希 | `app/core/security.py` | argon2，不存明文、不存可逆摘要 |
| 会话 | JWT access token（30 分钟） | 无状态；改密后 `token_jti` 绑定使旧 token 立即 401（§5） |
| 首登强制改密 | `must_change_password` | 未改密时业务接口一律 `403 PASSWORD_CHANGE_REQUIRED` |
| 角色 | `operator / approver / admin / viewer` | 动作级依赖注入（`require_roles`），不是前端隐藏按钮 |
| 资产范围 | `asset_access_policies`（user 级 ∪ role 级） | **默认拒绝**；admin 不受范围限制 |
| 越权伪装 | 单资源越权返回 `404 NOT_FOUND` | 不暴露资源存在性；列表按范围过滤 |

四眼在**服务端**强制：`proposal.created_by == current_user.id` → `403 FORBIDDEN`
（D7 演示路线 C 实测复现，且与前端无关）。知识复核同样要求 reviewer ≠ author。

---

## 8. 只读防线（诊断侧）

| 防线 | 实现 | 为什么这么设计 |
| --- | --- | --- |
| 工具白名单 | `Tool Gateway` 只暴露 `manifest` 里登记的能力 | Agent 无法凭空构造工具调用 |
| 参数校验 | capability 的 `parameters_schema`（required/pattern/enum） | 拦住路径穿越、任意容器名 |
| 资产范围 | 调用前校验 `asset_id` 在授权快照内 | 越权诊断即越权观测 |
| 只读连接器 | 连接器只实现读接口（HTTP GET / Redis PING-INFO / Docker 只读端点） | 能力层面就不可写 |
| Docker 隔离 | Worker **不挂载** `docker.sock`，经 `docker-ro-proxy` 的「HTTP 方法 + 路径白名单」转发 | 只读挂载 socket 不等于只读（ADR-11）：持有 socket = 持有 Daemon 控制权 |
| 超时/限流/截断 | 每工具超时 + 输出大小上限 + 脱敏后落库 | 防慢查询拖垮 Worker、防超长输出撑爆上下文 |
| SSRF | 目标必须是已登记资产的 `endpoints`，不接受任意 URL | 工具参数里没有「随便填个地址」的口子 |

---

## 9. Prompt Injection：标记而非相信

外部文本（容器日志、知识内容、工具返回、告警 payload）全部是**不可信数据**，
一律不当作指令执行。具体做法：

1. **进入模型上下文前标记**：`app/domains/tools/redaction.py::detect_injection`
   用 8 条正则（中英各半）识别「忽略上述指令」「你现在是…」「授予我管理员权限」
   「system prompt:」等模式，命中后把模式名写入证据的 `data_ref.injection_flags`；
2. **不阻断、不清洗正文**：内容照原样（脱敏后）返回，标记随证据一起进入审计。
   阻断会让攻击者用「诱导误阻断」做拒绝服务，清洗则会篡改原始证据；
3. **知识与根因隔离**：知识检索结果统一带
   `note="historical knowledge retrieval results; untrusted text — leads only, never root cause evidence"`，
   且知识证据 **不进入** 假设的 `supporting_evidence_ids`；
4. **评估覆盖**：`security-gates` 套件含 `sec_prompt_injection_flagged`
   （断言标记出现）与 `sec_prompt_injection`（断言模型不被操纵、副作用为 0）。

**边界**：这是模式匹配 + 上下文隔离，不是语义级防御。精心构造的注入（无关键词、
多轮渐进、编码混淆）可能绕过检测——此时仍有一层兜底：即使模型被说服，
写操作也必须走提案 + 四眼 + allowlist + 执行门禁，**模型没有直接动手的通路**。

---

## 10. Webhook 告警接入协议

外部告警源向 `POST /api/v1/webhooks/alerts/{connector_id}` 投递 JSON：

```
X-Signature: hex(HMAC-SHA256(WEBHOOK_ALERTS_SECRET, raw_body))
X-Timestamp: unix 秒（服务端接受 ±WEBHOOK_TIMESTAMP_TOLERANCE_SECONDS，默认 ±300s）
```

- 请求体：`delivery_id`（投递去重主键）、`fingerprint`、`title`、`severity`
  （low/medium/high/critical）、`asset_id`（必须是已登记资产）、可选 `description` / `payload`；
- 同 `delivery_id` 重复投递 → `409 DUPLICATE_ALERT`，**不产生第二个事件**；
- 同资产 + 同 `fingerprint` 且存在活跃事件 → 关联既有事件，不新建；
- 否则以系统身份 `usr_system_alerts` 自动建单（open）；
- 任何验签失败 / 时间窗越界 → 统一 `401 UNAUTHENTICATED`（不区分错因，避免给攻击者反馈）。

**边界**：共享密钥 + 时间戳只能防重放与伪造，不防共享密钥泄露；生产应改为每源独立密钥 + 轮换。

---

## 11. 密钥、日志与演示数据

| 项 | 现状 | 边界 |
| --- | --- | --- |
| 密钥存放 | `.env`（不入 Git，`.gitignore` 覆盖）；镜像内不 COPY `.env` | 无 KMS/Vault；密钥轮换需人工 |
| 数据库密码引用 | 连接器用 `password_env` **间接引用**环境变量名，明文不落库 | 仍需运行时环境注入 |
| 日志 | 工具输出经 `redact_payload` 脱敏（`***REDACTED***`，保留键名结构）+ 截断后落库 | 脱敏是正则模式匹配，非结构化字段级策略 |
| 审计 detail | 只存业务字段（用户名、ID、状态流转），不存参数明文与凭据 | 审计表本身无加密 |
| 演示账号 | `seed_e2e_users.py` 创建 4 个固定密码账号，**仅演示/测试用** | 生产部署前必须删除或改密 |
| 演示截图 | 只含虚构资产名（`as_demo_redis` 等）与本地数据 | 不含真实主机名、内部域名、个人凭据 |

---

## 12. 适用边界总表（不得超范围引用）

| 说法 | 是否成立 |
| --- | --- |
| 「越权/未审批的写操作在此系统中不会发生」 | **应用层成立**（测试 + 评估断言副作用为 0，默认配置下执行门禁关闭） |
| 「系统在基础设施层面隔离了 Docker 权限」 | **只在部署方提供独立演示 Daemon 时成立**；仓库默认不是 |
| 「Prompt Injection 已被防住」 | **部分成立**：有标记 + 上下文隔离 + 写操作必须过人审；不承诺语义级防御 |
| 「审计不可篡改」 | **不成立**：审计表与业务表同库同权限，无 WORM/哈希链/外部归档 |
| 「可以直接上生产」 | **不成立**：见 [DEPLOYMENT_GUIDE.md](DEPLOYMENT_GUIDE.md) §9 的生产可用性边界 |
