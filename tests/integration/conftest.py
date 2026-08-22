"""Shared fixtures and helpers for Argus's Milestone 9 end-to-end tests.

Everything that can *mutate* Docker in this directory funnels through
`safe_stop`/`safe_start`/`safe_restart` below, which refuse to act on
any container whose `com.docker.compose.project` label isn't exactly
`TEST_PROJECT_NAME`. This is test-harness code, not Argus itself --
Argus's own `argus.collectors.docker_client.DockerClient` has no
mutating methods at all, in production or in tests; the raw
docker-py client used here exists solely to drive the disposable
stack.
"""

from __future__ import annotations

import subprocess
import time
from pathlib import Path
from typing import Callable, Iterator, Optional

import docker
import pytest

TEST_PROJECT_NAME = "argus-test-stack"
COMPOSE_FILE = Path(__file__).resolve().parent.parent / "docker" / "docker-compose.test.yml"

# Test collector configuration -- faster than production defaults, never
# used outside this test package. See argus.domain.health.HealthRules /
# argus.collector.loop.CollectorConfig for the production defaults these
# deliberately do not touch.
TEST_POLL_INTERVAL = 2.0
TEST_UNKNOWN_AFTER = 8
TEST_RESTART_LOOP_WINDOW = 30
TEST_RESTART_LOOP_THRESHOLD = 3


# --------------------------------------------------------------------------
# Safety guard -- refuses to mutate anything outside argus-test-stack
# --------------------------------------------------------------------------


class UnsafeMutationError(RuntimeError):
    """Raised when a test tried to stop/start/restart a container that is
    not part of the disposable argus-test-stack project."""


def assert_safe_to_mutate(container_attrs: dict) -> None:
    """Raises `UnsafeMutationError` unless `container_attrs` (a real or
    fixture-shaped `container.attrs` dict) carries
    `com.docker.compose.project == TEST_PROJECT_NAME` *exactly*. Never a
    fuzzy/substring/name-based match -- a container with no compose
    labels at all, or belonging to any other project, is rejected.
    """

    labels = ((container_attrs.get("Config") or {}).get("Labels")) or {}
    project = labels.get("com.docker.compose.project")
    if project != TEST_PROJECT_NAME:
        raise UnsafeMutationError(
            f"refusing to mutate container: com.docker.compose.project={project!r}, "
            f"expected exactly {TEST_PROJECT_NAME!r}"
        )


def safe_stop(sdk_client, container_id: str) -> None:
    container = sdk_client.containers.get(container_id)
    assert_safe_to_mutate(container.attrs)
    container.stop()


def safe_start(sdk_client, container_id: str) -> None:
    container = sdk_client.containers.get(container_id)
    assert_safe_to_mutate(container.attrs)
    container.start()


def safe_restart(sdk_client, container_id: str) -> None:
    container = sdk_client.containers.get(container_id)
    assert_safe_to_mutate(container.attrs)
    container.restart()


# --------------------------------------------------------------------------
# Bounded wait
# --------------------------------------------------------------------------


def wait_until(
    predicate: Callable[[], bool],
    *,
    timeout: float,
    interval: float = 0.5,
    description: str = "condition",
    on_timeout: Optional[Callable[[], str]] = None,
) -> None:
    """Poll `predicate` until it returns True, or raise a clear
    `AssertionError` after `timeout` seconds. Never an unbounded loop.
    `on_timeout`, if given, is called on failure to attach diagnostic
    context (e.g. the last observed status) to the error message.
    """

    deadline = time.monotonic() + timeout
    last_exception: Optional[BaseException] = None
    while time.monotonic() < deadline:
        try:
            if predicate():
                return
        except Exception as exc:  # noqa: BLE001 -- a flaky predicate shouldn't abort the wait
            last_exception = exc
        time.sleep(interval)

    detail = f" (last error while evaluating predicate: {last_exception})" if last_exception else ""
    context = f" -- {on_timeout()}" if on_timeout is not None else ""
    raise AssertionError(f"timed out after {timeout}s waiting for: {description}{detail}{context}")


# --------------------------------------------------------------------------
# docker compose lifecycle (subprocess -- the real docker compose v2 CLI)
# --------------------------------------------------------------------------


def _compose(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["docker", "compose", "-f", str(COMPOSE_FILE), *args],
        capture_output=True, text=True, timeout=120,
    )


def compose_up(*services: str) -> None:
    result = _compose("up", "-d", "--no-recreate", *services)
    assert result.returncode == 0, f"docker compose up failed: {result.stderr}"


def compose_stop(*services: str) -> None:
    result = _compose("stop", *services)
    assert result.returncode == 0, f"docker compose stop failed: {result.stderr}"


def compose_rm(*services: str) -> None:
    result = _compose("rm", "-f", *services)
    assert result.returncode == 0, f"docker compose rm failed: {result.stderr}"


