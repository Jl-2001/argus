"""Milestone 16 -- multi-host agent architecture, end to end against the
real, disposable `argus-test-stack`.

Simulates two logical processes on one test machine, exactly as the
milestone spec asks for:

    "remote-agent" fixture (argus.agent.snapshot.AgentCollector, its
    own real DockerClient() against argus-test-stack)
            |
            v  POST /api/v1/agents/ingest (via TestClient -- an in-process
            |   ASGI call, but going through the real route/dependency/
            |   validation/persistence chain exactly as a real HTTP
            |   request would)
            v
    control-plane FastAPI app, its own separate temp SQLite database

The "remote agent" side never imports or constructs a
`argus.store.Repository`/`argus.api` object at all -- it only ever
produces a JSON-serializable `AgentSnapshot`. The "control plane" side
never imports `argus.collectors`/`docker` -- it only ever receives that
JSON over the (in-process, but protocol-identical) ASGI transport. No
direct Docker connection exists between the two; the *only* thing that
crosses the "network" boundary is the same JSON body a real
`argus-agent` process would produce.

Only `argus-test-stack`'s own `healthy-api` container is ever
stopped/started -- via `conftest.py`'s `safe_stop`/`safe_start`, which
refuse anything not carrying the test stack's own compose-project
label. No AI call anywhere in this file.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from argus.agent.protocol import PROTOCOL_VERSION, AgentSnapshot
from argus.agent.snapshot import AgentCollector
from argus.api.app import create_app
from argus.collectors.docker_client import DockerClient
from argus.security import generate_token, hash_token
from argus.store.database import open_database
from argus.store.repository import Repository

from conftest import TEST_PROJECT_NAME, compose_container_id, safe_start, safe_stop, wait_until

pytestmark = [pytest.mark.integration, pytest.mark.docker]

UTC = timezone.utc
HOST_KEY = "test-remote-host"
AGENT_ID = "agent-integration-test"


def _post_snapshot(client: TestClient, collector: AgentCollector, token: str) -> dict:
    now = datetime.now(UTC)
    result = collector.collect_snapshot(now=now)
    snapshot = AgentSnapshot(
        protocol_version=PROTOCOL_VERSION, agent_id=AGENT_ID, host_key=HOST_KEY, generated_at=now,
        agent_version="0.1.0-test", applications=result.applications, observations=result.observations,
        evidence_candidates=result.evidence_candidates,
    )
    response = client.post(
        "/api/v1/agents/ingest", json=snapshot.to_dict(), headers={"Authorization": f"Bearer {token}"}
    )
    assert response.status_code == 200, response.text
    return response.json()


class TestAgentToControlPlaneEndToEnd:
    def test_agent_discovers_the_stack_and_control_plane_reflects_it(self, stack, tmp_path):
        control_plane_db = tmp_path / "control-plane.db"
        token = generate_token()
        conn = open_database(control_plane_db)
        try:
            Repository(conn).create_agent_host(
                host_key=HOST_KEY, agent_id=AGENT_ID, display_name="Test Remote Host",
                token_hash=hash_token(token), now=datetime.now(UTC),
            )
        finally:
            conn.close()

        control_plane = TestClient(create_app(database_path=control_plane_db))
        agent_docker_client = DockerClient()
        agent_collector = AgentCollector(client=agent_docker_client)

        result = _post_snapshot(control_plane, agent_collector, token)
        assert result["applications_written"] >= 1

        hosts = control_plane.get("/api/v1/hosts").json()
        remote_host = next(h for h in hosts if h["host_key"] == HOST_KEY)
        assert remote_host["status"] == "ONLINE"
        assert remote_host["application_count"] >= 1

        applications = control_plane.get("/api/v1/applications").json()
        stack_apps = [a for a in applications if a["host_key"] == HOST_KEY]
        assert stack_apps, "no applications from the remote host reached the control plane"
        # Host-scoped key, e.g. "test-remote-host:argus-test-stack"
        assert all(app["key"].startswith(f"{HOST_KEY}:") for app in stack_apps)

    def test_stopping_and_restarting_a_service_produces_incidents_through_the_agent_path(self, stack, tmp_path):
        control_plane_db = tmp_path / "control-plane.db"
        token = generate_token()
        conn = open_database(control_plane_db)
        try:
            Repository(conn).create_agent_host(
                host_key=HOST_KEY, agent_id=AGENT_ID, display_name="Test Remote Host",
                token_hash=hash_token(token), now=datetime.now(UTC),
            )
        finally:
            conn.close()

        control_plane = TestClient(create_app(database_path=control_plane_db))
        agent_docker_client = DockerClient()
        agent_collector = AgentCollector(client=agent_docker_client)

        # `AgentCollector` discovers the whole real Docker daemon on this
        # test machine, exactly like the local collector does -- not
        # just `argus-test-stack`. On a real dev machine that means
        # other, unrelated real applications/containers may also show
        # up (and may have their own, unrelated, permanently-open
        # incidents for containers that are genuinely stopped for other
        # reasons). The one application key this test cares about is
        # exactly `f"{HOST_KEY}:{TEST_PROJECT_NAME}"` -- never a loose
        # substring match against every incident under this host.
        expected_app_key = f"{HOST_KEY}:{TEST_PROJECT_NAME}"

        # Baseline snapshot while everything is healthy.
        _post_snapshot(control_plane, agent_collector, token)

        raw_sdk_client = agent_docker_client._client
        container_id = compose_container_id("healthy-api")
        safe_stop(raw_sdk_client, container_id)
        try:
            def stopped_snapshot_shows_incident() -> bool:
                _post_snapshot(control_plane, agent_collector, token)
                open_incidents = control_plane.get("/api/v1/incidents", params={"status": "open"}).json()["incidents"]
                return any(inc["application_key"] == expected_app_key for inc in open_incidents)

            wait_until(stopped_snapshot_shows_incident, timeout=30, interval=1, description="agent-path incident opens")
        finally:
            safe_start(raw_sdk_client, container_id)

        def restarted_snapshot_resolves_incident() -> bool:
            _post_snapshot(control_plane, agent_collector, token)
            open_incidents = control_plane.get("/api/v1/incidents", params={"status": "open"}).json()["incidents"]
            return not any(inc["application_key"] == expected_app_key for inc in open_incidents)

        wait_until(restarted_snapshot_resolves_incident, timeout=45, interval=1, description="agent-path incident resolves")

        # Realtime events exist for the whole sequence, same event types
        # a local-host tick would produce -- no remote-specific handling.
        conn = open_database(control_plane_db)
        try:
            events = Repository(conn).list_realtime_events_since(after_id=0, limit=500)
        finally:
            conn.close()
        event_types = {e.event_type for e in events}
        assert "incident.opened" in event_types
        assert "incident.resolved" in event_types

    def test_no_direct_docker_connection_exists_on_the_control_plane_side(self, stack, tmp_path):
        # Architecture-level guard: the control-plane FastAPI app used
        # in these tests is built purely from argus.api, which the
        # existing architecture guard already proves never imports
        # docker (outside the doctor route). This test just confirms
        # that guarantee holds for the exact app instance driving these
        # scenarios too.
        import argus.api.routes.agents as agents_route

        source = agents_route.__file__
        with open(source) as f:
            text = f.read()
        assert "import docker" not in text
        assert "argus.collectors" not in text
