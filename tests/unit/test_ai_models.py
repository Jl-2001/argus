"""Tests for argus.ai.models: construction, bounds, enum coercion, and
to_dict/from_dict round-tripping for the structured explanation shape."""

from __future__ import annotations

import pytest

from argus.ai.models import (
    MAX_CAVEATS,
    MAX_CAVEAT_CHARS,
    MAX_CLAIM_TEXT_CHARS,
    MAX_RECOMMENDATION_EXPLANATION_CHARS,
    MAX_SUMMARY_CHARS,
    MAX_SUPPORTING_CLAIMS,
    ConfidenceLevel,
    ExplanationClaim,
    IncidentExplanation,
    Recommendation,
    RecommendationCategory,
)


def make_explanation(**overrides) -> IncidentExplanation:
    fields = dict(
        incident_id=14,
        summary="The API became unhealthy following repeated database connection failures.",
        root_cause_claim=ExplanationClaim(text="PostgreSQL instability is the likely cause.", evidence_references=("log_signal:42",)),
        supporting_claims=(ExplanationClaim(text="Repeated timeouts observed.", evidence_references=("log_signal:42",)),),
        confidence=ConfidenceLevel.MEDIUM,
        recommendation=Recommendation(category=RecommendationCategory.CHECK_DATABASE, explanation="Inspect Postgres."),
        caveats=("Temporal correlation alone does not establish causation.",),
    )
    fields.update(overrides)
    return IncidentExplanation(**fields)


class TestConstructionAndCoercion:
    def test_valid_construction(self):
        explanation = make_explanation()
        assert explanation.confidence is ConfidenceLevel.MEDIUM

    def test_string_confidence_is_coerced(self):
        explanation = make_explanation(confidence="high")
        assert explanation.confidence is ConfidenceLevel.HIGH

    def test_none_root_cause_is_allowed(self):
        explanation = make_explanation(root_cause_claim=None)
        assert explanation.root_cause_claim is None

    def test_none_recommendation_is_allowed(self):
        explanation = make_explanation(recommendation=None)
        assert explanation.recommendation is None

    def test_empty_supporting_claims_and_caveats_allowed(self):
        explanation = make_explanation(supporting_claims=(), caveats=())
        assert explanation.supporting_claims == ()
        assert explanation.caveats == ()


class TestBounds:
    def test_summary_too_long_rejected(self):
        with pytest.raises(ValueError):
            make_explanation(summary="x" * (MAX_SUMMARY_CHARS + 1))

    def test_empty_summary_rejected(self):
        with pytest.raises(ValueError):
            make_explanation(summary="")

    def test_too_many_supporting_claims_rejected(self):
        claims = tuple(ExplanationClaim(text="x", evidence_references=("log_signal:1",)) for _ in range(MAX_SUPPORTING_CLAIMS + 1))
        with pytest.raises(ValueError):
            make_explanation(supporting_claims=claims)

    def test_too_many_caveats_rejected(self):
        with pytest.raises(ValueError):
            make_explanation(caveats=tuple(f"caveat {i}" for i in range(MAX_CAVEATS + 1)))

    def test_caveat_too_long_rejected(self):
        with pytest.raises(ValueError):
            make_explanation(caveats=("x" * (MAX_CAVEAT_CHARS + 1),))

    def test_claim_text_too_long_rejected(self):
        with pytest.raises(ValueError):
            ExplanationClaim(text="x" * (MAX_CLAIM_TEXT_CHARS + 1), evidence_references=("log_signal:1",))

    def test_claim_with_no_references_rejected(self):
        with pytest.raises(ValueError):
            ExplanationClaim(text="valid text", evidence_references=())

    def test_recommendation_explanation_too_long_rejected(self):
        with pytest.raises(ValueError):
            Recommendation(category=RecommendationCategory.CHECK_DATABASE, explanation="x" * (MAX_RECOMMENDATION_EXPLANATION_CHARS + 1))


class TestNoNumericConfidence:
    def test_unknown_confidence_string_rejected(self):
        with pytest.raises(ValueError):
            make_explanation(confidence="87%")

    def test_numeric_confidence_rejected(self):
        with pytest.raises(ValueError):
            make_explanation(confidence="0.92")


class TestRecommendationCategoryEnum:
    def test_no_safe_recommendation_is_a_valid_category(self):
        rec = Recommendation(category=RecommendationCategory.NO_SAFE_RECOMMENDATION)
        assert rec.category is RecommendationCategory.NO_SAFE_RECOMMENDATION

    def test_seven_categories_exist(self):
        assert len(RecommendationCategory) == 7

    def test_no_executable_command_categories_exist(self):
        values = {c.value for c in RecommendationCategory}
        for forbidden in ("restart", "delete", "stop", "prune", "kill", "remove"):
            assert not any(forbidden in v for v in values)


class TestSerializationRoundTrip:
    def test_to_dict_from_dict_round_trip(self):
        original = make_explanation()
        rebuilt = IncidentExplanation.from_dict(original.to_dict())
        assert rebuilt == original

    def test_to_dict_uses_plain_json_safe_values(self):
        payload = make_explanation().to_dict()
        assert payload["confidence"] == "medium"
        assert payload["recommendation"]["category"] == "check_database"
        assert isinstance(payload["supporting_claims"], list)

    def test_round_trip_with_none_root_cause_and_recommendation(self):
        original = make_explanation(root_cause_claim=None, recommendation=None)
        rebuilt = IncidentExplanation.from_dict(original.to_dict())
        assert rebuilt == original
