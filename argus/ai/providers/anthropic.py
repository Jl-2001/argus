"""The Anthropic (Claude) provider adapter -- the only place in Argus
that imports the `anthropic` SDK and reads `ANTHROPIC_API_KEY`.

`build_request_payload` is pure -- no network, no SDK instance needed --
so request construction is fully testable offline: the same
`EvidenceBundle`, the same `PROMPT_VERSION` (baked into
`argus.ai.prompts`), and the same `AIConfig` always produce the same
payload. `AnthropicProvider` is the thin, injectable wrapper that
actually calls the network -- every real test mocks its underlying SDK
object, the same way `argus.collectors.docker_client.DockerClient`
accepts an injected fake SDK client.
"""

from __future__ import annotations

import os
from typing import Any, Optional

import anthropic

from argus.ai.prompts import EXPLANATION_TOOL_NAME, EXPLANATION_TOOL_SCHEMA, SYSTEM_PROMPT, build_user_message
from argus.ai.providers.base import (
    AIConfig,
    AIConfigurationError,
    AIProviderName,
    AIRequestError,
    AIUsage,
    RawModelResponse,
)
from argus.evidence.bundle import EvidenceBundle

__all__ = ["build_request_payload", "AnthropicProvider"]

_ENV_API_KEY = "ANTHROPIC_API_KEY"


def build_request_payload(
    bundle: EvidenceBundle, *, config: AIConfig, retry_feedback: Optional[str] = None
) -> dict[str, Any]:
    """Pure request construction -- no network. Given the same `bundle`
    (same `to_json()` output), the same `retry_feedback`, and the same
    `config`, this always returns an identical payload. The only thing
    Claude ever receives about the incident is `bundle.to_json()`,
    embedded verbatim in the single user message returned here.
    """

    return {
        "model": config.model,
        "max_tokens": config.max_output_tokens,
        "system": SYSTEM_PROMPT,
        "messages": [{"role": "user", "content": build_user_message(bundle, retry_feedback=retry_feedback)}],
        "tools": [EXPLANATION_TOOL_SCHEMA],
        "tool_choice": {"type": "tool", "name": EXPLANATION_TOOL_NAME},
    }


class AnthropicProvider:
    """A deliberately narrow adapter: one method, one job -- send a
    request built by `build_request_payload` and return a
    `RawModelResponse`. No streaming, no multi-turn conversation state,
    no tool-execution loop.
    """

    provider_name = AIProviderName.ANTHROPIC

    def __init__(self, *, api_key: Optional[str] = None, sdk_client: Any = None) -> None:
        # `sdk_client` exists only so tests can inject a fake -- never
        # exposed back out of this object afterward, same discipline as
        # `DockerClient`.
        if sdk_client is not None:
            self._client = sdk_client
            return
        resolved_key = api_key if api_key is not None else os.environ.get(_ENV_API_KEY)
        if not resolved_key:
            raise AIConfigurationError(f"Argus AI unavailable: {_ENV_API_KEY} is not configured.")
        self._client = anthropic.Anthropic(api_key=resolved_key)

    def create_explanation(
        self, bundle: EvidenceBundle, *, config: AIConfig, retry_feedback: Optional[str] = None
    ) -> RawModelResponse:
        payload = build_request_payload(bundle, config=config, retry_feedback=retry_feedback)

        try:
            response = self._client.messages.create(timeout=config.timeout_seconds, **payload)
        except anthropic.APITimeoutError as exc:
            raise AIRequestError(f"Claude request timed out: {exc}") from exc
        except anthropic.RateLimitError as exc:
            raise AIRequestError(f"Claude rate limit exceeded: {exc}") from exc
        except anthropic.APIConnectionError as exc:
            raise AIRequestError(f"Could not reach Claude: {exc}") from exc
        except anthropic.APIStatusError as exc:
            raise AIRequestError(f"Claude request failed: {exc}") from exc
        except anthropic.AnthropicError as exc:
            raise AIRequestError(f"Claude request failed: {exc}") from exc

        tool_use = next((block for block in response.content if getattr(block, "type", None) == "tool_use"), None)
        if tool_use is None:
            raise AIRequestError("Claude response contained no tool_use content -- nothing to validate")

        usage = response.usage
        return RawModelResponse(
            tool_input=tool_use.input,
            usage=AIUsage(
                input_tokens=getattr(usage, "input_tokens", None) if usage is not None else None,
                output_tokens=getattr(usage, "output_tokens", None) if usage is not None else None,
            ),
            model=response.model,
            provider=AIProviderName.ANTHROPIC,
        )
