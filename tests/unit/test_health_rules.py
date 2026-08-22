"""Tests for argus.domain.health.

Every test here constructs synthetic domain objects directly — no
Docker, no database, no filesystem, no network. Like
test_domain_models.py, this suite must pass with all of that
unavailable.
"""

from __future__ import annotations

import ast
import inspect
from datetime import datetime, timedelta, timezone

import pytest

from argus.domain import health
from argus.domain.health import (
    DEFAULT_HEALTH_RULES,
    HealthEvaluation,
    HealthRules,
    evaluate_application_health,
    evaluate_container_health,
    evaluate_service_health,
)
from argus.domain.models import (
    Container,
    DockerHealth,
    DockerState,
    HealthStatus,
    Observation,
)

UTC = timezone.utc
BASE = datetime(2026, 8, 21, 10, 0, 0, tzinfo=UTC)


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def make_container(container_id: str = "c" * 64, **overrides) -> Container:
    defaults = dict(
        container_id=container_id,
        name="api",
        image="cnstrct/api:latest",
        compose_project="cnstrct",
        compose_service="api",
        first_seen_at=BASE - timedelta(days=1),
        last_seen_at=BASE,
    )
    defaults.update(overrides)
    return Container(**defaults)


def make_observation(
    container: Container,
    *,
    observed_at: datetime = BASE,
    docker_state: DockerState = DockerState.RUNNING,
    docker_health: DockerHealth | None = DockerHealth.HEALTHY,
    restart_count: int = 0,
    exit_code: int | None = None,
) -> Observation:
    return Observation(
        container_ref=container,
        observed_at=observed_at,
        docker_state=docker_state,
        docker_health=docker_health,
        restart_count=restart_count,
        exit_code=exit_code,
        started_at=None,
        finished_at=None,
        ports=(),
        labels={},
        # Placeholder only — the evaluator never reads these two fields;
        # they exist on Observation purely as where its output later gets
        # recorded by something else.
        derived_status=HealthStatus.UNKNOWN,
        derived_detail=None,
    )


def evaluate(observation: Observation, *, now: datetime = BASE, prior=(), rules=DEFAULT_HEALTH_RULES):
    return evaluate_container_health(
        observation=observation, now=now, prior_observations=prior, rules=rules
    )


# --------------------------------------------------------------------------
# HealthRules configuration
# --------------------------------------------------------------------------


class TestHealthRulesConfig:
    def test_defaults_match_approved_specification(self):
        rules = HealthRules()
        assert rules.unknown_after == 60
        assert rules.restart_loop_window == 300
        assert rules.restart_loop_threshold == 3
        assert rules.degraded_restart_threshold == 1

    @pytest.mark.parametrize(
        "field, value",
        [
            ("unknown_after", 0),
            ("unknown_after", -5),
            ("restart_loop_window", 0),
            ("restart_loop_threshold", 0),
            ("degraded_restart_threshold", 0),
        ],
    )
    def test_nonpositive_values_rejected(self, field, value):
        kwargs = {field: value}
        with pytest.raises(ValueError):
            HealthRules(**kwargs)

    def test_degraded_threshold_must_be_below_loop_threshold(self):
        with pytest.raises(ValueError, match="strictly less than"):
            HealthRules(degraded_restart_threshold=3, restart_loop_threshold=3)


# --------------------------------------------------------------------------
# Container cases 1-13 from the specification
# --------------------------------------------------------------------------


