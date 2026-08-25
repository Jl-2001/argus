"""HTTP client for one agent -> control-plane POST.

Deliberately thin: build the request, send it, classify the outcome.
Retry/backoff *scheduling* across polls is ``argus.agent.app``'s job
(the same split ``argus.collector.loop`` already draws between "one
tick" and "the scheduling loop around ticks").
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

import httpx

from argus.agent.protocol import AgentSnapshot

__all__ = ["IngestOutcome", "post_snapshot", "DEFAULT_TIMEOUT_SECONDS"]

logger = logging.getLogger(__name__)

#: Bounded, so a control plane that's up but unresponsive can never
#: block this process indefinitely (see the milestone's own "Use
#: bounded timeout and backoff").
DEFAULT_TIMEOUT_SECONDS = 10.0


@dataclass(frozen=True, slots=True)
class IngestOutcome:
    """What happened trying to send one snapshot. ``success`` covers
    both a fresh 2xx *and* a duplicate-replay 2xx (see
    ``argus.api.routes.agents``'s own idempotency handling) -- both mean
    "the control plane has this data, move on"; only ``status_code`` (if
    the caller cares to distinguish) tells them apart. Never carries the
    agent's own token -- only the response's status/body, which is the
    control plane's own output, not agent secret material."""

    success: bool
    status_code: Optional[int]
    error: Optional[str]


def post_snapshot(
    *, control_plane_url: str, agent_token: str, snapshot: AgentSnapshot, timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
) -> IngestOutcome:
    """POSTs one snapshot. Never raises -- every failure mode (DNS,
    connection refused, timeout, non-2xx response) is caught and turned
    into a plain ``IngestOutcome`` so ``argus.agent.app``'s loop never
    needs a bare ``except Exception`` of its own around this call.

    The token is attached exactly once, as the standard
    ``Authorization: Bearer`` header -- never in the URL (which could
    end up in a proxy/access log) and never inside the JSON body itself.
    """

    url = f"{control_plane_url}/api/v1/agents/ingest"
    headers = {
        "Authorization": f"Bearer {agent_token}",
        "Content-Type": "application/json",
    }

    try:
        response = httpx.post(url, json=snapshot.to_dict(), headers=headers, timeout=timeout_seconds)
    except httpx.HTTPError as exc:
        return IngestOutcome(success=False, status_code=None, error=f"{type(exc).__name__}: could not reach control plane")

    if 200 <= response.status_code < 300:
        return IngestOutcome(success=True, status_code=response.status_code, error=None)

    # The response body is the control plane's own, already-sanitized
    # error envelope (see argus.api.errors) -- safe to log a short
    # excerpt of; never contains the agent's own token (that's a
    # request header, never echoed into a response body).
    detail = response.text[:200] if response.text else ""
    return IngestOutcome(
        success=False, status_code=response.status_code, error=f"control plane rejected snapshot: {detail}"
    )
