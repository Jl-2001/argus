"""The one deterministic pipeline Milestone 12/12.1 adds:

    incident -> bundle -> cache lookup -> provider call -> validate -> persist

No agent framework, no multi-step loop, no LangGraph --
`IncidentExplanationService.explain` is a single, linear method.
Everything it depends on (bundle assembly, the AI provider, validation,
persistence) is injected, so it is fully testable with a mocked
provider and a real temporary SQLite database -- never a live network
call in the default test suite.

This service depends only on the `AIProvider` Protocol
(`argus.ai.providers.base`) -- never on `AnthropicProvider` or
`GeminiProvider` directly. There is deliberately no
``if provider == "gemini":`` branch anywhere in this file; which
concrete provider is in play was already decided before this service
was constructed (see `argus.ai.providers.resolve_provider`, called at
the CLI/bootstrap boundary).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Optional

from argus.ai.models import IncidentExplanation
from argus.ai.prompts import PROMPT_VERSION
from argus.ai.providers.base import DEFAULT_AI_CONFIG, AIConfig, AIProvider, AIUsage
from argus.ai.validation import ExplanationValidationError, validate_explanation
from argus.evidence.assembler import DEFAULT_ASSEMBLER_CONFIG, AssemblerConfig, assemble_evidence_bundle
from argus.realtime.emitter import emit_explanation_available
from argus.store.database import DuplicateExplanationError
from argus.store.repository import Repository

__all__ = ["BundleTooLargeError", "ExplainResult", "IncidentExplanationService"]


class BundleTooLargeError(RuntimeError):
    """The assembled bundle exceeds `AIConfig.max_bundle_chars` --
    refused locally, before any network call. `argus.evidence.assembler`
    already bounds bundle size via `AssemblerConfig.max_total_chars`;
    this is a second, independent, provider-agnostic check at the AI
    boundary in case that guarantee is ever violated by a future bug."""


@dataclass(frozen=True, slots=True)
class ExplainResult:
    """What `IncidentExplanationService.explain` returns -- the trusted
    explanation plus enough bookkeeping to answer "was this cached",
    "which provider/model produced it", and "what did this cost"."""

    explanation: IncidentExplanation
    cached: bool
    bundle_fingerprint: str
    provider: str
    model: str
    prompt_version: str
    usage: Optional[AIUsage]


class IncidentExplanationService:
    """Wires together the evidence assembler, the explanation cache, an
    `AIProvider`, and response validation. Nothing here talks to
    Docker, and nothing here is reachable from any deterministic
    monitoring path -- this is only ever invoked by `argus explain`.
    """

    def __init__(
        self,
        *,
        repository: Repository,
        ai_provider: AIProvider,
        ai_config: AIConfig = DEFAULT_AI_CONFIG,
        assembler_config: AssemblerConfig = DEFAULT_ASSEMBLER_CONFIG,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self._repository = repository
        self._ai_provider = ai_provider
        self._ai_config = ai_config
        self._assembler_config = assembler_config
        self._clock = clock

    def explain(self, incident_id: int, *, force_refresh: bool = False) -> ExplainResult:
        """Assemble the bundle, check the cache, and -- only on a miss
        or an explicit `force_refresh` -- call the configured provider,
        validate (with one controlled retry on a first invalid
        response), and persist.

        Raises `argus.evidence.assembler.IncidentNotFoundError` for an
        unknown incident, `argus.ai.providers.AIConfigurationError` if
        the provider's credential isn't configured, `BundleTooLargeError`
        if the assembled bundle somehow exceeds the configured bound,
        `argus.ai.providers.AIRequestError` on a network/API failure, or
        `ExplanationValidationError` if the response is invalid twice in
        a row. None of these persist an untrusted explanation.
        """

        now = self._clock()
        bundle = assemble_evidence_bundle(self._repository, incident_id, now=now, config=self._assembler_config)

        serialized_length = len(bundle.to_json(indent=None))
        if serialized_length > self._ai_config.max_bundle_chars:
            raise BundleTooLargeError(
                f"assembled bundle for incident {incident_id} is {serialized_length} characters, "
                f"exceeding the configured max_bundle_chars ({self._ai_config.max_bundle_chars}) -- "
                "refusing to send it to the model provider"
            )

        provider_name = self._ai_provider.provider_name.value

        if not force_refresh:
            cached = self._repository.get_cached_explanation(
                incident_id=incident_id, bundle_fingerprint=bundle.metadata.fingerprint,
                provider=provider_name, model=self._ai_config.model, prompt_version=PROMPT_VERSION,
            )
            if cached is not None:
                return ExplainResult(
                    explanation=IncidentExplanation.from_dict(_parse_response_json(cached.response_json)),
                    cached=True, bundle_fingerprint=bundle.metadata.fingerprint, provider=provider_name,
                    model=self._ai_config.model, prompt_version=PROMPT_VERSION, usage=None,
                )

        raw_response = self._ai_provider.create_explanation(bundle, config=self._ai_config)
        try:
            explanation = validate_explanation(raw_response.tool_input, bundle=bundle)
        except ExplanationValidationError as first_error:
            retry_feedback = (
                "Your previous response was rejected for the following reason: "
                f"{first_error}. Regenerate your response using only evidence_references that are "
                "actually present in the EvidenceBundle below, and ensure incident_id matches exactly."
            )
            raw_response = self._ai_provider.create_explanation(
                bundle, config=self._ai_config, retry_feedback=retry_feedback
            )
            explanation = validate_explanation(raw_response.tool_input, bundle=bundle)  # a second failure propagates

        try:
            self._repository.save_explanation(
                incident_id=incident_id, bundle_fingerprint=bundle.metadata.fingerprint, provider=provider_name,
                model=self._ai_config.model, prompt_version=PROMPT_VERSION, created_at=now,
                summary=explanation.summary,
                root_cause=explanation.root_cause_claim.text if explanation.root_cause_claim is not None else None,
                confidence=explanation.confidence.value, input_tokens=raw_response.usage.input_tokens,
                output_tokens=raw_response.usage.output_tokens, response_json=_dump_response_json(explanation),
            )
            # Announces an *already-persisted* explanation -- see
            # argus.realtime.emitter.emit_explanation_available's own
            # docstring on why this never triggers generation itself and
            # never fails this method if the event write itself fails.
            emit_explanation_available(
                self._repository, incident_id=incident_id, provider=provider_name,
                model=self._ai_config.model, bundle_fingerprint=bundle.metadata.fingerprint, now=now,
            )
        except DuplicateExplanationError:
            # Only reachable via `force_refresh=True` against evidence
            # that hasn't actually changed (the ordinary cache-lookup
            # path above already prevents this): a fresh, validated
            # response for the exact same (incident, fingerprint,
            # provider, model, prompt_version) key an earlier call
            # already persisted. That combination is still cached under
            # this cache's own semantics (distinctness is by
            # fingerprint/provider/model, not by "was force_refresh
            # used") -- so this is a benign no-op, not an error; the
            # caller still sees `cached=False` and the real usage from
            # the call that was actually made.
            pass

        return ExplainResult(
            explanation=explanation, cached=False, bundle_fingerprint=bundle.metadata.fingerprint,
            provider=provider_name, model=self._ai_config.model, prompt_version=PROMPT_VERSION,
            usage=raw_response.usage,
        )


def _dump_response_json(explanation: IncidentExplanation) -> str:
    return json.dumps(explanation.to_dict(), sort_keys=True, separators=(",", ":"))


def _parse_response_json(response_json: str) -> dict:
    return json.loads(response_json)
