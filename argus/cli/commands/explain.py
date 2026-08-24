"""`argus explain <incident_id> [--json] [--force-refresh] [--provider ...]`
-- ask a configured AI provider (Anthropic Claude or Google Gemini) for
a structured, evidence-grounded explanation of one incident.

This is the only CLI command that makes a network call, the only one
that writes to the database (an audit record of the explanation, never
core monitoring truth), and the only one that can be unavailable
without a provider credential configured. Every other command keeps
working exactly as before whether or not this one is configured, and
regardless of which provider (if any) is selected -- see
`argus/ai/__init__.py`'s own docstring for the full boundary.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime

from argus.ai.explain import BundleTooLargeError, ExplainResult, IncidentExplanationService
from argus.ai.providers import AIConfig, AIConfigurationError, AIRequestError, default_ai_provider, resolve_provider
from argus.ai.validation import ExplanationValidationError
from argus.evidence.assembler import IncidentNotFoundError
from argus.store.repository import Repository

COMMAND = "explain"


def add_parser(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser(
        COMMAND, help="Ask a configured AI provider for a structured, evidence-grounded explanation of one incident"
    )
    parser.add_argument("incident_id", type=int, help="Incident id (see `argus incidents`)")
    parser.add_argument(
        "--provider", choices=["anthropic", "gemini"], default=None,
        help="Override the AI provider for this call (default: $ARGUS_AI_PROVIDER, or anthropic)",
    )
    parser.add_argument("--json", action="store_true", help="Print the full, machine-readable result as JSON")
    parser.add_argument(
        "--force-refresh", action="store_true",
        help="Skip the cache and ask the provider again even if a validated explanation already exists",
    )
    parser.set_defaults(func=run)


def run(args: argparse.Namespace, repository: Repository, now: datetime) -> int:
    try:
        provider_name = default_ai_provider(args.provider)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    ai_config = AIConfig(provider=provider_name)

    try:
        ai_provider = resolve_provider(ai_config)
    except AIConfigurationError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    service = IncidentExplanationService(
        repository=repository, ai_provider=ai_provider, ai_config=ai_config, clock=lambda: now
    )

    try:
        result = service.explain(args.incident_id, force_refresh=args.force_refresh)
    except IncidentNotFoundError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except BundleTooLargeError as exc:
        print(f"Argus AI error: {exc}", file=sys.stderr)
        return 1
    except AIRequestError as exc:
        print(f"Argus AI request failed: {exc}", file=sys.stderr)
        return 1
    except ExplanationValidationError as exc:
        print(f"Argus AI returned an invalid response and was rejected: {exc}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(_to_json(result), indent=2))
        return 0

    application_name = _application_name(repository, args.incident_id)
    print(_render_human(result, application_name))
    return 0


def _application_name(repository: Repository, incident_id: int) -> str:
    incident = repository.get_incident_by_id(incident_id)
    if incident is None:
        return "?"
    application = repository.get_application_by_id(incident.scope_id)
    return application.name if application is not None else "?"


def _to_json(result: ExplainResult) -> dict:
    return {
        "incident_id": result.explanation.incident_id,
        "cached": result.cached,
        "bundle_fingerprint": result.bundle_fingerprint,
        "provider": result.provider,
        "model": result.model,
        "prompt_version": result.prompt_version,
        "usage": result.usage.to_dict() if result.usage is not None else None,
        "explanation": result.explanation.to_dict(),
    }


def _render_human(result: ExplainResult, application_name: str) -> str:
    explanation = result.explanation
    lines = [f"INCIDENT #{explanation.incident_id} — {application_name}", ""]

    lines.append("Summary")
    lines.append(explanation.summary)
    lines.append("")

    if explanation.root_cause_claim is not None:
        lines.append("Probable root cause")
        refs = ", ".join(f"[{ref}]" for ref in explanation.root_cause_claim.evidence_references)
        lines.append(f"{explanation.root_cause_claim.text} {refs}")
        lines.append("")

    lines.append("Confidence")
    lines.append(explanation.confidence.value.upper())
    lines.append("")

    if explanation.supporting_claims:
        lines.append("Supporting evidence")
        for claim in explanation.supporting_claims:
            refs = ", ".join(f"[{ref}]" for ref in claim.evidence_references)
            lines.append(f"- {claim.text} {refs}")
        lines.append("")

    if explanation.recommendation is not None:
        lines.append("Recommended next step")
        category_text = explanation.recommendation.category.value.replace("_", " ")
        if explanation.recommendation.explanation:
            lines.append(f"{explanation.recommendation.explanation} ({category_text})")
        else:
            lines.append(category_text)
        lines.append("")

    if explanation.caveats:
        lines.append("Caveats")
        for caveat in explanation.caveats:
            lines.append(caveat)
        lines.append("")

    lines.append("Provider   " + result.provider)
    lines.append("Model      " + result.model)
    lines.append("Cached     " + ("yes" if result.cached else "no"))
    return "\n".join(lines)