class TestContainerHealthRules:
    def test_running_without_healthcheck_is_healthy(self):
        c = make_container()
        obs = make_observation(c, docker_state=DockerState.RUNNING, docker_health=None)
        result = evaluate(obs)
        assert result == HealthEvaluation(HealthStatus.HEALTHY, None)

    def test_running_and_docker_healthy_is_healthy(self):
        c = make_container()
        obs = make_observation(c, docker_health=DockerHealth.HEALTHY)
        assert evaluate(obs).status is HealthStatus.HEALTHY

    def test_running_and_docker_starting_is_degraded(self):
        c = make_container()
        obs = make_observation(c, docker_health=DockerHealth.STARTING)
        assert evaluate(obs).status is HealthStatus.DEGRADED

    def test_unhealthy_docker_health_is_unhealthy(self):
        c = make_container()
        obs = make_observation(c, docker_health=DockerHealth.UNHEALTHY)
        assert evaluate(obs).status is HealthStatus.UNHEALTHY

    def test_recent_single_restart_downgrades_healthy_to_degraded(self):
        c = make_container()
        prior = make_observation(c, observed_at=BASE - timedelta(seconds=60), restart_count=5)
        current = make_observation(c, docker_health=None, restart_count=6)
        result = evaluate(current, prior=[prior])
        assert result == HealthEvaluation(HealthStatus.DEGRADED, "recent_restarts")

    def test_exited_with_zero_exit_code_is_stopped_clean(self):
        c = make_container()
        obs = make_observation(c, docker_state=DockerState.EXITED, docker_health=None, exit_code=0)
        assert evaluate(obs) == HealthEvaluation(HealthStatus.STOPPED, "stopped_clean")

    def test_exited_with_nonzero_exit_code_is_stopped_error(self):
        c = make_container()
        obs = make_observation(c, docker_state=DockerState.EXITED, docker_health=None, exit_code=137)
        assert evaluate(obs) == HealthEvaluation(HealthStatus.STOPPED, "stopped_error")

    def test_exited_with_missing_exit_code_is_stopped_unknown_not_a_guess(self):
        c = make_container()
        obs = make_observation(c, docker_state=DockerState.EXITED, docker_health=None, exit_code=None)
        result = evaluate(obs)
        assert result.status is HealthStatus.STOPPED
        assert result.detail == "stopped_unknown"
        assert result.detail not in ("stopped_clean", "stopped_error")

    def test_docker_state_restarting_is_restarting(self):
        c = make_container()
        obs = make_observation(c, docker_state=DockerState.RESTARTING, docker_health=None)
        assert evaluate(obs).status is HealthStatus.RESTARTING

    def test_restart_loop_from_history_is_restarting(self):
        c = make_container()
        prior = make_observation(c, observed_at=BASE - timedelta(minutes=4), restart_count=5)
        current = make_observation(c, docker_health=DockerHealth.HEALTHY, restart_count=8)
        result = evaluate(current, prior=[prior])
        assert result == HealthEvaluation(HealthStatus.RESTARTING, "restart_loop")

    def test_dead_is_unhealthy(self):
        c = make_container()
        obs = make_observation(c, docker_state=DockerState.DEAD, docker_health=None)
        assert evaluate(obs).status is HealthStatus.UNHEALTHY

    def test_created_is_unknown(self):
        c = make_container()
        obs = make_observation(c, docker_state=DockerState.CREATED, docker_health=None)
        assert evaluate(obs).status is HealthStatus.UNKNOWN

    def test_paused_is_unknown(self):
        c = make_container()
        obs = make_observation(c, docker_state=DockerState.PAUSED, docker_health=None)
        assert evaluate(obs).status is HealthStatus.UNKNOWN

    def test_stale_observation_is_unknown(self):
        c = make_container()
        obs = make_observation(c, observed_at=BASE)
        now = BASE + timedelta(seconds=DEFAULT_HEALTH_RULES.unknown_after + 30)
        result = evaluate(obs, now=now)
        assert result == HealthEvaluation(HealthStatus.UNKNOWN, "stale")


# --------------------------------------------------------------------------
# Boundary tests
# --------------------------------------------------------------------------


class TestStalenessBoundary:
    def test_exactly_unknown_after_is_not_stale(self):
        c = make_container()
        obs = make_observation(c, observed_at=BASE, docker_health=DockerHealth.HEALTHY)
        now = BASE + timedelta(seconds=DEFAULT_HEALTH_RULES.unknown_after)
        result = evaluate(obs, now=now)
        assert result.status is HealthStatus.HEALTHY  # not stale: rule falls through

    def test_one_microsecond_past_unknown_after_is_stale(self):
        c = make_container()
        obs = make_observation(c, observed_at=BASE, docker_health=DockerHealth.HEALTHY)
        now = BASE + timedelta(seconds=DEFAULT_HEALTH_RULES.unknown_after, microseconds=1)
        result = evaluate(obs, now=now)
        assert result == HealthEvaluation(HealthStatus.UNKNOWN, "stale")


