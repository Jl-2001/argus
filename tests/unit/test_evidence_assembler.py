"""Tests for argus.evidence.assembler.assemble_evidence_bundle against a
real (temporary, file-backed) SQLite database -- the DB-facing half of
the assembler, on top of the pure selection-layer tests in
test_evidence_assembler_selection.py.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from argus.domain.models import Container, DockerState, HealthStatus, Observation
from argus.evidence.assembler import (
    DEFAULT_ASSEMBLER_CONFIG,
    AssemblerConfig,
    IncidentNotFoundError,
    assemble_evidence_bundle,
)
from argus.store.database import open_database
from argus.store.repository import Repository

UTC = timezone.utc
T0 = datetime(2026, 8, 23, 12, 0, 0, tzinfo=UTC)
EARLY = T0 - timedelta(hours=1)


# --------------------------------------------------------------------------
# Fixture builder
# --------------------------------------------------------------------------


def make_repo(tmp_path):
    conn = open_database(tmp_path / "a.db")
    return conn, Repository(conn)


def seed_application(repo, *, key="cnstrct", name="CNSTRCT"):
    app_id = repo.upsert_application(key=key, name=name, is_standalone=False, observed_at=EARLY)
    svc_id = repo.upsert_service(application_id=app_id, compose_service="api", name="api", observed_at=EARLY)
    container_row_id = repo.upsert_container(
        service_id=svc_id, container_id="docker-api", name=f"{key}-api-1", first_seen_at=EARLY, last_seen_at=EARLY
    )
    return app_id, svc_id, container_row_id


def make_observation(container_id="docker-api", *, at, restart_count=0, status=HealthStatus.HEALTHY):
    container = Container(
        container_id=container_id, name="cnstrct-api-1", image="cnstrct/api:1", compose_project="cnstrct",
        compose_service="api", first_seen_at=EARLY, last_seen_at=at,
    )
    return Observation(
        container_ref=container, observed_at=at, docker_state=DockerState.RUNNING, docker_health=None,
        restart_count=restart_count, exit_code=None, started_at=None, finished_at=None, ports=(), labels={},
        derived_status=status,
    )


def open_incident(repo, app_id, *, opened_at, key="cnstrct"):
    t = repo.insert_transition(
        scope="application", scope_id=app_id, from_status=None, to_status=HealthStatus.UNHEALTHY, occurred_at=opened_at
    )
    return repo.open_incident(
        scope_id=app_id, failure_signature=f"application:{key}", opened_at=opened_at,
        opening_status=HealthStatus.UNHEALTHY, opening_transition_id=t,
    )


# --------------------------------------------------------------------------
# Basic resolved / open incident
# --------------------------------------------------------------------------


class TestBasicResolvedIncident:
    def test_incident_and_application_context_are_correct(self, tmp_path):
        conn, repo = make_repo(tmp_path)
        app_id, _, _ = seed_application(repo)
        incident_id = open_incident(repo, app_id, opened_at=T0)
        closed_at = T0 + timedelta(minutes=5)
        transition = repo.get_last_transition(scope="application", scope_id=app_id)
        resolve_t = repo.insert_transition(
            scope="application", scope_id=app_id, from_status=HealthStatus.UNHEALTHY, to_status=HealthStatus.HEALTHY,
            occurred_at=closed_at,
        )
        repo.resolve_incident(incident_id=incident_id, closed_at=closed_at, resolving_transition_id=resolve_t)

        bundle = assemble_evidence_bundle(repo, incident_id, now=T0 + timedelta(hours=1))

        assert bundle.incident.incident_id == incident_id
        assert bundle.incident.reference == f"incident:{incident_id}"
        assert bundle.incident.status == "resolved"
        assert bundle.incident.opened_at == T0
        assert bundle.incident.closed_at == closed_at
        assert bundle.application.key == "cnstrct"
        assert bundle.application.name == "CNSTRCT"
        assert len(bundle.application.services) == 1
        assert bundle.application.services[0].compose_service == "api"
        assert bundle.application.services[0].containers[0].container_id == "docker-api"
        conn.close()

    def test_window_end_is_closed_at_plus_post_window(self, tmp_path):
        conn, repo = make_repo(tmp_path)
        app_id, _, _ = seed_application(repo)
        incident_id = open_incident(repo, app_id, opened_at=T0)
        closed_at = T0 + timedelta(minutes=5)
        resolve_t = repo.insert_transition(
            scope="application", scope_id=app_id, from_status=HealthStatus.UNHEALTHY, to_status=HealthStatus.HEALTHY,
            occurred_at=closed_at,
        )
        repo.resolve_incident(incident_id=incident_id, closed_at=closed_at, resolving_transition_id=resolve_t)

        config = AssemblerConfig(post_close_window_seconds=120)
        bundle = assemble_evidence_bundle(repo, incident_id, now=T0 + timedelta(hours=1), config=config)

        assert bundle.window.end == closed_at + timedelta(seconds=120)
        assert bundle.window.incident_open is False

    def test_window_start_is_opened_at_minus_pre_window(self, tmp_path):
        conn, repo = make_repo(tmp_path)
        app_id, _, _ = seed_application(repo)
        incident_id = open_incident(repo, app_id, opened_at=T0)
        config = AssemblerConfig(pre_open_window_seconds=120)
        bundle = assemble_evidence_bundle(repo, incident_id, now=T0 + timedelta(minutes=1), config=config)
        assert bundle.window.start == T0 - timedelta(seconds=120)


class TestBasicOpenIncident:
    def test_window_end_uses_generated_at_and_is_bounded(self, tmp_path):
        conn, repo = make_repo(tmp_path)
        app_id, _, _ = seed_application(repo)
        incident_id = open_incident(repo, app_id, opened_at=T0)
        now = T0 + timedelta(minutes=3)

        bundle = assemble_evidence_bundle(repo, incident_id, now=now)

        assert bundle.window.incident_open is True
        assert bundle.window.end == now
        assert bundle.incident.status == "open"
        assert bundle.incident.closed_at is None
        conn.close()


class TestNonexistentIncident:
    def test_raises_typed_error_not_a_raw_exception(self, tmp_path):
        conn, repo = make_repo(tmp_path)
        with pytest.raises(IncidentNotFoundError):
            assemble_evidence_bundle(repo, 999999, now=T0)
        conn.close()


# --------------------------------------------------------------------------
# Provenance against real SQLite
# --------------------------------------------------------------------------


class TestProvenanceAgainstRealDatabase:
    def test_every_reference_resolves_to_a_real_row(self, tmp_path):
        conn, repo = make_repo(tmp_path)
        app_id, svc_id, container_row_id = seed_application(repo)
        repo.insert_observation(container_row_id=container_row_id, observation=make_observation(at=T0))
        incident_id = open_incident(repo, app_id, opened_at=T0)
        sig_id = repo.insert_log_signal(
            application_id=app_id, container_row_id=container_row_id, category="db_connection_timeout",
            severity="high", normalized_signature="timeout", first_seen_at=T0, last_seen_at=T0, count=3,
            sample="connection timeout", source_type="container_log", source_ref="stdout+stderr",
        )
        repo.link_incident_evidence(incident_id=incident_id, log_signal_id=sig_id, linked_at=T0)

        bundle = assemble_evidence_bundle(repo, incident_id, now=T0 + timedelta(minutes=1))

        assert bundle.incident.reference == f"incident:{incident_id}"
        for signal in bundle.signals:
            assert signal.reference == f"log_signal:{signal.source_id}"
            assert repo.get_log_signal(signal.source_id) is not None
        for transition in bundle.transitions:
            assert transition.reference == f"health_transition:{transition.source_id}"
            row = conn.execute("SELECT id FROM health_transitions WHERE id = ?", (transition.source_id,)).fetchone()
            assert row is not None
        for observation in bundle.observations:
            assert observation.reference == f"observation:{observation.source_id}"
            row = conn.execute("SELECT id FROM observations WHERE id = ?", (observation.source_id,)).fetchone()
            assert row is not None
        conn.close()


# --------------------------------------------------------------------------
# Observation sampling
# --------------------------------------------------------------------------


class TestObservationSampling:
    def test_before_at_after_are_sampled_around_a_container_transition(self, tmp_path):
        conn, repo = make_repo(tmp_path)
        app_id, svc_id, container_row_id = seed_application(repo)
        repo.insert_observation(container_row_id=container_row_id, observation=make_observation(at=T0 - timedelta(seconds=10), restart_count=0))
        repo.insert_observation(container_row_id=container_row_id, observation=make_observation(at=T0, restart_count=1, status=HealthStatus.UNHEALTHY))
        repo.insert_observation(container_row_id=container_row_id, observation=make_observation(at=T0 + timedelta(seconds=10), restart_count=1, status=HealthStatus.UNHEALTHY))
        repo.insert_transition(scope="container", scope_id=container_row_id, from_status=HealthStatus.HEALTHY, to_status=HealthStatus.UNHEALTHY, occurred_at=T0)
        incident_id = open_incident(repo, app_id, opened_at=T0)

        bundle = assemble_evidence_bundle(repo, incident_id, now=T0 + timedelta(minutes=1))

        reasons = {o.sampling_reason for o in bundle.observations}
        assert reasons == {"before_transition", "at_transition", "after_transition"}
        assert len(bundle.observations) == 3

    def test_a_large_observation_series_is_reduced_not_dumped_wholesale(self, tmp_path):
        conn, repo = make_repo(tmp_path)
        app_id, svc_id, container_row_id = seed_application(repo)
        # 50 observations, 15s apart -- would never be reasonable to
        # include wholesale.
        for i in range(50):
            repo.insert_observation(
                container_row_id=container_row_id,
                observation=make_observation(at=T0 - timedelta(minutes=10) + timedelta(seconds=15 * i)),
            )
        repo.insert_transition(scope="container", scope_id=container_row_id, from_status=HealthStatus.HEALTHY, to_status=HealthStatus.UNHEALTHY, occurred_at=T0)
        incident_id = open_incident(repo, app_id, opened_at=T0)

        bundle = assemble_evidence_bundle(repo, incident_id, now=T0 + timedelta(minutes=1))

        # only the handful sampled around the one transition, never all 50
        assert len(bundle.observations) <= 3

    def test_observations_only_sampled_for_container_scope_transitions(self, tmp_path):
        conn, repo = make_repo(tmp_path)
        app_id, svc_id, container_row_id = seed_application(repo)
        incident_id = open_incident(repo, app_id, opened_at=T0)  # application-scope transition only
        bundle = assemble_evidence_bundle(repo, incident_id, now=T0 + timedelta(minutes=1))
        assert bundle.observations == ()


# --------------------------------------------------------------------------
# Signal sample truncation
# --------------------------------------------------------------------------


class TestSignalTruncation:
    def test_oversized_sample_is_bounded_but_reference_and_category_survive(self, tmp_path):
        conn, repo = make_repo(tmp_path)
        app_id, svc_id, container_row_id = seed_application(repo)
        incident_id = open_incident(repo, app_id, opened_at=T0)
        long_sample = "connection timeout " + ("x" * 2000)
        sig_id = repo.insert_log_signal(
            application_id=app_id, container_row_id=container_row_id, category="db_connection_timeout",
            severity="high", normalized_signature="timeout", first_seen_at=T0, last_seen_at=T0, count=1,
            sample=long_sample, source_type="container_log", source_ref="stdout+stderr",
        )
        repo.link_incident_evidence(incident_id=incident_id, log_signal_id=sig_id, linked_at=T0)

        config = AssemblerConfig(max_sample_chars=100)
        bundle = assemble_evidence_bundle(repo, incident_id, now=T0 + timedelta(minutes=1), config=config)

        assert len(bundle.signals) == 1
        assert len(bundle.signals[0].sample) <= 100
        assert bundle.signals[0].reference == f"log_signal:{sig_id}"
        assert bundle.signals[0].category == "db_connection_timeout"


# --------------------------------------------------------------------------
# Total character budget
# --------------------------------------------------------------------------


class TestTotalCharacterBudget:
    def test_serialized_bundle_never_exceeds_max_total_chars(self, tmp_path):
        conn, repo = make_repo(tmp_path)
        app_id, svc_id, container_row_id = seed_application(repo)
        incident_id = open_incident(repo, app_id, opened_at=T0)
        for i in range(40):
            sig_id = repo.insert_log_signal(
                application_id=app_id, container_row_id=container_row_id, category="db_connection_timeout",
                severity="high", normalized_signature=f"sig-{i}", first_seen_at=T0 + timedelta(seconds=i),
                last_seen_at=T0 + timedelta(seconds=i), count=1, sample="x" * 500,
                source_type="container_log", source_ref="stdout+stderr",
            )
            repo.link_incident_evidence(incident_id=incident_id, log_signal_id=sig_id, linked_at=T0)

        config = AssemblerConfig(max_signals=40, max_total_chars=5_000)
        bundle = assemble_evidence_bundle(repo, incident_id, now=T0 + timedelta(minutes=1), config=config)

        assert len(bundle.to_json(indent=None)) <= 5_000
        assert bundle.metadata.truncated is True

    def test_budget_fitting_never_produces_invalid_json(self, tmp_path):
        conn, repo = make_repo(tmp_path)
        app_id, svc_id, container_row_id = seed_application(repo)
        incident_id = open_incident(repo, app_id, opened_at=T0)
        for i in range(40):
            sig_id = repo.insert_log_signal(
                application_id=app_id, container_row_id=container_row_id, category="db_connection_timeout",
                severity="high", normalized_signature=f"sig-{i}", first_seen_at=T0 + timedelta(seconds=i),
                last_seen_at=T0 + timedelta(seconds=i), count=1, sample="y" * 500,
                source_type="container_log", source_ref="stdout+stderr",
            )
            repo.link_incident_evidence(incident_id=incident_id, log_signal_id=sig_id, linked_at=T0)

        config = AssemblerConfig(max_signals=40, max_total_chars=800)  # deliberately extreme
        bundle = assemble_evidence_bundle(repo, incident_id, now=T0 + timedelta(minutes=1), config=config)
        parsed = json.loads(bundle.to_json())  # must not raise
        assert parsed["incident"]["incident_id"] == incident_id


class TestOmissionMetadata:
    def test_omitted_counts_reflect_item_budget_overflow(self, tmp_path):
        conn, repo = make_repo(tmp_path)
        app_id, svc_id, container_row_id = seed_application(repo)
        incident_id = open_incident(repo, app_id, opened_at=T0)
        for i in range(5):
            sig_id = repo.insert_log_signal(
                application_id=app_id, container_row_id=container_row_id, category="db_connection_timeout",
                severity="high", normalized_signature=f"sig-{i}", first_seen_at=T0 + timedelta(seconds=i),
                last_seen_at=T0 + timedelta(seconds=i), count=1, sample="short",
                source_type="container_log", source_ref="stdout+stderr",
            )
            repo.link_incident_evidence(incident_id=incident_id, log_signal_id=sig_id, linked_at=T0)

        config = AssemblerConfig(max_signals=2)
        bundle = assemble_evidence_bundle(repo, incident_id, now=T0 + timedelta(minutes=1), config=config)
        assert bundle.metadata.omitted_counts["signals"] == 3
        assert bundle.metadata.truncated is True

    def test_no_omission_reports_truncated_false(self, tmp_path):
        conn, repo = make_repo(tmp_path)
        app_id, svc_id, container_row_id = seed_application(repo)
        incident_id = open_incident(repo, app_id, opened_at=T0)
        bundle = assemble_evidence_bundle(repo, incident_id, now=T0 + timedelta(minutes=1))
        assert bundle.metadata.truncated is False
        assert bundle.metadata.omitted_counts == {"signals": 0, "transitions": 0, "observations": 0}


# --------------------------------------------------------------------------
# No raw secrets
# --------------------------------------------------------------------------


class TestNoRawSecrets:
    def test_only_already_redacted_samples_appear(self, tmp_path):
        conn, repo = make_repo(tmp_path)
        app_id, svc_id, container_row_id = seed_application(repo)
        incident_id = open_incident(repo, app_id, opened_at=T0)
        redacted_sample = "authentication failed, Authorization: [REDACTED]"
        sig_id = repo.insert_log_signal(
            application_id=app_id, container_row_id=container_row_id, category="authentication_failure",
            severity="high", normalized_signature="auth failed", first_seen_at=T0, last_seen_at=T0, count=1,
            sample=redacted_sample, source_type="container_log", source_ref="stdout+stderr",
        )
        repo.link_incident_evidence(incident_id=incident_id, log_signal_id=sig_id, linked_at=T0)

        bundle = assemble_evidence_bundle(repo, incident_id, now=T0 + timedelta(minutes=1))
        raw = bundle.to_json()
        assert "[REDACTED]" in raw
        # the specific fake secret this test would have used, had
        # redaction not already happened upstream in Milestone 10 -- the
        # assembler itself performs no redaction and must never
        # reconstruct or expose one
        assert "eyJhbGciOiJIUzI1NiJ9" not in raw
        assert "hunter2" not in raw


# --------------------------------------------------------------------------
# Evidence subsystem status
# --------------------------------------------------------------------------


class TestEvidenceSubsystemStatus:
    def test_never_run_when_no_evidence_activity_recorded(self, tmp_path):
        conn, repo = make_repo(tmp_path)
        app_id, svc_id, container_row_id = seed_application(repo)
        incident_id = open_incident(repo, app_id, opened_at=T0)
        bundle = assemble_evidence_bundle(repo, incident_id, now=T0 + timedelta(minutes=1))
        assert bundle.metadata.evidence_subsystem_status == "never_run"

    def test_healthy_when_last_evidence_tick_succeeded(self, tmp_path):
        conn, repo = make_repo(tmp_path)
        app_id, svc_id, container_row_id = seed_application(repo)
        incident_id = open_incident(repo, app_id, opened_at=T0)
        repo.record_evidence_tick_success(at=T0)
        bundle = assemble_evidence_bundle(repo, incident_id, now=T0 + timedelta(minutes=1))
        assert bundle.metadata.evidence_subsystem_status == "healthy"

    def test_degraded_when_evidence_collection_is_currently_failing(self, tmp_path):
        conn, repo = make_repo(tmp_path)
        app_id, svc_id, container_row_id = seed_application(repo)
        incident_id = open_incident(repo, app_id, opened_at=T0)
        repo.record_evidence_tick_success(at=T0 - timedelta(minutes=5))
        repo.record_evidence_tick_failure(error="log read failed")
        bundle = assemble_evidence_bundle(repo, incident_id, now=T0 + timedelta(minutes=1))
        assert bundle.metadata.evidence_subsystem_status == "degraded"

    def test_absence_of_signals_with_degraded_status_does_not_read_as_clean(self, tmp_path):
        """Missing evidence vs. failed evidence subsystem -- represented
        distinctly: an incident with zero signals AND a degraded
        subsystem must not look the same as an incident with zero
        signals and a healthy subsystem."""

        conn, repo = make_repo(tmp_path)
        app_id, svc_id, container_row_id = seed_application(repo)
        incident_id = open_incident(repo, app_id, opened_at=T0)
        repo.record_evidence_tick_failure(error="Docker unreachable")

        bundle = assemble_evidence_bundle(repo, incident_id, now=T0 + timedelta(minutes=1))
        assert bundle.signals == ()
        assert bundle.metadata.evidence_subsystem_status == "degraded"


# --------------------------------------------------------------------------
# Determinism / fingerprint
# --------------------------------------------------------------------------


class TestByteIdenticalDeterminism:
    def test_same_state_same_generated_at_identical_json(self, tmp_path):
        conn, repo = make_repo(tmp_path)
        app_id, svc_id, container_row_id = seed_application(repo)
        incident_id = open_incident(repo, app_id, opened_at=T0)
        sig_id = repo.insert_log_signal(
            application_id=app_id, container_row_id=container_row_id, category="oom", severity="critical",
            normalized_signature="oom", first_seen_at=T0, last_seen_at=T0, count=1, sample="oom killed",
            source_type="container_log", source_ref="stdout+stderr",
        )
        repo.link_incident_evidence(incident_id=incident_id, log_signal_id=sig_id, linked_at=T0)

        now = T0 + timedelta(minutes=1)
        bundle_a = assemble_evidence_bundle(repo, incident_id, now=now)
        bundle_b = assemble_evidence_bundle(repo, incident_id, now=now)
        assert bundle_a.to_json() == bundle_b.to_json()


class TestFingerprintStability:
    def test_same_evidence_same_fingerprint_across_different_generated_at(self, tmp_path):
        """A resolved incident's window is fixed (closed_at + post
        window) -- reassembling it a week later must fingerprint
        identically."""

        conn, repo = make_repo(tmp_path)
        app_id, svc_id, container_row_id = seed_application(repo)
        incident_id = open_incident(repo, app_id, opened_at=T0)
        closed_at = T0 + timedelta(minutes=5)
        resolve_t = repo.insert_transition(
            scope="application", scope_id=app_id, from_status=HealthStatus.UNHEALTHY, to_status=HealthStatus.HEALTHY,
            occurred_at=closed_at,
        )
        repo.resolve_incident(incident_id=incident_id, closed_at=closed_at, resolving_transition_id=resolve_t)

        bundle_a = assemble_evidence_bundle(repo, incident_id, now=T0 + timedelta(hours=1))
        bundle_b = assemble_evidence_bundle(repo, incident_id, now=T0 + timedelta(days=7))
        assert bundle_a.metadata.fingerprint == bundle_b.metadata.fingerprint
        assert bundle_a.to_json() != bundle_b.to_json()  # generated_at differs, but not the fingerprint

    def test_changed_evidence_changes_the_fingerprint(self, tmp_path):
        conn, repo = make_repo(tmp_path)
        app_id, svc_id, container_row_id = seed_application(repo)
        incident_id = open_incident(repo, app_id, opened_at=T0)
        closed_at = T0 + timedelta(minutes=5)
        resolve_t = repo.insert_transition(
            scope="application", scope_id=app_id, from_status=HealthStatus.UNHEALTHY, to_status=HealthStatus.HEALTHY,
            occurred_at=closed_at,
        )
        repo.resolve_incident(incident_id=incident_id, closed_at=closed_at, resolving_transition_id=resolve_t)

        bundle_before = assemble_evidence_bundle(repo, incident_id, now=T0 + timedelta(hours=1))

        sig_id = repo.insert_log_signal(
            application_id=app_id, container_row_id=container_row_id, category="oom", severity="critical",
            normalized_signature="oom", first_seen_at=T0, last_seen_at=T0, count=1, sample="oom killed",
            source_type="container_log", source_ref="stdout+stderr",
        )
        repo.link_incident_evidence(incident_id=incident_id, log_signal_id=sig_id, linked_at=T0)

        bundle_after = assemble_evidence_bundle(repo, incident_id, now=T0 + timedelta(hours=1))
        assert bundle_before.metadata.fingerprint != bundle_after.metadata.fingerprint

    def test_open_incident_fingerprint_is_stable_when_no_new_evidence_arrives(self, tmp_path):
        """Milestone 12 correction: `window.end` (which equals `now` for
        an open incident) is deliberately excluded from the fingerprint
        -- otherwise an open incident's fingerprint would never match
        twice, even seconds apart with zero new evidence, silently
        defeating the whole explanation cache. Two calls with nothing
        new must fingerprint identically."""

        conn, repo = make_repo(tmp_path)
        app_id, svc_id, container_row_id = seed_application(repo)
        incident_id = open_incident(repo, app_id, opened_at=T0)

        bundle_a = assemble_evidence_bundle(repo, incident_id, now=T0 + timedelta(minutes=1))
        bundle_b = assemble_evidence_bundle(repo, incident_id, now=T0 + timedelta(minutes=5))
        assert bundle_a.metadata.fingerprint == bundle_b.metadata.fingerprint
        assert bundle_a.to_json() != bundle_b.to_json()  # generated_at/window.end differ, but not the fingerprint

    def test_open_incident_fingerprint_changes_when_new_evidence_actually_appears(self, tmp_path):
        conn, repo = make_repo(tmp_path)
        app_id, svc_id, container_row_id = seed_application(repo)
        incident_id = open_incident(repo, app_id, opened_at=T0)

        bundle_a = assemble_evidence_bundle(repo, incident_id, now=T0 + timedelta(minutes=1))

        sig_id = repo.insert_log_signal(
            application_id=app_id, container_row_id=container_row_id, category="oom", severity="critical",
            normalized_signature="oom", first_seen_at=T0, last_seen_at=T0, count=1, sample="oom killed",
            source_type="container_log", source_ref="stdout+stderr",
        )
        repo.link_incident_evidence(incident_id=incident_id, log_signal_id=sig_id, linked_at=T0)

        bundle_b = assemble_evidence_bundle(repo, incident_id, now=T0 + timedelta(minutes=5))
        # a real, new signal is a genuine content difference -- the
        # fingerprint must still change on its own merits.
        assert bundle_a.metadata.fingerprint != bundle_b.metadata.fingerprint


# --------------------------------------------------------------------------
# Architecture guard
# --------------------------------------------------------------------------


class TestArchitectureGuard:
    def test_assembler_module_has_no_docker_or_ai_or_network_imports(self):
        import ast
        import inspect

        from argus.evidence import assembler as assembler_module

        forbidden = {"docker", "anthropic", "openai", "langgraph", "transformers", "fastapi", "requests", "httpx"}
        source = inspect.getsource(assembler_module)
        tree = ast.parse(source)
        roots = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    roots.add(alias.name.split(".")[0])
            elif isinstance(node, ast.ImportFrom) and node.module:
                roots.add(node.module.split(".")[0])
        found = roots & forbidden
        assert not found, f"assembler.py imports forbidden module(s): {found}"

    def test_bundle_module_has_no_docker_or_ai_or_network_imports(self):
        import ast
        import inspect

        from argus.evidence import bundle as bundle_module

        forbidden = {"docker", "anthropic", "openai", "langgraph", "transformers", "fastapi", "requests", "httpx"}
        source = inspect.getsource(bundle_module)
        tree = ast.parse(source)
        roots = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    roots.add(alias.name.split(".")[0])
            elif isinstance(node, ast.ImportFrom) and node.module:
                roots.add(node.module.split(".")[0])
        found = roots & forbidden
        assert not found, f"bundle.py imports forbidden module(s): {found}"
