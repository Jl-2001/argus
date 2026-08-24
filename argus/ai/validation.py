"""Turns a raw model response (a plain dict -- the tool-use `input` the
Anthropic SDK already parsed as JSON) into a trusted `IncidentExplanation`,
or rejects it outright. The application decides whether a response is
valid; the model does not -- nothing here treats a well-formatted or
confidently-worded response as evidence of its own correctness.

Every check is deterministic and mechanical: existence of required
fields, enum membership, length bounds, item-count bounds, and -- the
one that actually prevents hallucinated evidence -- that every single
`evidence_reference` cited anywhere in the response already exists in
the `EvidenceBundle` the model was given. A single fabricated reference
(e.g. `"log_signal:9999"` when no such signal was supplied) rejects the
*entire* response; nothing here silently drops the bad reference and
keeps the rest.

This module does not attempt to semantically verify that any sentence
is "true" -- that is not a tractable or honest thing for code to check.
Grounding instead comes from the citation requirement (enforced here)
and the EvidenceBundle boundary (enforced by `argus.evidence.assembler`)
together.
"""

from __future__ import annotations

from typing import Any, Optional

from argus.ai.models import (
    MAX_CAVEATS,
    MAX_CAVEAT_CHARS,
    MAX_CLAIM_TEXT_CHARS,
    MAX_EVIDENCE_REFERENCES_PER_CLAIM,
    MAX_RECOMMENDATION_EXPLANATION_CHARS,
    MAX_SUMMARY_CHARS,
    MAX_SUPPORTING_CLAIMS,
    ConfidenceLevel,
    ExplanationClaim,
    IncidentExplanation,
    Recommendation,
    RecommendationCategory,
)
from argus.evidence.bundle import EvidenceBundle

__all__ = ["ExplanationValidationError", "known_references", "validate_explanation"]

_TOP_LEVEL_FIELDS = frozenset(
    {"incident_id", "summary", "root_cause_claim", "supporting_claims", "confidence", "recommendation", "caveats"}
)
_CLAIM_FIELDS = frozenset({"text", "evidence_references"})
_RECOMMENDATION_FIELDS = frozenset({"category", "explanation"})


class ExplanationValidationError(ValueError):
    """Raised whenever a model response fails validation, for any
    reason -- a fabricated evidence reference, a mismatched incident id,
    an invalid enum value, an oversized field, or a malformed shape.
    Never persisted as a trusted explanation; the caller
    (`argus.ai.explain`) may retry once with this error's own message as
    feedback, but a second failure is final.
    """


def known_references(bundle: EvidenceBundle) -> set[str]:
    """Every citation-able reference the given bundle actually contains --
    the single definition of "real" a citation is checked against here,
    and the one tests reach for too rather than reconstructing their own
    (necessarily partial) copy of it."""

    references = {bundle.incident.reference}
    references.update(signal.reference for signal in bundle.signals)
    references.update(transition.reference for transition in bundle.transitions)
    references.update(observation.reference for observation in bundle.observations)
    return references


def _fail(message: str) -> None:
    raise ExplanationValidationError(message)


def _check_unknown_fields(data: Any, allowed: frozenset[str], *, where: str) -> None:
    if not isinstance(data, dict):
        _fail(f"{where} must be a JSON object, got {type(data).__name__}")
    unknown = set(data.keys()) - allowed
    if unknown:
        _fail(f"{where} contains unknown field(s): {sorted(unknown)}")


def _validate_claim(data: Any, known_references: set[str], *, where: str) -> ExplanationClaim:
    _check_unknown_fields(data, _CLAIM_FIELDS, where=where)

    text = data.get("text")
    if not isinstance(text, str) or not text.strip():
        _fail(f"{where}.text must be a non-empty string")
    if len(text) > MAX_CLAIM_TEXT_CHARS:
        _fail(f"{where}.text exceeds {MAX_CLAIM_TEXT_CHARS} characters")

    raw_references = data.get("evidence_references")
    if not isinstance(raw_references, list) or not raw_references:
        _fail(f"{where}.evidence_references must be a non-empty list -- every claim needs at least one citation")
    if len(raw_references) > MAX_EVIDENCE_REFERENCES_PER_CLAIM:
        _fail(f"{where}.evidence_references exceeds {MAX_EVIDENCE_REFERENCES_PER_CLAIM} entries")

    fabricated = [ref for ref in raw_references if not isinstance(ref, str) or ref not in known_references]
    if fabricated:
        _fail(
            f"{where} cites evidence_reference(s) not present in the supplied EvidenceBundle: {fabricated} "
            "-- the entire response is rejected, not just the bad reference"
        )

    return ExplanationClaim(text=text, evidence_references=tuple(raw_references))


