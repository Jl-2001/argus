"""Tests for argus.evidence.persistence: reconciling in-memory
SignalCandidates against already-persisted log_signals rows (extend an
existing bucket vs. insert a new one)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from argus.domain.models import EvidenceCategory, EvidenceSeverity
from argus.evidence.aggregator import SignalCandidate
from argus.evidence.persistence import persist_candidates
from argus.store.database import open_database
from argus.store.repository import Repository

UTC = timezone.utc
T0 = datetime(2026, 8, 22, 10, 0, 0, tzinfo=UTC)


def make_candidate(offset_seconds: float, *, count: int = 1, text: str = "connection timeout after #s"):
    return SignalCandidate(
        category=EvidenceCategory.DB_CONNECTION_TIMEOUT,
        severity=EvidenceSeverity.HIGH,
        normalized_signature="connection timeout after #s",
        first_seen_at=T0 + timedelta(seconds=offset_seconds),
        last_seen_at=T0 + timedelta(seconds=offset_seconds),
        count=count,
        sample=text,
    )


def make_repo(tmp_path):
    conn = open_database(tmp_path / "a.db")
    repo = Repository(conn)
    app_id = repo.upsert_application(key="cnstrct", name="CNSTRCT", is_standalone=False, observed_at=T0)
    svc_id = repo.upsert_service(application_id=app_id, compose_service="api", name="api", observed_at=T0)
    container_row_id = repo.upsert_container(
        service_id=svc_id, container_id="docker-abc", name="cnstrct-api-1", first_seen_at=T0, last_seen_at=T0
    )
    return conn, repo, app_id, container_row_id


class TestPersistNewCandidate:
    def test_first_candidate_for_a_key_inserts_a_new_row(self, tmp_path):
        conn, repo, app_id, container_row_id = make_repo(tmp_path)
        n = persist_candidates(
            repo, [make_candidate(0, count=3)], application_id=app_id, container_row_id=container_row_id,
            source_type="container_log", source_ref="stdout+stderr", aggregation_window_seconds=300,
        )
        assert n == 1
        signals = repo.list_log_signals_for_application(app_id, since=T0 - timedelta(hours=1))
        assert len(signals) == 1
        assert signals[0].count == 3
        conn.close()


class TestExtendVsNew:
    def test_second_call_within_window_extends_the_existing_row(self, tmp_path):
        conn, repo, app_id, container_row_id = make_repo(tmp_path)
        persist_candidates(
            repo, [make_candidate(0, count=2)], application_id=app_id, container_row_id=container_row_id,
            source_type="container_log", source_ref="stdout+stderr", aggregation_window_seconds=300,
        )
        persist_candidates(
            repo, [make_candidate(30, count=3)], application_id=app_id, container_row_id=container_row_id,
            source_type="container_log", source_ref="stdout+stderr", aggregation_window_seconds=300,
        )
        signals = repo.list_log_signals_for_application(app_id, since=T0 - timedelta(hours=1))
        assert len(signals) == 1  # still one row -- extended, not duplicated
        assert signals[0].count == 5
        assert signals[0].last_seen_at == T0 + timedelta(seconds=30)
        conn.close()

    def test_call_outside_window_starts_a_new_row(self, tmp_path):
        conn, repo, app_id, container_row_id = make_repo(tmp_path)
        persist_candidates(
            repo, [make_candidate(0, count=2)], application_id=app_id, container_row_id=container_row_id,
            source_type="container_log", source_ref="stdout+stderr", aggregation_window_seconds=300,
        )
        persist_candidates(
            repo, [make_candidate(1000, count=3)], application_id=app_id, container_row_id=container_row_id,
            source_type="container_log", source_ref="stdout+stderr", aggregation_window_seconds=300,
        )
        signals = repo.list_log_signals_for_application(app_id, since=T0 - timedelta(hours=1))
        assert len(signals) == 2
        assert {s.count for s in signals} == {2, 3}
        conn.close()

    def test_sample_never_changes_once_a_row_is_extended(self, tmp_path):
        conn, repo, app_id, container_row_id = make_repo(tmp_path)
        persist_candidates(
            repo, [make_candidate(0, text="original sample")], application_id=app_id,
            container_row_id=container_row_id, source_type="container_log", source_ref="stdout+stderr",
            aggregation_window_seconds=300,
        )
        persist_candidates(
            repo, [make_candidate(10, text="a different later sample")], application_id=app_id,
            container_row_id=container_row_id, source_type="container_log", source_ref="stdout+stderr",
            aggregation_window_seconds=300,
        )
        signals = repo.list_log_signals_for_application(app_id, since=T0 - timedelta(hours=1))
        assert signals[0].sample == "original sample"
        conn.close()

    def test_different_category_never_extends_a_different_categorys_row(self, tmp_path):
        conn, repo, app_id, container_row_id = make_repo(tmp_path)
        persist_candidates(
            repo, [make_candidate(0)], application_id=app_id, container_row_id=container_row_id,
            source_type="container_log", source_ref="stdout+stderr", aggregation_window_seconds=300,
        )
        oom_candidate = SignalCandidate(
            category=EvidenceCategory.OOM, severity=EvidenceSeverity.CRITICAL,
            normalized_signature="oom killed", first_seen_at=T0 + timedelta(seconds=5),
            last_seen_at=T0 + timedelta(seconds=5), count=1, sample="oom killed",
        )
        persist_candidates(
            repo, [oom_candidate], application_id=app_id, container_row_id=container_row_id,
            source_type="container_log", source_ref="stdout+stderr", aggregation_window_seconds=300,
        )
        signals = repo.list_log_signals_for_application(app_id, since=T0 - timedelta(hours=1))
        assert len(signals) == 2
        conn.close()


class TestSourceFields:
    def test_source_type_and_ref_are_persisted(self, tmp_path):
        conn, repo, app_id, container_row_id = make_repo(tmp_path)
        persist_candidates(
            repo, [make_candidate(0)], application_id=app_id, container_row_id=container_row_id,
            source_type="docker_fact", source_ref="restart_count", aggregation_window_seconds=300,
        )
        signal = repo.list_log_signals_for_application(app_id, since=T0 - timedelta(hours=1))[0]
        assert signal.source_type == "docker_fact"
        assert signal.source_ref == "restart_count"
        conn.close()
