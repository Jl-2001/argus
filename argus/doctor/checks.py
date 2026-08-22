"""The six live prerequisite checks behind ``argus doctor``.

Fixed, deterministic order -- never reordered by status:

    configuration -> database -> docker_connection -> docker_read_access
    -> collector_heartbeat -> clock

``docker_read_access`` is skipped (not failed) when ``docker_connection``
already failed; ``collector_heartbeat`` is skipped when ``database``
already failed -- there is nothing trustworthy to check in either case,
and reporting a fabricated FAIL for a check that never actually ran
would misrepresent what Argus knows. ``clock``'s own runtime sanity
(does this process produce a UTC-aware ``now``) is independent of the
database and always runs; only its two DB-derived sub-checks
(future ``last_tick_at``, impossible ``last_success_at``/``last_tick_at``
ordering) are skipped in substance -- not flagged -- when there is no
collector state to compare against.
"""

from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Callable, Optional

from argus.collectors.docker_client import DockerClient, DockerUnavailableError
from argus.domain.health import DEFAULT_HEALTH_RULES, HealthRules
from argus.store.database import (
    SCHEMA_VERSION,
    inspect_database_readonly,
    open_database_readonly,
)
from argus.store.repository import Repository

__all__ = [
    "CheckStatus",
    "DoctorCheck",
    "DoctorResult",
    "run_checks",
]

#: A few seconds of slack for scheduling/write-ordering jitter between
#: `now` (read once, at the CLI boundary) and timestamps a collector
#: tick wrote a moment earlier or later. Not NTP, not network time --
#: a local sanity margin only.
CLOCK_TOLERANCE_SECONDS = 5


class CheckStatus(str, Enum):
    PASS = "PASS"
    WARN = "WARN"
    FAIL = "FAIL"
    SKIP = "SKIP"


@dataclass(frozen=True, slots=True)
class DoctorCheck:
    """One check's outcome. The check itself decides PASS/WARN/FAIL/SKIP
    -- formatters only render whatever is already decided here."""

    name: str
    status: CheckStatus
    message: Optional[str] = None


@dataclass(frozen=True, slots=True)
class DoctorResult:
    checks: tuple[DoctorCheck, ...]

    @property
    def operational(self) -> bool:
        """True unless at least one check FAILed.

        A WARN alone (e.g. the collector is currently retrying after a
        transient Docker outage, but its last success is still fresh)
        does not make Argus non-operational -- degraded, but usable. A
        SKIP caused by a failed parent check doesn't need special
        handling here: the parent itself is already FAIL, which this
        already accounts for.
        """

        return not any(check.status is CheckStatus.FAIL for check in self.checks)


def _relative_seconds(now: datetime, timestamp: Optional[datetime]) -> str:
    """A tiny, self-contained "Xs/Xm/Xh/Xd ago" formatter.

    Deliberately not imported from ``argus.cli.formatting``: this
    package must stay independent of the CLI's presentation layer (a
    future dashboard reusing these diagnostics shouldn't have to pull in
    CLI text rendering to get a human-readable message string). The
    bucketing matches `argus.cli.formatting.relative_time`'s for a
    consistent feel across the app -- duplicated on purpose, not out of
    neglect.
    """

    if timestamp is None:
        return "never"
    age = max(0.0, (now - timestamp).total_seconds())
    if age < 60:
        return f"{int(age)}s ago"
    if age < 3600:
        return f"{int(age // 60)}m ago"
    if age < 86400:
        return f"{int(age // 3600)}h ago"
    return f"{int(age // 86400)}d ago"


# --------------------------------------------------------------------------
# Check 1 -- Configuration
# --------------------------------------------------------------------------


def check_configuration(
    db_path, *, rules: HealthRules = DEFAULT_HEALTH_RULES
) -> DoctorCheck:
    """Validates the configuration Argus actually has today -- there is no
    settings.yaml yet, so this vouches for the same defaults/resolution
    logic every other command already uses, rather than inventing a
    second configuration system just for doctor.
    """

    problems: list[str] = []

    if not str(db_path).strip():
        problems.append("resolved database path is empty")

    # HealthRules validates its own invariants in __post_init__; this
    # re-affirms the *active* defaults are sane rather than assuming it.
    try:
        HealthRules(
            unknown_after=rules.unknown_after,
            restart_loop_window=rules.restart_loop_window,
            restart_loop_threshold=rules.restart_loop_threshold,
            degraded_restart_threshold=rules.degraded_restart_threshold,
        )
    except ValueError as exc:
        problems.append(f"health rules configuration invalid: {exc}")

    docker_host = os.environ.get("DOCKER_HOST")
    if docker_host is not None and not docker_host.strip():
        problems.append("DOCKER_HOST is set but empty")

    if problems:
        return DoctorCheck("configuration", CheckStatus.FAIL, "; ".join(problems))
    return DoctorCheck("configuration", CheckStatus.PASS, None)