class TestRestartThresholdBoundary:
    def test_delta_equal_to_loop_threshold_is_restarting(self):
        c = make_container()
        prior = make_observation(c, observed_at=BASE - timedelta(seconds=100), restart_count=5)
        current = make_observation(c, docker_health=DockerHealth.HEALTHY, restart_count=8)  # +3
        result = evaluate(current, prior=[prior])
        assert result == HealthEvaluation(HealthStatus.RESTARTING, "restart_loop")

    def test_delta_one_below_loop_threshold_is_degraded_not_restarting(self):
        c = make_container()
        prior = make_observation(c, observed_at=BASE - timedelta(seconds=100), restart_count=5)
        current = make_observation(c, docker_health=DockerHealth.HEALTHY, restart_count=7)  # +2
        result = evaluate(current, prior=[prior])
        assert result == HealthEvaluation(HealthStatus.DEGRADED, "recent_restarts")


class TestRestartWindowBoundary:
    def test_prior_exactly_at_window_edge_counts(self):
        c = make_container()
        window = DEFAULT_HEALTH_RULES.restart_loop_window
        prior = make_observation(c, observed_at=BASE - timedelta(seconds=window), restart_count=5)
        current = make_observation(c, docker_health=DockerHealth.HEALTHY, restart_count=8)  # +3
        result = evaluate(current, prior=[prior])
        assert result == HealthEvaluation(HealthStatus.RESTARTING, "restart_loop")

    def test_prior_just_outside_window_edge_is_excluded(self):
        c = make_container()
        window = DEFAULT_HEALTH_RULES.restart_loop_window
        prior = make_observation(
            c, observed_at=BASE - timedelta(seconds=window, microseconds=1), restart_count=5
        )
        current = make_observation(c, docker_health=DockerHealth.HEALTHY, restart_count=8)
        result = evaluate(current, prior=[prior])
        # no in-window baseline found -> delta defined as 0 -> plain HEALTHY
        assert result == HealthEvaluation(HealthStatus.HEALTHY, None)


# --------------------------------------------------------------------------
# Restart history / cumulative-counter tests
# --------------------------------------------------------------------------


class TestRestartHistory:
    def test_cumulative_counter_delta_is_one_not_eleven(self):
        c = make_container()
        prior = make_observation(c, observed_at=BASE - timedelta(seconds=30), restart_count=10)
        current = make_observation(c, docker_health=DockerHealth.HEALTHY, restart_count=11)
        result = evaluate(current, prior=[prior])
        assert result == HealthEvaluation(HealthStatus.DEGRADED, "recent_restarts")

    def test_cumulative_counter_delta_is_three_not_twentyfour(self):
        c = make_container()
        prior = make_observation(c, observed_at=BASE - timedelta(seconds=30), restart_count=21)
        current = make_observation(c, docker_health=DockerHealth.HEALTHY, restart_count=24)
        result = evaluate(current, prior=[prior])
        assert result == HealthEvaluation(HealthStatus.RESTARTING, "restart_loop")

    def test_new_container_identity_resets_restart_history(self):
        old_container = make_container(container_id="old" + "0" * 61)
        new_container = make_container(container_id="new" + "0" * 61)
        prior_from_old_identity = make_observation(
            old_container, observed_at=BASE - timedelta(seconds=30), restart_count=50
        )
        current_on_new_identity = make_observation(
            new_container, docker_health=DockerHealth.HEALTHY, restart_count=2
        )
        result = evaluate(current_on_new_identity, prior=[prior_from_old_identity])
        # unrelated identity's history must not leak in as a false baseline
        assert result == HealthEvaluation(HealthStatus.HEALTHY, None)

    def test_lower_current_restart_count_than_history_clamps_to_zero(self):
        c = make_container()
        prior = make_observation(c, observed_at=BASE - timedelta(seconds=30), restart_count=15)
        current = make_observation(c, docker_health=DockerHealth.HEALTHY, restart_count=10)
        result = evaluate(current, prior=[prior])
        # must not go negative or explode; deterministic "no restart signal"
        assert result == HealthEvaluation(HealthStatus.HEALTHY, None)

    def test_no_prior_observations_means_zero_delta(self):
        c = make_container()
        current = make_observation(c, docker_health=DockerHealth.HEALTHY, restart_count=999)
        result = evaluate(current, prior=[])
        assert result == HealthEvaluation(HealthStatus.HEALTHY, None)


# --------------------------------------------------------------------------
# Precedence tests
# --------------------------------------------------------------------------


