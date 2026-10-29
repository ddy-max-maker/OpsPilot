# 演示脚本（D7）

三条可复现路线，全部走真实 HTTP API 与真实运行中的演示容器，不含任何 Mock。
本文只描述「敲什么命令、看到什么、怎么恢复」，原理见 [ARCHITECTURE.md](ARCHITECTURE.md)。

- 环境准备与部署细节：[DEPLOYMENT_GUIDE.md](DEPLOYMENT_GUIDE.md)
- 每条路线的判定规则：[EVALUATION_REPORT.md](EVALUATION_REPORT.md)

---

## 0. 一次性准备（约 1 分钟）

```bash
git clone <repo> OpsPilot && cd OpsPilot
bash scripts/bootstrap_demo.sh --with-demo      # Windows: powershell -File scripts/bootstrap_demo.ps1 -WithDemo
```

脚本做的事（幂等，可重复执行）：

1. 无 `.env` 时从 `.env.example` 复制（**首次务必修改 `JWT_SECRET` 与 `BOOTSTRAP_ADMIN_PASSWORD`**）；
2. `docker compose up -d --build`（13 个容器：postgres / rabbitmq / api / worker / web / ops-action /
   docker-ro-proxy / demo-redis / demo-app / demo-gateway / demo-java / demo-mysql / prometheus / jaeger）；
3. 轮询 `http://localhost:8000/api/v1/health/ready` 直到 200（最多 180 秒）；
4. 在 `api` 容器内执行 `scripts/seed_demo.py`（连接器/资产/拓扑）与 `scripts/seed_e2e_users.py`（4 个演示账号）。

预期输出（脚本真实输出，节选）：

```
[bootstrap] 1/4 构建并启动核心栈（postgres/api/web/worker/rabbitmq/ops-action/docker-ro-proxy）
[bootstrap] 2/4 等待 API ready（首次需等迁移完成）
[bootstrap] API ready（N 次探测）
[bootstrap] 3/4 播种连接器 / 演示资产 / 演示账号（幂等）
[bootstrap] 启动演示故障环境（demo profile：demo-app / demo-redis / ...）
[bootstrap] 4/4 完成

  控制台：   http://localhost:8080
  API 文档： http://localhost:8000/docs
  演示账号（.env 未改时）：
    e2e_admin    / E2ePassw0rd!2026   （admin，可复核发布、审批执行）
    ...
  下一步：按 docs/DEMO_SCRIPT.md 走三条演示路线。
```

演示账号（`scripts/seed_e2e_users.py` 创建，密码统一 `E2ePassw0rd!2026`，可用 `DEMO_PASSWORD` 覆盖）：

| 账号 | 角色 | 用途 |
| --- | --- | --- |
| `e2e_admin` | admin | 知识审核发布、Runbook 发布 |
| `e2e_operator` | operator | 登记故障、发起诊断、发起提案（不能自审） |
| `e2e_approver` | approver | 四眼审批的第二人 |
| `e2e_viewer` | viewer | 只读浏览 |

同一个脚本也提供 `--with-knowledge`（播种 8 篇历史知识）与 `--all`（含 demo profile + 知识）。

---

## 1. 模型选择：Fake 还是 DeepSeek

| | Fake（默认，零成本） | DeepSeek（真实推理） |
| --- | --- | --- |
| `.env` | `AGENT_PROVIDER=fake`<br>`AGENT_MODEL_NAME=fake-diagnoser`<br>`AGENT_FAKE_PROVIDER_ENABLED=true` | `AGENT_PROVIDER=deepseek`<br>`AGENT_MODEL_NAME=deepseek-chat`<br>`AGENT_FAKE_PROVIDER_ENABLED=false`<br>`DEEPSEEK_API_KEY=sk-...` |
| 报告来源 | 由**真实工具结果**确定性合成 | 真实模型推理 + 真实报告生成 |
| 是否需要 Key | 否 | 是（演示不提供私人 Key） |
| 单次诊断耗时 | ~10s | ~12–27s（10 例实测均值 15.8s、中位 15.2s） |
| Token 消耗 | 0 | 约 2.0 万 in / 2.1 千 out 每例 |

切换后必须重建 api 与 worker（run 的 `model_config_snapshot` 由 **api 进程**写入，
两边环境不一致会导致快照与报告不一致）：

```bash
# 切到 DeepSeek
sed -i 's/^AGENT_PROVIDER=fake/AGENT_PROVIDER=deepseek/' .env
sed -i 's/^AGENT_MODEL_NAME=fake-diagnoser/AGENT_MODEL_NAME=deepseek-chat/' .env
sed -i 's/^AGENT_FAKE_PROVIDER_ENABLED=true/AGENT_FAKE_PROVIDER_ENABLED=false/' .env
echo 'DEEPSEEK_API_KEY=sk-你的key' >> .env
docker compose up -d api worker
```

