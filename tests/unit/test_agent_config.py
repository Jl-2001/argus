"""Milestone 16 -- `argus.agent.config`."""

from __future__ import annotations

import pytest

from argus.agent.config import AgentConfigError, DEFAULT_POLL_INTERVAL_SECONDS, load_agent_config

_VALID_ENV = {
    "ARGUS_CONTROL_PLANE_URL": "https://mac.example.internal",
    "ARGUS_AGENT_ID": "agent-abc",
    "ARGUS_AGENT_TOKEN": "sekret-token-value",
    "ARGUS_HOST_KEY": "dell-latitude-5400",
}


class TestLoadAgentConfig:
    def test_loads_a_complete_valid_config(self):
        config = load_agent_config(dict(_VALID_ENV))
        assert config.control_plane_url == "https://mac.example.internal"
        assert config.agent_id == "agent-abc"
        assert config.agent_token == "sekret-token-value"
        assert config.host_key == "dell-latitude-5400"
        assert config.poll_interval_seconds == DEFAULT_POLL_INTERVAL_SECONDS

    def test_host_name_defaults_to_host_key_when_unset(self):
        config = load_agent_config(dict(_VALID_ENV))
        assert config.host_name == "dell-latitude-5400"

    def test_host_name_uses_argus_host_name_when_set(self):
        env = {**_VALID_ENV, "ARGUS_HOST_NAME": "Ubuntu Dell"}
        config = load_agent_config(env)
        assert config.host_name == "Ubuntu Dell"

    def test_control_plane_url_trailing_slash_is_stripped(self):
        env = {**_VALID_ENV, "ARGUS_CONTROL_PLANE_URL": "https://mac.example.internal/"}
        config = load_agent_config(env)
        assert config.control_plane_url == "https://mac.example.internal"

    def test_custom_poll_interval_is_respected(self):
        env = {**_VALID_ENV, "ARGUS_AGENT_POLL_INTERVAL": "30"}
        config = load_agent_config(env)
        assert config.poll_interval_seconds == 30.0

    @pytest.mark.parametrize("missing", ["ARGUS_CONTROL_PLANE_URL", "ARGUS_AGENT_ID", "ARGUS_AGENT_TOKEN", "ARGUS_HOST_KEY"])
    def test_missing_required_variable_raises(self, missing):
        env = dict(_VALID_ENV)
        del env[missing]
        with pytest.raises(AgentConfigError):
            load_agent_config(env)

    def test_blank_required_variable_raises(self):
        env = {**_VALID_ENV, "ARGUS_AGENT_TOKEN": "   "}
        with pytest.raises(AgentConfigError):
            load_agent_config(env)

    def test_non_numeric_poll_interval_raises(self):
        env = {**_VALID_ENV, "ARGUS_AGENT_POLL_INTERVAL": "not-a-number"}
        with pytest.raises(AgentConfigError):
            load_agent_config(env)

    def test_zero_or_negative_poll_interval_raises(self):
        env = {**_VALID_ENV, "ARGUS_AGENT_POLL_INTERVAL": "0"}
        with pytest.raises(AgentConfigError):
            load_agent_config(env)

    def test_plaintext_http_to_a_non_local_host_is_rejected(self):
        env = {**_VALID_ENV, "ARGUS_CONTROL_PLANE_URL": "http://mac.example.internal"}
        with pytest.raises(AgentConfigError):
            load_agent_config(env)

    def test_plaintext_http_to_127_0_0_1_is_allowed_for_local_development(self):
        env = {**_VALID_ENV, "ARGUS_CONTROL_PLANE_URL": "http://127.0.0.1:8088"}
        config = load_agent_config(env)
        assert config.control_plane_url == "http://127.0.0.1:8088"

    def test_plaintext_http_to_localhost_is_allowed_for_local_development(self):
        env = {**_VALID_ENV, "ARGUS_CONTROL_PLANE_URL": "http://localhost:8088"}
        config = load_agent_config(env)
        assert config.control_plane_url == "http://localhost:8088"

    def test_error_message_never_includes_the_token_value(self):
        env = dict(_VALID_ENV)
        del env["ARGUS_HOST_KEY"]
        with pytest.raises(AgentConfigError) as exc_info:
            load_agent_config(env)
        assert _VALID_ENV["ARGUS_AGENT_TOKEN"] not in str(exc_info.value)
