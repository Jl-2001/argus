"""Tests for the pure selection layer,
argus.evidence.assembler.select_bundle_contents -- no SQLite, no Docker,
every candidate built in memory. This proves the priority/budget policy
independently of persistence.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from argus.evidence.assembler import AssemblerConfig, select_bundle_contents
from argus.evidence.bundle import ObservationItem, SignalItem, TransitionItem

UTC = timezone.utc
OPENED_AT = datetime(2026, 8, 23, 12, 0, 0, tzinfo=UTC)


def make_signal(id: int, *, severity="high", count=1, offset_seconds=0, category="db_connection_timeout"):
    at = OPENED_AT + timedelta(seconds=offset_seconds)
    return SignalItem(
        reference=f"log_signal:{id}", source_id=id, category=category, severity=severity, count=count,
        first_seen_at=at, last_seen_at=at, sample="sample text", source_type="container_log",
        source_ref="stdout+stderr", container_id=f"docker-{id}", source_label=f"service-{id}",
    )


def make_transition(id: int, *, scope="container", offset_seconds=0, label="api"):
    at = OPENED_AT + timedelta(seconds=offset_seconds)
    return TransitionItem(
        reference=f"health_transition:{id}", source_id=id, scope=scope, label=label,
        from_status="HEALTHY", to_status="UNHEALTHY", occurred_at=at,
    )


def make_observation(id: int, *, offset_seconds=0, related_transition_reference="health_transition:1", reason="at_transition"):
    at = OPENED_AT + timedelta(seconds=offset_seconds)
    return ObservationItem(
        reference=f"observation:{id}", source_id=id, container_id="docker-1", source_label="api",
        observed_at=at, docker_state="running", docker_health=None, restart_count=0,
        derived_status="UNHEALTHY", sampling_reason=reason, related_transition_reference=related_transition_reference,
    )


SMALL_CONFIG = AssemblerConfig(max_signals=2, max_transitions=2, max_observations=2)


class TestProvenance:
    def test_every_selected_signal_has_a_stable_reference(self):
        signals = [make_signal(1), make_signal(2)]
        result = select_bundle_contents(
            signals=signals, transitions=[], observations=[], config=SMALL_CONFIG, opened_at=OPENED_AT
        )
        assert {s.reference for s in result.signals} == {"log_signal:1", "log_signal:2"}

    def test_every_selected_transition_has_a_stable_reference(self):
        transitions = [make_transition(1), make_transition(2)]
        result = select_bundle_contents(
            signals=[], transitions=transitions, observations=[], config=SMALL_CONFIG, opened_at=OPENED_AT
        )
        assert {t.reference for t in result.transitions} == {"health_transition:1", "health_transition:2"}


class TestSeverityPriority:
    def test_higher_severity_survives_the_signal_budget(self):
        signals = [
            make_signal(1, severity="info"),
            make_signal(2, severity="critical"),
            make_signal(3, severity="warning"),
        ]
        config = AssemblerConfig(max_signals=1)
        result = select_bundle_contents(signals=signals, transitions=[], observations=[], config=config, opened_at=OPENED_AT)
        assert len(result.signals) == 1
        assert result.signals[0].severity == "critical"

    def test_full_ordering_is_critical_high_warning_info(self):
        signals = [make_signal(1, severity="info"), make_signal(2, severity="high"),
                   make_signal(3, severity="warning"), make_signal(4, severity="critical")]
        config = AssemblerConfig(max_signals=10)
        result = select_bundle_contents(signals=signals, transitions=[], observations=[], config=config, opened_at=OPENED_AT)
        assert [s.severity for s in result.signals] == ["critical", "high", "warning", "info"]


class TestTemporalPriorityWithinEqualSeverity:
    def test_nearer_to_opening_survives_when_severity_ties(self):
        signals = [
            make_signal(1, severity="high", offset_seconds=500),
            make_signal(2, severity="high", offset_seconds=5),
        ]
        config = AssemblerConfig(max_signals=1)
        result = select_bundle_contents(signals=signals, transitions=[], observations=[], config=config, opened_at=OPENED_AT)
        assert result.signals[0].reference == "log_signal:2"

    def test_distance_is_absolute_both_before_and_after_opening(self):
        signals = [
            make_signal(1, severity="high", offset_seconds=-5),  # 5s before opening
            make_signal(2, severity="high", offset_seconds=50),  # 50s after opening
        ]
        config = AssemblerConfig(max_signals=1)
        result = select_bundle_contents(signals=signals, transitions=[], observations=[], config=config, opened_at=OPENED_AT)
        assert result.signals[0].reference == "log_signal:1"  # closer in absolute time


class TestHigherCountTiebreak:
    def test_higher_count_wins_when_severity_and_distance_tie(self):
        signals = [make_signal(1, count=2, offset_seconds=10), make_signal(2, count=9, offset_seconds=10)]
        config = AssemblerConfig(max_signals=1)
        result = select_bundle_contents(signals=signals, transitions=[], observations=[], config=config, opened_at=OPENED_AT)
        assert result.signals[0].reference == "log_signal:2"


class TestDeterministicIdTiebreak:
    def test_identical_severity_distance_and_count_falls_back_to_id(self):
        signals = [make_signal(5, count=1, offset_seconds=0), make_signal(3, count=1, offset_seconds=0)]
        config = AssemblerConfig(max_signals=1)
        result = select_bundle_contents(signals=signals, transitions=[], observations=[], config=config, opened_at=OPENED_AT)
        assert result.signals[0].reference == "log_signal:3"  # lower id wins the tie


class TestDeterministicOrdering:
    def test_same_inputs_different_retrieval_order_same_result_order(self):
        signals_a = [make_signal(1, severity="high"), make_signal(2, severity="critical"), make_signal(3, severity="info")]
        signals_b = list(reversed(signals_a))
        config = AssemblerConfig(max_signals=10)
        result_a = select_bundle_contents(signals=signals_a, transitions=[], observations=[], config=config, opened_at=OPENED_AT)
        result_b = select_bundle_contents(signals=signals_b, transitions=[], observations=[], config=config, opened_at=OPENED_AT)
        assert [s.reference for s in result_a.signals] == [s.reference for s in result_b.signals]


class TestTransitionScopePriority:
    def test_application_scope_outranks_service_and_container(self):
        transitions = [
            make_transition(1, scope="container"),
            make_transition(2, scope="application"),
            make_transition(3, scope="service"),
        ]
        config = AssemblerConfig(max_transitions=10)
        result = select_bundle_contents(signals=[], transitions=transitions, observations=[], config=config, opened_at=OPENED_AT)
        assert [t.scope for t in result.transitions] == ["application", "service", "container"]

    def test_scope_budget_keeps_the_coarser_scope_first(self):
        transitions = [make_transition(1, scope="container"), make_transition(2, scope="application")]
        config = AssemblerConfig(max_transitions=1)
        result = select_bundle_contents(signals=[], transitions=transitions, observations=[], config=config, opened_at=OPENED_AT)
        assert result.transitions[0].scope == "application"


class TestContradictoryFactsBothRemain:
    def test_conflicting_signals_are_not_removed(self):
        # e.g. "API health check healthy" alongside "db_connection_timeout
        # signals exist" -- the selector never removes a fact merely
        # because it conflicts with another.
        healthy_note = make_signal(1, category="generic_error", severity="info")
        timeout_signal = make_signal(2, category="db_connection_timeout", severity="high")
        config = AssemblerConfig(max_signals=10)
        result = select_bundle_contents(
            signals=[healthy_note, timeout_signal], transitions=[], observations=[], config=config, opened_at=OPENED_AT
        )
        assert len(result.signals) == 2


class TestObservationDependsOnSurvivingTransition:
    def test_observation_tied_to_a_dropped_transition_is_also_dropped(self):
        transitions = [make_transition(1, scope="container"), make_transition(2, scope="application")]
        observations = [make_observation(10, related_transition_reference="health_transition:1")]
        config = AssemblerConfig(max_transitions=1, max_observations=10)  # only the application-scope one survives
        result = select_bundle_contents(
            signals=[], transitions=transitions, observations=observations, config=config, opened_at=OPENED_AT
        )
        assert result.transitions[0].reference == "health_transition:2"
        assert result.observations == ()
        assert result.omitted_counts["observations"] == 1

    def test_observation_tied_to_a_surviving_transition_is_kept(self):
        transitions = [make_transition(1, scope="application")]
        observations = [make_observation(10, related_transition_reference="health_transition:1")]
        config = AssemblerConfig(max_transitions=10, max_observations=10)
        result = select_bundle_contents(
            signals=[], transitions=transitions, observations=observations, config=config, opened_at=OPENED_AT
        )
        assert len(result.observations) == 1

    def test_duplicate_observation_references_are_deduplicated(self):
        transitions = [make_transition(1, scope="application")]
        observations = [
            make_observation(10, related_transition_reference="health_transition:1"),
            make_observation(10, related_transition_reference="health_transition:1"),  # same reference twice
        ]
        config = AssemblerConfig(max_transitions=10, max_observations=10)
        result = select_bundle_contents(
            signals=[], transitions=transitions, observations=observations, config=config, opened_at=OPENED_AT
        )
        assert len(result.observations) == 1


class TestOmittedCounts:
    def test_omitted_counts_reflect_exactly_what_was_dropped(self):
        signals = [make_signal(i) for i in range(5)]
        config = AssemblerConfig(max_signals=2)
        result = select_bundle_contents(signals=signals, transitions=[], observations=[], config=config, opened_at=OPENED_AT)
        assert len(result.signals) == 2
        assert result.omitted_counts["signals"] == 3

    def test_no_omission_when_everything_fits(self):
        signals = [make_signal(1)]
        config = AssemblerConfig(max_signals=10)
        result = select_bundle_contents(signals=signals, transitions=[], observations=[], config=config, opened_at=OPENED_AT)
        assert result.omitted_counts == {"signals": 0, "transitions": 0, "observations": 0}
