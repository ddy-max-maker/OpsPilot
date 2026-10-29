# Release Notes — `v0.7.0-d7`

> 发布日期：2026-10-29　|　类型：**交付版（delivery-only）**
> 起点：`c0e81db`（D6 收尾）　→　终点 Tag：`v0.7.0-d7`
> 一句话：**这一版没有新功能，只有「把它交出去」需要的东西，以及交出去时才发现的问题。**

---

## 如何验证这一版

```bash
git clone <repo> OpsPilot && cd OpsPilot && git checkout v0.7.0-d7
cp .env.example .env                       # 至少改 JWT_SECRET / BOOTSTRAP_ADMIN_PASSWORD / WEBHOOK_ALERTS_SECRET
bash scripts/bootstrap_demo.sh --with-demo # Windows: powershell -File scripts/bootstrap_demo.ps1 -WithDemo
python scripts/demo_routes.py a            # 约 10 秒后打印一次完整诊断
```

预期：`/health/ready` = 200、<http://localhost:8080> 出现五个导航域、路线 A 输出 4 条证据 + 3 条假设判定。
完整三步（诊断 / RAG / 审批门禁）见 [DEMO_SCRIPT.md](DEMO_SCRIPT.md)。

---

## 亮点

### 1. 干净环境一次跑通（这是本版最重要的事）

把仓库克隆到空目录、清空数据卷、只凭 `.env.example`，实测：

| 步骤 | 耗时 |
| --- | --- |
| `git clone` | 2.4s |
| `bootstrap_demo.sh --with-demo`（13 容器 + 播种） | 32.5s |
| 路线 A（真实 `docker stop` → 诊断） | 9.4s |
| 路线 B（hybrid RAG） | 9.6s |
| 路线 C（提案 → 四眼 → 执行门禁） | 10.1s |
| 两个自定义镜像 `--no-cache` 冷构建 | 54s |

「全新开发环境 30 分钟内跑通核心演示」**达成，实测约 1.5 分钟**。
前提是基础镜像已在本机（全新机器还需拉取 python/node/nginx/maven/postgres/rabbitmq，时间取决于网络）。

### 2. 修掉了 4 个只有「交给别人」才会暴露的缺陷

见 [D7-DELIVERY.md](D7-DELIVERY.md) §2 与 [FAILURE_POSTMORTEMS.md](FAILURE_POSTMORTEMS.md)：

1. **`web` 镜像里根本没有 Vue 控制台** —— 前端从 `web/static` 迁到 `web/console`，Dockerfile 没跟着改；开发机从没重建过该镜像。按 README 打开 8080 只能看到 D1 的原生页面。
2. **`demo-java` 从 D3 起编译不过** —— 源码改了但镜像没重建，旧镜像一直能跑，`--no-cache` 构建必现。
3. **容器身份被双重加前缀**（`opspilot-demo-demo-redis`）—— 导致证据链失真：停止的容器被当成「探测成功但无异常」，模型可能得出「容器没问题」的结论。
4. **`/platform` 整页空白** —— `/integrations` 返回 `{types, instances, tests}`，前端按 `{items}` 解析。

每一个都补了防回归：前两个进了构建 / 单测，第三个加 4 个容器身份单测，第四个加了 E2E「导航域渲染冒烟」。

### 3. 三条路线都能独立复现

- **A 诊断**：真实故障注入 → 4 类证据（inspect / logs / 健康检查 / HTTP）→ 结构化报告；**「不可观测」被显式建模**（`timeout` / `unavailable` + `error_code`），不是「没问题」。
- **B RAG**：8 篇已发布知识，`hybrid` 双路召回 + RRF（`k=60`）；某条命中 `ranks.text=None, vector=2`（纯向量召回）——证明不是「把文档塞进 prompt」。知识证据标注 `leads only, never root cause evidence`。
- **C 审批执行**：Runbook 幂等自举 → 提案 → dry-run **7/7 PASS** → 发起人自审 **403 FORBIDDEN** → 审批 → 执行时 `pre_check_failed`（`reason` 写明门禁原因）→ `docker inspect RestartCount=0`。**拒绝也是结构化结果，外部效果确定为零。**

### 4. 面试材料（25 问）

[interview_notes.md](interview_notes.md)：DeepAgents/LangGraph/LangChain 职责切分、AgentRuntime 与 Tool Gateway、Checkpoint 与业务库状态、证据与根因验证、pgvector/Hybrid/知识权限、Prompt Injection、Worker Lease/Fencing、Outbox/幂等、审批与恢复验证、跨语言诊断、SSE 与浏览器 E2E、评估与成本。每题都锚到源码路径，并写明「坑在哪」。

---

## 变更明细

### 新增

