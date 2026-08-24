"""Tests for argus.evidence.association: time-window linking, backward
(before-incident-open) association, idempotency, and the explicit
no-causation guarantee."""

from __future__ import annotations

import ast
import inspect
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from argus.domain.models import EvidenceCategory, EvidenceSeverity, HealthStatus
from argus.evidence import association as association_module
from argus.evidence.association import DEFAULT_ASSOCIATION_WINDOW_SECONDS, associate_evidence
from argus.store.database import open_database
from argus.store.repository import Repository

UTC = timezone.utc
T0 = datetime(2026, 8, 22, 10, 0, 0, tzinfo=UTC)


def make_repo(tmp_path):
    conn = open_database(tmp_path / "a.db")
    repo = Repository(conn)
    app_id = repo.upsert_application(key="cnstrct", name="CNSTRCT", is_standalone=False, observed_at=T0)
    svc_id = repo.upsert_service(application_id=app_id, compose_service="api", name="api", observed_at=T0)
    container_row_id = repo.upsert_container(
        service_id=svc_id, container_id="docker-abc", name="cnstrct-api-1", first_seen_at=T0, last_seen_at=T0
    )
    return conn, repo, app_id, container_row_id


def make_signal(repo, app_id, container_row_id, *, at, category=EvidenceCategory.DB_CONNECTION_TIMEOUT):
    return repo.insert_log_signal(
        application_id=app_id, container_row_id=container_row_id, category=category.value,
        severity=EvidenceSeverity.HIGH.value, normalized_signature="connection timeout",
        first_seen_at=at, last_seen_at=at, count=1, sample="connection timeout",
        source_type="container_log", source_ref="stdout+stderr",
    )


def open_incident(repo, app_id, *, opened_at, key="cnstrct"):
    t = repo.insert_transition(
        scope="application", scope_id=app_id, from_status=None, to_status=HealthStatus.UNHEALTHY,
        occurred_at=opened_at,
    )
    return repo.open_incident(
        scope_id=app_id, failure_signature=f"application:{key}", opened_at=opened_at,
        opening_status=HealthStatus.UNHEALTHY, opening_transition_id=t,
    )


class TestBackwardAssociation:
    def test_evidence_before_incident_open_is_still_linked(self, tmp_path):
        conn, repo, app_id, container_row_id = make_repo(tmp_path)
        # Evidence at T0, incident opens 45s later -- well within the
        # default 60s backward window.
        signal_id = make_signal(repo, app_id, container_row_id, at=T0)
        incident_id = open_incident(repo, app_id, opened_at=T0 + timedelta(seconds=45))

        associate_evidence(repo, now=T0 + timedelta(seconds=46))

        linked = repo.list_evidence_for_incident(incident_id)
        assert len(linked) == 1
        assert linked[0].id == signal_id
        conn.close()

    def test_evidence_further_back_than_the_window_is_not_linked(self, tmp_path):
        conn, repo, app_id, container_row_id = make_repo(tmp_path)
        make_signal(repo, app_id, container_row_id, at=T0)
        incident_id = open_incident(repo, app_id, opened_at=T0 + timedelta(seconds=120))  # 120s > 60s window

        associate_evidence(repo, now=T0 + timedelta(seconds=121))

        assert repo.list_evidence_for_incident(incident_id) == ()
        conn.close()


class TestForwardAssociationWhileOpen:
    def test_evidence_arriving_while_incident_still_open_is_linked(self, tmp_path):
        conn, repo, app_id, container_row_id = make_repo(tmp_path)
        incident_id = open_incident(repo, app_id, opened_at=T0)
        signal_id = make_signal(repo, app_id, container_row_id, at=T0 + timedelta(minutes=5))

        associate_evidence(repo, now=T0 + timedelta(minutes=5, seconds=1))

        linked = repo.list_evidence_for_incident(incident_id)
        assert [s.id for s in linked] == [signal_id]
        conn.close()


class TestAssociationAfterResolution:
    def test_evidence_within_grace_period_after_close_is_linked(self, tmp_path):
        conn, repo, app_id, container_row_id = make_repo(tmp_path)
        incident_id = open_incident(repo, app_id, opened_at=T0)
        resolve_t = repo.insert_transition(
            scope="application", scope_id=app_id, from_status=HealthStatus.UNHEALTHY,
            to_status=HealthStatus.HEALTHY, occurred_at=T0 + timedelta(minutes=2),
        )
        repo.resolve_incident(incident_id=incident_id, closed_at=T0 + timedelta(minutes=2), resolving_transition_id=resolve_t)

        # Evidence discovered on a later tick, 30s after resolution --
        # within the 60s post-close grace window.
        signal_id = make_signal(repo, app_id, container_row_id, at=T0 + timedelta(minutes=2, seconds=30))

        associate_evidence(repo, now=T0 + timedelta(minutes=2, seconds=31))

        linked = repo.list_evidence_for_incident(incident_id)
        assert [s.id for s in linked] == [signal_id]
        conn.close()

    def test_incident_stops_being_rescanned_once_grace_period_elapses(self, tmp_path):
        conn, repo, app_id, container_row_id = make_repo(tmp_path)
        incident_id = open_incident(repo, app_id, opened_at=T0)
        resolve_t = repo.insert_transition(
            scope="application", scope_id=app_id, from_status=HealthStatus.UNHEALTHY,
            to_status=HealthStatus.HEALTHY, occurred_at=T0 + timedelta(minutes=2),
        )
        repo.resolve_incident(incident_id=incident_id, closed_at=T0 + timedelta(minutes=2), resolving_transition_id=resolve_t)

        # "now" is long past closed_at + 60s -- this incident is no
        # longer eligible for association at all.
        make_signal(repo, app_id, container_row_id, at=T0 + timedelta(minutes=10))
        associate_evidence(repo, now=T0 + timedelta(minutes=10, seconds=1))

        assert repo.list_evidence_for_incident(incident_id) == ()
        conn.close()


