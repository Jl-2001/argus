"""Milestone 16 -- `argus.agent.client.post_snapshot`: never raises,
always attaches the bearer token as a header (never in the body/URL),
and classifies every outcome."""

from __future__ import annotations

from datetime import datetime, timezone

import httpx
import pytest

from argus.agent.client import post_snapshot
from argus.agent.protocol import PROTOCOL_VERSION, AgentSnapshot

T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _snapshot() -> AgentSnapshot:
    return AgentSnapshot(
        protocol_version=PROTOCOL_VERSION, agent_id="agent-1", host_key="dell", generated_at=T0,
        agent_version="0.1.0", applications=(), observations=(), evidence_candidates=(),
    )


class TestPostSnapshot:
    def test_success_returns_success_outcome(self, monkeypatch):
        captured = {}

        def fake_post(url, *, json, headers, timeout):
            captured["url"] = url
            captured["headers"] = headers
            captured["json"] = json
            return httpx.Response(200, json={"status": "accepted"}, request=httpx.Request("POST", url))

        monkeypatch.setattr(httpx, "post", fake_post)
        outcome = post_snapshot(control_plane_url="https://mac.example", agent_token="secret-token", snapshot=_snapshot())

        assert outcome.success is True
        assert outcome.status_code == 200
        assert captured["url"] == "https://mac.example/api/v1/agents/ingest"
        assert captured["headers"]["Authorization"] == "Bearer secret-token"

    def test_token_never_appears_in_the_url_or_body(self, monkeypatch):
        captured = {}

        def fake_post(url, *, json, headers, timeout):
            captured["url"] = url
            captured["json"] = json
            return httpx.Response(200, json={}, request=httpx.Request("POST", url))

        monkeypatch.setattr(httpx, "post", fake_post)
        post_snapshot(control_plane_url="https://mac.example", agent_token="super-secret-value", snapshot=_snapshot())

        assert "super-secret-value" not in captured["url"]
        assert "super-secret-value" not in str(captured["json"])

    def test_non_2xx_response_returns_failure_outcome_not_an_exception(self, monkeypatch):
        def fake_post(url, *, json, headers, timeout):
            return httpx.Response(401, json={"error": {"code": "invalid_agent_credentials"}}, request=httpx.Request("POST", url))

        monkeypatch.setattr(httpx, "post", fake_post)
        outcome = post_snapshot(control_plane_url="https://mac.example", agent_token="bad", snapshot=_snapshot())

        assert outcome.success is False
        assert outcome.status_code == 401
        assert outcome.error is not None

    def test_connection_error_never_raises(self, monkeypatch):
        def fake_post(url, *, json, headers, timeout):
            raise httpx.ConnectError("connection refused")

        monkeypatch.setattr(httpx, "post", fake_post)
        outcome = post_snapshot(control_plane_url="https://mac.example", agent_token="t", snapshot=_snapshot())

        assert outcome.success is False
        assert outcome.status_code is None
        assert outcome.error is not None

    def test_timeout_never_raises(self, monkeypatch):
        def fake_post(url, *, json, headers, timeout):
            raise httpx.TimeoutException("timed out")

        monkeypatch.setattr(httpx, "post", fake_post)
        outcome = post_snapshot(control_plane_url="https://mac.example", agent_token="t", snapshot=_snapshot())

        assert outcome.success is False
        assert outcome.error is not None

    def test_error_outcome_never_leaks_the_token(self, monkeypatch):
        def fake_post(url, *, json, headers, timeout):
            raise httpx.ConnectError("connection refused")

        monkeypatch.setattr(httpx, "post", fake_post)
        outcome = post_snapshot(control_plane_url="https://mac.example", agent_token="super-secret-value", snapshot=_snapshot())

        assert "super-secret-value" not in (outcome.error or "")

    def test_timeout_is_bounded_and_passed_through(self, monkeypatch):
        captured = {}

        def fake_post(url, *, json, headers, timeout):
            captured["timeout"] = timeout
            return httpx.Response(200, json={}, request=httpx.Request("POST", url))

        monkeypatch.setattr(httpx, "post", fake_post)
        post_snapshot(
            control_plane_url="https://mac.example", agent_token="t", snapshot=_snapshot(), timeout_seconds=5.0
        )
        assert captured["timeout"] == 5.0
