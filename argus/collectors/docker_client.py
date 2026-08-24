"""A deliberately narrow, read-only adapter over docker-py.

``DockerClient`` exposes exactly two operations — list, and inspect one
by id. There is no generic escape hatch (``raw_client()``,
``execute()``, a public attribute holding the underlying SDK client,
...) that would let a caller reach past this surface and call
something mutating. See ``tests/unit/test_docker_client.py`` for the
automated guard that enforces this.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Optional

import docker
import docker.errors

__all__ = [
    "ContainerAttrs",
    "DockerClient",
    "DockerUnavailableError",
    "ContainerVanishedError",
]


class DockerUnavailableError(RuntimeError):
    """The Docker daemon could not be reached at all.

    Milestone 3 does not retry — that belongs to Milestone 5's
    collector loop. This is just a clear, typed failure instead of a
    raw docker-py/urllib3 stack trace.
    """


class ContainerVanishedError(RuntimeError):
    """A container existed at list() time but was gone by inspect time."""

    def __init__(self, container_id: str) -> None:
        super().__init__(f"container vanished before it could be inspected: {container_id}")
        self.container_id = container_id


@dataclass(frozen=True, slots=True)
class ContainerAttrs:
    """The raw inspect payload for one container, exactly as docker-py
    returns it (``container.attrs``).

    Deliberately unparsed and unvalidated — ``DockerClient``'s job ends
    at handing back Docker's own data. Translating this into Argus
    domain objects is ``docker_collector.py``'s job.
    """

    id: str
    attrs: dict[str, Any]


class DockerClient:
    """Read-only discovery operations against the local Docker daemon.

    Connects via ``docker.from_env()``, which respects ``DOCKER_HOST``
    and the rest of the standard Docker CLI environment, rather than
    hardcoding ``/var/run/docker.sock`` — Argus v0.1 still only ever
    talks to a single, local daemon, but *which* local daemon (rootless
    Docker, Colima, a non-default socket path, ...) is exactly what
    ``from_env()`` already knows how to resolve.
    """

    def __init__(self, *, client: Any = None) -> None:
        # `client` exists only so tests can inject a mock/fake SDK client.
        # It is never exposed back out of this object afterward.
        if client is not None:
            self._client = client
            return
        try:
            self._client = docker.from_env()
        except (docker.errors.DockerException, OSError) as exc:
            raise DockerUnavailableError(f"could not connect to Docker: {exc}") from exc

    def list_containers(self, *, all: bool = True) -> list[str]:
        """Return the ids of currently known containers.

        ``all=True`` (the default) includes stopped containers, so
        that discovery covers them too. This calls Docker's *list*
        endpoint only, which returns an abbreviated summary shape —
        materially different from a full inspect payload (for
        instance, its ``State`` is a bare string, not the
        ``{Status, Health, ...}`` object a real inspect returns). Only
        the id is trustworthy for this method's purpose — "which
        containers exist right now" — everything discovery actually
        needs about a container comes from a subsequent
        ``inspect_container()`` call.
        """

        try:
            containers = self._client.containers.list(all=all)
        except (docker.errors.DockerException, OSError) as exc:
            raise DockerUnavailableError(f"could not list containers: {exc}") from exc
        return [container.id for container in containers]

    def inspect_container(self, container_id: str) -> ContainerAttrs:
        """Fetch a fresh, full inspect payload for one container by id."""

        try:
            container = self._client.containers.get(container_id)
        except docker.errors.NotFound as exc:
            raise ContainerVanishedError(container_id) from exc
        except (docker.errors.DockerException, OSError) as exc:
            raise DockerUnavailableError(
                f"could not inspect container {container_id}: {exc}"
            ) from exc
        return ContainerAttrs(id=container.id, attrs=container.attrs)

    def get_logs(
        self, container_id: str, *, since: Optional[datetime] = None, tail: int = 500
    ) -> list[str]:
        """Fetch recent, bounded raw log lines for one container.

        Read-only, same as every other method here: this calls
        docker-py's ``Container.logs()`` -- the SDK's log-*retrieval*
        endpoint -- never ``exec_run()`` (``docker exec``), and never
        shells out to the ``docker`` CLI. It cannot mutate the
        container it reads from.

        Bounded on two axes: ``tail`` caps the number of lines returned
        (applied by the Docker daemon itself, not client-side), and
        ``since`` (when given) excludes anything at or before that
        timestamp from *Docker's* own filtering. Docker/docker-py may
        apply ``since`` with only whole-second precision -- callers that
        need an exact, sub-second dedup boundary (see
        ``argus.evidence.collector``'s log cursor) must still re-check
        each returned line's own parsed timestamp themselves; this
        method does not guarantee ``since`` is applied at
        microsecond/nanosecond precision, only that it narrows what the
        daemon returns.

        Each returned line is prefixed with Docker's own RFC3339,
        nanosecond-precision timestamp (``timestamps=True``) followed by
        a space -- parsing that prefix is ``argus.evidence.collector``'s
        job, not this module's; ``DockerClient`` only ever hands back
        Docker's own bytes, decoded to text.
        """

        try:
            container = self._client.containers.get(container_id)
        except docker.errors.NotFound as exc:
            raise ContainerVanishedError(container_id) from exc
        except (docker.errors.DockerException, OSError) as exc:
            raise DockerUnavailableError(f"could not fetch logs for {container_id}: {exc}") from exc

        kwargs: dict[str, Any] = {"stdout": True, "stderr": True, "timestamps": True, "tail": tail}
        if since is not None:
            kwargs["since"] = since

        try:
            raw = container.logs(**kwargs)
        except (docker.errors.DockerException, OSError) as exc:
            raise DockerUnavailableError(f"could not fetch logs for {container_id}: {exc}") from exc

        text = raw.decode("utf-8", errors="replace") if isinstance(raw, (bytes, bytearray)) else str(raw)
        return text.splitlines()
