"""单仓三入口约定（ARCHITECTURE §2.2）：

- api     HTTP API 进程（D1-A 实现）
- worker  Agent Worker 进程（D2 引入 RabbitMQ 消费，本文件仅为预留位置）
- action  Action Service 进程（D5 引入受控执行，本文件仅为预留位置）
"""
