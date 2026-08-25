"""``argus-agent`` process configuration -- read once, from environment
variables only (see the milestone's own "Token Storage" note: never a
committed file, never a CLI flag that would land in shell history next
to a secret).
"""

from __future__ import annotations

import os
from dataclasses import dataclass

__all__ = ["AgentConfig", "AgentConfigError", "load_agent_config", "DEFAULT_POLL_INTERVAL_SECONDS"]

#: Milestone 16's own "Poll Interval" requirement: "Default: 15 seconds
#: ... Do not create sub-second network churn."
DEFAULT_POLL_INTERVAL_SECONDS = 15.0

_REQUIRED_VARS = (
    "ARGUS_CONTROL_PLANE_URL",
    "ARGUS_AGENT_ID",
    "ARGUS_AGENT_TOKEN",
    "ARGUS_HOST_KEY",
)


class AgentConfigError(RuntimeError):
    """Required agent configuration is missing or malformed. Always
    raised before anything touches Docker or the network -- a
    misconfigured agent should fail immediately and loudly, not start
    polling with a blank/guessed identity."""


@dataclass(frozen=True, slots=True)
class AgentConfig:
    control_plane_url: str
    agent_id: str
    agent_token: str
    host_key: str
    host_name: str
    poll_interval_seconds: float


def load_agent_config(env: "dict[str, str] | None" = None) -> AgentConfig:
    """Reads and validates every ``ARGUS_AGENT_*``/``ARGUS_CONTROL_PLANE_URL``/
    ``ARGUS_HOST_*`` variable this process needs, from ``env`` (defaults
    to the real ``os.environ``) -- injectable purely for tests, never
    for production use.

    ``ARGUS_AGENT_TOKEN`` is read here and held only in this process's
    own memory for the rest of its life (attached to every outbound
    request by ``argus.agent.client`` as an ``Authorization: Bearer``
    header) -- never written to a log line, never echoed back, and
    never written to disk by this module. See the milestone's own
    "Token Storage" section: this is the one deliberate v1 mechanism
    ("read plaintext credential from ARGUS_AGENT_TOKEN"), not a
    substitute for real secret-management on whatever host runs this.
    """

    source = env if env is not None else os.environ

    missing = [name for name in _REQUIRED_VARS if not source.get(name, "").strip()]
    if missing:
        raise AgentConfigError(
            "missing required agent configuration: " + ", ".join(missing)
            + " (see argus.agent.config's own docstring for what each controls)"
        )

    raw_interval = source.get("ARGUS_AGENT_POLL_INTERVAL", "")
    if raw_interval.strip():
        try:
            poll_interval = float(raw_interval)
        except ValueError as exc:
            raise AgentConfigError(
                f"ARGUS_AGENT_POLL_INTERVAL must be a number of seconds, got {raw_interval!r}"
            ) from exc
        if poll_interval <= 0:
            raise AgentConfigError("ARGUS_AGENT_POLL_INTERVAL must be positive")
    else:
        poll_interval = DEFAULT_POLL_INTERVAL_SECONDS

    control_plane_url = source["ARGUS_CONTROL_PLANE_URL"].strip().rstrip("/")
    if not (control_plane_url.startswith("https://") or control_plane_url.startswith("http://127.0.0.1")
            or control_plane_url.startswith("http://localhost")):
        raise AgentConfigError(
            "ARGUS_CONTROL_PLANE_URL must be https://, or http://127.0.0.1/http://localhost for local "
            f"development only -- refusing to send an agent token in plaintext to {control_plane_url!r}. "
            "See the milestone's own 'TLS / Private Network' requirement."
        )

    return AgentConfig(
        control_plane_url=control_plane_url,
        agent_id=source["ARGUS_AGENT_ID"].strip(),
        agent_token=source["ARGUS_AGENT_TOKEN"],
        host_key=source["ARGUS_HOST_KEY"].strip(),
        host_name=source.get("ARGUS_HOST_NAME", "").strip() or source["ARGUS_HOST_KEY"].strip(),
        poll_interval_seconds=poll_interval,
    )
