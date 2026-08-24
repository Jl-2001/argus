"""Shared seeding helpers for the Milestone 13 API test files
(`test_api_*.py`). Not a test module itself (no `test_` prefix, mirroring
`tests/integration/test_chaos_stack.py`'s own double duty as both a
real test file and a shared-constants module) -- plain, bare-imported
(`from api_fixtures import ...`) the same way those integration tests
already import `from conftest import ...`.

Builds a small, realistic stack directly through `Repository` -- no
Docker, no collector process, no AI provider -- so every API test
exercises real persisted data through the real read models, the same
discipline `tests/unit/test_cli_commands.py`'s own `seed_full_stack`
already established for CLI tests.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from argus.domain.models import EvidenceCategory, EvidenceSeverity, HealthStatus
from argus.store.database import open_database
from argus.store.repository import Repository

UTC = timezone.utc


def real_now() -> datetime:
    """A function, not a module-level constant -- see
    `test_cli_commands.py`'s identical helper for why: a constant
    captured at collection time can go stale by the time a slow test
    suite actually reaches this test."""

    return datetime.now(UTC)


def seed_incident_stack(
    db_path: Path,
    *,
    key: str = "cnstrct",
    name: str = "CNSTRCT",
    with_explanation: bool = True,
) -> dict:
    """Builds one application (with a service/container), one open
    incident, one linked evidence signal, and (unless
    `with_explanation=False`) one persisted explanation -- everything
    every incident-sub-resource endpoint (evidence/bundle/explanations)
    needs to return something non-empty. Returns the ids a test needs to
    hit those endpoints, plus `now` (the same clock every seeded
    timestamp is relative to).
    """

    now = real_now()
    conn = open_database(db_path)
    try:
        repo = Repository(conn)
        app_id = repo.upsert_application(
            key=key, name=name, is_standalone=False, observed_at=now - timedelta(minutes=10)
        )
        service_id = repo.upsert_service(
            application_id=app_id, compose_service="web", name="web", observed_at=now - timedelta(minutes=10)
        )
        container_docker_id = "deadbeef" * 5
        repo.upsert_container(
            service_id=service_id, container_id=container_docker_id, name=f"{key}-web-1",
            first_seen_at=now - timedelta(minutes=10), last_seen_at=now,
        )
        container_record = repo.get_container_by_docker_id(container_docker_id)

        transition_id = repo.insert_transition(
            scope="application", scope_id=app_id, from_status=None,
            to_status=HealthStatus.UNHEALTHY, occurred_at=now - timedelta(minutes=5),
        )
        repo.record_tick_started(at=now)
        repo.record_tick_success(at=now)

        incident_id = repo.open_incident(
            scope_id=app_id, failure_signature=f"application:{key}", opened_at=now - timedelta(minutes=5),
            opening_status=HealthStatus.UNHEALTHY, opening_transition_id=transition_id,
        )

        signal_id = repo.insert_log_signal(
            application_id=app_id, container_row_id=container_record.id,
            category=EvidenceCategory.OOM.value, severity=EvidenceSeverity.CRITICAL.value,
            normalized_signature="oom-killed", first_seen_at=now - timedelta(minutes=4),
            last_seen_at=now - timedelta(minutes=4), count=1,
            sample="[REDACTED] container killed: out of memory", source_type="container_log", source_ref="stdout",
        )
        repo.link_incident_evidence(incident_id=incident_id, log_signal_id=signal_id, linked_at=now)

        explanation_id = None
        if with_explanation:
            response_json = json.dumps(
                {
                    "incident_id": incident_id,
                    "summary": "The container was killed after exceeding its memory limit.",
                    "root_cause_claim": {
                        "text": "Out-of-memory condition triggered a container kill.",
                        "evidence_references": [f"log_signal:{signal_id}"],
                    },
                    "supporting_claims": [],
                    "confidence": "high",
                    "recommendation": None,
                    "caveats": [],
                }
            )
            explanation_id = repo.save_explanation(
                incident_id=incident_id, bundle_fingerprint="deadbeefcafe", provider="anthropic",
                model="claude-sonnet-5", prompt_version="incident-explanation-v1", created_at=now,
                summary="The container was killed after exceeding its memory limit.",
                root_cause="Out-of-memory condition triggered a container kill.", confidence="high",
                input_tokens=321, output_tokens=64, response_json=response_json,
            )
    finally:
        conn.close()

    return {
        "now": now,
        "application_key": key,
        "application_id": app_id,
        "service_id": service_id,
        "incident_id": incident_id,
        "signal_id": signal_id,
        "explanation_id": explanation_id,
    }