- `scripts/bootstrap_demo.sh` / `.ps1` —— 起栈 → 轮询 `/health/ready` → 容器内播种，`--with-demo` / `--with-knowledge` / `--all`
- `scripts/demo_routes.py` —— 三条演示路线，**纯标准库**（不依赖 `uv sync`），内置代理绕行
- `web/Dockerfile` 多阶段构建（node 构建 → nginx 托管 + `/api` 反代 `api:8000`），`NPM_REGISTRY` build arg 默认 npmmirror
- `app/domains/tools/gateway.py::resolve_container_name` —— 工具侧容器身份唯一权威：`assets.endpoints.container_name` 优先，未登记才回退 `instance.container_prefix + asset.name`（动作执行侧 `app/domains/approvals/allowlist.py` 同名函数更严：未登记直接 `None`，与 D6 门禁一致）
- 文档：`DEPLOYMENT_GUIDE.md` / `DEMO_SCRIPT.md` / `API_REFERENCE.md` / `EVALUATION_REPORT.md` / `FAILURE_POSTMORTEMS.md` / `D7-DELIVERY.md` / `interview_notes.md` / `RELEASE_NOTES.md`
- 图与截图：`docs/diagrams/*.svg`（架构 / 诊断时序 / 审批执行时序）、`docs/screenshots/*.png`（6 张真实栈截图）
- 测试：`tests/unit/test_container_identity.py`（4 例）、E2E `08 导航域渲染冒烟`

### 修复

- `run.model_config_snapshot.rag` 硬编码 `"off"`，与真实 `RAG_MODE` 不符（快照撒谎）
- compose 的 `api` 服务缺 `AGENT_*` 变量 → 快照 `provider=fake` 但报告 `generator=deepseek`
- `seed_demo.py` 在容器内连 `127.0.0.1:5432` 失败 → 三级回退 `SEED_DSN` → `DATABASE_URL` → 默认值
- `bootstrap` 就绪探测被宿主代理劫持（`HTTP_PROXY` 把 `127.0.0.1` 转给代理 → 502 假失败）
- 路线 C 在干净环境因「没有已发布 Runbook」中止 → 幂等自举
- 审批人 ID 被 `slice(0,14)` 截断、执行时间列折行
- `tests/conftest.py` 钉死测试基线，防开发者本地 `.env`（deepseek/hybrid）污染

### 删除

- `web/static/`（D1 原生 JS 页面，已被 Vue 控制台取代）+ 对应 `.dockerignore`

### 未改动（有意为之）

- **没有新增业务功能、没有引入新框架、没有改 D0–D6 冻结契约**
- **没有放宽** `ACTION_EXECUTION_ENABLED=false` / `ACTION_ISOLATED_DAEMON_ACK=false`
- 控制台仍无 runs 列表端点（查看历史诊断用 `?run=<run_id>`）

---

## 质量基线

| 项 | 结果 |
| --- | --- |
| 全量 pytest | **347 passed / 3 skipped**（300s，真实 PostgreSQL + 真实 RabbitMQ） |
| 确定性评估 | **39/39**（fault-core 12 / security-gates 13 / rag-ablation 6 / reliability-recovery 8） |
| 浏览器 E2E | **8/8**（24.6s） |
| Fake 演示 | 39/39 |
| ruff / ruff format / mypy | 全绿（213 files / 129 source files） |
| `vue-tsc --noEmit` | 通过 |
| GitHub CI | `CI` 40860768658 ✅、`d6` 40860768704 ✅ |

> 基线从 D6 的 343 + E2E 7 变为 **347 + E2E 8**，两处增量都是**回归防护**，不是功能。

---

## 已知边界（引用本节即可，不必另找）

1. **不是生产系统**：无 TLS、无 KMS/语钥管理、单实例数据库、审计表可写不可验（无 WORM/哈希链）。逐项见 [DEPLOYMENT_GUIDE.md](DEPLOYMENT_GUIDE.md) §9 与 [SECURITY_ACCEPTANCE.md](SECURITY_ACCEPTANCE.md) §12。
2. **真实写操作默认关闭**，且宿主 `docker.sock` 不是隔离环境——这是**有意保留**的安全默认值，不是没做完。
3. **`10/10` 是「固定案例规则通过率」**：10 例、单模型、单次运行、基础设施为确定性 stub，**不是泛化根因准确率**。
4. **成本未计算**：provider 不返回计价 → 报告写 `unavailable`，不编造金额。
5. **无多租户**：单一组织，靠 `asset_access_policies` 做用户/角色级资产范围。
6. 演示账号 `E2ePassw0rd!2026` 为演示/测试专用，**生产部署前必须删除或改密**。

---

## 下一阶段

P1 未启动。本版到此为止。
