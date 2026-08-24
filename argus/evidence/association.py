"""Links evidence to incidents by time proximity -- and only by time
proximity. Nothing in this module (or the schema it writes to) ever
claims or implies causation; see the module-level warning below.

    Association means: "this evidence occurred near this incident."
    Association does NOT mean: "this evidence caused this incident."

That distinction is enforced structurally, not just by convention:
``incident_evidence.relation`` is CHECK-constrained in the schema to the
single value ``"temporal_proximity"`` -- there is no ``caused_by``
value this code (or any future code) could set without a real schema
change, and this module never even has an opinion on causation to
begin with. Deciding what *actually* caused an incident is explicitly
out of scope for all of Milestone 10 -- see the milestone's own "No
Root-Cause Claims Yet" section.

Time-window semantics
----------------------
For one incident, the association window is::

    window_start = incident.opened_at - ASSOCIATION_WINDOW_SECONDS
    window_end   = incident.closed_at + ASSOCIATION_WINDOW_SECONDS   (if resolved)
                 = now                                                (if still open)

``window_start`` looks *backward* from the incident's own opening --
this is deliberate and important: evidence that already existed before
Argus even recorded the incident (e.g. a database restart logged a few
seconds before the API started timing out) must still be eligible for
association the first time this incident is ever scanned. Association
is never limited to "evidence collected after the incident row was
created."

An incident is re-scanned by ``associate_evidence`` on every tick it is
either still open (so newly-arriving evidence keeps getting linked
while ``window_end`` keeps sliding forward with ``now``) or was resolved
within the last ``ASSOCIATION_WINDOW_SECONDS`` (so evidence that arrives
just after resolution -- discovered on a *later* tick, since evidence
collection only ever looks at what already happened -- still gets its
chance to be linked before the window closes for good). Once
``now - closed_at`` exceeds the window, that incident is no longer
re-scanned; its association is final.

Linking is idempotent (``Repository.link_incident_evidence``), so
re-scanning the same still-open incident tick after tick never produces
duplicate links for evidence it already linked -- only genuinely new
overlapping signals produce a new row.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from argus.store.repository import Repository

__all__ = ["DEFAULT_ASSOCIATION_WINDOW_SECONDS", "associate_evidence"]

#: How far before an incident opens, and after it resolves, evidence is
#: still eligible for association. Symmetric on both sides, per the
#: Milestone 10 specification's own worked example.
DEFAULT_ASSOCIATION_WINDOW_SECONDS = 60


def associate_evidence(
    repository: Repository,
    *,
    now: datetime,
    window_seconds: int = DEFAULT_ASSOCIATION_WINDOW_SECONDS,
) -> int:
    """Link evidence to every incident whose association window could
    still be active, per the module docstring's exact semantics.

    Returns the number of link attempts made this call (idempotent
    re-links included) -- a coarse activity count for logging, not a
    "new links only" count; ``Repository.link_incident_evidence`` is
    what actually makes repeats free.
    """

    window = timedelta(seconds=window_seconds)
    grace_cutoff = now - window
    link_attempts = 0

    for incident in repository.list_incidents_for_association(grace_cutoff=grace_cutoff):
        window_start = incident.opened_at - window
        window_end = (incident.closed_at + window) if incident.closed_at is not None else now

        signals = repository.list_log_signals_in_window(
            incident.scope_id, window_start=window_start, window_end=window_end
        )
        for signal in signals:
            repository.link_incident_evidence(incident_id=incident.id, log_signal_id=signal.id, linked_at=now)
            link_attempts += 1

    return link_attempts