def _validate_recommendation(data: Any, *, where: str) -> Optional[Recommendation]:
    if data is None:
        return None
    _check_unknown_fields(data, _RECOMMENDATION_FIELDS, where=where)

    category_raw = data.get("category")
    valid_categories = {c.value for c in RecommendationCategory}
    if category_raw not in valid_categories:
        _fail(f"{where}.category {category_raw!r} is not one of {sorted(valid_categories)}")

    explanation = data.get("explanation")
    if explanation is not None:
        if not isinstance(explanation, str):
            _fail(f"{where}.explanation must be a string or null")
        if len(explanation) > MAX_RECOMMENDATION_EXPLANATION_CHARS:
            _fail(f"{where}.explanation exceeds {MAX_RECOMMENDATION_EXPLANATION_CHARS} characters")

    return Recommendation(category=RecommendationCategory(category_raw), explanation=explanation)


def validate_explanation(raw: Any, *, bundle: EvidenceBundle) -> IncidentExplanation:
    """Validate a raw model response (already-parsed tool-use `input`
    dict) against `bundle`. Raises `ExplanationValidationError` with a
    specific, actionable message on any failure; returns a fully
    constructed, trusted `IncidentExplanation` on success.
    """

    _check_unknown_fields(raw, _TOP_LEVEL_FIELDS, where="response")

    incident_id = raw.get("incident_id")
    if not isinstance(incident_id, int) or isinstance(incident_id, bool):
        _fail("response.incident_id must be an integer")
    if incident_id != bundle.incident.incident_id:
        _fail(
            f"response.incident_id ({incident_id}) does not match the bundle's own incident id "
            f"({bundle.incident.incident_id})"
        )

    summary = raw.get("summary")
    if not isinstance(summary, str) or not summary.strip():
        _fail("response.summary must be a non-empty string")
    if len(summary) > MAX_SUMMARY_CHARS:
        _fail(f"response.summary exceeds {MAX_SUMMARY_CHARS} characters")

    confidence_raw = raw.get("confidence")
    valid_confidence = {c.value for c in ConfidenceLevel}
    if confidence_raw not in valid_confidence:
        _fail(f"response.confidence {confidence_raw!r} is not one of {sorted(valid_confidence)} -- no numeric confidence is accepted")

    known = known_references(bundle)

    root_cause_raw = raw.get("root_cause_claim")
    root_cause_claim = (
        _validate_claim(root_cause_raw, known, where="response.root_cause_claim")
        if root_cause_raw is not None else None
    )

    supporting_raw = raw.get("supporting_claims")
    if not isinstance(supporting_raw, list):
        _fail("response.supporting_claims must be a list")
    if len(supporting_raw) > MAX_SUPPORTING_CLAIMS:
        _fail(f"response.supporting_claims exceeds {MAX_SUPPORTING_CLAIMS} entries")
    supporting_claims = tuple(
        _validate_claim(claim, known, where=f"response.supporting_claims[{i}]")
        for i, claim in enumerate(supporting_raw)
    )

    recommendation = _validate_recommendation(raw.get("recommendation"), where="response.recommendation")

    caveats_raw = raw.get("caveats")
    if not isinstance(caveats_raw, list):
        _fail("response.caveats must be a list")
    if len(caveats_raw) > MAX_CAVEATS:
        _fail(f"response.caveats exceeds {MAX_CAVEATS} entries")
    for i, caveat in enumerate(caveats_raw):
        if not isinstance(caveat, str) or not caveat.strip():
            _fail(f"response.caveats[{i}] must be a non-empty string")
        if len(caveat) > MAX_CAVEAT_CHARS:
            _fail(f"response.caveats[{i}] exceeds {MAX_CAVEAT_CHARS} characters")

    return IncidentExplanation(
        incident_id=incident_id,
        summary=summary,
        root_cause_claim=root_cause_claim,
        supporting_claims=supporting_claims,
        confidence=ConfidenceLevel(confidence_raw),
        recommendation=recommendation,
        caveats=tuple(caveats_raw),
    )
