# D6 交付说明｜产品与质量

> 阶段目标（IMPLEMENTATION_PLAN §3.4）：可写进简历的完整版本——SSE、评估与审计齐备。
> 本文档对照计划范围逐项说明交付物、验证结果与遗留项。

---

## 1. 交付范围总览

| 计划范围项 | 交付状态 | 说明 |
|---|---|---|
| SSE 实时诊断事件（run_events 重放） | ✅ 完成 | `GET /runs/{id}/events` 对 `Accept: text/event-stream` 开放 SSE；Last-Event-ID 断线重放 + 心跳；10 项集成测试全过 |
| ≥30 确定性故障/安全场景（开发集+保留集） | ✅ 完成 | **39 案例 / 4 套件**，12 个固定保留集案例（holdout），全部通过 |
| 评估框架（执行器 + judge + 指标） | ✅ 完成 | 0006 迁移 + 4 张评估表 + runner/judge/cases/stubs；正式 API 测试 7/7 |
| PRD §13 必须记录项 | ✅ 完成（确定性部分） | top1_hit / top3_hit、knowledge_hits、evidence_total、tool_calls、latency_ms、tokens、unauthorized_success、repeated_side_effects、security_denied、verification_misreport 均落库可查 |
| 强门禁三项自动化断言 | ✅ 完成 | 越权执行=0、重复副作用=0、恢复误报=0，由 security-gates / reliability-recovery 套件 + judge 指标自动判定 |
| 安全用例（注入/越权/未审批/过期/篡改） | ✅ 完成 | security-gates 13 案例覆盖全部 5 类 |
| 5 大导航域 UI（PRD §12） | ❌ 未开始 | 工作台与事件 / 诊断详情 / 知识中心 / 审批与恢复 / 平台与质量 |
| 浏览器 E2E（UI 关键路径） | ❌ 未开始 | 依赖 UI |
| 真实 DeepSeek 评估 | ❌ 未开始 | 当前 39 案例全部以 fake 模型（确定性规则）驱动，真实模型评估属后续里程碑 |
| CI 流水线 | ❌ 未开始 | — |
| 审计完善 | ✅ 完成（D5 收尾带入） | JWT jti 防重放、审批防篡改、观察窗口等 12 项安全加固测试全过 |

---

## 2. 评估体系（本阶段核心交付）

### 2.1 架构

```
cases.py   → 39 个确定性案例定义（suite / case_key / scenario / expected）
stubs.py   → 故障注入 stub（HTTP/Redis/Docker/日志/指标/链路），按 scenario 装配世界
runner.py  → 按案例播种世界 → 构造 run → 驱动 agent/worker 路径 → 采集证据
judge.py   → 对照 expected 逐项判定，产出 PRD §13 指标 → evaluation_results 落库
service.py / API → POST /evaluations/suites/{name}/runs（wait 同步 / 异步）+ 结果查询
```

数据层：0006 迁移新增 4 表（数据集、套件运行、案例结果、检索事件），案例以代码为单一事实来源，套件运行时幂等播种。

### 2.2 套件与案例分布（39 案例，12 保留集）

| 套件 | 案例 | 覆盖内容 |
|---|---|---|
| fault-core | 12 | 单服务故障、跨服务链路、误导性历史相似事件、未知故障预算退出、工具超时/不可用 |
| security-gates | 13 | Prompt Injection、越权资产查询、未审批操作、审批过期、参数篡改、凭据泄露 |
| rag-ablation | 6 | off vs text-only vs hybrid 消融、误导性旧知识抵抗、过期知识版本排除 |
| reliability-recovery | 8 | Outbox 幂等去重、消息重投、Worker 崩溃恢复、合作式取消、执行/验证间崩溃、验证窗口翻转 |

保留集（12 个 case_key 固定于 `_HOLDOUT_KEYS`）与开发集分离，满足「不得为分数对保留集调优」红线。

### 2.3 强门禁结果（自动化断言）

| 门禁 | 结果 |
|---|---|
| 越权/未审批操作成功执行次数 | **0** |
| 重复 side-effect 次数（超出合法登记的重复数） | **0** |
| 恢复验证失败时误报成功次数 | **0** |

---

