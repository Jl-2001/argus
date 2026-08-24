"""Shared provider contract: the types and config every AI provider
adapter produces/consumes, and nothing else. `IncidentExplanationService`
(argus.ai.explain) depends only on what's defined here (the `AIProvider`
Protocol and `RawModelResponse`) -- never on a concrete provider class --
so adding a third provider later never touches the service itself.

No provider adapter may receive a `Repository`, a `DockerClient`, or
filesystem access -- the only infrastructure input any provider ever
sees is the `EvidenceBundle` handed to `create_explanation`.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Optional, Protocol

from argus.evidence.bundle import EvidenceBundle

__all__ = [
    "AIProviderName",
    "DEFAULT_ANTHROPIC_MODEL",
    "DEFAULT_GEMINI_MODEL",
    "default_model_for",
    "AIConfig",
    "DEFAULT_AI_CONFIG",
    "AIConfigurationError",
    "AIRequestError",
    "AIUsage",
    "RawModelResponse",
    "AIProvider",
]

ENV_PROVIDER = "ARGUS_AI_PROVIDER"


class AIProviderName(str, Enum):
    """A closed set -- never an arbitrary provider string internally."""

    ANTHROPIC = "anthropic"
    GEMINI = "gemini"


#: One centrally-chosen, currently-supported model per provider. Never
#: hardcoded a second time anywhere else -- everything reads
#: `AIConfig.model` (auto-filled from here when not given explicitly).
#: No automatic model routing/fallback: one model per provider, one
#: config.
DEFAULT_ANTHROPIC_MODEL = "claude-sonnet-5"
#: `gemini-3.5-flash` -- a current, stable (non-preview) flash-tier
#: model in the installed `google-genai` SDK, chosen to match
#: Anthropic's own mid-tier "sonnet" choice in spirit: capable enough
#: for structured reasoning over a bounded EvidenceBundle, without
#: reaching for the (costlier, slower) "pro" tier Milestone 12.1 has no
#: need for.
DEFAULT_GEMINI_MODEL = "gemini-3.5-flash"

_DEFAULT_MODELS: dict[AIProviderName, str] = {
    AIProviderName.ANTHROPIC: DEFAULT_ANTHROPIC_MODEL,
    AIProviderName.GEMINI: DEFAULT_GEMINI_MODEL,
}


def default_model_for(provider: AIProviderName) -> str:
    return _DEFAULT_MODELS[provider]


def default_ai_provider(explicit: Optional[str] = None) -> AIProviderName:
    """Resolve the provider the same way everywhere: `explicit` (e.g. a
    CLI `--provider` flag) > the `ARGUS_AI_PROVIDER` environment
    variable > Anthropic (the pre-Milestone-12.1 default, kept so
    existing deployments/scripts see no behavior change unless they
    opt in). Mirrors `argus.store.database.default_database_path`'s own
    precedence pattern -- one shared resolution rule, not two competing
    ideas of "which provider is active".

    Raises `ValueError` (not `AIConfigurationError` -- this is a caller
    passing a bad value, not a missing-credential runtime condition) for
    anything other than a recognized provider name.
    """

    import os

    raw = explicit if explicit is not None else os.environ.get(ENV_PROVIDER, AIProviderName.ANTHROPIC.value)
    try:
        return AIProviderName(raw)
    except ValueError:
        valid = [p.value for p in AIProviderName]
        raise ValueError(f"unknown AI provider {raw!r}; must be one of {valid}") from None


@dataclass(frozen=True, slots=True)
class AIConfig:
    """The handful of numbers/choices the AI layer needs, now
    provider-aware. `model=None` auto-resolves to the given provider's
    own default (see `default_model_for`) at construction time, so
    `AIConfig()` alone still means exactly what it meant before
    Milestone 12.1: Anthropic, `claude-sonnet-5`.
    """

    provider: AIProviderName = AIProviderName.ANTHROPIC
    model: Optional[str] = None
    max_output_tokens: int = 1024
    timeout_seconds: float = 30.0
    #: Defense in depth against sending an unexpectedly huge prompt --
    #: `argus.evidence.assembler.AssemblerConfig.max_total_chars`
    #: (default 20_000) already bounds this in the normal path; this is
    #: a second, independent, provider-agnostic check at the AI
    #: boundary in case that guarantee is ever violated by a future bug.
    max_bundle_chars: int = 20_000

    def __post_init__(self) -> None:
        provider = self.provider if isinstance(self.provider, AIProviderName) else AIProviderName(self.provider)
        object.__setattr__(self, "provider", provider)
        if self.model is None:
            object.__setattr__(self, "model", default_model_for(provider))
        elif not self.model.strip():
            raise ValueError("model must be a non-empty string")
        if self.max_output_tokens <= 0:
            raise ValueError("max_output_tokens must be positive")
        if self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if self.max_bundle_chars <= 0:
            raise ValueError("max_bundle_chars must be positive")


DEFAULT_AI_CONFIG = AIConfig()


class AIConfigurationError(RuntimeError):
    """The AI layer cannot run at all -- e.g. the configured provider's
    API key is not set. Deliberately distinct from `AIRequestError` (a
    configured client that failed to get a usable response): every
    other, purely-deterministic Argus command must keep working
    whichever of these two ways the AI layer is unavailable, and this
    must never depend on which provider is configured."""


class AIRequestError(RuntimeError):
    """A configured request to a model provider failed -- timeout,
    network error, rate limit/quota, or server error, from *either*
    provider. Wraps the real SDK exception (chained via ``from``) so
    the original cause stays visible, but callers never see a raw,
    provider-specific SDK exception type -- `argus.ai.explain` and the
    CLI handle exactly one error shape regardless of provider."""


@dataclass(frozen=True, slots=True)
class AIUsage:
    """Token usage normalized across providers -- exactly as the
    provider's own SDK reported it, never estimated locally. `None`
    fields mean the SDK response didn't expose that figure, not that
    usage was zero."""

    input_tokens: Optional[int]
    output_tokens: Optional[int]

    def to_dict(self) -> dict[str, Optional[int]]:
        return {"input_tokens": self.input_tokens, "output_tokens": self.output_tokens}


@dataclass(frozen=True, slots=True)
class RawModelResponse:
    """The not-yet-validated result of one model call, from *either*
    provider -- the parsed structured-output dict (still untrusted --
    see `argus.ai.validation`), plus usage/model/provider bookkeeping.
    Both providers produce exactly this same shape; everything past
    this point (validation, persistence, CLI rendering) is provider-
    agnostic.
    """

    tool_input: dict[str, Any]
    usage: AIUsage
    model: str
    provider: AIProviderName


class AIProvider(Protocol):
    """What `IncidentExplanationService` depends on -- never a concrete
    provider class. No provider implementation may accept a
    `Repository`, a `DockerClient`, or any filesystem path; the only
    infrastructure fact any provider ever sees is the `EvidenceBundle`
    passed into `create_explanation`.
    """

    provider_name: AIProviderName

    def create_explanation(
        self, bundle: EvidenceBundle, *, config: AIConfig, retry_feedback: Optional[str] = None
    ) -> RawModelResponse: ...
