# D7 交付报告：作品交付、可复现部署与面试材料

> 状态：**D7 完成（2026-10-29）**。
> 起点 `c0e81db`（D6 收尾，343 passed / 3 skipped、39/39、E2E 7/7、CI 全绿）。
> 终点 **Tag `v0.7.0-d7`**（本文件所在提交）。
> 本轮**没有新增任何业务功能或框架**，只做交付、修复与文档。

---

## 1. 一句话结论

一个陌生人 `git clone` 后 **一条命令 32 秒** 起完整栈、**45 秒内**跑出第一次真实诊断；
三条演示路线（诊断 / RAG / 审批执行门禁）全部可复现；
文档、三张图、六张真实截图、25 条面试追问全部落到具体文件。
过程中**暴露并修复了 5 个真实缺陷**（3 个只有干净环境才能发现、1 个前端整页白屏、1 个现场演示时才暴露）。

---

## 2. 本轮修复的真实缺陷（这是 D7 最有价值的部分）

| # | 缺陷 | 为什么会存在 | 影响 | 验证 |
| --- | --- | --- | --- | --- |
| 1 | `web` 镜像只托管 D1 的原生 JS 页面，**完全没有 Vue 控制台** | 前端从 `web/static` 换成 `web/console`，但 Dockerfile 没跟着改；开发机从没重建过该镜像 | 按 README 打开 8080 看不到控制台，等于交付缺半个产品 | 干净环境实测；改为多阶段构建（node 构建 → nginx 托管 + `/api` 反代） |
| 2 | `demo-java` 从 D3 起**编译不过**（`cannot find symbol: ResponseEntity`） | 源码改了但镜像没重建，旧镜像一直能跑 | 跨栈诊断（Java 503）在全新环境无法演示 | `--no-cache` 构建必现；补 import |
| 3 | 容器身份被**双重加前缀**（`opspilot-demo-demo-redis`） | 资产已登记 `endpoints.container_name`，代码又叠加了实例前缀 | **证据链失真**：停止的容器被当成「探测成功但无异常」，模型可能得出「容器没问题」 | 4 个单测（`tests/unit/test_container_identity.py`）；统一为「资产登记优先」 |
| 4 | `/platform` 页**整页空白** | `/integrations` 返回 `{types, instances, tests}`，前端按 `{items}` 解析 → `undefined.length` 抛错 | 五个导航域坏了一个，且没有任何用例拦住 | 修客户端契约 + 新增 E2E `08 导航域渲染冒烟` |
| 5 | 事件详情页把「查不到历史诊断」写成「**本事件尚未发起诊断**」（D7 现场演示时发现） | `GET /incidents/{id}` 不返回任何关联 run 字段（契约冻结未新增），前端靠 `?run=` / localStorage 恢复；都没有时直接断言「没诊断过」 | 把「看不见」说成「不存在」——与第 3 条同源，只是发生在文案层；运维会重复发起诊断 | 改为如实陈述 + 指明既有出口；同轮全局扫描 `slice(0,14)`，又修掉 `ApprovalsView` 发起人截断、`AuditView` 三处 ID 截断与时间折行 |

另外修掉的一致性/体验问题：

- `run.model_config_snapshot.rag` 硬编码为 `"off"`，与真实 `RAG_MODE` 不符（快照撒谎）；
- `api` 容器缺 `AGENT_*` 环境变量 → 快照 `provider=fake` 但报告 `generator=deepseek`（同一份数据自相矛盾）；
- `seed_demo.py` 在容器内连 `127.0.0.1:5432` 失败 → 支持 `SEED_DSN` / `DATABASE_URL`；
- `bootstrap` 的就绪探测走宿主代理 → 一律 `--noproxy '*'` / `HttpWebRequest.Proxy = null`；
- 演示脚本依赖 httpx、需要 `uv sync` → 改为**纯标准库**，干净环境零依赖；
- 审批人 ID 被 `slice(0,14)` 截成 `usr_e2e_approv`；执行时间列在窄列折行；
- 开发者本地 `.env`（deepseek/hybrid）泄漏进测试基线 → `tests/conftest.py` 钉死测试配置。

全部细节与定位过程见 [FAILURE_POSTMORTEMS.md](FAILURE_POSTMORTEMS.md)。

---

## 3. 干净环境验收（`git clone` 到空目录重跑）

环境：`D:\d7-clean\OpsPilot`（从本仓库 `git clone`），`docker compose --profile demo down -v` 清空数据卷，
`.env` 由 `.env.example` 全新生成。

| 步骤 | 实测 | 说明 |
| --- | --- | --- |
| `git clone` | **2.4s** | 本地路径克隆，不含网络 |
| `bash scripts/bootstrap_demo.sh --with-demo` | **32.5s** | 13 容器启动 + 连接器/资产/账号播种；**宿主机不需要 Python/Node** |
| `/health/ready` | 200 | 迁移在 api 启动时自动执行 |
| 控制台 `:8080` | 200 | Vue 控制台（修复后） |
| `python scripts/demo_routes.py a` | **9.4s** | 真实 `docker stop` + 真实证据采集（fake 模型） |
| `seed_knowledge.py`（8 篇，含 e5 加载） | **21s** | |
| `python scripts/demo_routes.py b` | **9.6s** | `RAG_MODE=hybrid`，RRF 排名可见 |
| `python scripts/demo_routes.py c` | **10.1s** | Runbook 幂等自举 + 7/7 dry-run PASS + 403 四眼 + 门禁拒绝 |
| 两个自定义镜像 `--no-cache` 冷构建 | **54s** | web（npm ci）+ demo-java（maven 拉依赖） |

