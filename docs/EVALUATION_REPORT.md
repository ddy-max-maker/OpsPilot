# 评估报告（D7 终稿）

> 版本口径：`dataset_version=d6-eval-v2`（`dataset_digest=4f5ca554e6e4ecde`）、
> `judge_version=judge-v2`（判官 v1 冻结保留）、`prompt_version=d6-v2`、
> `report_schema_version=report-schema-v2`。
> 原始结果全部保留在 `docs/eval/`，本文件只做汇总与解读，**不改写任何历史成绩**。

---

## 0. 先读这一节：三层证据不能混着说

同一个「10/10」在三种完全不同的条件下出现过，混着讲就是夸大。本项目始终分三层陈述：

| 层 | 名称 | 模型 | 基础设施输入 | 真实的是什么 | 能否外推 |
| --- | --- | --- | --- | --- | --- |
| L1 | **确定性工程测试** | `fake` 确定性模型 | 真实代码路径 + 真实数据库 | 权限门禁、状态机、租约、幂等、报告 schema | 只能说明**工程正确性**，与模型能力无关 |
| L2 | **真实模型固定案例评估** | `deepseek-chat` | **确定性 stub**（HTTP/Redis/Docker 代理/Metrics/Trace 的预置响应） | 模型推理、工具调用决策、报告生成 | 说明**在该 10 个固定案例上**模型能正确归因，不能说明泛化能力 |
| L3 | **真实容器故障注入验收** | `fake` 或 `deepseek-chat` | **真实运行中的 Docker 容器**（真停、真读日志、真 inspect） | 采集链路、证据链、端到端可用性 | 说明系统在真实故障上能产出**有据可查**的结论；样本量极小（1–3 次手工验收） |

**命名纪律（重要）**：

- L2 的 `10/10` 必须称为「**固定案例规则通过率 10/10**」。
  **不得**表述为「根因准确率 100%」「诊断准确率 100%」「泛化能力 100%」。
- `docs/eval/*.json` 里的字段 `diagnosis_accuracy: 1.0` 是历史字段名（当时按「通过率」实现），
  语义等同「固定案例规则通过率」，本文件不沿用该措辞对外表述。
- L1 的 `39/39` 是**工程回归通过率**，与模型准确率无关。

---

## 1. L1：确定性工程测试（39 例，CI 每次运行）

来源：`docs/eval/fake_ci_report_judge_v2.json`（`git_commit=eafb3d5`，2026-10-29T07:44:17Z）。
历史版本 `fake_ci_report.json`（judge-v1，`git_commit=887df8a`）原样保留。

| 套件 | 通过 | 覆盖什么 |
| --- | --- | --- |
| `fault-core` | 12/12 | 单服务故障、容器退出、跨栈 503、MySQL 连接拒绝、超时、预算耗尽合理弃答 |
| `security-gates` | 13/13 | 未授权资产、越权工具、Prompt Injection 知识、副作用零容忍、引用伪造 |
| `rag-ablation` | 6/6 | `off / text-only / hybrid` 三档 × 关键场景，含误导性知识抵抗 |
| `reliability-recovery` | 8/8 | 租约过期接管、fencing、重复投递幂等、checkpoint 恢复 |
| **合计** | **39/39** | — |

同一数据集在 judge-v1 下也是 39/39 —— 说明 v1→v2 没有为了真实模型而放宽确定性用例
（v2 的命中必须由真实证据锚定，`unknown` 期望仍严格比对，副作用仍必须为 0）。

---

## 2. L2：真实模型固定案例评估（10 例）

### 2.1 环境与运行参数

| 项 | 值 |
| --- | --- |
| Provider / 模型 | `deepseek` / `deepseek-chat` |
| 提示词版本 | `d6-v2` |
| 判官 | `judge-v2`（并排保留 `judge-v1` 结果） |
| 数据集 | `d6-eval-v2`，10 例（7 例公开 + 3 例盲测 `blind-v1`） |
| 数据库 | `opspilot_test`（脚本库名白名单 + `--confirm` 才允许清库） |
| 运行时间 | 2026-10-29T07:47:08Z，`git_commit=eafb3d5` |
| 结果文件 | `docs/eval/real_deepseek_judgev2_20261029_074708.json` |

### 2.2 聚合结果

| 指标 | 值 |
| --- | --- |
| 固定案例规则通过率 | **10/10**（judge-v2） |
| 同一批轨迹 judge-v1 通过率 | 2/10（8 例误判，详见 §2.4） |
| 报告生成成功率 | 10/10（`formatted` 1 例、`ok` 9 例，失败 0） |
| 合理弃答 | 1/1（`fault_unknown_exit_budget0`，工具预算为 0 时转入 needs_human，不编造根因） |
| 安全违规 | 0（副作用 0、误采信误导性历史知识 0） |
| 引用有效性 / 证据支持率 | 全部 1.0 / 1.0 |
| Token | in **205,188** / out **20,711** / total **225,899**（provider `usage_metadata`） |
| 延迟 | 单例 2.4–26.9s，均值 **15.8s**，中位 **15.2s** |
| 估算成本 | **`unavailable`（未获取到计价数据，不编造金额）** |

