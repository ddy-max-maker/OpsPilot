# 上手试用：三条流程照着点

> 面向"我想自己点一遍"。全流程在浏览器里完成，只有第 6 节需要命令行。
> 环境若还没起：`bash scripts/bootstrap_demo.sh --with-demo`（详见 [DEPLOYMENT_GUIDE.md](DEPLOYMENT_GUIDE.md)）。

---

## 0. 现在就能用

| 入口 | 地址 |
| --- | --- |
| 控制台 | <http://localhost:8080> |
| API 文档 | <http://localhost:8000/docs> |
| RabbitMQ 管理 | <http://localhost:15672>（`opspilot` / 见 `.env`） |

---

## 1. 三个账号，权限不一样

密码统一 `E2ePassw0rd!2026`。

| 账号 | 角色 | 能干 | 不能干 |
| --- | --- | --- | --- |
| `e2e_operator` | operator | 登记故障、启动诊断、建提案、建知识 | 审批提案、看审计 |
| `e2e_approver` | approver | 审批别人的提案 | — |
| `e2e_admin` | admin | 知识复核/发布、审计、评估、平台 | — |
| `e2e_viewer` | viewer | 只读 | 任何写操作 |

**流程二和流程三必须用两个账号**——四眼原则是服务端强制的，自己批自己会 403。

---

## 2. 流程一：故障诊断（约 1 分钟）

目标：看到「结论有证据撑着」，以及「观测不到」被明确标出来，而不是被当成「没问题」。

1. 用 `e2e_operator` 登录 → **工作台与事件**
2. 点 **`+ 登记故障`**，填：
   - 标题：`试用：Redis 不可达`
   - 环境：`sandbox`
   - 主资产：选 **`redis（sandbox）`** ← 列表里那一堆 `e2e-svc-*` 是历史 E2E 残留，忽略
   - 严重度：`high`
   - 描述：随便写
3. 回到列表，点该行右侧 **`确认`**（状态 `open` → `acknowledged`）
4. 点标题进入详情 → 右上角 **`启动 Agent 诊断`**
5. 等 **10–30 秒**（真实 DeepSeek 推理），页面会实时推送事件流

**该看到什么**（这是重点）：

| 看这里 | 预期 | 说明 |
| --- | --- | --- |
| 结论 | 容器 `exited`、`exit_code=0`、收到 SIGTERM 优雅关闭 | 带 `next_manual_steps`，会说「谁发起的停止无法确定」 |
| 假设 | **三种状态同时出现**：`supported` / `insufficient_data` / `refuted` | 不是二元对错，这是设计 |
| 证据 | 4–6 条，注意 `redis.health` = **timeout**、`http.health_check` = **unavailable** | 采集失败 ≠ 服务健康，两者语义分开 |
| 假设里被 `refuted` 的那条 | 「连接池耗尽」——来自历史知识 | 历史经验提出方向，被实时证据否掉 |

> **记住这个页面的 run 标识**（面板标题 `诊断运行 run_xxx`，可直接选中复制）。流程三要用。

---

## 3. 流程二：知识治理与检索（约 2 分钟，需要两个账号）

目标：看到「知识要人工复核才能被 Agent 检索」和「检索真的是语义的」。

### 3.1 建一篇知识（`e2e_operator`）

1. **知识中心** → 点 **`+ 新建知识`**
2. 类型 `postmortem`，标题 `试用：Redis 连接被拒处置`，内容写：
   `Redis 连接被拒绝时，先确认端口监听与 maxclients，再排查连接池泄漏。`
3. 保存后它是 **`draft`** 状态——此时**检索不到**（Agent 只认已发布版本）

### 3.2 复核并发布（换 `e2e_admin`）

1. 退出 → `e2e_admin` 登录 → **知识中心**
2. 找到刚建的文档，版本行上点 **`复核`**（`draft` → `reviewed`），再点 **`发布`**（→ `published`）

> 两点值得注意：① 只有 admin 能看到「复核 / 发布」按钮，创建人自己没有发布权；
> ② 复核人不能等于创建人（服务端校验，报 four-eyes violation）——所以用 admin 去复核
> **admin 自己建的**文档会被拒绝，必须换人。

### 3.3 检索：两个查询看差异

在检索框里依次输入下面两个，**注意「全文」「向量」两列**：

| 查询 | 全文列 | 向量列 | 说明 |
| --- | --- | --- | --- |
| `缓存服务连不上，下游接口大面积超时` | **全是 `—`** | 1、2、3… | 字面零重叠（"缓存"≠"Redis"），**纯语义召回** |
| `Redis 连接池耗尽 maxclients` | 1、2、6… | 2、1、3… | 两路都召回，交错排序 |

两列都出现且排名不同 = RRF 融合在工作（`score = Σ 1/(60+rank)`，`k=60`）。

