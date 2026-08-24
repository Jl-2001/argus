"""Tests for argus.ai.explain.IncidentExplanationService -- the full
pipeline (bundle -> cache check -> provider call -> validate -> persist)
against a real temporary SQLite database and mocked Anthropic/Gemini
providers. No real network call anywhere in this file.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from argus.ai.providers.anthropic import AnthropicProvider
from argus.ai.providers.base import AIConfig, AIProviderName
from argus.ai.providers.gemini import GeminiProvider
from argus.ai.explain import BundleTooLargeError, IncidentExplanationService
from argus.ai.prompts import PROMPT_VERSION
from argus.ai.validation import ExplanationValidationError
from argus.domain.models import HealthStatus
from argus.evidence.assembler import AssemblerConfig
from argus.store.database import open_database
from argus.store.repository import Repository

UTC = timezone.utc
T0 = datetime(2026, 8, 23, 12, 0, 0, tzinfo=UTC)


# --------------------------------------------------------------------------
# Fake Anthropic SDK plumbing
# --------------------------------------------------------------------------


class _FakeToolUseBlock:
    type = "tool_use"

    def __init__(self, input_dict):
        self.input = input_dict


class _FakeUsage:
    def __init__(self, input_tokens=100, output_tokens=50):
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens


class _FakeMessage:
    def __init__(self, content, usage=None, model="claude-sonnet-5"):
        self.content = content
        self.usage = usage if usage is not None else _FakeUsage()
        self.model = model


class _ScriptedMessagesAPI:
    """Returns one scripted response dict per call, in order."""

    def __init__(self, response_dicts: list[dict]):
        self._responses = list(response_dicts)
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return _FakeMessage([_FakeToolUseBlock(self._responses.pop(0))])


class _ScriptedSDKClient:
    def __init__(self, response_dicts: list[dict]):
        self.messages = _ScriptedMessagesAPI(response_dicts)


def make_client(response_dicts: list[dict]) -> tuple[AnthropicProvider, _ScriptedSDKClient]:
    sdk = _ScriptedSDKClient(response_dicts)
    return AnthropicProvider(sdk_client=sdk), sdk


def valid_response(incident_id: int, **overrides) -> dict:
    base = {
        "incident_id": incident_id,
        "summary": "The API became unhealthy following repeated database connection failures.",
        "root_cause_claim": None,
        "supporting_claims": [],
        "confidence": "low",
        "recommendation": None,
        "caveats": ["Temporal correlation alone does not establish causation."],
    }
    base.update(overrides)
    return base


# --------------------------------------------------------------------------
# Fixture builder
# --------------------------------------------------------------------------


def make_repo(tmp_path):
    conn = open_database(tmp_path / "a.db")
    return conn, Repository(conn)


def seed_incident(repo, *, opened_at=T0, key="cnstrct"):
    app_id = repo.upsert_application(key=key, name="CNSTRCT", is_standalone=False, observed_at=opened_at)
    t = repo.insert_transition(scope="application", scope_id=app_id, from_status=None, to_status=HealthStatus.UNHEALTHY, occurred_at=opened_at)
    incident_id = repo.open_incident(
        scope_id=app_id, failure_signature=f"application:{key}", opened_at=opened_at,
        opening_status=HealthStatus.UNHEALTHY, opening_transition_id=t,
    )
    return app_id, incident_id


# --------------------------------------------------------------------------
# Cache hit / miss
# --------------------------------------------------------------------------


class TestCacheMiss:
    def test_first_call_makes_exactly_one_api_call_and_persists(self, tmp_path):
        conn, repo = make_repo(tmp_path)
        app_id, incident_id = seed_incident(repo)
        client, sdk = make_client([valid_response(incident_id)])
        service = IncidentExplanationService(repository=repo, ai_provider=client, clock=lambda: T0 + timedelta(minutes=1))

        result = service.explain(incident_id)

        assert result.cached is False
        assert len(sdk.messages.calls) == 1
        assert repo.list_explanations_for_incident(incident_id) != ()
        conn.close()

    def test_usage_is_returned_and_persisted(self, tmp_path):
        conn, repo = make_repo(tmp_path)
        app_id, incident_id = seed_incident(repo)
        client, sdk = make_client([valid_response(incident_id)])
        service = IncidentExplanationService(repository=repo, ai_provider=client, clock=lambda: T0 + timedelta(minutes=1))

        result = service.explain(incident_id)

        assert result.usage.input_tokens == 100
        assert result.usage.output_tokens == 50
        stored = repo.list_explanations_for_incident(incident_id)[0]
        assert stored.input_tokens == 100
        assert stored.output_tokens == 50
        conn.close()


class TestCacheHit:
    def test_second_call_is_a_cache_hit_zero_additional_api_calls(self, tmp_path):
        conn, repo = make_repo(tmp_path)
        app_id, incident_id = seed_incident(repo)
        client, sdk = make_client([valid_response(incident_id)])
        service = IncidentExplanationService(repository=repo, ai_provider=client, clock=lambda: T0 + timedelta(minutes=1))

        first = service.explain(incident_id)
        second = service.explain(incident_id)

        assert first.cached is False
        assert second.cached is True
        assert len(sdk.messages.calls) == 1
        assert second.explanation.summary == first.explanation.summary
        conn.close()

    def test_cached_result_has_no_usage(self, tmp_path):
        conn, repo = make_repo(tmp_path)
        app_id, incident_id = seed_incident(repo)
        client, sdk = make_client([valid_response(incident_id)])
        service = IncidentExplanationService(repository=repo, ai_provider=client, clock=lambda: T0 + timedelta(minutes=1))

        service.explain(incident_id)
        second = service.explain(incident_id)
        assert second.usage is None


class TestUpdatedFingerprintTriggersNewCall:
    def test_new_evidence_changes_fingerprint_and_makes_a_new_call(self, tmp_path):
        conn, repo = make_repo(tmp_path)
        app_id, incident_id = seed_incident(repo)
        client, sdk = make_client([valid_response(incident_id), valid_response(incident_id, summary="updated summary after new evidence")])
        service = IncidentExplanationService(repository=repo, ai_provider=client, clock=lambda: T0 + timedelta(minutes=1))

        first = service.explain(incident_id)

        # new evidence arrives -- changes the bundle's fingerprint
        svc_id = repo.upsert_service(application_id=app_id, compose_service="api", name="api", observed_at=T0)
        container_row_id = repo.upsert_container(service_id=svc_id, container_id="docker-api", name="cnstrct-api-1", first_seen_at=T0, last_seen_at=T0)
        repo.insert_log_signal(
            application_id=app_id, container_row_id=container_row_id, category="db_connection_timeout",
            severity="high", normalized_signature="timeout", first_seen_at=T0, last_seen_at=T0, count=5,
            sample="connection timeout", source_type="container_log", source_ref="stdout+stderr",
        )

        second = service.explain(incident_id)

        assert second.cached is False
        assert second.bundle_fingerprint != first.bundle_fingerprint
        assert len(sdk.messages.calls) == 2
        assert len(repo.list_explanations_for_incident(incident_id)) == 2
        conn.close()


class TestForceRefresh:
    def test_force_refresh_bypasses_cache_even_with_unchanged_evidence(self, tmp_path):
        conn, repo = make_repo(tmp_path)
        app_id, incident_id = seed_incident(repo)
        client, sdk = make_client([valid_response(incident_id), valid_response(incident_id)])
        service = IncidentExplanationService(repository=repo, ai_provider=client, clock=lambda: T0 + timedelta(minutes=1))

        service.explain(incident_id)
        result = service.explain(incident_id, force_refresh=True)

        assert result.cached is False
        assert len(sdk.messages.calls) == 2


# --------------------------------------------------------------------------
# Retry logic
# --------------------------------------------------------------------------


class TestRetryLogic:
    def test_first_invalid_response_triggers_exactly_one_retry(self, tmp_path):
        conn, repo = make_repo(tmp_path)
        app_id, incident_id = seed_incident(repo)
        fabricated = valid_response(incident_id, root_cause_claim={"text": "fake", "evidence_references": ["log_signal:9999"]})
        client, sdk = make_client([fabricated, valid_response(incident_id)])
        service = IncidentExplanationService(repository=repo, ai_provider=client, clock=lambda: T0 + timedelta(minutes=1))

        result = service.explain(incident_id)

        assert result.cached is False
        assert len(sdk.messages.calls) == 2
        # the retry call includes feedback about what was wrong
        retry_message = sdk.messages.calls[1]["messages"][0]["content"]
        assert "rejected" in retry_message.lower()
        conn.close()

    def test_second_invalid_response_fails_with_no_persistence(self, tmp_path):
        conn, repo = make_repo(tmp_path)
        app_id, incident_id = seed_incident(repo)
        fabricated = valid_response(incident_id, root_cause_claim={"text": "fake", "evidence_references": ["log_signal:9999"]})
        client, sdk = make_client([fabricated, fabricated])
        service = IncidentExplanationService(repository=repo, ai_provider=client, clock=lambda: T0 + timedelta(minutes=1))

        with pytest.raises(ExplanationValidationError):
            service.explain(incident_id)

        assert len(sdk.messages.calls) == 2  # no uncontrolled retry loop
        assert repo.list_explanations_for_incident(incident_id) == ()
        conn.close()

    def test_wrong_incident_id_twice_fails_with_no_persistence(self, tmp_path):
        conn, repo = make_repo(tmp_path)
        app_id, incident_id = seed_incident(repo)
        wrong_id_response = valid_response(999999)
        client, sdk = make_client([wrong_id_response, wrong_id_response])
        service = IncidentExplanationService(repository=repo, ai_provider=client, clock=lambda: T0 + timedelta(minutes=1))

        with pytest.raises(ExplanationValidationError):
            service.explain(incident_id)
        assert len(sdk.messages.calls) == 2
        assert repo.list_explanations_for_incident(incident_id) == ()
        conn.close()


# --------------------------------------------------------------------------
# Bundle size guard
# --------------------------------------------------------------------------


class TestBundleTooLarge:
    def test_bundle_exceeding_configured_max_chars_fails_locally_no_api_call(self, tmp_path):
        conn, repo = make_repo(tmp_path)
        app_id, incident_id = seed_incident(repo)
        client, sdk = make_client([valid_response(incident_id)])
        ai_config = AIConfig(max_bundle_chars=10)  # deliberately tiny
        service = IncidentExplanationService(
            repository=repo, ai_provider=client, ai_config=ai_config, clock=lambda: T0 + timedelta(minutes=1)
        )

        with pytest.raises(BundleTooLargeError):
            service.explain(incident_id)

        assert len(sdk.messages.calls) == 0  # never even attempted a network call
        conn.close()


# --------------------------------------------------------------------------
# Nonexistent incident
# --------------------------------------------------------------------------


class TestNonexistentIncident:
    def test_raises_incident_not_found(self, tmp_path):
        from argus.evidence.assembler import IncidentNotFoundError

        conn, repo = make_repo(tmp_path)
        client, sdk = make_client([])
        service = IncidentExplanationService(repository=repo, ai_provider=client)

        with pytest.raises(IncidentNotFoundError):
            service.explain(999999)
        assert len(sdk.messages.calls) == 0
        conn.close()


# --------------------------------------------------------------------------
# Prompt version persisted
# --------------------------------------------------------------------------


class TestPromptVersionPersisted:
    def test_prompt_version_stored_with_explanation(self, tmp_path):
        conn, repo = make_repo(tmp_path)
        app_id, incident_id = seed_incident(repo)
        client, sdk = make_client([valid_response(incident_id)])
        service = IncidentExplanationService(repository=repo, ai_provider=client, clock=lambda: T0 + timedelta(minutes=1))

        result = service.explain(incident_id)

        assert result.prompt_version == PROMPT_VERSION
        stored = repo.get_cached_explanation(
            incident_id=incident_id, bundle_fingerprint=result.bundle_fingerprint,
            provider=result.provider, model=result.model, prompt_version=PROMPT_VERSION,
        )
        assert stored is not None
        conn.close()


# --------------------------------------------------------------------------
# Gemini plumbing -- mirrors the Anthropic fakes above, but returning raw
# JSON text (Gemini's own structured-output shape) instead of a tool_use
# block.
# --------------------------------------------------------------------------


class _FakeGeminiUsageMetadata:
    def __init__(self, prompt_token_count=200, candidates_token_count=75):
        self.prompt_token_count = prompt_token_count
        self.candidates_token_count = candidates_token_count


class _FakeGeminiResponse:
    def __init__(self, text):
        self.text = text
        self.usage_metadata = _FakeGeminiUsageMetadata()


class _ScriptedGeminiModelsAPI:
    def __init__(self, response_dicts: list[dict]):
        import json

        self._responses = [json.dumps(d) for d in response_dicts]
        self.calls = []

    def generate_content(self, **kwargs):
        self.calls.append(kwargs)
        return _FakeGeminiResponse(self._responses.pop(0))


class _ScriptedGeminiSDKClient:
    def __init__(self, response_dicts: list[dict]):
        self.models = _ScriptedGeminiModelsAPI(response_dicts)


def make_gemini_client(response_dicts: list[dict]) -> tuple[GeminiProvider, _ScriptedGeminiSDKClient]:
    sdk = _ScriptedGeminiSDKClient(response_dicts)
    return GeminiProvider(sdk_client=sdk), sdk


GEMINI_CONFIG = AIConfig(provider=AIProviderName.GEMINI)


# --------------------------------------------------------------------------
# Provider separation -- the core of Milestone 12.1's cache change
# --------------------------------------------------------------------------


class TestCacheSeparationByProvider:
    def test_gemini_and_anthropic_explanations_for_the_same_incident_coexist(self, tmp_path):
        conn, repo = make_repo(tmp_path)
        app_id, incident_id = seed_incident(repo)

        anthropic_client, anthropic_sdk = make_client([valid_response(incident_id, summary="claude summary")])
        anthropic_service = IncidentExplanationService(repository=repo, ai_provider=anthropic_client, clock=lambda: T0 + timedelta(minutes=1))
        claude_result = anthropic_service.explain(incident_id)

        gemini_client, gemini_sdk = make_gemini_client([valid_response(incident_id, summary="gemini summary")])
        gemini_service = IncidentExplanationService(
            repository=repo, ai_provider=gemini_client, ai_config=GEMINI_CONFIG, clock=lambda: T0 + timedelta(minutes=1)
        )
        gemini_result = gemini_service.explain(incident_id)

        assert claude_result.cached is False
        assert gemini_result.cached is False  # Gemini's own first call is NOT a hit against Claude's cache entry
        assert claude_result.explanation.summary == "claude summary"
        assert gemini_result.explanation.summary == "gemini summary"

        history = repo.list_explanations_for_incident(incident_id)
        assert len(history) == 2
        assert {row.provider for row in history} == {"anthropic", "gemini"}
        conn.close()

    def test_second_call_to_the_same_provider_is_still_a_cache_hit_with_two_providers_present(self, tmp_path):
        conn, repo = make_repo(tmp_path)
        app_id, incident_id = seed_incident(repo)

        anthropic_client, anthropic_sdk = make_client([valid_response(incident_id)])
        anthropic_service = IncidentExplanationService(repository=repo, ai_provider=anthropic_client, clock=lambda: T0 + timedelta(minutes=1))
        anthropic_service.explain(incident_id)

        gemini_client, gemini_sdk = make_gemini_client([valid_response(incident_id)])
        gemini_service = IncidentExplanationService(
            repository=repo, ai_provider=gemini_client, ai_config=GEMINI_CONFIG, clock=lambda: T0 + timedelta(minutes=1)
        )
        gemini_service.explain(incident_id)

        # re-asking Claude again must still be a cache hit for Claude's
        # own entry, unaffected by Gemini's entry now also existing
        second_claude = anthropic_service.explain(incident_id)
        assert second_claude.cached is True
        assert len(anthropic_sdk.messages.calls) == 1
        conn.close()


class TestCacheSeparationByModel:
    def test_different_model_same_provider_is_not_a_cache_hit(self, tmp_path):
        conn, repo = make_repo(tmp_path)
        app_id, incident_id = seed_incident(repo)

        client_a, sdk_a = make_client([valid_response(incident_id)])
        service_a = IncidentExplanationService(
            repository=repo, ai_provider=client_a, ai_config=AIConfig(model="claude-sonnet-5"),
            clock=lambda: T0 + timedelta(minutes=1),
        )
        result_a = service_a.explain(incident_id)

        client_b, sdk_b = make_client([valid_response(incident_id)])
        service_b = IncidentExplanationService(
            repository=repo, ai_provider=client_b, ai_config=AIConfig(model="claude-opus-5"),
            clock=lambda: T0 + timedelta(minutes=1),
        )
        result_b = service_b.explain(incident_id)

        assert result_a.cached is False
        assert result_b.cached is False  # a different model is a genuinely different cache entry
        assert len(repo.list_explanations_for_incident(incident_id)) == 2
        conn.close()


class TestGeminiCacheCycle:
    def test_gemini_cache_hit_on_second_identical_call(self, tmp_path):
        conn, repo = make_repo(tmp_path)
        app_id, incident_id = seed_incident(repo)
        client, sdk = make_gemini_client([valid_response(incident_id)])
        service = IncidentExplanationService(
            repository=repo, ai_provider=client, ai_config=GEMINI_CONFIG, clock=lambda: T0 + timedelta(minutes=1)
        )

        first = service.explain(incident_id)
        second = service.explain(incident_id)

        assert first.cached is False
        assert second.cached is True
        assert len(sdk.models.calls) == 1
        assert second.explanation.summary == first.explanation.summary
        conn.close()

    def test_gemini_new_call_when_fingerprint_changes(self, tmp_path):
        conn, repo = make_repo(tmp_path)
        app_id, incident_id = seed_incident(repo)
        client, sdk = make_gemini_client(
            [valid_response(incident_id), valid_response(incident_id, summary="updated after new evidence")]
        )
        service = IncidentExplanationService(
            repository=repo, ai_provider=client, ai_config=GEMINI_CONFIG, clock=lambda: T0 + timedelta(minutes=1)
        )

        service.explain(incident_id)

        svc_id = repo.upsert_service(application_id=app_id, compose_service="api", name="api", observed_at=T0)
        container_row_id = repo.upsert_container(service_id=svc_id, container_id="docker-api", name="cnstrct-api-1", first_seen_at=T0, last_seen_at=T0)
        repo.insert_log_signal(
            application_id=app_id, container_row_id=container_row_id, category="oom", severity="critical",
            normalized_signature="oom", first_seen_at=T0, last_seen_at=T0, count=1, sample="oom killed",
            source_type="container_log", source_ref="stdout+stderr",
        )

        second = service.explain(incident_id)
        assert second.cached is False
        assert len(sdk.models.calls) == 2
        conn.close()

    def test_gemini_usage_is_normalized_and_persisted(self, tmp_path):
        conn, repo = make_repo(tmp_path)
        app_id, incident_id = seed_incident(repo)
        client, sdk = make_gemini_client([valid_response(incident_id)])
        service = IncidentExplanationService(
            repository=repo, ai_provider=client, ai_config=GEMINI_CONFIG, clock=lambda: T0 + timedelta(minutes=1)
        )

        result = service.explain(incident_id)
        assert result.usage.input_tokens == 200
        assert result.usage.output_tokens == 75
        stored = repo.list_explanations_for_incident(incident_id)[0]
        assert stored.provider == "gemini"
        assert stored.input_tokens == 200
        assert stored.output_tokens == 75
        conn.close()
