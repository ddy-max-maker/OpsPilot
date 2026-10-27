"""应用配置（pydantic-settings）。

安全约定：
- JWT_SECRET 无默认值：未提供时应用拒绝启动（禁止弱默认密钥）。
- 初始 Admin 仅通过环境变量引导（BOOTSTRAP_ADMIN_*），禁止硬编码默认密码。
"""

from __future__ import annotations

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

    # ---- 初始 Admin 引导（环境变量，不硬编码密码）----
    bootstrap_admin_username: str | None = None
    bootstrap_admin_email: str | None = None
    bootstrap_admin_password: str | None = None

    # ---- 列表分页（契约：limit <= 200）----
    default_page_size: int = 50
    max_page_size: int = 200

    # ---- 密码策略 ----
    password_min_length: int = 8

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