**「全新环境 30 分钟内跑通核心演示」：达成，实测约 1.5 分钟**
（克隆 → 起栈 → 三条路线）。前提是基础镜像已在本机；全新机器需额外拉取
`python:3.12-slim` / `node:22-alpine` / `nginx:1.27-alpine` / `maven` / `postgres` / `rabbitmq` 等，
时间取决于网络——这一点在 DEMO_SCRIPT §6 里如实标注，不藏在脚注里。

---

## 4. 三条演示路线的实测结论

| 路线 | 关键实测 | 证明了什么 |
| --- | --- | --- |
| **A 诊断** | fake：4 条证据（`docker.inspect`/`docker.logs` = ok，`redis.health` = timeout，`http.health_check` = unavailable + error_code）；DeepSeek：8–9 条证据、`tokens_in≈24,136 / out≈2,255`、假设 #1 `supported` / #2 `refuted`(OOM) / #3 `insufficient_data` | 结论可追溯到证据；「不可观测」被显式建模，不是「没问题」 |
| **B RAG** | 8 篇已发布知识；`mode=hybrid`、命中 5 条、`latency 46ms`；某条 `ranks.text=None, vector=2`（纯向量召回）；知识证据 2 条，标注 `leads only, never root cause evidence` | RAG 不是「把文档塞进 prompt」：有权限前置过滤、有双路召回、有版本 |
| **C 审批执行** | Runbook 幂等自举 → 提案 → dry-run **7/7 PASS** → 发起人自审 **403 FORBIDDEN** → `e2e_approver` 审批 → 执行 `pre_check_failed`，`reason` 写明门禁原因 → 提案 `execution_failed`；`docker inspect RestartCount=0` | Agent 不能自己动手；拒绝也是**结构化结果**，外部效果确定为零 |

C 路线的真实写动作按任务书要求**未开启**：宿主 `docker.sock` 不是隔离环境，
`ACTION_EXECUTION_ENABLED=false` + `ACTION_ISOLATED_DAEMON_ACK=false` 是 D6 冻结的安全默认值，
D7 **没有做任何放宽**。`verified` 全链路证据由集成测试
（`tests/integration/test_action_flow.py`，13 例）提供。

---

## 5. 文档与素材交付清单

| 文件 | 内容 | 状态 |
| --- | --- | --- |
| `README.md` | 重写为交付视角：能跑出什么、60 秒起步、三条路线在证明什么、架构与边界 | ✅ |
| `docs/DEPLOYMENT_GUIDE.md` | Windows(Docker Desktop) / Linux、环境变量必改项、端口表、排障、清理、生产缺口 | ✅ 新增 |
| `docs/DEMO_SCRIPT.md` | 三条路线的命令 / 预期输出 / 恢复清理 + 实测耗时 | ✅ 新增 |
| `docs/API_REFERENCE.md` | 由运行中服务的 `openapi.json` 核对生成（49 路径 / 56 操作 / 73 schema）、统一包裹、错误码、幂等、SSE | ✅ 新增 |
| `docs/EVALUATION_REPORT.md` | 三层证据分级、10 例逐案人工语义复核、命名纪律、样本量/Token/延迟/成本缺失说明、适用边界 | ✅ 新增 |
| `docs/FAILURE_POSTMORTEMS.md` | 13 个真实故障的现象/根因/定位/修复/防回归 | ✅ 新增 |
| `docs/SECURITY_ACCEPTANCE.md` | D6 内容保留 + 补 D7 身份/RBAC、只读防线、注入、Webhook、密钥日志、**适用边界总表** | ✅ 扩充 |
| `docs/ARCHITECTURE.md` | 三张 SVG 图嵌入（保留 mermaid 版便于 diff） | ✅ 更新 |
| `docs/interview_notes.md` | 25 个追问，每题含源码锚点与「坑」 | ✅ 新增 |
| `docs/diagrams/*.svg` | 总体架构 / 故障诊断时序 / 审批与执行时序 | ✅ 新增 |
| `docs/screenshots/*.png` + README | 6 张真实栈截图（工作台 / 知识中心 / 证据与报告 / 审批门禁 / 方案列表 / 平台能力） | ✅ 新增 |
| `docs/RELEASE_NOTES.md` | `v0.7.0-d7` 发布说明（验证路径 / 亮点 / 变更明细 / 质量基线 / 已知边界） | ✅ 新增 |
| `docs/D7-DELIVERY.md` | 本文件：交付结论、缺陷表、干净环境验收、核查表、边界 | ✅ 新增 |

---

## 6. 测试与质量现状

