"""Tests for argus.ai.validation.validate_explanation -- the hallucination
guard. This is the single most important test file in Milestone 12: it
proves a fabricated evidence reference, a mismatched incident id, an
invalid confidence value, or an unknown field gets the *entire*
response rejected, not silently repaired.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from argus.ai.models import MAX_SUMMARY_CHARS, MAX_SUPPORTING_CLAIMS
from argus.ai.validation import ExplanationValidationError, validate_explanation
from argus.evidence.bundle import (
    ApplicationSummary,
    BundleMetadata,
    EvidenceBundle,
    EvidenceWindow,
    IncidentSummary,
    SignalItem,
    TransitionItem,
)

UTC = timezone.utc
T0 = datetime(2026, 8, 23, 12, 0, 0, tzinfo=UTC)


def make_bundle(*, incident_id=14, signals=(), transitions=(), observations=()) -> EvidenceBundle:
    incident = IncidentSummary(
        reference=f"incident:{incident_id}", incident_id=incident_id, status="open", opened_at=T0,
        closed_at=None, opening_status="UNHEALTHY", worst_status="UNHEALTHY", failure_signature="application:cnstrct",
    )
    application = ApplicationSummary(key="cnstrct", name="CNSTRCT", services=())
    window = EvidenceWindow(start=T0, end=T0, incident_open=True)
    metadata = BundleMetadata(
        generated_at=T0, window_start=T0, window_end=T0, assembler_version="1", truncated=False,
        omitted_counts={"signals": 0, "transitions": 0, "observations": 0}, evidence_subsystem_status="healthy",
        fingerprint="deadbeef",
    )
    return EvidenceBundle(
        incident=incident, application=application, window=window, timeline=(), signals=tuple(signals),
        transitions=tuple(transitions), observations=tuple(observations), metadata=metadata,
    )


def make_signal(ref_id=42) -> SignalItem:
    return SignalItem(
        reference=f"log_signal:{ref_id}", source_id=ref_id, category="db_connection_timeout", severity="high",
        count=27, first_seen_at=T0, last_seen_at=T0, sample="connection timeout after 30s",
        source_type="container_log", source_ref="stdout+stderr", container_id="docker-api", source_label="api",
    )


def make_transition(ref_id=18) -> TransitionItem:
    return TransitionItem(
        reference=f"health_transition:{ref_id}", source_id=ref_id, scope="service", label="postgres",
        from_status="HEALTHY", to_status="RESTARTING", occurred_at=T0,
    )


VALID_BUNDLE = make_bundle(signals=(make_signal(),), transitions=(make_transition(),))

VALID_RESPONSE = {
    "incident_id": 14,
    "summary": "The API became unhealthy following repeated database connection failures.",
    "root_cause_claim": {"text": "PostgreSQL instability is the likely cause.", "evidence_references": ["log_signal:42", "health_transition:18"]},
    "supporting_claims": [{"text": "Repeated timeouts observed.", "evidence_references": ["log_signal:42"]}],
    "confidence": "medium",
    "recommendation": {"category": "check_database", "explanation": "Inspect Postgres restart behavior."},
    "caveats": ["Temporal correlation alone does not establish causation."],
}


class TestValidResponse:
    def test_parses_successfully(self):
        explanation = validate_explanation(VALID_RESPONSE, bundle=VALID_BUNDLE)
        assert explanation.incident_id == 14
        assert explanation.confidence.value == "medium"
        assert explanation.root_cause_claim.text == "PostgreSQL instability is the likely cause."
        assert explanation.recommendation.category.value == "check_database"

    def test_missing_root_cause_is_allowed_when_evidence_insufficient(self):
        response = dict(VALID_RESPONSE, root_cause_claim=None, recommendation=None)
        explanation = validate_explanation(response, bundle=VALID_BUNDLE)
        assert explanation.root_cause_claim is None
        assert explanation.recommendation is None

    def test_empty_supporting_claims_and_caveats_allowed(self):
        response = dict(VALID_RESPONSE, supporting_claims=[], caveats=[])
        explanation = validate_explanation(response, bundle=VALID_BUNDLE)
        assert explanation.supporting_claims == ()
        assert explanation.caveats == ()


class TestFabricatedReference:
    def test_root_cause_claim_citing_unknown_reference_is_rejected(self):
        response = dict(VALID_RESPONSE, root_cause_claim={"text": "fabricated", "evidence_references": ["log_signal:9999"]})
        with pytest.raises(ExplanationValidationError, match="log_signal:9999"):
            validate_explanation(response, bundle=VALID_BUNDLE)

    def test_supporting_claim_citing_unknown_reference_rejects_entire_response(self):
        response = dict(
            VALID_RESPONSE,
            supporting_claims=[
                {"text": "real", "evidence_references": ["log_signal:42"]},
                {"text": "fake", "evidence_references": ["health_transition:99999"]},
            ],
        )
        with pytest.raises(ExplanationValidationError):
            validate_explanation(response, bundle=VALID_BUNDLE)

    def test_one_fabricated_reference_rejects_the_whole_response_not_just_that_reference(self):
        # A mix of one real and one fake reference in the same claim --
        # the entire response must still be rejected, not silently
        # trimmed down to the real reference alone.
        response = dict(
            VALID_RESPONSE,
            root_cause_claim={"text": "mixed", "evidence_references": ["log_signal:42", "log_signal:9999"]},
        )
        with pytest.raises(ExplanationValidationError):
            validate_explanation(response, bundle=VALID_BUNDLE)


class TestWrongIncidentId:
    def test_mismatched_incident_id_is_rejected(self):
        response = dict(VALID_RESPONSE, incident_id=999)
        with pytest.raises(ExplanationValidationError, match="999"):
            validate_explanation(response, bundle=VALID_BUNDLE)

    def test_non_integer_incident_id_is_rejected(self):
        response = dict(VALID_RESPONSE, incident_id="14")
        with pytest.raises(ExplanationValidationError):
            validate_explanation(response, bundle=VALID_BUNDLE)


class TestInvalidConfidence:
    def test_numeric_confidence_rejected(self):
        response = dict(VALID_RESPONSE, confidence="0.92")
        with pytest.raises(ExplanationValidationError):
            validate_explanation(response, bundle=VALID_BUNDLE)

    def test_percentage_confidence_rejected(self):
        response = dict(VALID_RESPONSE, confidence="87%")
        with pytest.raises(ExplanationValidationError):
            validate_explanation(response, bundle=VALID_BUNDLE)

    def test_unknown_confidence_word_rejected(self):
        response = dict(VALID_RESPONSE, confidence="certain")
        with pytest.raises(ExplanationValidationError):
            validate_explanation(response, bundle=VALID_BUNDLE)


class TestUnknownSchemaField:
    def test_unknown_top_level_field_rejected(self):
        response = dict(VALID_RESPONSE, extra_field="should not be here")
        with pytest.raises(ExplanationValidationError, match="extra_field"):
            validate_explanation(response, bundle=VALID_BUNDLE)

    def test_unknown_claim_field_rejected(self):
        response = dict(
            VALID_RESPONSE,
            root_cause_claim={"text": "x", "evidence_references": ["log_signal:42"], "confidence_score": 0.9},
        )
        with pytest.raises(ExplanationValidationError):
            validate_explanation(response, bundle=VALID_BUNDLE)

    def test_unknown_recommendation_field_rejected(self):
        response = dict(
            VALID_RESPONSE,
            recommendation={"category": "check_database", "command": "restart postgres"},
        )
        with pytest.raises(ExplanationValidationError):
            validate_explanation(response, bundle=VALID_BUNDLE)


class TestInvalidRecommendationCategory:
    def test_unknown_category_rejected(self):
        response = dict(VALID_RESPONSE, recommendation={"category": "restart_service", "explanation": None})
        with pytest.raises(ExplanationValidationError):
            validate_explanation(response, bundle=VALID_BUNDLE)


class TestClaimShapeValidation:
    def test_claim_without_evidence_references_rejected(self):
        response = dict(VALID_RESPONSE, root_cause_claim={"text": "no citations", "evidence_references": []})
        with pytest.raises(ExplanationValidationError):
            validate_explanation(response, bundle=VALID_BUNDLE)

    def test_claim_missing_text_field_rejected(self):
        response = dict(VALID_RESPONSE, root_cause_claim={"evidence_references": ["log_signal:42"]})
        with pytest.raises(ExplanationValidationError):
            validate_explanation(response, bundle=VALID_BUNDLE)


class TestBoundedFields:
    def test_summary_over_limit_rejected(self):
        response = dict(VALID_RESPONSE, summary="x" * (MAX_SUMMARY_CHARS + 1))
        with pytest.raises(ExplanationValidationError):
            validate_explanation(response, bundle=VALID_BUNDLE)

    def test_too_many_supporting_claims_rejected(self):
        claims = [{"text": "x", "evidence_references": ["log_signal:42"]} for _ in range(MAX_SUPPORTING_CLAIMS + 1)]
        response = dict(VALID_RESPONSE, supporting_claims=claims)
        with pytest.raises(ExplanationValidationError):
            validate_explanation(response, bundle=VALID_BUNDLE)

    def test_empty_summary_rejected(self):
        response = dict(VALID_RESPONSE, summary="")
        with pytest.raises(ExplanationValidationError):
            validate_explanation(response, bundle=VALID_BUNDLE)


class TestIncidentReferenceItselfIsCitable:
    def test_citing_the_incidents_own_reference_is_accepted(self):
        response = dict(
            VALID_RESPONSE,
            root_cause_claim={"text": "referring to the incident itself", "evidence_references": ["incident:14"]},
        )
        explanation = validate_explanation(response, bundle=VALID_BUNDLE)
        assert explanation.root_cause_claim.evidence_references == ("incident:14",)


class TestObservationReferencesAreCitable:
    def test_observation_reference_is_accepted_when_present_in_bundle(self):
        from argus.evidence.bundle import ObservationItem

        observation = ObservationItem(
            reference="observation:829", source_id=829, container_id="docker-api", source_label="api",
            observed_at=T0, docker_state="running", docker_health=None, restart_count=1,
            derived_status="UNHEALTHY", sampling_reason="at_transition", related_transition_reference="health_transition:18",
        )
        bundle = make_bundle(signals=(make_signal(),), transitions=(make_transition(),), observations=(observation,))
        response = dict(
            VALID_RESPONSE,
            supporting_claims=[{"text": "observed restart", "evidence_references": ["observation:829"]}],
        )
        explanation = validate_explanation(response, bundle=bundle)
        assert explanation.supporting_claims[0].evidence_references == ("observation:829",)
