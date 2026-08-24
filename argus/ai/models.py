"""The structured incident-explanation shape Claude must produce, and
nothing else. No free-form text is ever accepted as a trusted
explanation -- see ``argus.ai.validation`` for how a raw model response
gets turned into (or rejected as) one of these.

Every numeric-looking confidence claim ("87%", "0.92") is deliberately
impossible to represent here: ``confidence`` is a closed three-value
enum, not a float, because Argus has no real calibration system backing
a number -- a fake-precise percentage would be exactly the kind of
manufactured certainty this milestone exists to prevent.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Optional

__all__ = [
    "ConfidenceLevel",
    "RecommendationCategory",
    "MAX_SUMMARY_CHARS",
    "MAX_CLAIM_TEXT_CHARS",
    "MAX_EVIDENCE_REFERENCES_PER_CLAIM",
    "MAX_SUPPORTING_CLAIMS",
    "MAX_CAVEATS",
    "MAX_CAVEAT_CHARS",
    "MAX_RECOMMENDATION_EXPLANATION_CHARS",
    "ExplanationClaim",
    "Recommendation",
    "IncidentExplanation",
]

# --------------------------------------------------------------------------
# Output bounds -- keep future UI/storage safe and response cost bounded.
# Deliberately generous enough for a real explanation, deliberately far
# short of "arbitrary length free text".
# --------------------------------------------------------------------------

MAX_SUMMARY_CHARS = 1000
MAX_CLAIM_TEXT_CHARS = 500
MAX_EVIDENCE_REFERENCES_PER_CLAIM = 20
MAX_SUPPORTING_CLAIMS = 10
MAX_CAVEATS = 10
MAX_CAVEAT_CHARS = 300
MAX_RECOMMENDATION_EXPLANATION_CHARS = 500


class ConfidenceLevel(str, Enum):
    """How strongly the supplied evidence supports the explanation --
    never a number. See the Milestone 12 report for the model-facing
    interpretation guidance (HIGH = evidence directly identifies a
    cause; MEDIUM = correlated facts strongly suggest one without a
    direct causal signal; LOW = only temporal correlation or incomplete
    evidence)."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class RecommendationCategory(str, Enum):
    """A closed set of *advisory, read-only* recommendation shapes --
    deliberately not an executable command. Milestone 12 has no
    remediation/policy architecture; a model is never allowed to
    recommend (let alone trigger) a mutating action such as "restart
    postgres" or "docker system prune"."""

    INSPECT_LOGS = "inspect_logs"
    CHECK_DATABASE = "check_database"
    CHECK_RESOURCE_USAGE = "check_resource_usage"
    CHECK_NETWORK = "check_network"
    CHECK_CONFIGURATION = "check_configuration"
    CHECK_DEPENDENCY = "check_dependency"
    NO_SAFE_RECOMMENDATION = "no_safe_recommendation"


def _require_bounded_str(value: Any, field_name: str, max_length: int) -> str:
    if not isinstance(value, str) or value.strip() == "":
        raise ValueError(f"{field_name} must be a non-empty string")
    if len(value) > max_length:
        raise ValueError(f"{field_name} exceeds {max_length} characters ({len(value)})")
    return value


@dataclass(frozen=True, slots=True)
class ExplanationClaim:
    """One factual claim, with its own citations -- preferred over a
    single giant evidence list attached to the whole explanation, so a
    future reader (human or another model) knows exactly which claim
    each reference backs.
    """

    text: str
    evidence_references: tuple[str, ...]

    def __post_init__(self) -> None:
        _require_bounded_str(self.text, "claim text", MAX_CLAIM_TEXT_CHARS)
        refs = tuple(self.evidence_references)
        if not refs:
            raise ValueError("a claim must cite at least one evidence_reference -- an uncited claim is not grounded")
        if len(refs) > MAX_EVIDENCE_REFERENCES_PER_CLAIM:
            raise ValueError(f"claim has more than {MAX_EVIDENCE_REFERENCES_PER_CLAIM} evidence references")
        for ref in refs:
            if not isinstance(ref, str) or not ref.strip():
                raise ValueError("evidence_references must be non-empty strings")
        object.__setattr__(self, "evidence_references", refs)

    def to_dict(self) -> dict[str, Any]:
        return {"text": self.text, "evidence_references": list(self.evidence_references)}


