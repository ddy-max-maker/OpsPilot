# 部署指南（DEPLOYMENT_GUIDE）

面向两种读者：想在**自己的机器上跑起来看**的人，和想知道**每一层靠什么隔离**的人。
一条命令起步看 [DEMO_SCRIPT.md](DEMO_SCRIPT.md)；本文解释命令背后的东西和踩坑。

---

## 1. 前置条件

| 项 | 要求 | 校验命令 |
| --- | --- | --- |
| Docker | Engine 24+ / Docker Desktop 4.x（Windows 走 WSL2 后端） | `docker version` |
| Docker Compose | v2（`docker compose` 子命令，不是 `docker-compose`） | `docker compose version` |
| 内存 | Docker 可用内存 **≥ 6 GB**（13 容器实测峰值约 2.7 GB，留余量给构建） | `docker info --format '{{.MemTotal}}'` |
| 磁盘 | ≥ 10 GB（镜像 + 卷；`api` 镜像含 torch/sentence-transformers，约 2–3 GB） | — |
| 端口 | 8000 / 8080（必需）；5432 / 5672 / 15672 / 8081 / 8082 / 9090 / 16686 / 4318 / 3307（演示与可观测） | 见 §5 |
| 网络 | 构建期需访问 pypi 镜像（清华源）、npm 镜像（npmmirror）、maven 镜像（aliyun）、HF 镜像（hf-mirror） | — |

**宿主机不需要 Python 或 Node。** 种子脚本在 `api` 容器内跑；前端在镜像构建阶段编译。
只有想跑 `make test` / `uv run pytest` 时才需要本地 Python 3.12 + uv。

---

## 2. Windows（Docker Desktop）

```powershell
git clone <repo> OpsPilot; cd OpsPilot
copy .env.example .env
notepad .env            # 至少改 JWT_SECRET / BOOTSTRAP_ADMIN_PASSWORD / WEBHOOK_ALERTS_SECRET
powershell -ExecutionPolicy Bypass -File scripts/bootstrap_demo.ps1 -WithDemo
```

也可以直接用 Git Bash 跑同款 shell 脚本：`bash scripts/bootstrap_demo.sh --with-demo`。

Windows 上的三个具体注意点：

1. **路径不要含中文和空格**（本仓库的开发路径恰好含中文，能跑；但 Docker 卷挂载与
   `demo-java` 的 maven 构建在极少数环境会对非 ASCII 路径敏感，遇到怪问题先换路径排除）。
2. **WSL2 后端 + 8 GB 内存上限**是默认值；Docker Desktop 设置里把 Memory 调到 ≥ 6 GB。
3. **执行策略**：`-ExecutionPolicy Bypass` 只对本次进程生效，不改系统设置。

---

## 3. Linux

```bash
git clone <repo> OpsPilot && cd OpsPilot
cp .env.example .env && vi .env      # 改三个密钥
bash scripts/bootstrap_demo.sh --with-demo
```

非 root 用户需在 `docker` 组内（`sudo usermod -aG docker $USER` 后重新登录）。
若用 rootless Docker，`ops-action` 与 `docker-ro-proxy` 挂载的 socket 路径需改为
`$XDG_RUNTIME_DIR/docker.sock`——compose 里是**硬编码**的
`/var/run/docker.sock`（两处：`ops-action` 读写、`docker-ro-proxy` 只读），需自行改挂载。

---

## 4. 环境变量：哪些必须改、哪些能不懂

### 4.1 必须改（生产或对外暴露时）

| 变量 | 为什么不改就是事故 |
| --- | --- |
| `JWT_SECRET` | 签发 token 的密钥；默认值公开在仓库里，等于人人可伪造身份 |
| `WEBHOOK_ALERTS_SECRET` | 告警接入的 HMAC 密钥；泄露即可伪造任意告警 |
| `BOOTSTRAP_ADMIN_PASSWORD` | 初始 admin；首登强制改密能兜底，但不该依赖兜底 |
| `RABBITMQ_PASSWORD` | 消息总线凭据 |

