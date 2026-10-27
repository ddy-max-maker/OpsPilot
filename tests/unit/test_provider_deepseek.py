"""DeepSeek Provider 单元测试（无网络：仅验证构建与配置解析）。"""

from __future__ import annotations

import pytest

from app.core.config import Settings
from app.domains.agent.provider import ProviderNotConfigured, build_chat_model


def _settings(**overrides: object) -> Settings:
    base: dict[str, object] = {
        "jwt_secret": "test-jwt-secret-0123456789abcdef",
        "webhook_alerts_secret": "test-webhook-secret-0123456789abcdef",
        "agent_provider": "deepseek",
        "agent_model_name": "deepseek-chat",
        "agent_api_key_env": "DEEPSEEK_API_KEY_UNITTEST",
    }
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


def test_deepseek_provider_builds_chat_model(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY_UNITTEST", "sk-unit-test-key")
    handle = build_chat_model(_settings())
    assert handle.provider == "deepseek"
    assert handle.model_name == "deepseek-chat"
    # 模型实例可序列化进 model_config 快照（不泄漏 Key）
    snapshot = handle.as_model_config(_settings())
    assert snapshot["provider"] == "deepseek"
    assert "sk-" not in str(snapshot)


def test_deepseek_provider_missing_key_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DEEPSEEK_API_KEY_UNITTEST", raising=False)
    with pytest.raises(ProviderNotConfigured, match="DEEPSEEK_API_KEY_UNITTEST"):
        build_chat_model(_settings())


def test_deepseek_custom_api_base_passed(monkeypatch: pytest.MonkeyPatch) -> None:
    """自定义 API 地址经 AGENT_API_BASE 传入（OpenAI 兼容网关场景）。"""
    monkeypatch.setenv("DEEPSEEK_API_KEY_UNITTEST", "sk-unit-test-key")
    handle = build_chat_model(_settings(agent_api_base="https://gw.example.com/v1"))
    assert getattr(handle.model, "api_base", None) == "https://gw.example.com/v1"


def test_unknown_provider_rejected() -> None:
    with pytest.raises(ProviderNotConfigured, match="unknown AGENT_PROVIDER"):
        build_chat_model(_settings(agent_provider="nope"))
