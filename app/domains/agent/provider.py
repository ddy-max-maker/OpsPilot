"""模型 Provider 层（IMPLEMENTATION_PLAN §4#6：配置切换，版本锁定）。

约束：
- 模型名/API Key 不写死在业务层；Key 经 `agent_api_key_env` 指定的环境变量间接读取；
- 统一记录 provider/model 版本（进 run.model_config 与报告 generator 快照）；
- `fake` provider：确定性假模型由测试侧注入（tests/fake_model.py），
  运行时若 provider=fake 而未注入模型 → 报错（不静默编造）。
"""

from __future__ import annotations

from typing import Any

from langchain_core.language_models import BaseChatModel

from app.core.config import Settings
from app.core.enums import AgentProvider


class ProviderNotConfigured(RuntimeError):
    """Provider 缺少凭据或未知（统一模型错误，禁止伪造成功）。"""


class ModelHandle:
    """Provider 解析结果：模型实例 + 版本快照（run.model_config / report.generator）。"""

    def __init__(self, model: BaseChatModel, *, provider: str, model_name: str) -> None:
        self.model = model
        self.provider = provider
        self.model_name = model_name

    def as_model_config(self, settings: Settings) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "model_name": self.model_name,
            "prompt_version": settings.agent_prompt_version,
            "rag": "off",  # D2 禁区：无 RAG（IMPLEMENTATION_PLAN §2.3）
        }


def build_chat_model(settings: Settings) -> ModelHandle:
    """按配置构建 Chat Model；失败统一抛 ProviderNotConfigured。"""
    try:
        provider = AgentProvider(settings.agent_provider)
    except ValueError as exc:
        raise ProviderNotConfigured(f"unknown AGENT_PROVIDER '{settings.agent_provider}'") from exc

    if provider is AgentProvider.FAKE:
        raise ProviderNotConfigured(
            "provider 'fake' requires an injected model (tests/demo); "
            "configure AGENT_PROVIDER=anthropic|google|deepseek with credentials for real runs"
        )

    api_key = settings.agent_api_key
    if not api_key:
        raise ProviderNotConfigured(
            f"agent api key missing: set {settings.agent_api_key_env or 'AGENT_API_KEY_ENV'}"
        )

    if provider is AgentProvider.ANTHROPIC:
        from langchain_anthropic import ChatAnthropic  # noqa: PLC0415

        anthropic_model = ChatAnthropic(
            model=settings.agent_model_name,
            api_key=api_key,
            timeout=60,
            max_retries=1,
        )
        return ModelHandle(
            anthropic_model, provider=provider.value, model_name=settings.agent_model_name
        )

    if provider is AgentProvider.GOOGLE:
        from langchain_google_genai import ChatGoogleGenerativeAI  # noqa: PLC0415

        google_model = ChatGoogleGenerativeAI(
            model=settings.agent_model_name,
            google_api_key=api_key,
            timeout=60,
            max_retries=1,
        )
        return ModelHandle(
            google_model, provider=provider.value, model_name=settings.agent_model_name
        )

    if provider is AgentProvider.DEEPSEEK:
        # 官方集成 langchain-deepseek（ChatDeepSeek 继承 BaseChatOpenAI，
        # OpenAI 兼容协议；api_base 缺省 https://api.deepseek.com/v1）
        from langchain_deepseek import ChatDeepSeek  # noqa: PLC0415

        kwargs: dict[str, Any] = {
            "model": settings.agent_model_name,
            "api_key": api_key,
            "timeout": 120,
            "max_retries": 1,
        }
        if settings.agent_api_base:
            kwargs["api_base"] = settings.agent_api_base
        deepseek_model = ChatDeepSeek(**kwargs)
        return ModelHandle(
            deepseek_model, provider=provider.value, model_name=settings.agent_model_name
        )

    raise ProviderNotConfigured(f"provider '{provider}' not supported in D2")
