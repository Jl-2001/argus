"""Provider abstraction: `IncidentExplanationService` (argus.ai.explain)
depends only on the `AIProvider` Protocol from `.base` -- never on
`AnthropicProvider`/`GeminiProvider` directly. `resolve_provider` is the
one place a concrete provider class gets chosen, so that decision lives
at a clean boundary (CLI/bootstrap code calls it) rather than as
scattered `if provider == "gemini":` branches through the service layer.
"""

from __future__ import annotations

from typing import Any, Optional

from argus.ai.providers.base import (
    DEFAULT_AI_CONFIG,
    DEFAULT_ANTHROPIC_MODEL,
    DEFAULT_GEMINI_MODEL,
    AIConfig,
    AIConfigurationError,
    AIProvider,
    AIProviderName,
    AIRequestError,
    AIUsage,
    RawModelResponse,
    default_ai_provider,
    default_model_for,
)

__all__ = [
    "AIProviderName",
    "AIConfig",
    "DEFAULT_AI_CONFIG",
    "DEFAULT_ANTHROPIC_MODEL",
    "DEFAULT_GEMINI_MODEL",
    "default_model_for",
    "default_ai_provider",
    "AIConfigurationError",
    "AIRequestError",
    "AIUsage",
    "RawModelResponse",
    "AIProvider",
    "resolve_provider",
]


def resolve_provider(config: AIConfig, *, sdk_client: Any = None) -> AIProvider:
    """Construct the concrete provider `config.provider` names. Raises
    `AIConfigurationError` if that provider's own API key isn't
    configured (unless `sdk_client` is given, for tests). This is the
    *only* place in Argus that branches on provider name -- everything
    downstream (`IncidentExplanationService`, validation, persistence,
    the CLI) works with the returned `AIProvider` alone.
    """

    if config.provider is AIProviderName.ANTHROPIC:
        from argus.ai.providers.anthropic import AnthropicProvider

        return AnthropicProvider(sdk_client=sdk_client)
    if config.provider is AIProviderName.GEMINI:
        from argus.ai.providers.gemini import GeminiProvider

        return GeminiProvider(sdk_client=sdk_client)
    raise ValueError(f"unknown AI provider {config.provider!r}")  # unreachable: AIConfig already validates this