class TestPrecedence:
    def test_staleness_precedes_restarting_docker_state(self):
        c = make_container()
        obs = make_observation(c, observed_at=BASE, docker_state=DockerState.RESTARTING)
        now = BASE + timedelta(seconds=DEFAULT_HEALTH_RULES.unknown_after + 1)
        result = evaluate(obs, now=now)
        assert result == HealthEvaluation(HealthStatus.UNKNOWN, "stale")

    def test_restart_loop_precedes_exited_state(self):
        c = make_container()
        prior = make_observation(c, observed_at=BASE - timedelta(seconds=100), restart_count=5)
        current = make_observation(
            c, docker_state=DockerState.EXITED, docker_health=None, exit_code=1, restart_count=8
        )
        result = evaluate(current, prior=[prior])
        assert result == HealthEvaluation(HealthStatus.RESTARTING, "restart_loop")
        assert result.status is not HealthStatus.STOPPED

    def test_restart_loop_precedes_dead_state(self):
        c = make_container()
        prior = make_observation(c, observed_at=BASE - timedelta(seconds=100), restart_count=5)
        current = make_observation(c, docker_state=DockerState.DEAD, docker_health=None, restart_count=8)
        result = evaluate(current, prior=[prior])
        assert result == HealthEvaluation(HealthStatus.RESTARTING, "restart_loop")


# --------------------------------------------------------------------------
# Service rollup
# --------------------------------------------------------------------------


class TestServiceRollup:
    def test_single_healthy_container(self):
        result = evaluate_service_health(
            container_evaluations=[HealthEvaluation(HealthStatus.HEALTHY)]
        )
        assert result.status is HealthStatus.HEALTHY

    def test_single_degraded_container(self):
        result = evaluate_service_health(
            container_evaluations=[HealthEvaluation(HealthStatus.DEGRADED, "recent_restarts")]
        )
        assert result == HealthEvaluation(HealthStatus.DEGRADED, "recent_restarts")

    def test_single_unhealthy_container(self):
        result = evaluate_service_health(
            container_evaluations=[HealthEvaluation(HealthStatus.UNHEALTHY)]
        )
        assert result.status is HealthStatus.UNHEALTHY

    def test_zero_containers_is_unknown(self):
        result = evaluate_service_health(container_evaluations=[])
        assert result == HealthEvaluation(HealthStatus.UNKNOWN, "no_containers")

    def test_multiple_containers_raises_v01_assumption_error(self):
        with pytest.raises(ValueError, match="one container per service"):
            evaluate_service_health(
                container_evaluations=[
                    HealthEvaluation(HealthStatus.HEALTHY),
                    HealthEvaluation(HealthStatus.HEALTHY),
                ]
            )


# --------------------------------------------------------------------------
# Application rollup
# --------------------------------------------------------------------------