| 项 | 结果 |
| --- | --- |
| 全量 pytest | **347 passed / 3 skipped**（300s，真实 PostgreSQL + 真实 RabbitMQ） |
| 确定性评估 | **39/39**（fault-core 12 / security-gates 13 / rag-ablation 6 / reliability-recovery 8），`docs/eval/fake_ci_report_judge_v2.json` @ `8f1dcfa` |
| 浏览器 E2E | **8/8（24.6s）**——D6 的 7 例 + D7 新增「导航域渲染冒烟」 |
| Ruff / Ruff format / mypy | 全绿（213 files formatted / 129 source files no issues） |
| 前端类型 | `vue-tsc --noEmit` 通过 |
| GitHub CI | `CI` **40860768658 success**（lint + 迁移演练 + pytest）、`d6` **40860768704 success**（39 例评估 + 前端构建 + E2E） |
| 真实模型评估 | 沿用 D6 终态：固定案例规则通过率 10/10（judge-v2），无新增运行 |

> 测试基线变化：343 → **347**（+4 容器身份单测）；E2E 7 → **8**（+1 导航冒烟）。
> 两处都是**回归防护**，不是功能。

---

## 7. 密钥、日志、账号、Git 历史核查

| 检查 | 结果 |
| --- | --- |
| `.env` / `.env.local` 是否入库 | ❌ 未入库（`git ls-files` 为空；`.gitignore` + `.dockerignore` 双覆盖） |
| 历史中是否出现过 `.env` | ❌ 无 |
| 真实 Key 扫描（`sk-…` / `AKIA…` / 私钥块，工作区 + 近 60 个提交） | 仅文档中的占位符 `sk-...` / `sk-你的key`，**无真实凭据** |
| 镜像是否带 `.env` | ❌ 不带（`.dockerignore` 排除 `.env`、`docs`、`tests`） |
| 日志是否打印密码/token | ❌ 无；审计 detail 只存业务字段；工具载荷落库前经 `redact_payload` 脱敏 |
| 演示账号密码 | `E2ePassw0rd!2026` 硬编码在 `seed_e2e_users.py` / `demo_routes.py`——**演示/测试专用**，已在安全验收 §11 标注「生产部署前必须删除或改密」 |
| 截图是否含敏感数据 | ❌ 不含：资产名 `as_demo_redis`、容器名 `opspilot-demo-*`、账号 `e2e_*` 全部由种子脚本生成 |

---

## 8. 交付边界（不得超范围引用）

1. **不是生产系统**：无 TLS、无语钥管理/KMS、单实例数据库、审计表可写不可验（无 WORM/哈希链）。
   逐项清单在 [DEPLOYMENT_GUIDE.md](DEPLOYMENT_GUIDE.md) §9。
2. **真实写操作默认关闭**，且宿主 socket 不是隔离环境——这是**有意保留**的安全默认值。
3. **`10/10` 是「固定案例规则通过率」**：10 例、单模型、单次运行、基础设施为确定性 stub；
   不是泛化根因准确率。命名纪律见 [EVALUATION_REPORT.md](EVALUATION_REPORT.md) §0。
4. **成本未计算**（provider 不返回计价）→ 报告写 `unavailable`，不编造金额。
5. **控制台无 runs 列表端点**：查看历史诊断需 `?run=<run_id>`（契约冻结，D7 未新增端点）。
6. **无多租户**：单一组织，`asset_access_policies` 做用户/角色级资产范围。

---

## 9. 本轮不做的事（明确声明）

- 不新增业务功能、不引入新框架、不改 D0–D6 冻结契约（唯一接口层面变化是**前端按既有契约修正解析**）；
- 不放宽 `ACTION_EXECUTION_ENABLED` / `ACTION_ISOLATED_DAEMON_ACK`；
- 不公开任何含敏感数据的演示素材（截图已逐张核对）；
- **不开启 P1 新功能开发**。

---

## 10. 复现命令（复制即用）

```bash
git clone <repo> OpsPilot && cd OpsPilot
cp .env.example .env          # 改 JWT_SECRET / BOOTSTRAP_ADMIN_PASSWORD / WEBHOOK_ALERTS_SECRET
bash scripts/bootstrap_demo.sh --with-demo     # Windows: powershell -File scripts/bootstrap_demo.ps1 -WithDemo
python scripts/demo_routes.py a                # 诊断
# 可选：RAG 路线
sed -i 's/^RAG_MODE=off/RAG_MODE=hybrid/' .env && docker compose up -d api worker
docker compose exec -T api python scripts/demo/seed_knowledge.py
python scripts/demo_routes.py b
python scripts/demo_routes.py c                # 审批与执行门禁
python scripts/demo_routes.py recover          # 撤销故障注入
```

验收断言（应全部成立）：

- `curl --noproxy '*' localhost:8000/api/v1/health/ready` → 200
- 控制台 <http://localhost:8080> 能看到五个导航域
- 路线 C 输出 `dry-run … 全部 PASS`、`发起人自审批 → 403 FORBIDDEN`、`pre_check_failed`
- `docker inspect opspilot-demo-redis --format '{{.RestartCount}}'` → `0`
