"""ops-action：独立受控执行进程（D5）。

职责（PRD §8 / CONTRACTS §2.13 / 用户指令 §3–§4）：
- outbox dispatcher：Outbox 事件 → RabbitMQ（退避重试，进程重启可恢复）；
- consumer：消费经业务服务授权/审批的执行请求（手动 ack + 幂等账本）；
- executor：代码级 Allowlist 动作执行（容器精确绑定，禁止任意命令）；
- convergence：崩溃收敛（claimed 超时 → unknown，禁止盲目重试）；
- verification：恢复验证（真实健康检查 + 观察窗口；PASSED 才允许 RESOLVED）。

核心原则：模型不能直接调用有副作用的基础设施接口——本进程只接受
RabbitMQ 中携带审批链路证据的执行请求，并在执行前再次复核。
"""