class TestApplicationRollup:
    def test_all_healthy_is_healthy(self):
        result = evaluate_application_health(
            service_evaluations=[HealthEvaluation(HealthStatus.HEALTHY)] * 4
        )
        assert result.status is HealthStatus.HEALTHY

    def test_all_stopped_is_stopped(self):
        result = evaluate_application_health(
            service_evaluations=[HealthEvaluation(HealthStatus.STOPPED, "stopped_clean")] * 3
        )
        assert result == HealthEvaluation(HealthStatus.STOPPED, None)

    def test_one_stopped_among_healthy_is_unhealthy_not_stopped(self):
        evaluations = [HealthEvaluation(HealthStatus.HEALTHY)] * 3 + [
            HealthEvaluation(HealthStatus.STOPPED, "stopped_clean")
        ]
        result = evaluate_application_health(service_evaluations=evaluations)
        assert result == HealthEvaluation(HealthStatus.UNHEALTHY, "partial_stop")

    def test_one_unhealthy_among_healthy_is_unhealthy(self):
        evaluations = [HealthEvaluation(HealthStatus.HEALTHY)] * 3 + [
            HealthEvaluation(HealthStatus.UNHEALTHY)
        ]
        result = evaluate_application_health(service_evaluations=evaluations)
        assert result.status is HealthStatus.UNHEALTHY

    def test_one_restarting_among_healthy_is_restarting(self):
        evaluations = [HealthEvaluation(HealthStatus.HEALTHY)] * 3 + [
            HealthEvaluation(HealthStatus.RESTARTING, "restart_loop")
        ]
        result = evaluate_application_health(service_evaluations=evaluations)
        assert result.status is HealthStatus.RESTARTING

    def test_one_degraded_among_healthy_is_degraded(self):
        evaluations = [HealthEvaluation(HealthStatus.HEALTHY)] * 3 + [
            HealthEvaluation(HealthStatus.DEGRADED)
        ]
        result = evaluate_application_health(service_evaluations=evaluations)
        assert result.status is HealthStatus.DEGRADED

    def test_one_unknown_among_healthy_is_unknown(self):
        evaluations = [HealthEvaluation(HealthStatus.HEALTHY)] * 3 + [
            HealthEvaluation(HealthStatus.UNKNOWN, "stale")
        ]
        result = evaluate_application_health(service_evaluations=evaluations)
        assert result.status is HealthStatus.UNKNOWN

    def test_unknown_and_degraded_mix_is_degraded(self):
        """Would fail under naive string/enum-order comparison (UNKNOWN
        sorts after DEGRADED alphabetically and would win incorrectly)."""
        evaluations = [
            HealthEvaluation(HealthStatus.UNKNOWN, "stale"),
            HealthEvaluation(HealthStatus.DEGRADED),
        ]
        result = evaluate_application_health(service_evaluations=evaluations)
        assert result.status is HealthStatus.DEGRADED

    def test_degraded_and_restarting_mix_is_restarting(self):
        evaluations = [
            HealthEvaluation(HealthStatus.DEGRADED),
            HealthEvaluation(HealthStatus.RESTARTING, "restart_loop"),
        ]
        result = evaluate_application_health(service_evaluations=evaluations)
        assert result.status is HealthStatus.RESTARTING

    def test_restarting_and_unhealthy_mix_is_unhealthy(self):
        evaluations = [
            HealthEvaluation(HealthStatus.RESTARTING, "restart_loop"),
            HealthEvaluation(HealthStatus.UNHEALTHY),
        ]
        result = evaluate_application_health(service_evaluations=evaluations)
        assert result.status is HealthStatus.UNHEALTHY

    def test_healthy_and_degraded_mix_is_degraded_not_healthy(self):
        """Would fail under naive string comparison (HEALTHY sorts after
        DEGRADED alphabetically and would win incorrectly)."""
        evaluations = [
            HealthEvaluation(HealthStatus.HEALTHY),
            HealthEvaluation(HealthStatus.DEGRADED),
        ]
        result = evaluate_application_health(service_evaluations=evaluations)
        assert result.status is HealthStatus.DEGRADED

    def test_zero_services_is_unknown(self):
        result = evaluate_application_health(service_evaluations=[])
        assert result == HealthEvaluation(HealthStatus.UNKNOWN, "no_services")


# --------------------------------------------------------------------------
# No-mutation guarantee
# --------------------------------------------------------------------------


class TestNoMutation:
    def test_evaluating_health_does_not_change_the_observation(self):
        c = make_container()
        obs = make_observation(c, docker_health=DockerHealth.UNHEALTHY)
        before = obs.to_dict()

        evaluate_container_health(observation=obs, now=BASE)

        after = obs.to_dict()
        assert before == after
        # the placeholder fields the evaluator deliberately never touches
        assert obs.derived_status is HealthStatus.UNKNOWN
        assert obs.derived_detail is None


# --------------------------------------------------------------------------
# Architecture guard
# --------------------------------------------------------------------------

FORBIDDEN_IMPORT_ROOTS = {
    "docker",
    "sqlite3",
    "sqlalchemy",
    "anthropic",
    "openai",
    "langgraph",
    "fastapi",
    "requests",
    "httpx",
}

FORBIDDEN_CLOCK_CALLS = {("datetime", "now"), ("datetime", "utcnow"), ("time", "time")}


def _imported_roots(source: str) -> set[str]:
    tree = ast.parse(source)
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                roots.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                roots.add(node.module.split(".")[0])
    return roots


def _clock_reads(source: str) -> list[str]:
    tree = ast.parse(source)
    found: list[str] = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and (node.func.value.id, node.func.attr) in FORBIDDEN_CLOCK_CALLS
        ):
            found.append(f"{node.func.value.id}.{node.func.attr}")
    return found


class TestArchitectureGuard:
    def test_health_module_has_no_infrastructure_imports(self):
        source = inspect.getsource(health)
        found = _imported_roots(source) & FORBIDDEN_IMPORT_ROOTS
        assert not found, f"argus.domain.health imports forbidden module(s): {found}"

    def test_health_module_does_not_read_the_wall_clock(self):
        source = inspect.getsource(health)
        found = _clock_reads(source)
        assert not found, f"argus.domain.health reads the wall clock directly: {found}"