@dataclass(frozen=True, slots=True)
class Recommendation:
    """An advisory, read-only next step -- a closed category plus an
    optional human-readable elaboration, never an executable command."""

    category: RecommendationCategory
    explanation: Optional[str] = None

    def __post_init__(self) -> None:
        category = self.category if isinstance(self.category, RecommendationCategory) else RecommendationCategory(self.category)
        object.__setattr__(self, "category", category)
        if self.explanation is not None:
            _require_bounded_str(self.explanation, "recommendation explanation", MAX_RECOMMENDATION_EXPLANATION_CHARS)

    def to_dict(self) -> dict[str, Any]:
        return {"category": self.category.value, "explanation": self.explanation}


@dataclass(frozen=True, slots=True)
class IncidentExplanation:
    """The complete, validated, trusted explanation for one incident.

    This type is only ever constructed by ``argus.ai.validation``
    (fresh from a model response) or reconstructed from Argus's own
    previously-persisted, already-validated JSON (``from_dict``, used
    for cache hits) -- never built directly from untrusted input.
    """

    incident_id: int
    summary: str
    root_cause_claim: Optional[ExplanationClaim]
    supporting_claims: tuple[ExplanationClaim, ...]
    confidence: ConfidenceLevel
    recommendation: Optional[Recommendation]
    caveats: tuple[str, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.incident_id, int) or isinstance(self.incident_id, bool):
            raise TypeError("incident_id must be an int")
        _require_bounded_str(self.summary, "summary", MAX_SUMMARY_CHARS)
        confidence = self.confidence if isinstance(self.confidence, ConfidenceLevel) else ConfidenceLevel(self.confidence)
        object.__setattr__(self, "confidence", confidence)

        supporting = tuple(self.supporting_claims)
        if len(supporting) > MAX_SUPPORTING_CLAIMS:
            raise ValueError(f"more than {MAX_SUPPORTING_CLAIMS} supporting claims")
        object.__setattr__(self, "supporting_claims", supporting)

        caveats = tuple(self.caveats)
        if len(caveats) > MAX_CAVEATS:
            raise ValueError(f"more than {MAX_CAVEATS} caveats")
        for caveat in caveats:
            _require_bounded_str(caveat, "caveat", MAX_CAVEAT_CHARS)
        object.__setattr__(self, "caveats", caveats)

    def to_dict(self) -> dict[str, Any]:
        return {
            "incident_id": self.incident_id,
            "summary": self.summary,
            "root_cause_claim": self.root_cause_claim.to_dict() if self.root_cause_claim is not None else None,
            "supporting_claims": [claim.to_dict() for claim in self.supporting_claims],
            "confidence": self.confidence.value,
            "recommendation": self.recommendation.to_dict() if self.recommendation is not None else None,
            "caveats": list(self.caveats),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "IncidentExplanation":
        """Reconstructs an `IncidentExplanation` from Argus's own
        previously-persisted, already-validated JSON (a cache hit) --
        deliberately *not* the path a fresh, untrusted model response
        goes through (see `argus.ai.validation.validate_explanation`,
        which additionally checks evidence references against a live
        bundle -- a check that would be redundant, not just unnecessary,
        against data Argus itself already validated and wrote)."""

        root_cause = data.get("root_cause_claim")
        recommendation = data.get("recommendation")
        return cls(
            incident_id=data["incident_id"],
            summary=data["summary"],
            root_cause_claim=(
                ExplanationClaim(text=root_cause["text"], evidence_references=tuple(root_cause["evidence_references"]))
                if root_cause is not None else None
            ),
            supporting_claims=tuple(
                ExplanationClaim(text=claim["text"], evidence_references=tuple(claim["evidence_references"]))
                for claim in data.get("supporting_claims", [])
            ),
            confidence=data["confidence"],
            recommendation=(
                Recommendation(category=recommendation["category"], explanation=recommendation.get("explanation"))
                if recommendation is not None else None
            ),
            caveats=tuple(data.get("caveats", [])),
        )
