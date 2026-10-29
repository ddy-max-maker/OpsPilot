# 关键运行截图

全部由 Playwright 在**真实运行的栈**上采集（`docker compose` + 真实数据），
不含任何真实主机名、内网地址、个人凭据或客户数据——出现的资产名（`as_demo_redis`）、
容器名（`opspilot-demo-redis`）、账号（`e2e_*`）都是本仓库种子脚本生成的演示数据。

采集方式：登录真实控制台 → 访问对应路由 → 等待网络空闲后整页截图；未做任何人为修饰。

---

## 一、故障工作台

| 文件 | 内容 |
| --- | --- |
| `01-workbench-overview.png` | 工作台总览：四类状态计数 + 事件列表（环境/主资产/严重度/状态/来源/时间）。数据来自路线 A/B/C 三次真实演示。 |

看点：`状态=investigating` 不是渲染错误，而是诊断结束后事件停在「调查中」，
等待有人决策是否进入处置——系统不会自己把事件标记成已解决。

---

## 二、Agent 证据与诊断报告

| 文件 | 内容 |
| --- | --- |
| `03-incident-evidence-report.png` | 事件详情 + 诊断运行面板：运行状态、**model_config_snapshot**（`rag=hybrid`、`provider=fake`、`prompt=d6-v2`）、预算与用量、22 条实时轨迹、**4 条实时证据**（含 `unavailable` / `timeout` 与对应 `error_code`）、**2 条历史知识引用**、结构化诊断报告（结论 + 关键词发现 + 候选根因 + 证据引用 + 生成器） |
| `02-knowledge-base.png` | 知识中心：文档/版本/状态（draft / reviewed / published），四眼复核入口 |

这张图是「结论有据可查」的完整证据：

- 证据状态 `unavailable` 也带 `error_code=HEALTH_URL_NOT_REGISTERED` / `CONNECTOR_TIMEOUT`——
  **采集失败被如实记录成「不可观测」，而不是「没有异常」**；
- 「历史知识引用（2 条）——仅供参照，不作为根因直接证据」是独立分区，
  知识永远进不了假设的 `supporting_evidence_ids`；
- 报告每个候选根因都列出「证据引用」，可逐条点回上面 4 条证据。

---

## 三、审批与恢复验证

| 文件 | 内容 |
| --- | --- |
| `04-approval-execution.png` | 恢复方案详情：`sandbox` + `r1` + `execution_failed`；危险操作公示（目标环境、参数 JSON、**参数哈希**、审批有效期）；审批记录（`approved` by `usr_e2e_approver`，四眼要求的第二个人）；执行与验证（`restart_container@v1` / `pre_check_failed` / 门禁拒绝原因原文 / 「尚未验证」） |
| `05-approvals-list.png` | 方案列表：发起人、风险级、状态、关联 run |
| `06-platform-status.png` | 平台与质量：6 个连接器实例（enabled）+ 资产可观测性绑定 + **Capability Manifest**（9 个只读能力、数据源、只读标记、限流 30 次/run · 2 次/s、超时） |

`04` 这张图要讲的是**拒绝也是结构化结果**：

```
status=pre_check_failed
reason="action execution is disabled (demo-only guard);
        real execution requires ACTION_EXECUTION_ENABLED=true on an isolated demo daemon"
```

不是抛异常、不是静默失败、不是「执行成功但没人知道有没有生效」。
提案落 `execution_failed`，执行记录留原因，验证列显示「尚未验证」——
三段状态各自诚实，谁也没替谁圆场。

---

## 复现你自己的截图

```bash
bash scripts/bootstrap_demo.sh --with-demo
python scripts/demo_routes.py all          # 造出事件 / 诊断 / 提案三类数据
# 打开 http://localhost:8080，用 e2e_admin / E2ePassw0rd!2026 登录
```

事件详情页需要 `?run=<run_id>` 才能直接看到已有诊断
（后端无 runs 列表端点，契约冻结不加新端点；`run_id` 可从 `docker compose exec -T postgres psql`
或诊断受理响应中获得）。