诊断详情里 `run.model_config_snapshot` 与报告 `generator` 必须同时显示 `provider=deepseek`
（`generator.provider` 与快照一致是 D7 修复项之一，见 FAILURE_POSTMORTEMS「快照失真」）。

---

## 2. 路线 A：故障登记 → Agent 诊断 → Evidence → 报告

**目的**：证明「诊断结论有据可查」——每个假设都能挂到真实采集的证据上。

```bash
python scripts/demo_routes.py a
```

脚本动作（不需要手工点页面）：

1. `e2e_operator` 登录，读取资产 `as_demo_redis`；
2. `docker stop opspilot-demo-redis`（真实故障注入；`recover` 可撤销）；
3. `POST /incidents`（带 `Idempotency-Key`）→ `POST /incidents/{id}/acknowledge`
   → `POST /incidents/{id}/investigations`；
4. 轮询 `GET /runs/{id}` 到终态，打印证据与报告。

预期输出（实测，Fake 模式）：

```
[A] 故障注入：docker stop opspilot-demo-redis（真实演示容器，可 recover 撤销）
[A] 事件已登记 inc_...（state=open, environment=sandbox）
[A] 事件确认 → acknowledged
[A] 诊断已受理 run=...（Worker 认领后开始执行）
[A] run 状态 → queued / running / completed
[A] 运行配置快照：provider=fake model=fake-diagnoser rag=off prompt=d6-v2
[A] 证据 4 条（真实采集，payload 已脱敏）：
      - [infra/docker.inspect] status=ok
      - [log/docker.logs] status=ok
      - [health/redis.health] status=unavailable error_code=CONNECTOR_TIMEOUT
      - [health/http.health_check] status=unavailable error_code=HEALTH_URL_NOT_REGISTERED
[A] 报告 v1 status=final ... report_status=ok
[A] 假设#1 [supported] ... (support=N)
```

DeepSeek 模式下的实测结果（D6 记录，`docs/eval/real_deepseek_*`）：8–9 条证据，
`tokens_in≈24136 / tokens_out≈2255`，假设#1 `supported`、#2 `refuted`（排除 OOM）、
#3 `insufficient_data`（深层根因，明确不给结论）。

**看点**：`status=ok` 是工具真的读到了容器；`unavailable` 也带明确的 `error_code`，
不是「没有报错所以没问题」。

**恢复**：

```bash
python scripts/demo_routes.py recover     # docker start opspilot-demo-redis
```

---

## 3. 路线 B：历史知识审核发布 → RAG 检索 → 辅助诊断

**目的**：证明 RAG 不是「把文档塞进 prompt」，而是有权限过滤、有版本、有降级的检索链路。

前置（干净环境没有知识集时必须做一次）：

```bash
# 1) 打开 RAG
sed -i 's/^RAG_MODE=off/RAG_MODE=hybrid/' .env && docker compose up -d api worker
# 2) 播种 8 篇历史知识（draft→review→publish 真实生命周期，reviewer≠author）
docker compose exec -T api python scripts/demo/seed_knowledge.py
```

预期：`[seed] embedding provider = e5-small-384-v1 (intfloat/multilingual-e5-small)`，
`[seed] 完成：8/8 条知识已发布（含过期/未授权/注入标记样本）`。实测 21 秒（含模型首次加载）。

然后：

```bash
python scripts/demo_routes.py b
```

预期输出（实测，hybrid）：

```
[B] 已发布知识文档 8 篇（draft 对 Agent 不可见）
[B] 检索模式 = hybrid，命中 5 条，延迟 46ms
[B]   - 复盘：Redis 连接池耗尽导致业务超时（S1 类） v1 RRF 排名 text=1 vector=1
[B]   - 复盘：Redis 高延迟（含注入测试文本） v1 RRF 排名 text=2 vector=3
[B]   - 复盘：MySQL 连接数激增（历史：曾因 max_connections 过低） v1 RRF 排名 text=None vector=2
[B] 本次 run 的 rag_mode = hybrid
[B] knowledge 来源证据 2 条（知识不参与根因支撑，只引导排查方向）
[A] 报告 ... generator={... 'rag_mode': 'hybrid', 'report_status': 'ok' ...}
```

**看点**：

- `text=None / vector=2` 说明两路召回不同——纯全文检索召回不到的那条靠向量捞回来了，
  RRF 融合后进前 3；
- 第三条是**跨资产知识**（MySQL 的经验出现在 Redis 故障的检索结果里），
  它只作为排查方向，绝不进入根因支撑集（`knowledge 来源证据` 与假设的
  `supporting_evidence_ids` 是分开的）；
- 含注入文本的知识被检索层标记，模型不会执行其中的指令（对应评估套件 `security-gates`）。

**恢复**：同路线 A（`recover`）。知识集不需要清理，重复播种幂等。

---

## 4. 路线 C：Proposal → 四眼审批 → 执行门禁 → 恢复验证

