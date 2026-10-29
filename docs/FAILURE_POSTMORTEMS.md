# 故障复盘（FAILURE_POSTMORTEMS）

只记录**真实发生过**的缺陷：有现象、有定位过程、有修复、有防回归措施。
按时间倒序，最近的在最前。每条都尽量给出当初会被误判成什么——那才是真正值钱的部分。

---

## 1. 干净环境里打开 8080 看到的是 D1 的旧页面（D7）

**现象**：按 README 走完一键启动，浏览器打开 `http://localhost:8080`，看到的是 D1 时期的原生
JS 页面（只有事件列表），D6 的 Vue 控制台完全不见。

**影响**：交付物「看起来」缺了整个前端。这是最容易被忽略的致命项——开发机上一切正常，
因为开发机从来没重建过 `web` 镜像。

**根因**：`web/Dockerfile` 仍是 D1 写法，只 `COPY static/` 并用 nginx 托管静态目录；
D6 把前端换成了 `console/`（Vue 3 + TS + Vite），但镜像从未跟着改。开发时 `web` 容器一直
跑着旧镜像，没人重建，问题被完全掩盖。

**定位**：在干净目录 `git clone` → `bootstrap_demo.sh` → 打开 8080，与开发栈对比，疑点立刻变成
「镜像内容 vs 仓库内容」。`docker run --rm --entrypoint sh <web-image> -c 'ls /usr/share/nginx/html'`
只看到 `app.js/index.html/styles.css`，没有 `assets/*.js`。

**修复**：改成多阶段构建——`node:22-alpine` 里 `npm ci && npm run build`，
产物 `dist` 交给 `nginx:1.27-alpine`；同时加 `/api` 反代到 `api:8000`，
`NPM_REGISTRY` 作为 build arg（默认 npmmirror，海外可改官方源）。
新增 `web/.dockerignore`（忽略 `console/node_modules`、`console/dist`、`e2e`），
`git rm -r web/static`（旧页面删除，避免再次误导）。

**防回归**：干净环境验收清单第一条就是「控制台 200 且能看到五导航域」；
`docker compose build --no-cache web` 可在 54s 内完成，随时可验证真实构建结果。

---

## 2. 跨语言诊断的 Java 演示服务从 D3 起就编译不过（D7）

**现象**：干净环境 `bootstrap_demo.sh --with-demo` 在构建 `demo-java` 阶段失败：
`cannot find symbol: class ResponseEntity`。

**影响**：跨栈诊断（Java 服务 503）在全新环境无法演示，这条能力等于不存在。

**根因**：某次修改 `OrdersController.java` 时用了 `ResponseEntity` 但忘了 `import
org.springframework.http.ResponseEntity;`。开发栈的 `demo-java` 容器是旧镜像且一直在跑，
`docker compose up -d` 不会因为源码变化就重建镜像——**镜像比源码旧，编译错误被冻结在时间胶囊里**。

**修复**：补 import（commit `4bc6c63`）。

**教训（可推广）**：`docker compose up -d` 不等于「用当前源码重建」。凡是「开发机正常、干净环境炸」
的问题，第一步永远是 `--no-cache` 重建受影响的镜像，而不是读业务代码。

---

## 3. 容器身份被双重加前缀，把「没看到容器」当成「容器没问题」（D7）

**现象**：路线 A 诊断一台已 `docker stop` 的 Redis，证据里出现
`container 'opspilot-demo-demo-redis' not found`，但工具状态却是 `status=ok`。

**影响**：**证据链失真**——最严重的一类问题。停止的容器本该产出「容器不存在/已退出」的强证据，
实际变成了「探测成功但没有异常信息」，模型据此可能得出「容器没问题」的结论。
这类缺陷不会报错，只会让结论静默变错。

**根因**：容器名解析规则是 `instance.container_prefix + asset.name`，演示资产的 `name` 是
`redis`、实例前缀是 `opspilot-demo-`，拼出 `opspilot-demo-redis`——看起来对。
但资产本身已经登记过 `endpoints.container_name = "opspilot-demo-redis"`，
两处来源叠加就变成 `opspilot-demo-demo-redis`。而 D6 的动作门禁用的正是「资产登记优先」规则，
两套规则不一致。

**修复**：抽出 `resolve_container_name(endpoints, instance_config, asset_name)` 放在
`app/domains/tools/gateway.py` 顶层，规则统一为「资产登记优先，未登记才回退前缀+名」，
与 D6 动作门禁完全一致；`seed_demo.py` 显式登记 `container_name`。

**防回归**：`tests/unit/test_container_identity.py` 4 例——登记优先、回退、登记值为空、都没有。

---

## 4. run 的配置快照说 `rag=off`，报告却是 hybrid（D7）