# --------------------------------------------------------------------------
# Check 2 -- Database
# --------------------------------------------------------------------------


def check_database(db_path) -> DoctorCheck:
    """Read-only: never creates, initializes, or migrates anything (see
    `inspect_database_readonly`). Distinguishes missing / unreadable /
    malformed / wrong-schema-version, each with its own clear message.
    """

    inspection = inspect_database_readonly(db_path)

    if not inspection.exists:
        return DoctorCheck("database", CheckStatus.FAIL, f"database file does not exist at {db_path}")
    if not inspection.opened:
        return DoctorCheck("database", CheckStatus.FAIL, inspection.error)
    if inspection.schema_version == 0:
        return DoctorCheck("database", CheckStatus.FAIL, "database file has no Argus schema")
    if inspection.schema_version != SCHEMA_VERSION:
        return DoctorCheck(
            "database", CheckStatus.FAIL,
            f"unsupported schema version {inspection.schema_version} "
            f"(this build supports {SCHEMA_VERSION}); doctor does not migrate -- see MILESTONE 4-6 for the migration policy",
        )
    if inspection.missing_tables:
        return DoctorCheck(
            "database", CheckStatus.FAIL,
            f"missing required table(s): {', '.join(inspection.missing_tables)}",
        )
    return DoctorCheck("database", CheckStatus.PASS, None)


# --------------------------------------------------------------------------
# Check 3 & 4 -- Docker connection / read access
# --------------------------------------------------------------------------


def check_docker_connection(
    docker_client_factory: Callable[[], DockerClient],
) -> tuple[DoctorCheck, Optional[DockerClient]]:
    """Reuses the existing Milestone 3 `DockerClient` -- never a second
    connection implementation. Constructing it already performs the
    real "can we reach the daemon" negotiation (see `DockerClient`'s own
    docstring)."""

    try:
        client = docker_client_factory()
    except DockerUnavailableError as exc:
        return DoctorCheck("docker_connection", CheckStatus.FAIL, str(exc)), None
    return DoctorCheck("docker_connection", CheckStatus.PASS, None), client


def check_docker_read_access(client: Optional[DockerClient]) -> DoctorCheck:
    """SKIP (not FAIL) when `client` is None -- the connection check
    already failed, so there is nothing to evaluate here; fabricating a
    second FAIL for the same root cause would be misleading. Only ever
    calls `list_containers`, the existing read-only method -- never a
    write.
    """

    if client is None:
        return DoctorCheck("docker_read_access", CheckStatus.SKIP, "skipped: Docker connection failed")
    try:
        client.list_containers(all=True)
    except DockerUnavailableError as exc:
        return DoctorCheck("docker_read_access", CheckStatus.FAIL, str(exc))
    return DoctorCheck("docker_read_access", CheckStatus.PASS, None)


# --------------------------------------------------------------------------
# Check 5 -- Collector heartbeat
# --------------------------------------------------------------------------


def check_collector_heartbeat(
    db_path, *, database_ok: bool, now: datetime, rules: HealthRules = DEFAULT_HEALTH_RULES
) -> DoctorCheck:
    """Classifies `collector_state` using the same `unknown_after`
    threshold Milestone 2 already defined for observation staleness --
    not a second, invented number.

    * no tick ever attempted -> FAIL. An installed Argus meant to
      monitor that has never completed a tick cannot back up any status
      it might otherwise report.
    * ticked, but never once succeeded -> FAIL, for the same reason.
    * last success older than `unknown_after` -> FAIL: current
      monitoring truth is no longer trustworthy, regardless of how
      recently a (failing) tick was *attempted*.
    * last success still fresh, but `consecutive_failures > 0` -> WARN:
      degraded (something is currently wrong and being retried) but the
      data on hand is still within its freshness window.
    * otherwise -> PASS.
    """

    if not database_ok:
        return DoctorCheck("collector_heartbeat", CheckStatus.SKIP, "skipped: database unavailable")

    try:
        connection = open_database_readonly(db_path)
    except sqlite3.Error as exc:
        return DoctorCheck("collector_heartbeat", CheckStatus.FAIL, f"could not read collector state: {exc}")
    try:
        state = Repository(connection).get_collector_state()
    finally:
        connection.close()

    if state.last_tick_at is None:
        return DoctorCheck("collector_heartbeat", CheckStatus.FAIL, "collector has never completed a tick")

    if state.last_success_at is None:
        return DoctorCheck(
            "collector_heartbeat", CheckStatus.FAIL, "collector has ticked but has never succeeded"
        )

    success_age = (now - state.last_success_at).total_seconds()
    if success_age > rules.unknown_after:
        return DoctorCheck(
            "collector_heartbeat", CheckStatus.FAIL,
            f"last successful collector tick was {_relative_seconds(now, state.last_success_at)} "
            f"(stale beyond {rules.unknown_after}s)",
        )

    if state.consecutive_failures > 0:
        return DoctorCheck(
            "collector_heartbeat", CheckStatus.WARN,
            f"collector is currently failing ({state.consecutive_failures} consecutive failure"
            f"{'s' if state.consecutive_failures != 1 else ''}), but last success "
            f"({_relative_seconds(now, state.last_success_at)}) is still within the freshness window",
        )

    return DoctorCheck("collector_heartbeat", CheckStatus.PASS, None)


