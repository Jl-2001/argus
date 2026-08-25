"""Milestone 16 -- `argus.domain.host`: pure host identity/connectivity
vocabulary. No persistence, no Docker, no clock reads."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from argus.domain.host import (
    LOCAL_HOST_KEY,
    HostStatus,
    evaluate_host_status,
    scope_application_key,
)

T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)


class TestScopeApplicationKey:
    def test_local_host_is_a_complete_no_op(self):
        assert scope_application_key(LOCAL_HOST_KEY, "cnstrct") == "cnstrct"

    def test_non_local_host_prefixes_the_key(self):
        assert scope_application_key("dell-latitude-5400", "cnstrct") == "dell-latitude-5400:cnstrct"

    def test_two_hosts_with_identically_named_local_keys_never_collide(self):
        dell = scope_application_key("dell-latitude-5400", "cnstrct")
        mac_local = scope_application_key(LOCAL_HOST_KEY, "cnstrct")
        assert dell != mac_local


class TestEvaluateHostStatus:
    def test_within_online_threshold_is_online(self):
        status = evaluate_host_status(last_seen_at=T0, now=T0 + timedelta(seconds=29), poll_interval_seconds=15.0)
        assert status is HostStatus.ONLINE

    def test_exactly_at_online_threshold_boundary_is_online(self):
        status = evaluate_host_status(last_seen_at=T0, now=T0 + timedelta(seconds=30), poll_interval_seconds=15.0)
        assert status is HostStatus.ONLINE

    def test_just_past_online_threshold_is_stale(self):
        status = evaluate_host_status(last_seen_at=T0, now=T0 + timedelta(seconds=31), poll_interval_seconds=15.0)
        assert status is HostStatus.STALE

    def test_within_stale_threshold_is_stale(self):
        status = evaluate_host_status(last_seen_at=T0, now=T0 + timedelta(seconds=74), poll_interval_seconds=15.0)
        assert status is HostStatus.STALE

    def test_past_stale_threshold_is_offline(self):
        status = evaluate_host_status(last_seen_at=T0, now=T0 + timedelta(seconds=76), poll_interval_seconds=15.0)
        assert status is HostStatus.OFFLINE

    def test_last_seen_in_the_future_reads_as_online_not_negative_age(self):
        status = evaluate_host_status(
            last_seen_at=T0 + timedelta(seconds=100), now=T0, poll_interval_seconds=15.0
        )
        assert status is HostStatus.ONLINE

    def test_never_raises_on_any_input(self):
        # No exception path exists in this function at all -- a smoke
        # check that a wildly-off now/last_seen_at pair still returns a
        # valid enum member, never propagates.
        status = evaluate_host_status(
            last_seen_at=T0, now=T0 + timedelta(days=365), poll_interval_seconds=15.0
        )
        assert status is HostStatus.OFFLINE