**现象**：演示路线 B（`RAG_MODE=hybrid`）跑完后，报告的 `generator.rag_mode=hybrid`，
但 `run.model_config_snapshot.rag` 是 `off`。

**影响**：快照是「这次诊断到底在什么配置下跑的」的唯一凭据。快照错了，所有事后对比
（消融、回归、事故复盘）都会得出错误结论。

**根因**：两处。① `app/domains/runs/service.py` 里 `model_config_snapshot["rag"]` 硬编码 `"off"`
（D4 引入 RAG 时漏改）；② 演示脚本读的是 `snap["rag_mode"]`，而实际 key 是 `rag`。

**修复**：快照写入真实 `settings.rag_mode`；脚本兼容两种 key 并优先 `rag`。

**教训**：「配置快照」这类字段必须从**唯一的配置源**取，任何硬编码都会在下一个阶段变成谎言。

---

## 5. report 说是 deepseek 生成的，快照却是 fake provider（D7）

**现象**：真实模型演示中，`run.model_config_snapshot.provider=fake`，
但报告 `generator.provider=deepseek`，同一份数据自相矛盾。

**影响**：无法判断这次报告到底是谁生成的，等于评估数据不可信。

**根因**：`docker-compose.yml` 只给 `worker` 注入了 `AGENT_PROVIDER` 等变量，`api` 服务没有。
而 `model_config_snapshot` 是**API 进程**在创建 run 时写的。于是 api 用默认值 `fake` 写快照，
worker 用 `.env` 的 `deepseek` 真跑。

**修复**：给 `api` 补齐 `AGENT_PROVIDER / AGENT_MODEL_NAME / AGENT_PROMPT_VERSION /
AGENT_FAKE_PROVIDER_ENABLED` 四个变量，与 worker 保持同源。

**教训**：跨进程共享的**配置事实**必须由所有相关进程同时持有。只给「干活的那个」配环境变量
是常见错误——写元数据的那一方也需要。

---

## 6. 发布一个知识版本要 32.7 秒，浏览器 E2E 连环超时（D6）

**现象**：Playwright 用例 04 卡在「等待 `published` 徽章」30 秒超时，随后 05/06 也因登录态
失败。直连实测 `POST /knowledge/versions/{id}/publish` 耗时 **32.7s**。

**影响**：E2E 7 例中 3 例红；用户侧发布一篇知识要等半分钟。

**根因**：`app/api/v1/knowledge.py::_provider()` 每次请求都调 `build_embedding_provider()`，
新实例意味着**每次重新加载本地 e5 模型**（几百 MB 的 transformer 权重）。
第一次改单例后降到 5.6s，仍有首次加载开销。

**定位过程中的误判**：一开始看 uvicorn 访问日志里**没有**这次 publish 请求，判断「前端根本没发请求」。
错了——uvicorn 只在响应完成后写访问日志，客户端先超时断开会让请求被取消，日志自然不出现。
**排查请求丢失时不要只信访问日志，要用服务端耗时埋点或代理抓包。**

**修复**：① 改为进程级单例 `get_embedding_provider()`；
② `app/main.py` 启动后台任务 `_warm_embeddings()` 预热（不阻塞 startup，避免健康检查超时）；
③ 本地 provider 的 `warm_up()` 加双检锁并记录 `load_error`。

**结果**：32.7s → 5.6s（单例）→ 0.05s（预热后），E2E 7/7（29.3s），未改任何测试断言。

---

## 7. 开发者本地 `.env` 污染测试基线，全量回归 2 例红（D7）

**现象**：全量测试 2 例失败：`test_get_run_shape` 断言 `rag=="off"` 实得 `hybrid`；
`test_fault_suite_full_run` 断言 `provider=="fake"` 实得 `deepseek`。

**影响**：同一份代码在不同开发者机器上结果不同——测试失去意义。

**根因**：`tests/conftest.py` 只固定了 `EMBEDDING_PROVIDER=fake`，没有固定 `AGENT_PROVIDER` 与
`RAG_MODE`。开发者为演示把 `.env` 改成 `deepseek` / `hybrid`，pytest 进程读到就被带偏。

**修复**：conftest 用 `os.environ.setdefault` 固定测试基线——`AGENT_PROVIDER=fake`、
`AGENT_MODEL_NAME=fake-diagnoser`、`AGENT_FAKE_PROVIDER_ENABLED=true`、`RAG_MODE=off`。
复跑 2 passed，全量 **347 passed / 3 skipped**。

**教训**：「本地演示配置」与「测试基线配置」必须是两个世界。凡是读全局 settings 的测试，
都要在 conftest 层显式钉死。

---

## 8. 种子脚本在容器内连不上 127.0.0.1:5432（D7）

**现象**：`bootstrap_demo.sh` 在 `docker compose exec -T api python scripts/seed_demo.py` 阶段
报 `ConnectionRefusedError: 127.0.0.1:5432`。