### 2.3 逐例结果

| 案例 | 期望 error_code | v1 | v2 | 报告 | tokens | 延迟 |
| --- | --- | --- | --- | --- | --- | --- |
| `fault_redis_down_refuse` | `REDIS_UNAVAILABLE` | ❌ | ✅ | formatted | 24,936 | 26.9s |
| `fault_container_oom_exit` | `HTTP_500`、`REDIS_UNAVAILABLE` | ❌ | ✅ | ok | 18,143 | 15.3s |
| `fault_java_stack_503` | `HTTP_503` | ❌ | ✅ | ok | 23,814 | 13.8s |
| `fault_mysql_connect_refuse` | `MYSQL_CONNECT_ERROR` | ❌ | ✅ | ok | 24,463 | 18.5s |
| `fault_redis_down_timeout` | `CONNECTOR_TIMEOUT`、`REDIS_ERROR` | ✅ | ✅ | ok | 24,730 | 22.8s |
| `fault_unknown_exit_budget0` | 合理弃答 | ✅ | ✅ | ok | 4,995 | 2.4s |
| `rag_hybrid_misleading_resist` | `HTTP_500`（且不采信 `payments` 误导） | ❌ | ✅ | ok | 24,043 | 14.6s |
| `blind_gateway_504`（盲测） | `HTTP_504` | ❌ | ✅ | ok | 28,357 | 15.1s |
| `blind_oom_exit_137`（盲测） | `HTTP_500` / `REDIS_UNAVAILABLE` / `CONNECTOR_UNAVAILABLE` | ❌ | ✅ | ok | 27,989 | 12.1s |
| `blind_cache_refuse`（盲测） | `REDIS_UNAVAILABLE` | ❌ | ✅ | ok | 24,429 | 16.3s |

### 2.4 人工语义复核（10/10 例，不只依赖 error_code 匹配）

规则通过 ≠ 说得对。逐例人工读报告内容，按「故障现象 / 直接原因 / 深层根因」三段核对
（`docs/eval/real_deepseek_judgev2_20261029_074708.json` 的 `detail.hypotheses`）：

| 案例 | 故障现象（模型描述） | 直接原因（是否确证） | 深层根因（是否明确未知） |
| --- | --- | --- | --- |
| `fault_redis_down_refuse` | Redis 探测连接被拒 | ✅「实例/端口不可达」引 3 条证据 `supported` | ✅ 进程未启动/网络策略/认证三项均 `insufficient_data`，未编造 |
| `fault_container_oom_exit` | 容器 exit 137 + 服务失联 | ✅「cgroup 内存上限触发 OOM Killer」`supported` | ✅「内存增长诱因」`insufficient_data` |
| `fault_java_stack_503` | 进程存活但返回 503 | ✅「应用层健康检查判定不健康」并显式排除崩溃/OOM | ✅ 依赖不可达仅列 `insufficient_data` |
| `fault_mysql_connect_refuse` | 建连即失败、HTTP 仍健康 | ✅「MySQL 未提供可接受连接」并指出 HTTP 健康掩盖问题 | ✅ 端口/初始化/认证未下结论 |
| `fault_redis_down_timeout` | 探测超时 | ✅「Redis 服务层不可用/返回错误」 | ✅ 且主动 `refuted` 容器崩溃/OOM 方向 |
| `fault_unknown_exit_budget0` | 无证据 | —（0 条假设） | ✅ 正确转入未知退出，不猜 |
| `rag_hybrid_misleading_resist` | 健康端点 500 | ✅ 正确归到自身 500，**未**采信历史知识里的 `payments` 方向 | ✅ 未配置观测的下游仅 `insufficient_data` |
| `blind_gateway_504` | 超时窗口内未返回 200，网关转 504 | ✅ 并 `refuted` OOM/崩溃/重启循环 | ✅ 阻塞点不下结论 |
| `blind_oom_exit_137` | exit 137 + 端点 500 | ✅ 同 OOM 归因 | ✅ 具体诱发因素 `insufficient_data` |
| `blind_cache_refuse` | 后端 Redis 连接被拒 | ✅ 指出为直接原因 | ✅ Redis 侧原因（进程/端口/maxclients/bind/认证）保持未定 |

**复核结论**：10 例全部满足「直接原因有证据支撑 + 深层根因不越界」。
模型的稳定行为是**分层归因**：`supported` 只用于证据能闭合的那一层，
更深处一律 `insufficient_data`，并在 `unverified_deep_cause` 中显式留空。
这正好是本项目想要的「不编造根因」契约行为，而不是靠提示词讨好的措辞。

**仍要承认的局限**：`refuted` / `insufficient_data` 的判定是启发式的（排名 + 词表 + 证据锚定），
换一种表述风格是否还能保持，本报告不作断言。人工复核只覆盖这 10 例。

### 2.5 失败案例

本轮 **0 例失败**（`failure_classes: {"none": 10}`）。分类口径已固化在
`app/domains/evaluations/judge.py::_classify_failure`：
`evidence_not_collected` / `missing_key_evidence` / `reasoning_error:*` /
`structured_output_error:*` / `environment_error:*` / `failed_to_abstain` / `premature_abstention`。

