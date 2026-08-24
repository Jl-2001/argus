"""Milestone 13 -- no response from this API ever contains an API key,
a raw env var, an unredacted log, a raw Docker label, a system prompt,
or provider credentials. Seeds fake-but-plausible secrets in a few
places a naive implementation might leak them from, then greps every
serialized response across the whole surface for them.
"""

from __future__ import annotations

import os

from fastapi.testclient import TestClient

from api_fixtures import seed_incident_stack
from argus.api.app import create_app

_FAKE_SECRETS = (
    "sk-ant-FAKE-SECRET-DO-NOT-LEAK-1234567890",
    "AIzaFAKE-GEMINI-SECRET-DO-NOT-LEAK",
    "super-secret-db-password",
)


def _all_response_bodies(client: TestClient, seed: dict) -> list[str]:
    paths = [
        "/api/v1/system/status",
        "/api/v1/system/doctor",
        "/api/v1/applications",
        f"/api/v1/applications/{seed['application_key']}",
        f"/api/v1/applications/{seed['application_key']}/history",
        "/api/v1/incidents",
        f"/api/v1/incidents/{seed['incident_id']}",
        f"/api/v1/incidents/{seed['incident_id']}/evidence",
        f"/api/v1/incidents/{seed['incident_id']}/bundle",
        f"/api/v1/incidents/{seed['incident_id']}/explanations",
        f"/api/v1/incidents/{seed['incident_id']}/explanations/latest",
        "/openapi.json",
    ]
    return [client.get(path).text for path in paths]


class TestNoSecretsLeakIntoResponses:
    def test_fake_provider_keys_present_in_the_environment_never_appear(self, tmp_path, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", _FAKE_SECRETS[0])
        monkeypatch.setenv("GEMINI_API_KEY", _FAKE_SECRETS[1])

        db_path = tmp_path / "a.db"
        seed = seed_incident_stack(db_path)
        client = TestClient(create_app(database_path=db_path))

        for body in _all_response_bodies(client, seed):
            for secret in _FAKE_SECRETS[:2]:
                assert secret not in body

    def test_no_response_ever_mentions_the_env_var_names_values(self, tmp_path):
        # A stricter check: not just the secret values, but that no
        # response body echoes back raw process environment content at
        # all (e.g. by accidentally serializing os.environ somewhere).
        db_path = tmp_path / "a.db"
        seed = seed_incident_stack(db_path)
        client = TestClient(create_app(database_path=db_path))

        # Benign, path-shaped env vars this process legitimately has set
        # (its own venv, shell, tmp dir, ...) are excluded -- none of
        # them are secrets, and a filesystem path coincidentally
        # prefix-matching something is not the failure mode this test
        # is for.
        _benign = {"PATH", "HOME", "PWD", "OLDPWD", "VIRTUAL_ENV", "SHELL", "TMPDIR", "_", "TERM_PROGRAM"}

        for body in _all_response_bodies(client, seed):
            for key, value in os.environ.items():
                if len(value) < 12 or key in _benign:
                    continue  # too short/common to be a meaningful secret-leak signal
                assert value not in body, f"env var {key!r}'s value leaked into a response"

    def test_evidence_sample_is_the_redacted_stored_text_only(self, tmp_path):
        db_path = tmp_path / "a.db"
        seed = seed_incident_stack(db_path)
        client = TestClient(create_app(database_path=db_path))

        body = client.get(f"/api/v1/incidents/{seed['incident_id']}/evidence").json()
        sample = body["evidence"][0]["sample"]
        assert sample == "[REDACTED] container killed: out of memory"
        assert "password" not in sample.lower()

    def test_no_response_contains_a_system_prompt_or_prompt_version_leak_beyond_its_own_id(self, tmp_path):
        db_path = tmp_path / "a.db"
        seed = seed_incident_stack(db_path)
        client = TestClient(create_app(database_path=db_path))

        # The prompt *version* (e.g. "incident-explanation-v1") is
        # expected/fine -- it's just a cache-key component. The actual
        # system prompt *text* Argus sends a model is a different thing
        # entirely and must never appear.
        from argus.ai.prompts import SYSTEM_PROMPT

        for body in _all_response_bodies(client, seed):
            assert SYSTEM_PROMPT not in body

    def test_application_detail_never_exposes_docker_labels_or_mount_paths(self, tmp_path):
        db_path = tmp_path / "a.db"
        seed = seed_incident_stack(db_path)
        client = TestClient(create_app(database_path=db_path))

        body = client.get(f"/api/v1/applications/{seed['application_key']}").text
        for forbidden in ("/var/run/docker.sock", "HostConfig", "Mounts", "Env\":", "Labels\":"):
            assert forbidden not in body