def compose_down() -> None:
    result = _compose("down", "--volumes", "--remove-orphans", "--timeout", "5")
    assert result.returncode == 0, f"docker compose down failed: {result.stderr}"


def compose_container_id(service: str) -> str:
    result = _compose("ps", "-q", service)
    container_id = result.stdout.strip()
    assert container_id, f"no running container found for service {service!r}: {result.stderr}"
    return container_id


# --------------------------------------------------------------------------
# Real host preservation -- read-only snapshot of everything NOT ours
# --------------------------------------------------------------------------


def snapshot_non_test_containers(sdk_client) -> dict[str, str]:
    """id -> status, for every container whose compose project is not
    TEST_PROJECT_NAME (including containers with no compose project at
    all). Read-only. Used to prove the test run never touched anything
    real."""

    snapshot = {}
    for container in sdk_client.containers.list(all=True):
        labels = (container.attrs.get("Config") or {}).get("Labels") or {}
        if labels.get("com.docker.compose.project") != TEST_PROJECT_NAME:
            snapshot[container.id] = container.status
    return snapshot


# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------


@pytest.fixture(scope="session")
def raw_docker():
    """The real docker-py SDK client -- used only by this test package to
    drive the disposable stack (start/stop/restart) and to read the real
    host's container list for the before/after safety comparison. Never
    imported by Argus's own production code."""

    client = docker.from_env()
    yield client
    client.close()


@pytest.fixture(scope="session")
def host_preservation_check(raw_docker):
    """Captures the real host's non-test container state before anything
    in this session runs, and asserts it is byte-for-byte unchanged after
    the whole session finishes -- the hard safety backstop for this
    entire milestone."""

    before = snapshot_non_test_containers(raw_docker)
    yield
    after = snapshot_non_test_containers(raw_docker)
    assert after == before, (
        "a non-test container's id or status changed during the integration run -- "
        f"before={before!r} after={after!r}"
    )


@pytest.fixture(scope="module")
def stack(host_preservation_check, raw_docker):
    """Starts the disposable stack's always-on services (everything
    except intentional-failure-service -- see the compose file's own
    comment on why that one is scenario-scoped instead), waits for them
    to report real Docker health, and tears the whole project down
    (containers, network, the anonymous postgres volume) afterward --
    regardless of test outcome.
    """

    compose_up("healthy-api", "redis", "postgres")

    def all_healthy() -> bool:
        for service in ("healthy-api", "redis", "postgres"):
            container_id = compose_container_id(service)
            container = raw_docker.containers.get(container_id)
            health = (container.attrs.get("State") or {}).get("Health", {}).get("Status")
            if health != "healthy":
                return False
        return True

    wait_until(all_healthy, timeout=60, interval=1, description="all baseline test services healthy")

    try:
        yield
    finally:
        compose_down()
        _assert_no_test_resources_remain(raw_docker)


def _assert_no_test_resources_remain(raw_docker) -> None:
    leftover_containers = [
        c for c in raw_docker.containers.list(all=True)
        if ((c.attrs.get("Config") or {}).get("Labels") or {}).get("com.docker.compose.project")
        == TEST_PROJECT_NAME
    ]
    assert not leftover_containers, f"leftover test containers after cleanup: {leftover_containers}"

    leftover_networks = [n for n in raw_docker.networks.list() if TEST_PROJECT_NAME in n.name]
    assert not leftover_networks, f"leftover test networks after cleanup: {leftover_networks}"

    leftover_volumes = [v for v in raw_docker.volumes.list() if TEST_PROJECT_NAME in v.name]
    assert not leftover_volumes, f"leftover test volumes after cleanup: {leftover_volumes}"


@pytest.fixture
def failure_service(stack):
    """Starts `intentional-failure-service` (see the compose file's own
    comment on why it isn't part of the always-on `stack` fixture)
    only for the one scenario that needs a real crash loop, and stops
    and removes it again immediately after -- so it never affects any
    other scenario's "recovers to fully HEALTHY" assertion. Depends on
    `stack` so it's guaranteed to run inside an already-started,
    already-cleaned-up-afterward project.
    """

    compose_up("intentional-failure-service")
    try:
        container_id = compose_container_id("intentional-failure-service")
        yield container_id
    finally:
        compose_stop("intentional-failure-service")
        compose_rm("intentional-failure-service")


@pytest.fixture
def argus_db(tmp_path):
    """A fresh, temporary Argus SQLite database per test -- never the
    real ./data/argus.db. Removed (including -wal/-shm) on teardown."""

    from argus.store.database import open_database
    from argus.store.repository import Repository

    db_path = tmp_path / "argus-integration.db"
    connection = open_database(db_path)
    repository = Repository(connection)
    try:
        yield db_path, connection, repository
    finally:
        connection.close()
        for suffix in ("", "-wal", "-shm"):
            candidate = Path(str(db_path) + suffix)
            if candidate.exists():
                candidate.unlink()