### 4.2 演示相关（有默认值，通常不用动）

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `RAG_MODE` | `off` | `off` / `text-only` / `hybrid`；改完必须 `docker compose up -d api worker` |
| `EMBEDDING_PROVIDER` | `local_e5` | `local_e5`（384 维，需走 HF 镜像下载）/ `fake`（确定性哈希，CI 用） |
| `HF_ENDPOINT` | `https://hf-mirror.com` | 模型下载端点；海外可改官方地址 |
| `AGENT_PROVIDER` | `fake` | `fake` / `deepseek` / `anthropic` / `google` |
| `AGENT_FAKE_PROVIDER_ENABLED` | `true` | 是否允许内置假模型；**生产应置 `false`**，缺凭据就直接报错而不是偷偷降级 |
| `AGENT_PROMPT_VERSION` | `d6-v2` | 随 run 落快照，用于复现对比 |
| `ACTION_EXECUTION_ENABLED` | `false` | 真实写操作总闸，**默认关闭** |
| `ACTION_ISOLATED_DAEMON_ACK` | `false` | 隔离环境确认，与上一项同时为真才真正放行 |
| `NPM_REGISTRY` | `https://registry.npmmirror.com` | 控制台镜像构建源 |
| `DEMO_API` / `DEMO_PASSWORD` | `http://127.0.0.1:8000` / `E2ePassw0rd!2026` | 演示脚本用 |

### 4.3 与容器/凭据相关的几个

| 变量 | 作用 |
| --- | --- |
| `DATABASE_URL` | API 侧连接串（容器内主机名 `postgres`） |
| `SEED_DSN` | 种子脚本连接串；留空则取 `DATABASE_URL`（去 `+asyncpg`）再退 `127.0.0.1:5432` |
| `ACTION_DOCKER_SOCKET` | 受控恢复专用 socket；留空用容器内默认 `/var/run/docker.sock` |
| `D3_MYSQL_RO_PASSWORD` | MySQL 只读诊断账号；连接器经 `password_env` 间接引用，**不明文入库** |
| `RABBITMQ_URL` | 显式覆盖连接串（不填则由 user/password 拼） |

---

## 5. 端口表

| 端口 | 服务 | profile | 用途 |
| --- | --- | --- | --- |
| 8000 | `api` | 核心 | REST + SSE + `/docs` |
| 8080 | `web` | 核心 | Vue 控制台（nginx 托管 + `/api` 反代） |
| 5432 | `postgres` | 核心 | 业务库 + 知识库 + pgvector + langgraph schema |
| 5672 / 15672 | `rabbitmq` | 核心 | AMQP / 管理界面（`opspilot` / `RABBITMQ_PASSWORD`） |
| 8081 | `demo-gateway` | demo | 演示网关（S4 上游故障场景） |
| 8082 | `demo-java` | demo | Java Spring Boot 演示服务（跨语言诊断） |
| 3307 | `demo-mysql` | demo | MySQL 演示实例（宿主端口避让 3306） |
| 9090 | `prometheus` | demo | 指标源 |
| 16686 / 4318 | `jaeger` | demo | UI / OTLP-HTTP 接入 |

端口冲突时改 `.env` 或 compose 的宿主侧映射即可（容器内端口不要动，连接器配置依赖它）。

---

## 6. 常用命令

```bash
make bootstrap        # = bash scripts/bootstrap_demo.sh
make up / make down   # 起停核心栈
make up-demo          # 追加 demo profile
make ps / make logs   # 状态 / 日志
make seed             # 资产与账号（幂等）
make knowledge        # 8 篇历史知识（幂等，需 RAG_MODE!=off）
make test             # 全量 pytest（约 4 分钟）
make eval-39          # 39 例确定性评估
make e2e              # Playwright 浏览器 E2E（7 例）
make lint / make typecheck / make format
```

`make` 在 Windows Git Bash 下可用（`VENV_PY` 会自动选 `.venv/Scripts/python.exe`）；
没有 make 就直接跑 `scripts/` 与 `uv run` 里的原始命令。