## 3. 调试过程记录（根因 → 修复）

调试采用临时 `test_eval_debug.py`（`EVAL_DEBUG_SUITE` 环境变量选套件）逐案例打印 metrics，修完即删。共 12 处修复（commit `a7c94e4`）：

| # | 症状 | 根因 | 修复 |
|---|---|---|---|
| 1 | cross_service_trace 崩溃 | jaeger payload 缺 `processes`，ownership 校验失败 | cases.py 补全数据结构 |
| 2 | prometheus anomaly 假设不触发 | 连接器只读 `status/data`，stub 的 `anomaly` 标记被丢弃 | prometheus/jaeger 连接器透传 `anomaly` 进证据摘要 |
| 3 | redis 超时误报为通用错误 | redis-py 的 `TimeoutError` 不继承内建 `TimeoutError` | 补独立 except → `CONNECTOR_TIMEOUT` |
| 4 | redis 连接拒绝落 REDIS_ERROR | `redis.exceptions.ConnectionError` 继承 `RedisError` 而非 `OSError` | 补独立 except → `REDIS_UNAVAILABLE` |
| 5 | 知识版本 not found 崩溃 | `publish_version` 开独立会话建向量索引，播种事务未 commit 不可见 | 播种路径 publish 前 commit |
| 6 | 注入案例知识 0 命中 | fake 标题正则 `\S+：(.+)` 贪婪吞掉 case_key | 改非贪婪 `\S+?` |
| 7 | 中文标题 ILIKE 不命中 | text-route 按空白分词，无空格整串匹配不上 | 分词增加 CJK/全角标点分隔 |
| 8 | rag 两案例随执行顺序抖动 | http 连接器全局单例，r4 执行时尚未创建 → NOT_CONFIGURED 抢 rank1 | http-ro 实例常驻，消除顺序耦合 |
| 9 | 保留集排除案例误判失败 | judge 把 hit_count=0 的空检索也算「知识命中」 | knowledge_hits 仅计 hit_count>0 |
| 10 | cancel 案例 DB 无终态 | eval 执行器直调 execute_run，绕过 Worker 终态回写 | 补 claim + finish_run |
| 11 | crash 案例断言失败 | 断言只接受 EXECUTED，但验证通过后状态机正确推进为 VERIFIED | 断言对齐状态机 |
| 12 | API wait=true 返回 pending | 端点返回执行前加载的 ORM 对象 | 同步执行后 `session.refresh` |

另：sec_tampered / window_flip 的 1 次合法登记重启不计入 `repeated_side_effects`（口径与 rel_duplicate_message 对齐）。

---

## 4. 验证结果

| 检查项 | 结果 |
|---|---|
| fault-core / security-gates / rag-ablation / reliability-recovery | 12 + 13 + 6 + 8 = **39/39** |
| 正式评估 API 测试 `test_evaluations.py` | 7/7 |
| 全量回归 | **317 passed + 1 skipped**（基线 310 + 评估 7，零回归） |
| ruff / mypy（126 源文件） | 全绿 |
| SSE 集成测试（含重放/心跳） | 10/10 |
| D5 安全加固回归 | 12/12 |

诚实性红线遵守情况：评估结果全部由确定性 fake 模型产出，**未接入真实 LLM，未声称模型效果**；保留集未用于任何调优；强门禁为实测断言而非目标值。

---

## 5. 关键提交

- `bd7cd0b` — D6 起点：D5 收尾 + SSE + 评估体系代码（40 文件 +5608 行）
- `a7c94e4` — 评估体系调试完成：12 处修复，10 文件 +102/-112（删除临时调试文件）

---

## 6. D6 遗留 → 下一阶段计划

1. **Vue 3 控制台**（五大导航域，PRD §12）：工作台与事件 → 诊断详情 → 知识中心 → 审批与恢复 → 平台与质量
2. **浏览器 E2E**：普通操作员在 UI 完成核心闭环（D6 验收条款）
3. **真实 DeepSeek 评估**：切换 provider 重跑数据集，产出含成本/延迟的真实指标报告
4. **CI**：全量回归 + ruff + mypy 门禁自动化
5. 进入 D7：复现脚本、README、12 项交付报告、interview_notes、演示视频