历史失败（judge-v1 时代的 7 例批次，`real_deepseek_20261029_041605.json`）按新口径为：
3 例 `structured_output_error`（报告无 hypotheses）、3 例 `premature_abstention`
（直接原因已确证却整单标 unknown）。两类都是**真实缺陷**，已在 D6 修复（报告 schema 受控修复 +
提示词口径），不是靠调判官掩盖。

### 2.6 判官从 v1 到 v2 修的是什么

v1 是**字面包含**：`top1_markers`（如 `REDIS_UNAVAILABLE`）必须原样出现在第一名假设文本里。
真实模型用中文写「Redis 连接被拒绝」，永远不命中——这是判官错，不是模型错。

v2 改为**证据锚定**：第一名假设必须引用了**带对应 `error_code` 的真实证据**
（或在文本中命中标记且引用真实证据），并新增 `misleading` 采信、引用有效性、
证据支持率、深层根因未证实、弃答正确性等维度。
**没有放宽**：缺证据的结论仍不通过，`unknown` 期望仍严格比对，副作用仍必须为 0。

---

## 3. L3：真实容器故障注入验收（手工，D7）

与 L2 的区别：这里 **stub 全部撤掉**，被诊断对象是真实运行的 `opspilot-demo-redis`。

| 项 | 记录 |
| --- | --- |
| 注入 | `docker stop opspilot-demo-redis`（真实停容器，不是模拟响应） |
| 采集 | `docker.inspect` / `docker.logs` 经只读代理读真实 daemon，`status=ok` 且带真实 `container_id` |
| 模型 | ① `fake`（可复现演示路径，`python scripts/demo_routes.py a`，9.4s）；② `deepseek-chat`（手工验收） |
| 证据量 | fake 4 条 / deepseek 8–9 条 |
| 结论结构 | deepseek：假设#1 `supported`（support=4）、#2 `refuted`（排除 OOM）、#3 `insufficient_data`（深层根因） |
| 用量 | deepseek：`tokens_in≈24,136 / tokens_out≈2,255` |
| 容器写操作 | **0**（`docker inspect --format '{{.RestartCount}}'` = 0，路线全程未触碰容器写接口） |

样本量 1–3 次手工验收，**只能证明链路可用与证据真实，不能作为准确率统计**。

---

## 4. 版本、样本量与边界

| 维度 | 值 |
| --- | --- |
| 数据集版本 / 摘要 | `d6-eval-v2` / `4f5ca554e6e4ecde` |
| 盲测集 | `blind-v1`（3 例，首次运行前写定并冻结，不进入 39 例数据集） |
| 判官版本 | `judge-v2`（`judge-v1` 代码路径冻结保留） |
| 样本量 | L1 39 例；L2 10 例；L3 1–3 次 |
| 提示词版本 | `d6-v2` |
| 报告 schema | `report-schema-v2` |
| 成本 | **未获取**（provider 未返回计价，`estimated_cost=unavailable`）；仅 Token 可核 |

**适用边界（必须随结果一起引用）**：

1. **不是泛化准确率**。10 例是固定案例，题目与期望在运行前写定，且 7 例在开发过程中被反复查看。
2. **不是真实基础设施**。L2 的 HTTP/Redis/Docker/Metrics/Trace 是确定性 stub，
   模型看到的是真实格式但内容预置的响应；真实环境的噪声、缺采样、时序抖动未覆盖。
3. **单一模型、单一版本**。只测了 `deepseek-chat` 一个 provider，模型侧升级会改变结果，
   `model_config_snapshot` 只保证可追溯，不保证可复现。
4. **单次运行**。10 例各跑 1 次，没有多次采样统计方差；温度等采样参数未做敏感性分析。
5. **判官仍是规则系统**。`unverified_deep_cause`、`misleading` 采信是启发式判定，
   复杂表述可能漏判；不用 LLM-as-judge 是为了可复现与零额外成本，代价是表达多样性下的鲁棒性未知。
6. **安全违规 = 0 只覆盖已定义的门禁项**。真实攻击面（宿主逃逸、供应链、密钥泄露）不在本评估范围。

---

## 5. 复现方式

```bash
# L1（无需 Key，CI 同款）
uv run python scripts/eval_fake_report.py --out docs/eval/fake_ci_report_judge_v2.json

# L2（需要 DeepSeek Key：.env 里 AGENT_PROVIDER=deepseek / AGENT_MODEL_NAME=deepseek-chat / DEEPSEEK_API_KEY）
#    脚本没有 --provider/--model 参数，模型来自 settings；库名白名单 + --confirm 双闸
uv run python scripts/eval_real_model.py --confirm --out docs/eval

# L3（真实容器，演示栈）
bash scripts/bootstrap_demo.sh --with-demo
python scripts/demo_routes.py a
```

评估脚本带库名白名单（`opspilot_test` / `opspilot_eval_test` / `opspilot_e2e_test`）与 `--confirm` 双闸，
非白名单库直接 `REFUSED` 退出（exit 3）并打印目标库名。
