"""POST /api/v1/agents/ingest -- machine-to-machine snapshot ingestion.

This is Milestone 16's one deliberate, explicitly-scoped exception to
the rest of ``/api/v1`` being GET-only (see this package's own
architecture-guard test for how that exception is enforced, not just
asserted in a comment): a remote ``argus-agent`` process authenticates
with its own bearer token and POSTs a sanitized
``argus.agent.protocol.AgentSnapshot`` -- never a dashboard/user
action, never reachable with a session cookie or browser credential,
and never itself capable of mutating anything beyond "here is what my
own Docker daemon looked like just now" (see this module's own
docstring further down on the trust boundary).

Everything this route does with an authenticated, validated snapshot
goes through the exact same shared pipeline
(``argus.ingestion.pipeline``) the local collector uses -- see that
module's own docstring on why there is only one incident pipeline, not
two.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime

from fastapi import APIRouter, Depends, Request

from argus.agent.protocol import (
    MAX_APPLICATIONS_PER_SNAPSHOT,
    MAX_CLOCK_SKEW_SECONDS,
    MAX_EVIDENCE_ITEMS_PER_SNAPSHOT,
    MAX_OBSERVATIONS_PER_SNAPSHOT,
    MAX_REQUEST_BYTES,
    PROTOCOL_VERSION,
    AgentSnapshot,
    ProtocolError,
)
from argus.api.dependencies import get_now, get_repository
from argus.api.errors import (
    host_identity_mismatch,
    invalid_agent_credentials,
    malformed_snapshot,
    snapshot_too_large,
    unsupported_protocol_version,
)
from argus.api.models import AgentIngestResponse
from argus.domain.host import scope_application_key
from argus.evidence.aggregator import DEFAULT_AGGREGATION_WINDOW_SECONDS
from argus.evidence.association import DEFAULT_ASSOCIATION_WINDOW_SECONDS, associate_evidence
from argus.evidence.persistence import persist_candidates
from argus.incidents.engine import IncidentProcessingError
from argus.ingestion.pipeline import persist_snapshot, process_incidents_for_snapshot
from argus.realtime.emitter import emit_evidence_updated
from argus.security import hash_token, tokens_match
from argus.store.database import DuplicateObservationError, PersistenceError
from argus.store.repository import Repository

router = APIRouter()

logger = logging.getLogger(__name__)


def _extract_bearer_token(request: Request) -> "str | None":
    header = request.headers.get("authorization")
    if header is None or not header.startswith("Bearer "):
        return None
    token = header[len("Bearer ") :].strip()
    return token or None


def _authenticate(repository: Repository, snapshot: AgentSnapshot, token: str):
    """Look the presented credential up by the snapshot's own
    ``agent_id`` (never by its ``host_key`` -- see schema.sql's own
    comment on ``hosts.agent_id`` for why those must be two separate
    lookups), verify the token, then -- and only then -- check that the
    same request's ``host_key`` actually matches the host that
    ``agent_id`` was issued to.

    Deliberately raises the *same* generic ``invalid_agent_credentials``
    (401) whether ``agent_id`` is unknown or the token simply doesn't
    match -- never two different messages that would let a caller
    enumerate valid agent ids. A real ``agent_id`` with a *wrong*
    ``host_key`` claim is the one case that gets its own, different
    status (403) -- the credential was real, the claim wasn't.
    """

    host = repository.get_host_by_agent_id(snapshot.agent_id)
    if host is None or host.kind != "agent" or host.agent_token_hash is None:
        raise invalid_agent_credentials()

    if not tokens_match(hash_token(token), host.agent_token_hash):
        raise invalid_agent_credentials()

    if host.host_key != snapshot.host_key:
        raise host_identity_mismatch()

    return host


@router.post(
    "/ingest",
    response_model=AgentIngestResponse,
    summary="Ingest one sanitized snapshot from a remote argus-agent",
    description=(
        "Machine-to-machine only -- authenticated with a per-agent bearer token (`argus agents add`), "
        "never a dashboard/browser action. See argus.agent.protocol for the request body shape."
    ),
)
async def ingest(
    request: Request, repository: Repository = Depends(get_repository), now: datetime = Depends(get_now)
) -> AgentIngestResponse:
    body = await request.body()
    if len(body) > MAX_REQUEST_BYTES:
        raise snapshot_too_large(f"request body of {len(body)} bytes exceeds the {MAX_REQUEST_BYTES}-byte limit")

    token = _extract_bearer_token(request)
    if token is None:
        raise invalid_agent_credentials()

    try:
        payload = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        raise malformed_snapshot("request body is not valid JSON")

    if not isinstance(payload, dict):
        raise malformed_snapshot("request body must be a JSON object")

    protocol_version = payload.get("protocol_version")
    if protocol_version != PROTOCOL_VERSION:
        raise unsupported_protocol_version(protocol_version)

    try:
        snapshot = AgentSnapshot.from_dict(payload)
    except ProtocolError as exc:
        raise malformed_snapshot(str(exc))

    if len(snapshot.applications) > MAX_APPLICATIONS_PER_SNAPSHOT:
        raise snapshot_too_large(
            f"{len(snapshot.applications)} applications exceeds the {MAX_APPLICATIONS_PER_SNAPSHOT}-application limit"
        )
    if len(snapshot.observations) > MAX_OBSERVATIONS_PER_SNAPSHOT:
        raise snapshot_too_large(
            f"{len(snapshot.observations)} observations exceeds the {MAX_OBSERVATIONS_PER_SNAPSHOT}-observation limit"
        )
    if len(snapshot.evidence_candidates) > MAX_EVIDENCE_ITEMS_PER_SNAPSHOT:
        raise snapshot_too_large(
            f"{len(snapshot.evidence_candidates)} evidence items exceeds the "
            f"{MAX_EVIDENCE_ITEMS_PER_SNAPSHOT}-item limit"
        )

    host = _authenticate(repository, snapshot, token)

    skew_seconds = abs((now - snapshot.generated_at).total_seconds())
    if skew_seconds > MAX_CLOCK_SKEW_SECONDS:
        raise malformed_snapshot(
            f"generated_at is {skew_seconds:.0f}s away from the control plane's own clock "
            f"(limit {MAX_CLOCK_SKEW_SECONDS}s) -- check the remote host's own clock"
        )

    # Every check above ran before touching any write path -- an
    # unauthenticated, malformed, oversized, or clock-skewed request
    # never reaches persistence. Every host_id/host below is trusted
    # server-side state (the authenticated `host`, resolved above),
    # never anything client-supplied.
    try:
        persist_report, scoped_applications = persist_snapshot(
            repository,
            host_id=host.id,
            host_key=snapshot.host_key,
            applications=snapshot.applications,
            observations=snapshot.observations,
        )
    except DuplicateObservationError:
        # Idempotent replay (see the milestone's own "Snapshot
        # Idempotency" requirement): this exact (container, observed_at)
        # set was already committed by an earlier, successful attempt of
        # this same POST -- `persist_discovery`'s own transaction is
        # atomic, so a partial-then-retried commit is not possible; a
        # duplicate here always means the whole snapshot was already
        # ingested. Still updates the host heartbeat (a retry proves the
        # agent is alive right now, even if there's nothing new to
        # persist) and returns success -- a retry must never look like a
        # failure to the agent.
        repository.record_host_heartbeat(host_id=host.id, at=now, agent_version=snapshot.agent_version)
        return AgentIngestResponse(
            status="duplicate", host_key=snapshot.host_key, applications_written=0, observations_written=0
        )
    except PersistenceError as exc:
        logger.error("agent ingest persistence failed for host %s: %s", snapshot.host_key, type(exc).__name__)
        raise malformed_snapshot("snapshot could not be persisted") from exc

    try:
        process_incidents_for_snapshot(
            repository, applications=scoped_applications, observations=snapshot.observations, tick_at=now
        )
    except (IncidentProcessingError, PersistenceError) as exc:
        # Observations from this snapshot are already committed (a
        # separate, already-closed transaction -- see
        # `argus.ingestion.pipeline`'s own docstring on why persistence
        # and incident processing are deliberately not one transaction);
        # only this request's transition/incident bookkeeping is
        # missing, and the next successful ingest for this host catches
        # up on its own, exactly like a local collector tick would.
        logger.error("agent ingest incident processing failed for host %s: %s", snapshot.host_key, type(exc).__name__)
        raise malformed_snapshot("snapshot was persisted but incident processing failed") from exc

    evidence_signals_created = _persist_evidence(repository, snapshot, host_key=snapshot.host_key)
    associations = 0
    try:
        associations = associate_evidence(repository, now=now, window_seconds=DEFAULT_ASSOCIATION_WINDOW_SECONDS)
    except Exception:  # noqa: BLE001 -- evidence association failure must never fail the whole ingest
        logger.warning("evidence association failed after agent ingest for host %s", snapshot.host_key)

    if evidence_signals_created or associations:
        emit_evidence_updated(
            repository, signals_created=evidence_signals_created, associations=associations, tick_at=now, now=now
        )

    repository.record_host_heartbeat(host_id=host.id, at=now, agent_version=snapshot.agent_version)

    return AgentIngestResponse(
        status="accepted",
        host_key=snapshot.host_key,
        applications_written=persist_report.applications_written,
        observations_written=persist_report.observations_written,
    )


def _persist_evidence(repository: Repository, snapshot: AgentSnapshot, *, host_key: str) -> int:
    """Persists every ``EvidenceCandidateWire`` in ``snapshot`` via the
    exact same ``argus.evidence.persistence.persist_candidates`` the
    local collector uses -- resolving each candidate's (locally-keyed)
    ``application_key``/``container_id`` back to the row ids that
    matters through the same host-scoped key
    (`argus.domain.host.scope_application_key`) `persist_snapshot` just
    used to persist those very rows.

    Never raises -- an unresolvable/malformed evidence item (e.g. one
    naming a container this snapshot's own ``observations`` never
    mentioned) is skipped and logged, exactly like a single container's
    log-read failure is isolated in ``CollectorLoop``; evidence is
    always auxiliary to the core snapshot it rode in on.
    """

    created = 0
    for item in snapshot.evidence_candidates:
        try:
            scoped_key = scope_application_key(host_key, item.application_key)
            application_record = repository.get_application(scoped_key)
            container_record = repository.get_container_by_docker_id(item.container_id)
            if application_record is None or container_record is None:
                logger.warning(
                    "skipping agent evidence item for unresolved application/container (host=%s)", host_key
                )
                continue
            created += persist_candidates(
                repository, [item.to_signal_candidate()],
                application_id=application_record.id, container_row_id=container_record.id,
                source_type=item.source_type, source_ref=item.source_ref,
                aggregation_window_seconds=DEFAULT_AGGREGATION_WINDOW_SECONDS,
            )
        except Exception:  # noqa: BLE001 -- one bad evidence item must never fail the whole ingest
            logger.warning("failed to persist one agent evidence item (host=%s)", host_key)
    return created