---

## 7. 排障：报告里出现过的真实问题

| 症状 | 原因 | 处理 |
| --- | --- | --- |
| 打开 8080 是旧版极简页面 | `web` 镜像是旧构建 | `docker compose build --no-cache web && docker compose up -d web` |
| `curl localhost:8000` 返回 502 | 宿主 `HTTP_PROXY` 把 127.0.0.1 也交给代理 | 加 `--noproxy '*'`；脚本已内置绕过 |
| `bootstrap` 一直等到 180s 未就绪 | 同上（旧版脚本）/ 端口被占 / DB 迁移失败 | 先 `docker compose logs api`；确认端口未占 |
| `demo-java` 构建失败 `cannot find symbol` | 源码改动但镜像未重建 | `docker compose build --no-cache demo-java` |
| `seed_demo.py` 报 `127.0.0.1:5432` 拒绝 | 在容器内跑却用了宿主 DSN | 设 `SEED_DSN` 或直接 `docker compose exec -T api ...`（会自动用容器内 `DATABASE_URL`） |
| 发布知识要等 30 秒 | 嵌入模型每次请求重新加载（D6 已修） | 确认代码含单例 + 预热；首次启动仍有一次加载 |
| 检索返回 `KNOWLEDGE_WARMING` | 嵌入预热未完成 | 等预热完成再发起诊断；或预热后重跑 |
| 全量测试偶发 2 例红 | 本地 `.env` 的 `RAG_MODE`/`AGENT_PROVIDER` 泄漏进测试 | 已由 `tests/conftest.py` 钉死；若仍复现，检查是否覆盖了 `setdefault` |
| `npm ci` 拉不到包 | 公司网络墙了 npmmirror | `docker compose build --build-arg NPM_REGISTRY=https://registry.npmjs.org web` |
| HF 模型下载失败 | hf-mirror 不可达 | 改 `HF_ENDPOINT`；或 `EMBEDDING_PROVIDER=fake` 先跑通主链路 |

---

## 8. 清理与重置

```bash
# 只停服务，保留数据
docker compose --profile demo down

# 彻底重置（删除数据卷：业务库、pgvector、checkpoint、HF 模型缓存）
docker compose --profile demo down -v --remove-orphans
bash scripts/bootstrap_demo.sh --with-demo
```

`-v` 会删掉 `pgdata` 与 `hf-cache` 两个卷。删 `hf-cache` 意味着下次要重新下载嵌入模型
（约 1 分钟，取决于网络）。

---

## 9. 生产可用性边界（必须与「已验证」区分开）

本项目的定位是**可运行、可验证、可审计的工程作品**，不是开箱即用的生产系统。以下项目
在代码里做了正确的事，但**没有**做生产级加固：

| 项 | 现状 | 生产还缺什么 |
| --- | --- | --- |
| TLS | 无；全部 HTTP | 反向代理终止 TLS、HSTS、证书轮换 |
| 密钥管理 | `.env` 文件 | KMS / Vault、密钥轮换、最小权限下发 |
| 数据库 | 单实例，无备份 | 主从/托管库、PITR 备份、连接池上限调优 |
| 消息队列 | 单节点 RabbitMQ | 镜像队列/仲裁队列、磁盘告警、死信策略运维 |
| 可观测 | Prometheus + Jaeger（演示级） | 集中日志、告警规则、SLO 与容量规划 |
| 多租户 | 单一组织 | 租户隔离、配额、跨租户越权测试 |
| 动作执行 | 默认 `false`，宿主 socket 不是隔离环境 | 独立专用 Daemon / 远端执行器 + 网络级隔离 |
| 备份恢复演练 | 未做 | 定期恢复到演练环境并验证 RTO/RPO |

D6 起真实写操作默认关闭（`ACTION_EXECUTION_ENABLED=false` + `ACTION_ISOLATED_DAEMON_ACK=false`），
D7 未做任何放宽——**这是有意保留的安全默认值，不是未完成项**。
