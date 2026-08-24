"""Milestone 12 / 12.1 -- optional real-provider smoke tests.

Each provider's real test is skipped independently, gated on BOTH that
provider's own credential AND an explicit opt-in
(`live_ai_test_eligible` in `live_ai_gate.py`): `TestRealClaudeSmokeTest`
skips unless `ANTHROPIC_API_KEY` is set *and* `ARGUS_RUN_LIVE_AI_TESTS=1`;
`TestRealGeminiSmokeTest` skips unless `GEMINI_API_KEY` is set *and*
`ARGUS_RUN_LIVE_AI_TESTS=1`. A key alone is deliberately not enough --
`python -m pytest` (no flag) must make zero real AI network calls even
in a shell where a provider key happens to be configured for unrelated
work; a live run requires `ARGUS_RUN_LIVE_AI_TESTS=1 python -m pytest
-m ai`. Neither test ever constructs the other provider's client -- a
missing/blocked Anthropic key (e.g. exhausted credits) must never
prevent the Gemini test from running, and vice versa. Each makes
exactly one real, paid API call (a second, cache-hit invocation must
make zero additional calls) against a real incident from the
disposable `argus-test-stack`. Never sends real/private application
logs -- only the disposable stack's own synthetic log-emitter content.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

import pytest

import anthropic
from argus.ai.explain import IncidentExplanationService
from argus.ai.providers.anthropic import AnthropicProvider
from argus.ai.providers.base import AIConfig, AIProviderName
from argus.ai.providers.gemini import GeminiProvider
from argus.ai.validation import known_references
from argus.collector.loop import CollectorLoop
from argus.collectors.docker_client import DockerClient
from argus.evidence.assembler import DEFAULT_ASSEMBLER_CONFIG, assemble_evidence_bundle

from conftest import TEST_PROJECT_NAME, safe_stop, safe_start, wait_until, compose_container_id
from test_chaos_stack import TEST_CONFIG, TEST_RULES, APPLICATION_FAILURE_SIGNATURE, _argus_test_stack_is_healthy
from tests.integration.live_ai_gate import live_ai_test_eligible

pytestmark = [pytest.mark.integration, pytest.mark.docker, pytest.mark.ai]

_HAS_ANTHROPIC_KEY = bool(os.environ.get("ANTHROPIC_API_KEY"))
_HAS_GEMINI_KEY = bool(os.environ.get("GEMINI_API_KEY"))
_RUN_LIVE_AI_TESTS = os.environ.get("ARGUS_RUN_LIVE_AI_TESTS")
_ANTHROPIC_LIVE_ELIGIBLE = live_ai_test_eligible(
    api_key=os.environ.get("ANTHROPIC_API_KEY"), live_flag=_RUN_LIVE_AI_TESTS
)
_GEMINI_LIVE_ELIGIBLE = live_ai_test_eligible(
    api_key=os.environ.get("GEMINI_API_KEY"), live_flag=_RUN_LIVE_AI_TESTS
)


class _CountingMessagesAPI:
    """Wraps the real Anthropic SDK's messages API purely to count calls
    -- every actual request still goes to the real Anthropic API
    unchanged."""

    def __init__(self, real_messages_api) -> None:
        self._real = real_messages_api
        self.call_count = 0

    def create(self, **kwargs):
        self.call_count += 1
        return self._real.create(**kwargs)


class _CountingModelsAPI:
    """Same idea for the real google-genai SDK's `models.generate_content`
    -- counts calls while every actual request still goes to the real
    Gemini API unchanged."""

    def __init__(self, real_models_api) -> None:
        self._real = real_models_api
        self.call_count = 0

    def generate_content(self, **kwargs):
        self.call_count += 1
        return self._real.generate_content(**kwargs)


def _wait_for_open_incident(repository, loop, raw_docker):
    """Shared setup for both providers' smoke tests: drive the real
    disposable stack until evidence exists and an application-scope
    incident is open, and return its id. Never touches private/real
    application logs -- only the test stack's own synthetic
    log-emitter output."""

    def evidence_ready():
        tick = loop.run_once()
        assert tick.success
        app = repository.get_application(TEST_PROJECT_NAME)
        if app is None:
            return False
        signals = repository.list_log_signals_for_application(
            app.id, since=datetime.now(timezone.utc) - timedelta(hours=1)
        )
        return len(signals) >= 1

    wait_until(evidence_ready, timeout=30, interval=1, description="real evidence collected from log-emitter")

    container_id = compose_container_id("healthy-api")
    safe_stop(raw_docker, container_id)
    wait_until(
        lambda: raw_docker.containers.get(container_id).status == "exited",
        timeout=20, interval=1, description="healthy-api exited",
    )

    tick = loop.run_once()
    assert tick.success

    open_incident = repository.get_open_incident(failure_signature=APPLICATION_FAILURE_SIGNATURE)
    assert open_incident is not None
    return container_id, open_incident.id


def _assert_citations_are_real(result, repository, incident_id):
    # The same anti-hallucination invariant `argus.ai.validation` already
    # enforced inside `service.explain()` (a fabricated citation would
    # have raised there, before we ever got `result`) -- re-derived here
    # from a freshly-assembled bundle, not reconstructed by hand, so this
    # check can't drift out of sync with what "real" means (signals,
    # health transitions, *and* observations, not just signals).
    bundle = assemble_evidence_bundle(
        repository, incident_id, now=datetime.now(timezone.utc), config=DEFAULT_ASSEMBLER_CONFIG
    )
    known = known_references(bundle)
    all_refs = set()
    if result.explanation.root_cause_claim is not None:
        all_refs.update(result.explanation.root_cause_claim.evidence_references)
    for claim in result.explanation.supporting_claims:
        all_refs.update(claim.evidence_references)
    assert all_refs <= known or all_refs == set()  # every cited ref (if any) is real


@pytest.mark.skipif(
    not _ANTHROPIC_LIVE_ELIGIBLE,
    reason=(
        "real AI smoke test: SKIPPED — requires ANTHROPIC_API_KEY configured AND "
        "ARGUS_RUN_LIVE_AI_TESTS=1 (a key alone never triggers a real, billed call)"
    ),
)
class TestRealClaudeSmokeTest:
    def test_real_explanation_against_a_real_incident(self, stack, raw_docker, argus_db, log_emitter, capsys):
        db_path, connection, repository = argus_db
        client = DockerClient()
        loop = CollectorLoop(client=client, repository=repository, config=TEST_CONFIG, rules=TEST_RULES)

        try:
            container_id, incident_id = _wait_for_open_incident(repository, loop, raw_docker)

            real_sdk_client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
            counting_api = _CountingMessagesAPI(real_sdk_client.messages)
            real_sdk_client.messages = counting_api  # type: ignore[assignment]
            ai_provider = AnthropicProvider(sdk_client=real_sdk_client)

            service = IncidentExplanationService(
                repository=repository, ai_provider=ai_provider,
                ai_config=AIConfig(provider=AIProviderName.ANTHROPIC, timeout_seconds=60.0),
                clock=lambda: datetime.now(timezone.utc),
            )

            # -- exactly one real API call --
            result = service.explain(incident_id)
            assert result.cached is False
            assert counting_api.call_count == 1

            # -- schema validation already happened inside explain() (it
            # would have raised otherwise); confirm the shape directly --
            assert result.explanation.incident_id == incident_id
            assert result.explanation.confidence.value in ("low", "medium", "high")
            assert result.provider == "anthropic"

            _assert_citations_are_real(result, repository, incident_id)

            # -- usage returned --
            assert result.usage is not None
            assert result.usage.input_tokens is not None
            assert result.usage.output_tokens is not None

            # -- explanation persisted --
            stored = repository.get_cached_explanation(
                incident_id=incident_id, bundle_fingerprint=result.bundle_fingerprint,
                provider=result.provider, model=result.model, prompt_version=result.prompt_version,
            )
            assert stored is not None

            # -- second identical invocation is a cache hit, zero
            # additional API calls --
            second = service.explain(incident_id)
            assert second.cached is True
            assert counting_api.call_count == 1  # unchanged

            print(
                f"\nreal AI smoke test [anthropic]: model={result.model} "
                f"input_tokens={result.usage.input_tokens} output_tokens={result.usage.output_tokens} "
                f"confidence={result.explanation.confidence.value} cache_hit_verified=True"
            )
        finally:
            safe_start(raw_docker, container_id)
            wait_until(
                lambda: _argus_test_stack_is_healthy(DockerClient()),
                timeout=30, interval=2, description="argus-test-stack healthy before next test",
                on_timeout=lambda: "leaving healthy-api in whatever state it is in -- see the failure above",
            )


@pytest.mark.skipif(
    not _GEMINI_LIVE_ELIGIBLE,
    reason=(
        "real AI smoke test: SKIPPED — requires GEMINI_API_KEY configured AND "
        "ARGUS_RUN_LIVE_AI_TESTS=1 (a key alone never triggers a real, billed call)"
    ),
)
class TestRealGeminiSmokeTest:
    """Independent of Anthropic entirely: this class never constructs
    `anthropic.Anthropic` or `AnthropicProvider`. A missing or
    credit-exhausted Anthropic account must never affect this test."""

    def test_real_explanation_against_a_real_incident(self, stack, raw_docker, argus_db, log_emitter, capsys):
        from google import genai

        db_path, connection, repository = argus_db
        client = DockerClient()
        loop = CollectorLoop(client=client, repository=repository, config=TEST_CONFIG, rules=TEST_RULES)

        try:
            container_id, incident_id = _wait_for_open_incident(repository, loop, raw_docker)

            real_sdk_client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
            counting_api = _CountingModelsAPI(real_sdk_client.models)
            real_sdk_client._models = counting_api  # type: ignore[attr-defined]  # `.models` is a read-only property; patch the backing attribute it reads from
            ai_provider = GeminiProvider(sdk_client=real_sdk_client)

            service = IncidentExplanationService(
                repository=repository, ai_provider=ai_provider,
                ai_config=AIConfig(provider=AIProviderName.GEMINI, timeout_seconds=60.0),
                clock=lambda: datetime.now(timezone.utc),
            )

            # -- exactly one real API call --
            result = service.explain(incident_id)
            assert result.cached is False
            assert counting_api.call_count == 1

            # -- structured output parsed and validated by the same
            # shared validator Anthropic uses; confirm the shape --
            assert result.explanation.incident_id == incident_id
            assert result.explanation.confidence.value in ("low", "medium", "high")
            assert result.provider == "gemini"

            _assert_citations_are_real(result, repository, incident_id)

            # -- usage returned (normalized AIUsage; Gemini's own field
            # names never leak past the provider adapter) --
            assert result.usage is not None
            assert result.usage.input_tokens is not None
            assert result.usage.output_tokens is not None

            # -- explanation persisted under its own provider-scoped
            # cache key -- coexists with any Anthropic row for the same
            # incident rather than colliding with it --
            stored = repository.get_cached_explanation(
                incident_id=incident_id, bundle_fingerprint=result.bundle_fingerprint,
                provider=result.provider, model=result.model, prompt_version=result.prompt_version,
            )
            assert stored is not None
            assert stored.provider == "gemini"

            # -- second identical invocation is a cache hit, zero
            # additional API calls --
            second = service.explain(incident_id)
            assert second.cached is True
            assert counting_api.call_count == 1  # unchanged

            print(
                f"\nreal AI smoke test [gemini]: model={result.model} "
                f"input_tokens={result.usage.input_tokens} output_tokens={result.usage.output_tokens} "
                f"confidence={result.explanation.confidence.value} cache_hit_verified=True"
            )
        finally:
            safe_start(raw_docker, container_id)
            wait_until(
                lambda: _argus_test_stack_is_healthy(DockerClient()),
                timeout=30, interval=2, description="argus-test-stack healthy before next test",
                on_timeout=lambda: "leaving healthy-api in whatever state it is in -- see the failure above",
            )


def test_real_ai_smoke_test_skip_is_reported_when_no_anthropic_key(capsys):
    if _ANTHROPIC_LIVE_ELIGIBLE:
        pytest.skip("ANTHROPIC_API_KEY + ARGUS_RUN_LIVE_AI_TESTS=1 are both set -- the real smoke test above ran instead")
    print(
        "real AI smoke test [anthropic]: SKIPPED — requires ANTHROPIC_API_KEY configured "
        f"(present={_HAS_ANTHROPIC_KEY}) AND ARGUS_RUN_LIVE_AI_TESTS=1 (present={_RUN_LIVE_AI_TESTS == '1'})"
    )


def test_real_ai_smoke_test_skip_is_reported_when_no_gemini_key(capsys):
    if _GEMINI_LIVE_ELIGIBLE:
        pytest.skip("GEMINI_API_KEY + ARGUS_RUN_LIVE_AI_TESTS=1 are both set -- the real smoke test above ran instead")
    print(
        "real AI smoke test [gemini]: SKIPPED — requires GEMINI_API_KEY configured "
        f"(present={_HAS_GEMINI_KEY}) AND ARGUS_RUN_LIVE_AI_TESTS=1 (present={_RUN_LIVE_AI_TESTS == '1'})"
    )
