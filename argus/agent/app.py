"""``argus-agent`` -- the console entry point (see ``[project.scripts]``
in ``pyproject.toml``). Pure composition and the poll/backoff loop, the
same split ``argus.run_collector``/``argus.collector.loop`` already
draw between "wire things up" and "one tick, then schedule the next".

Lifecycle, exactly as the milestone spec names it: collect -> sanitize
-> POST -> wait -> repeat. "Sanitize" is not a separate step called out
below -- it already happened by construction: ``AgentCollector`` only
ever produces already-redacted, already-bounded
``argus.agent.protocol`` types (see that module and
``argus.evidence.redaction``), so there is no raw/unsanitized
intermediate value this loop could accidentally send.
"""

from __future__ import annotations

import logging
import sys
import time
from datetime import datetime, timezone
from typing import Callable, Optional

from argus.agent.client import post_snapshot
from argus.agent.config import AgentConfig, AgentConfigError, load_agent_config
from argus.agent.protocol import PROTOCOL_VERSION, AgentSnapshot
from argus.agent.snapshot import AgentCollector
from argus.collectors.docker_client import DockerClient, DockerUnavailableError

__all__ = ["AGENT_VERSION", "compute_agent_backoff", "run_agent_forever", "main"]

logger = logging.getLogger("argus.agent")

#: Reported to the control plane on every ingest (`hosts.agent_version`)
#: -- bump alongside `pyproject.toml`'s own `[project] version`.
AGENT_VERSION = "0.1.0"

_BACKOFF_MAX_SECONDS = 240.0


def compute_agent_backoff(consecutive_failures: int, poll_interval_seconds: float) -> float:
    """Exponential backoff, capped, deterministic -- no jitter. Mirrors
    ``argus.collector.loop.compute_backoff``'s own shape exactly, but is
    its own small function rather than an import from it: this agent
    package must never depend on ``argus.collector`` (the control
    plane's own scheduling module, which transitively imports the
    central incident engine) -- see ``argus.agent``'s own docstring on
    its import boundary."""

    if consecutive_failures <= 0:
        return 0.0
    return min(poll_interval_seconds * (2 ** (consecutive_failures - 1)), _BACKOFF_MAX_SECONDS)


def run_agent_forever(
    config: AgentConfig,
    collector: AgentCollector,
    *,
    clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    sleep: Callable[[float], None] = time.sleep,
    stop: Optional[Callable[[], bool]] = None,
    stop_after_n_polls: Optional[int] = None,
) -> None:
    """Collect -> POST -> wait, forever (or until ``stop``/
    ``stop_after_n_polls``, both test-only seams -- production always
    calls this with neither).

    On a failed POST (control plane unreachable, rejected the snapshot,
    etc.), this deliberately does *not* keep the failed
    ``AgentSnapshot`` around to retry byte-for-byte -- the *next* poll
    collects fresh current state and sends that instead. This is a
    deliberate reading of the milestone's own "retries latest snapshot":
    holding a stale, increasingly-clock-skewed payload across a long
    outage would only ever get rejected by the control plane's own
    clock-skew check once connectivity returns (see
    ``argus.agent.protocol.MAX_CLOCK_SKEW_SECONDS``) -- always retrying
    with *current* state is what actually keeps a reconnecting agent's
    very next successful send meaningful. This is also what keeps this
    loop's own memory bounded to "one snapshot at a time", never a
    growing backlog (see the milestone's own "keeps no unbounded queue
    in memory").
    """

    consecutive_failures = 0
    polls = 0

    while True:
        if stop is not None and stop():
            break
        if stop_after_n_polls is not None and polls >= stop_after_n_polls:
            break

        now = clock()
        try:
            result = collector.collect_snapshot(now=now)
            snapshot = AgentSnapshot(
                protocol_version=PROTOCOL_VERSION,
                agent_id=config.agent_id,
                host_key=config.host_key,
                generated_at=now,
                agent_version=AGENT_VERSION,
                applications=result.applications,
                observations=result.observations,
                evidence_candidates=result.evidence_candidates,
            )
            outcome = post_snapshot(
                control_plane_url=config.control_plane_url, agent_token=config.agent_token, snapshot=snapshot
            )
        except DockerUnavailableError as exc:
            # Docker itself being briefly unreachable is exactly the
            # kind of transient failure this loop must survive, not
            # crash on -- same treatment `CollectorLoop` gives it.
            outcome = None
            logger.warning("agent poll skipped: Docker unavailable: %s", exc)
        except Exception:  # noqa: BLE001 -- deliberate outer safety net around one poll, mirroring CollectorLoop.run_forever
            outcome = None
            logger.exception("agent poll failed with an unexpected error")

        polls += 1

        if outcome is not None and outcome.success:
            consecutive_failures = 0
            wait = config.poll_interval_seconds
        else:
            if outcome is not None:
                logger.warning("agent snapshot POST failed: %s", outcome.error)
            consecutive_failures += 1
            wait = compute_agent_backoff(consecutive_failures, config.poll_interval_seconds)

        sleep(wait)


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

    try:
        config = load_agent_config()
    except AgentConfigError as exc:
        logger.error("cannot start argus-agent: %s", exc)
        return 1

    try:
        client = DockerClient()
    except DockerUnavailableError as exc:
        logger.error("cannot start argus-agent: Docker unavailable: %s", exc)
        return 1

    collector = AgentCollector(client=client)

    logger.info(
        "argus-agent starting (host_key=%s, control_plane=%s, poll_interval=%ss)",
        config.host_key, config.control_plane_url, config.poll_interval_seconds,
    )
    try:
        run_agent_forever(config, collector)
    except KeyboardInterrupt:
        logger.info("argus-agent stopping (interrupted)")

    return 0


def run() -> None:
    """Console entry point (``argus-agent`` -- see ``[project.scripts]``
    in ``pyproject.toml``)."""

    sys.exit(main())
