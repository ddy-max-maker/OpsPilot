"""OpsPilot Next 应用包。

模块化单体结构：
- app/core        基础设施（配置、数据库、安全、错误、响应包裹、分页）
- app/models      SQLAlchemy 2 声明式模型（跨领域共享的持久化层）
- app/domains     领域模块（auth / assets / access / audit）
- app/api         FastAPI 路由与依赖注入（/api/v1）
- entrypoints     单仓三入口约定（api / worker / action），D1-A 仅实现 api
"""