**目的**：证明「Agent 能诊断，但不能自己动手」——写操作要经过提案、双人审批、
前置检查、执行侧兜底校验四道闸，且在默认配置下**根本不落盘**。

```bash
python scripts/demo_routes.py c          # 或 c --run-id <已有 run>
```

不传 `--run-id` 时会先跑一次诊断作为提案依据（约 10 秒）。

预期输出（实测）：

```
[C] 未提供 run_id：先跑一次诊断作为恢复方案的前置依据
[C] 无可用已发布 Runbook：自举创建并发布演示用 Runbook
[C] Runbook D7 演示 Redis 受控重启 v1 已发布（version_id=rbv_...）
[C] 提案 prp_... status=pending_approval 参数哈希=75e8f4b96bec0684…
[C] dry-run 前置检查：runbook_version_published=PASS asset_active=PASS environment_match=PASS
    action_registered=PASS parameters_valid=PASS container_binding=PASS not_expired=PASS
[C] 发起人自审批 → 403 FORBIDDEN（四眼拒绝，期望 403）
[C] e2e_approver 审批通过 → approved
[C] 执行已受理 execution=act_...（等待 ops-action 消费）
[C] 提案终态 = execution_failed
[C] 执行记录 status=pre_check_failed reason=action execution is disabled (demo-only guard);
    real execution requires ACTION_EXECUTION_ENABLED=true on an isolated demo daemon
```

**为什么终态是 `execution_failed` 而不是 `verified`**：

`ops-action` 容器挂载的是**宿主 docker.sock**，这不是隔离环境。D6 安全收尾把真实写操作
默认关闭（`ACTION_EXECUTION_ENABLED=false` + `ACTION_ISOLATED_DAEMON_ACK=false`），
执行器在 `fail_execution_precheck` 阶段就返回拒绝，**外部效果确定为零**：
账本收敛到 `completed`，提案落 `execution_failed`，事件回到 `investigating`。
这条路径演示的是「拒绝」，不是「失败」——拒绝原因被结构化记录在 `result_detail.reason`。

验证「零写操作」：

```bash
docker inspect opspilot-demo-redis --format 'Running={{.State.Running}} RestartCount={{.RestartCount}}'
# RestartCount 必须为 0（容器被路线 A/B 停过的话 Running=false，但重启计数仍是 0）
```

**想看 `verified` 全链路**（四眼 → 真实重启 → 观察窗口 → 验证通过）有两种合规方式：

1. 集成测试（推荐，无需宿主权限）：`uv run pytest tests/integration/test_action_flow.py -q`；
2. 在**独立演示 Docker Daemon**（如 `docker context create demo --docker host=tcp://127.0.0.1:2376`）
   上部署，并把 `ACTION_DOCKER_SOCKET` 指向该 daemon 的 socket，再显式设置
   `ACTION_EXECUTION_ENABLED=true` + `ACTION_ISOLATED_DAEMON_ACK=true`。
   未完成隔离确认前不得开启——这是 D6 冻结的安全默认值，D7 未做任何放宽。

**恢复与清理**：

```bash
python scripts/demo_routes.py recover         # 起回演示容器
```

提案/执行/审批记录全部保留（演示证据）。需要清空重来：

```bash
docker compose --profile demo down -v --remove-orphans   # 注意 -v 会删除数据卷
bash scripts/bootstrap_demo.sh --with-demo
```

---

## 5. 全套一次跑完

```bash
python scripts/demo_routes.py all
```

`all` = A → B → C。B 在 `RAG_MODE=off` 时会打印提示并跳过知识部分（不是失败），
C 复用 A 的 run。实测总耗时约 30 秒（Fake 模式）。

---

## 6. 实测耗时（D7 干净环境，2026-10-29）

| 步骤 | 耗时 | 备注 |
| --- | --- | --- |
| `git clone` | 2.4s | 本地路径克隆 |
| `bootstrap_demo.sh --with-demo` | 32.5s | 含 13 容器启动 + 两个 seed；镜像层已缓存 |
| 两个自定义镜像 `--no-cache` 冷构建 | 54s | web（npm ci）+ demo-java（maven 拉依赖） |
| `seed_knowledge.py` | 21s | 含 e5 模型首次加载 |
| 路线 A | 9.4s | Fake |
| 路线 B | 9.6s | hybrid |
| 路线 C | 10.1s | 含自举 Runbook |

「克隆 → 首次诊断」累计 **约 45 秒**；三条路线全部跑完 **约 75 秒**，
远低于「30 分钟内跑通核心演示」的验收线。前提：基础镜像已在本机
（`python:3.12-slim`、`node:22-alpine`、`nginx:1.27-alpine`、`maven:3.9-eclipse-temurin-17`、
`postgres`、`rabbitmq`、`redis`、`mysql` 等）。全新机器首次拉取基础镜像需额外数分钟，
取决于网络。