**根因**：脚本硬编码 `DEFAULT_DSN=...@127.0.0.1:5432/...`（为宿主机运行设计）。
容器内 127.0.0.1 是容器自己，Postgres 在 `postgres:5432`。

**修复**：`SEED_DSN` → `DATABASE_URL`（去掉 `+asyncpg` 后缀）→ 默认值，三级回退。
容器内 `DATABASE_URL` 已指向 `postgres:5432`，宿主机运行仍用默认值。

---

## 9. 所有本地探测都返回 502（D7）

**现象**：`curl http://localhost:8000/api/v1/health/ready` 返回 502，浏览器却能正常访问。

**根因**：宿主机设置了 `HTTP_PROXY=127.0.0.1:63144` 且 `NO_PROXY` 为空，
curl 把 127.0.0.1 的请求也交给了代理。

**影响**：不只是调试噪音——`bootstrap_demo.sh` 的就绪探测用的就是 `curl`，
在同样的机器上会**永远等不到就绪**然后报「180s 未就绪」，把用户引向错误方向。

**修复**：脚本探测一律 `curl --noproxy '*'`；PowerShell 版改用
`HttpWebRequest` 并显式 `$req.Proxy = $null`；`demo_routes.py` 用
`build_opener(ProxyHandler({}))`，纯标准库也绕开系统代理。

---

## 10. 干净环境 `uv sync` 装不上依赖（D7）

**现象**：干净目录里 `uv sync` 报 `Failed to fetch https://pypi.org/simple/hatchling/`。

**影响**：如果演示脚本依赖第三方包，演示的第一步就断了。

**根因**：本机网络对 pypi.org 不稳定，Docker 镜像构建里早就配了清华源
（`UV_DEFAULT_INDEX=https://pypi.tuna.tsinghua.edu.cn/simple`），但宿主机没有。

**修复**：`scripts/demo_routes.py` 改为**纯标准库**（`urllib.request`），
演示三条路线不需要 `uv sync`、也不需要宿主机 Python 环境以外的任何依赖。
镜像构建侧继续用清华源 + uv cache mount。

---

## 11. 判官把「中文说对了」判成「全错」（D6）

**现象**：真实模型 7 例回归只有 1 例通过，人工读报告却发现模型说得挺对。

**根因**：judge-v1 用**字面包含**比对 `top1_markers`（如 `REDIS_UNAVAILABLE`）。
模型用中文写「Redis 连接被拒绝」，永远不命中。**这是评估器的错，不是模型的错。**

**修复**：judge-v2 改为**证据锚定**——第一名假设必须引用带对应 `error_code` 的真实证据。
v1 代码路径冻结保留，同一批轨迹并排对比：v1 2/10、v2 10/10，8 例为 v1 误判
（`judge_v1_false_negatives` 有明确清单）。

**为什么不用 LLM-as-judge**：可复现性与零额外成本。代价是表达多样性下的鲁棒性未知——
这一条写进了评估报告的适用边界。

---

## 12. 报告没生成，被记成「诊断结论未知」（D6）

**现象**：3 例真实模型运行里报告 `hypotheses` 为空，评估却记成「模型没诊断出来」。

**根因**：报告生成失败被**混同**为「诊断结论 unknown」。审计时无法区分「模型不知道」和
「报告没写出来」。

**修复**：报告链路改为 `report-schema-v2` + 受控修复（一次定向重试 + 格式化救回），
`report_status` / `report_failure_kind` 分列落库；评估器传递报告状态。
结果：报告生成成功率 4/7 → 10/10，其中 1 例由格式化救回。

---

## 13. 直接原因已确证，模型却整单弃答（D6）

**现象**：模型已经确认「Redis 不可达」，但把整份报告标成 `is_unknown=true`。

**根因**：提示词没有区分「直接原因」与「深层根因」。模型一遇到深层原因不确定，就整单弃答。

**修复**：在诊断提示词与报告格式提示词里写明口径——**直接原因有证据就必须给结论，
深层根因不明确只能标 `insufficient_data`，不允许因为深层不确定而整单弃答**。
3 例 `premature_abstention` 随之消失（D7 的 10 例人工复核里可以看到这个行为的稳定复现）。

---

## 复盘清单（可复用的检查项）

1. 开发机正常 / 干净环境炸 → 先 `--no-cache` 重建镜像，再读代码。
2. 「没看到异常」不等于「没有异常」——证据缺失必须显式建模成 `unavailable` + `error_code`。
3. 同一事实（容器名、配置快照）只能有一个权威来源。
4. 跨进程共享的配置，所有持有者都要注入。
5. 本地 `.env` 不准进测试基线。
6. 请求「消失」时，访问日志不是可靠证据。
7. 模型说对了但判官说错了 → 先怀疑判官。
8. 本地探测一律绕过系统代理。