class TestIdempotency:
    def test_running_association_twice_does_not_duplicate_links(self, tmp_path):
        conn, repo, app_id, container_row_id = make_repo(tmp_path)
        incident_id = open_incident(repo, app_id, opened_at=T0)
        make_signal(repo, app_id, container_row_id, at=T0 + timedelta(seconds=5))

        associate_evidence(repo, now=T0 + timedelta(seconds=6))
        associate_evidence(repo, now=T0 + timedelta(seconds=7))

        assert len(repo.list_evidence_for_incident(incident_id)) == 1
        conn.close()


class TestMultipleSignalsAndReturnValue:
    def test_return_value_counts_link_attempts(self, tmp_path):
        conn, repo, app_id, container_row_id = make_repo(tmp_path)
        open_incident(repo, app_id, opened_at=T0)
        make_signal(repo, app_id, container_row_id, at=T0 + timedelta(seconds=1))
        make_signal(repo, app_id, container_row_id, at=T0 + timedelta(seconds=2))

        count = associate_evidence(repo, now=T0 + timedelta(seconds=3))
        assert count == 2
        conn.close()


class TestNoOpenIncidentsMeansNoLinks:
    def test_no_incidents_at_all_is_a_safe_no_op(self, tmp_path):
        conn, repo, app_id, container_row_id = make_repo(tmp_path)
        make_signal(repo, app_id, container_row_id, at=T0)
        count = associate_evidence(repo, now=T0 + timedelta(seconds=1))
        assert count == 0
        conn.close()


class TestNoCausationClaim:
    """The association model must never contain (or be able to express)
    a causal claim -- only "occurred near", never "caused"."""

    def test_relation_is_always_temporal_proximity(self, tmp_path):
        conn, repo, app_id, container_row_id = make_repo(tmp_path)
        incident_id = open_incident(repo, app_id, opened_at=T0)
        make_signal(repo, app_id, container_row_id, at=T0 + timedelta(seconds=1))
        associate_evidence(repo, now=T0 + timedelta(seconds=2))

        row = conn.execute(
            "SELECT relation FROM incident_evidence WHERE incident_id = ?", (incident_id,)
        ).fetchone()
        assert row["relation"] == "temporal_proximity"
        conn.close()

    def test_schema_check_constraint_rejects_a_causal_value(self, tmp_path):
        conn, repo, app_id, container_row_id = make_repo(tmp_path)
        incident_id = open_incident(repo, app_id, opened_at=T0)
        signal_id = make_signal(repo, app_id, container_row_id, at=T0)

        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO incident_evidence (incident_id, log_signal_id, linked_at, relation) "
                "VALUES (?, ?, ?, 'caused_by')",
                (incident_id, signal_id, T0.isoformat()),
            )
        conn.close()

    def test_no_caused_by_field_or_semantic_in_actual_code(self):
        """"caused_by" is explicitly discussed in this module's own
        docstrings (as a disclaimer of what it is NOT) -- that's
        legitimate documentation, not a violation. What must never exist
        is a "caused_by"-shaped *identifier* or *string literal value* in
        the actual code: a variable/argument/attribute name, or a string
        constant the code could pass as ``relation=``."""

        source = inspect.getsource(association_module)
        tree = ast.parse(source)

        causal_identifiers = set()
        causal_string_literals = set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.Name, ast.arg)):
                name = node.id if isinstance(node, ast.Name) else node.arg
                if "cause" in name.lower():
                    causal_identifiers.add(name)
            if (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and len(node.value) < 50  # short values only -- excludes docstrings/comments/prose
                and "cause" in node.value.lower()
            ):
                causal_string_literals.add(node.value)

        assert not causal_identifiers, f"found causally-named identifier(s): {causal_identifiers}"
        assert not causal_string_literals, f"found causally-named string literal(s): {causal_string_literals}"


class TestConstants:
    def test_default_window_is_60_seconds(self):
        assert DEFAULT_ASSOCIATION_WINDOW_SECONDS == 60