# --------------------------------------------------------------------------
# Check 6 -- Clock / timestamp sanity
# --------------------------------------------------------------------------


def check_clock(db_path, *, database_ok: bool, now: datetime) -> DoctorCheck:
    """Local sanity only -- no NTP, no external time service.

    Always checks that `now` itself is UTC-aware (the one thing every
    other check in this module implicitly trusts). When the database is
    available, additionally checks two impossible orderings against
    `collector_state`:

    * `last_tick_at` ahead of `now` by more than `CLOCK_TOLERANCE_SECONDS`
      -- the collector wrote a timestamp from the future.
    * `last_success_at` after `last_tick_at` by more than the same
      tolerance -- a tick cannot have succeeded after a *later* tick
      started (ownership note: this specific impossible relationship is
      Clock's to report, not Collector heartbeat's -- it is a timestamp
      *ordering* defect, not a statement about whether monitoring is
      currently fresh).
    """

    if now.tzinfo is None or now.utcoffset() != timedelta(0):
        return DoctorCheck("clock", CheckStatus.FAIL, "system clock did not produce a UTC-aware instant")

    if not database_ok:
        # Nothing further to compare against; the clock itself is fine.
        return DoctorCheck("clock", CheckStatus.PASS, None)

    try:
        connection = open_database_readonly(db_path)
    except sqlite3.Error:
        # Database check already covers a broken/missing file; Clock
        # doesn't re-report the same root cause under its own name.
        return DoctorCheck("clock", CheckStatus.PASS, None)
    try:
        state = Repository(connection).get_collector_state()
    finally:
        connection.close()

    tolerance = timedelta(seconds=CLOCK_TOLERANCE_SECONDS)

    if state.last_tick_at is not None and (state.last_tick_at - now) > tolerance:
        return DoctorCheck(
            "clock", CheckStatus.FAIL,
            f"collector's last_tick_at is ahead of the system clock by more than "
            f"{CLOCK_TOLERANCE_SECONDS}s",
        )

    if (
        state.last_success_at is not None
        and state.last_tick_at is not None
        and (state.last_success_at - state.last_tick_at) > tolerance
    ):
        return DoctorCheck(
            "clock", CheckStatus.FAIL,
            "collector's last_success_at is after last_tick_at beyond tolerance -- "
            "an impossible ordering",
        )

    return DoctorCheck("clock", CheckStatus.PASS, None)


# --------------------------------------------------------------------------
# Orchestration
# --------------------------------------------------------------------------


def run_checks(
    *,
    db_path,
    now: datetime,
    docker_client_factory: Callable[[], DockerClient] = DockerClient,
    rules: HealthRules = DEFAULT_HEALTH_RULES,
) -> DoctorResult:
    """Run all six checks in the fixed order below and return one
    `DoctorResult`. Never mutates Docker or the database -- every check
    above is read-only, and this function performs no I/O of its own
    beyond calling them.
    """

    configuration_check = check_configuration(db_path, rules=rules)
    database_check = check_database(db_path)
    docker_connection_check, docker_client = check_docker_connection(docker_client_factory)
    docker_read_check = check_docker_read_access(docker_client)
    heartbeat_check = check_collector_heartbeat(
        db_path, database_ok=database_check.status is CheckStatus.PASS, now=now, rules=rules
    )
    clock_check = check_clock(db_path, database_ok=database_check.status is CheckStatus.PASS, now=now)

    return DoctorResult(
        checks=(
            configuration_check,
            database_check,
            docker_connection_check,
            docker_read_check,
            heartbeat_check,
            clock_check,
        )
    )
