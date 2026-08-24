"""Tests for the provider factory/resolver itself: `resolve_provider`
(the one place Argus branches on `AIConfig.provider` to construct a
concrete adapter) and `default_ai_provider` (the one place Argus
resolves *which* provider name is in effect: explicit arg >
`ARGUS_AI_PROVIDER` env var > Anthropic default). No network call
anywhere in this file.
"""

from __future__ import annotations

import pytest

from argus.ai.providers import (
    AIConfig,
    AIConfigurationError,
    AIProviderName,
    default_ai_provider,
    resolve_provider,
)
from argus.ai.providers.anthropic import AnthropicProvider
from argus.ai.providers.gemini import GeminiProvider


class _FakeSDKClient:
    """A stand-in accepted by either provider's `sdk_client=` parameter
    -- resolve_provider itself never inspects it, it only threads it
    through to the chosen concrete class."""


class TestResolveProviderConstructsTheRequestedConcreteClass:
    def test_anthropic_config_resolves_to_anthropic_provider(self):
        provider = resolve_provider(AIConfig(provider=AIProviderName.ANTHROPIC), sdk_client=_FakeSDKClient())
        assert isinstance(provider, AnthropicProvider)
        assert provider.provider_name is AIProviderName.ANTHROPIC

    def test_gemini_config_resolves_to_gemini_provider(self):
        provider = resolve_provider(AIConfig(provider=AIProviderName.GEMINI), sdk_client=_FakeSDKClient())
        assert isinstance(provider, GeminiProvider)
        assert provider.provider_name is AIProviderName.GEMINI

    def test_resolving_one_provider_never_constructs_the_other(self):
        # a fake sdk_client that would blow up if the wrong provider
        # class tried to use it as if it were its own SDK shape --
        # resolve_provider must not, e.g., instantiate GeminiProvider
        # while resolving an Anthropic config, or vice versa.
        provider = resolve_provider(AIConfig(provider=AIProviderName.ANTHROPIC), sdk_client=_FakeSDKClient())
        assert not isinstance(provider, GeminiProvider)
        provider = resolve_provider(AIConfig(provider=AIProviderName.GEMINI), sdk_client=_FakeSDKClient())
        assert not isinstance(provider, AnthropicProvider)


class TestResolveProviderCredentialErrorsArePerProvider:
    def test_missing_anthropic_key_raises_configuration_error_naming_that_key(self, monkeypatch):
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        with pytest.raises(AIConfigurationError, match="ANTHROPIC_API_KEY"):
            resolve_provider(AIConfig(provider=AIProviderName.ANTHROPIC))

    def test_missing_gemini_key_raises_configuration_error_naming_that_key(self, monkeypatch):
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)
        with pytest.raises(AIConfigurationError, match="GEMINI_API_KEY"):
            resolve_provider(AIConfig(provider=AIProviderName.GEMINI))

    def test_missing_anthropic_key_never_raises_a_gemini_error_or_vice_versa(self, monkeypatch):
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)
        with pytest.raises(AIConfigurationError, match="ANTHROPIC_API_KEY") as exc_info:
            resolve_provider(AIConfig(provider=AIProviderName.ANTHROPIC))
        assert "GEMINI_API_KEY" not in str(exc_info.value)

    def test_sdk_client_injection_bypasses_the_credential_check_for_each_provider(self, monkeypatch):
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)
        resolve_provider(AIConfig(provider=AIProviderName.ANTHROPIC), sdk_client=_FakeSDKClient())
        resolve_provider(AIConfig(provider=AIProviderName.GEMINI), sdk_client=_FakeSDKClient())


class TestDefaultAIProviderPrecedence:
    def test_explicit_argument_wins_over_env_var(self, monkeypatch):
        monkeypatch.setenv("ARGUS_AI_PROVIDER", "gemini")
        assert default_ai_provider("anthropic") is AIProviderName.ANTHROPIC

    def test_env_var_wins_when_no_explicit_argument(self, monkeypatch):
        monkeypatch.setenv("ARGUS_AI_PROVIDER", "gemini")
        assert default_ai_provider(None) is AIProviderName.GEMINI

    def test_anthropic_is_the_default_when_neither_is_given(self, monkeypatch):
        monkeypatch.delenv("ARGUS_AI_PROVIDER", raising=False)
        assert default_ai_provider(None) is AIProviderName.ANTHROPIC

    def test_unknown_provider_name_raises_a_clean_value_error(self, monkeypatch):
        monkeypatch.delenv("ARGUS_AI_PROVIDER", raising=False)
        with pytest.raises(ValueError, match="unknown AI provider"):
            default_ai_provider("chatgpt")

    def test_unknown_env_var_value_also_raises_a_clean_value_error(self, monkeypatch):
        monkeypatch.setenv("ARGUS_AI_PROVIDER", "not-a-real-provider")
        with pytest.raises(ValueError, match="unknown AI provider"):
            default_ai_provider(None)
