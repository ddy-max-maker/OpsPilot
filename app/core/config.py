"""应用配置（pydantic-settings）。

安全约定：
- JWT_SECRET 无默认值：未提供时应用拒绝启动（禁止弱默认密钥）。
- 初始 Admin 仅通过环境变量引导（BOOTSTRAP_ADMIN_*），禁止硬编码默认密码。
"""

from __future__ import annotations

import os
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict

DEFAULT_DATABASE_URL = "postgresql+asyncpg://opspilot:opspilot@localhost:5432/opspilot"


class Settings(BaseSettings):
    """全局配置，环境变量优先于 .env 文件。"""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_name: str = "OpsPilot Next"
    api_v1_prefix: str = "/api/v1"

    # ---- 数据库 ----
    database_url: str = DEFAULT_DATABASE_URL

    # ---- 认证（契约：JWT access token 30 分钟，无 Refresh Token）----
    jwt_secret: str
    access_token_expire_minutes: int = 30

    # ---- cursor 分页签名（缺省复用 JWT_SECRET）----
    cursor_secret: str | None = None

    # ---- Webhook 告警接入（CONTRACTS §5：HMAC-SHA256 + timestamp 防重放，容差 ±5min）----
    webhook_alerts_secret: str
    webhook_timestamp_tolerance_seconds: int = 300

    # ---- API 幂等键保留时长（契约：服务端保存 24h）----
    idempotency_ttl_hours: int = 24

    # ---- 初始 Admin 引导（环境变量，不硬编码密码）----
    bootstrap_admin_username: str | None = None
    bootstrap_admin_email: str | None = None
    bootstrap_admin_password: str | None = None

    # ---- 列表分页（契约：limit <= 200）----
    default_page_size: int = 50
    max_page_size: int = 200

    # ---- 密码策略 ----
    password_min_length: int = 8

    # ===== D2：诊断运行 / Agent Worker（IMPLEMENTATION_PLAN §2.2 / §4#10）=====

    # ---- 模型 Provider（部署期锁定，#6；模型名不写死在业务层）----
    agent_provider: str = "fake"  # fake | anthropic | google | deepseek
    agent_model_name: str = "fake-diagnoser"
    # API Key 经环境变量间接引用（密钥不入代码/Prompt/日志）
    agent_api_key_env: str | None = None  # 如 DEEPSEEK_API_KEY
    # OpenAI 兼容自定义 API 地址（deepseek 缺省官方 https://api.deepseek.com/v1）
    agent_api_base: str | None = None
    agent_prompt_version: str = "d4-v1"

    # ---- Run 预算默认值（服务端硬限制，ADR-8；超限强制终止）----
    budget_max_tool_calls: int = 24
    budget_max_iterations: int = 40
    budget_max_tokens: int = 200_000
    budget_deadline_seconds: int = 900

    # ---- Worker DB 认领（ADR-4/ADR-12）----
    worker_lease_ttl_seconds: int = 60
    worker_heartbeat_interval_seconds: float = 15.0
    worker_poll_interval_seconds: float = 2.0
    worker_id: str | None = None  # 缺省自动生成（主机名+pid）

    # ---- checkpoint 保留（DATA_MODEL §5：随 run 终态保留 N 天，清理任务随 Worker 上线）----
    checkpoint_retention_days: int = 30

    # ---- Evidence 上限（CONTRACTS §3：summary ≤ 2KB、payload ≤ 256KB）----
    evidence_summary_max_bytes: int = 2048
    evidence_payload_max_bytes: int = 262_144

    # ---- 连接器默认超时/限流（manifest 未声明时的兜底）----
    connector_timeout_seconds: float = 5.0
    connector_max_calls_per_run: int = 30

    # ---- 停止条件（IMPLEMENTATION_PLAN §2.2：连续 N 轮无新证据 → no_new_evidence）----
    no_new_evidence_rounds: int = 3

    # ===== D4：知识库与 RAG（IMPLEMENTATION_PLAN §3.2 / PRD §6）=====

    # RAG 检索模式：off（D3 行为）| text-only（关键词/全文）| hybrid（文本+pgvector）
    rag_mode: str = "off"

    # Embedding Provider：local_e5（本地 intfloat/multilingual-e5-small，384 维，
    # 需安装 embedding 依赖组）| fake（确定性哈希向量，CI 使用）
    embedding_provider: str = "local_e5"
    embedding_model_name: str = "intfloat/multilingual-e5-small"
    embedding_dimension: int = 384
    # e5 上下文 512 token；分块上限取安全值（中文约 1 token/字）
    embedding_max_chunk_chars: int = 450
    chunking_version: str = "1"
    # 检索默认参数
    rag_retrieve_limit: int = 5
    rag_max_chunks_per_hit: int = 3

    @property
    def agent_api_key(self) -> str | None:
        """按间接引用的环境变量名读取 API Key（缺省 None → 仅 fake 可用）。"""
        if not self.agent_api_key_env:
            return None
        return os.environ.get(self.agent_api_key_env)

    @property
    def psycopg_database_url(self) -> str:
        """checkpoint 专用 psycopg 连接串（langgraph schema，ADR-5）。"""
        url = self.database_url.replace("postgresql+asyncpg://", "postgresql://")
        sep = "&" if "?" in url else "?"
        return f"{url}{sep}options=-c%20search_path%3Dlanggraph"

    @property
    def signing_secret(self) -> str:
        """cursor 签名密钥：优先 CURSOR_SECRET，缺省复用 JWT_SECRET。"""
        return self.cursor_secret or self.jwt_secret

    @property
    def bootstrap_admin_configured(self) -> bool:
        """三个引导变量必须同时提供；部分提供视为配置错误。"""
        fields = (
            self.bootstrap_admin_username,
            self.bootstrap_admin_email,
            self.bootstrap_admin_password,
        )
        provided = [f for f in fields if f]
        if len(provided) not in (0, 3):
            msg = "BOOTSTRAP_ADMIN_USERNAME/EMAIL/PASSWORD 必须同时提供或全部留空"
            raise ValueError(msg)
        return len(provided) == 3


@lru_cache
def get_settings() -> Settings:
    """获取缓存的 Settings 单例。"""
    return Settings()
