"""The Gemini provider adapter -- the only place in Argus that imports
`google.genai` and reads `GEMINI_API_KEY`.

Uses the current `google-genai` SDK's own JSON-schema-constrained output
mode (`response_mime_type="application/json"` +
`response_json_schema=...`) against *the exact same*
`EXPLANATION_TOOL_SCHEMA` Anthropic's tool-use call already uses (see
`argus.ai.prompts`) -- one schema, shared between providers, not two
that could silently drift apart. Gemini has no tool-call concept
equivalent to Anthropic's forced tool_choice, so its structured output
comes back as a JSON string (`response.text`) rather than an
already-parsed dict; this module parses that JSON itself and hands it
to the exact same `argus.ai.validation.validate_explanation` Anthropic's
response goes through -- there is no Gemini-specific validation
shortcut anywhere.
"""

from __future__ import annotations

import json
import os
from typing import Any, Optional

import httpx
from google import genai
from google.genai import errors as genai_errors
from google.genai import types as genai_types

from argus.ai.prompts import EXPLANATION_TOOL_SCHEMA, SYSTEM_PROMPT, build_user_message
from argus.ai.providers.base import (
    AIConfig,
    AIConfigurationError,
    AIProviderName,
    AIRequestError,
    AIUsage,
    RawModelResponse,
)
from argus.evidence.bundle import EvidenceBundle

__all__ = ["build_generation_config", "GeminiProvider"]

_ENV_API_KEY = "GEMINI_API_KEY"


def build_generation_config(config: AIConfig) -> genai_types.GenerateContentConfig:
    """Pure config construction -- no network. Reuses the identical
    `EXPLANATION_TOOL_SCHEMA["input_schema"]` JSON schema Anthropic's
    tool-use call is built from, so both providers are held to the
    exact same structural contract."""

    return genai_types.GenerateContentConfig(
        system_instruction=SYSTEM_PROMPT,
        response_mime_type="application/json",
        response_json_schema=EXPLANATION_TOOL_SCHEMA["input_schema"],
        max_output_tokens=config.max_output_tokens,
        # Flash-tier Gemini models reserve part of `max_output_tokens`
        # for internal "thinking" tokens by default -- against a bundle
        # this small, that has been observed to leave too little budget
        # for the structured JSON answer itself and truncate it
        # mid-string. Argus asks for a single structured tool-style
        # response, not visible reasoning, so thinking is disabled
        # outright rather than padding the token budget around it.
        thinking_config=genai_types.ThinkingConfig(thinking_budget=0),
        http_options=genai_types.HttpOptions(timeout=int(config.timeout_seconds * 1000)),
    )


class GeminiProvider:
    """A deliberately narrow adapter, mirroring `AnthropicProvider`'s own
    shape exactly: one method, one job. No streaming, no multi-turn
    conversation state, no tool-execution loop.
    """

    provider_name = AIProviderName.GEMINI

    def __init__(self, *, api_key: Optional[str] = None, sdk_client: Any = None) -> None:
        # `sdk_client` exists only so tests can inject a fake -- never
        # exposed back out of this object afterward, same discipline as
        # `DockerClient`/`AnthropicProvider`.
        if sdk_client is not None:
            self._client = sdk_client
            return
        resolved_key = api_key if api_key is not None else os.environ.get(_ENV_API_KEY)
        if not resolved_key:
            raise AIConfigurationError(f"Argus AI unavailable: {_ENV_API_KEY} is not configured.")
        self._client = genai.Client(api_key=resolved_key)

    def create_explanation(
        self, bundle: EvidenceBundle, *, config: AIConfig, retry_feedback: Optional[str] = None
    ) -> RawModelResponse:
        user_message = build_user_message(bundle, retry_feedback=retry_feedback)
        generation_config = build_generation_config(config)

        try:
            response = self._client.models.generate_content(
                model=config.model, contents=user_message, config=generation_config,
            )
        except genai_errors.APIError as exc:
            raise AIRequestError(f"Gemini request failed: {exc}") from exc
        except httpx.HTTPError as exc:
            raise AIRequestError(f"Could not reach Gemini: {exc}") from exc

        raw_text = response.text
        if not raw_text:
            raise AIRequestError("Gemini response contained no text content -- nothing to validate")

        try:
            tool_input = json.loads(raw_text)
        except json.JSONDecodeError as exc:
            raise AIRequestError(f"Gemini response was not valid JSON: {exc}") from exc

        if not isinstance(tool_input, dict):
            raise AIRequestError(f"Gemini response JSON was not an object (got {type(tool_input).__name__})")

        usage_metadata = response.usage_metadata
        return RawModelResponse(
            tool_input=tool_input,
            usage=AIUsage(
                input_tokens=getattr(usage_metadata, "prompt_token_count", None) if usage_metadata is not None else None,
                output_tokens=getattr(usage_metadata, "candidates_token_count", None) if usage_metadata is not None else None,
            ),
            model=config.model,
            provider=AIProviderName.GEMINI,
        )