> 首次检索可能要 2–4 秒（本地 e5 模型加载），之后 50–100ms。

---

## 4. 流程三：提案 → 审批 → 执行门禁（约 2 分钟，需要两个账号）

目标：看到 Agent **不能自己动手**，而且「不执行」也是结构化结果。

1. `e2e_operator` 登录 → **审批与恢复** → **`+ 新建方案`**，填：
   - 诊断 Run：**粘贴流程一的 run_id**（`run_01...`）
   - Runbook 版本：选 **`D5 演示 Redis 受控重启 v1（r1）`**（下拉里一堆 `e2e-restart-*` 是 E2E 残留，别选）
   - 目标资产：`redis（sandbox）`
   - 目标环境：`sandbox`
   - 参数 JSON：`{"container_name": "opspilot-demo-redis"}`
2. 进入方案详情 → 点 **`Dry-run 前置检查`** → 应显示 **7 项全 PASS**
3. 观察按钮：以 operator 身份**看不到 `批准（四眼）`**——前端按「角色 + 是否发起人」先挡一层
4. 退出，换 `e2e_approver` 登录 → 同一个方案 → 备注随便写 → 点 **`批准（四眼）`**
5. 换回 `e2e_operator` → 点 **`执行恢复（sandbox/staging）`**

**该看到什么**：

| 结果 | 值 | 为什么 |
| --- | --- | --- |
| 方案状态 | `execution_failed` | 期望内 |
| 执行记录 | `pre_check_failed`，`external_ref=null` | 门禁在执行前拦下 |
| 原因 | `action execution is disabled (demo-only guard)` | `ACTION_EXECUTION_ENABLED=false` 是安全默认值 |
| 恢复验证 | 「尚未验证」 | 执行没发生，没有可验证对象 |

**外部效果确实为零**——命令行核验：

```bash
docker inspect opspilot-demo-redis --format 'Running={{.State.Running}} RestartCount={{.RestartCount}}'
# RestartCount 不会增加；StartedAt 也不会变（真重启过的话这个时间会跳）
```

6. 最后用 `e2e_admin` 看 **平台与质量 → 审计**：能查到 `proposal.created` → `proposal.approved` → `execution.pre_check_failed` 的完整链路，以及每一次 `tool.invoke`。

---

## 5. 下面这些「失败」是**设计如此**，不是 bug

| 你看到的 | 真实含义 |
| --- | --- |
| 执行永远 `pre_check_failed` | 真实写操作默认关闭。宿主 `docker.sock` 不是隔离环境，**故意不放行** |
| 自己批自己 | **两层拦截**：界面上按钮不出现（前端按角色+发起人判断）；绕过前端直调 API 也是 403（服务端裁决） |
| admin 复核自己建的文档 → 被拒 | 四眼：复核人 ≠ 创建人 |
| 证据里 `timeout` / `unavailable` | 采集失败或观测盲区，**不等于「服务正常」** |
| 假设 `insufficient_data` | 深层根因证据不够，系统拒绝瞎猜 |
| 事件详情页说「查不到历史诊断」 | 接口没有「事件→run」反查（契约冻结），不是没诊断过；用 `?run=<run_id>` 或「诊断详情」页粘贴 run 标识 |
| 打开 `平台与质量 → 审计` 报 403 | 审计只对 admin 开放 |
| `e2e-svc-*` 一堆资产 | 历史 E2E 残留，演示请选 `redis` / `mysql` / `java` 等 `as_demo_*` 资产 |

---

## 6. 想看真实故障注入（命令行，可选）

界面走的是「登记一个故障」，如果想让容器真的挂掉再诊断：

```bash
# 注入：真 docker stop opspilot-demo-redis
python scripts/demo_routes.py a

# 提案 → 四眼 → 门禁（会自动跑一次前置诊断）
python scripts/demo_routes.py c

# 撤销注入，恢复演示容器
python scripts/demo_routes.py recover
```

Windows 用 `.venv/Scripts/python.exe scripts/demo_routes.py a`。
脚本是纯标准库，不依赖 `uv sync`；输出里的耗时、token、证据条数都是实测值。

---

## 7. 卡住了先看这里

| 现象 | 处理 |
| --- | --- |
| 控制台打不开 | `docker compose ps` 看 `opspilot-web`；`curl --noproxy '*' localhost:8080` |
| API 502 / 连不上 | 宿主有 `HTTP_PROXY` 时一律加 `--noproxy '*'` |
| 改了前端看不到变化 | **`docker compose build web && docker compose up -d web`**——`up -d` 不会重建镜像 |
| 诊断一直 queued | `docker compose logs worker --tail 50`，看 Worker 是否在消费 |
| 知识发布很慢（>10s） | 本地 e5 模型首次加载，之后正常 |
| 检索无命中 | 确认查询环境下有已发布知识；`draft` 对 Agent 不可见 |
