"""Tests for argus.evidence.patterns -- one or more tests per category,
plus explicit "different categories aren't merged" and "innocent lines
match nothing" tests."""

from __future__ import annotations

import pytest

from argus.domain.models import EvidenceCategory, EvidenceSeverity
from argus.evidence.patterns import DEFAULT_PATTERNS, classify_line


class TestEachCategoryHasWorkingPatterns:
    @pytest.mark.parametrize(
        "line, expected_category",
        [
            ("connection terminated unexpectedly", EvidenceCategory.DB_CONNECTION_TIMEOUT),
            ("connect ECONNREFUSED 127.0.0.1:5432", EvidenceCategory.DB_CONNECTION_TIMEOUT),
            ("could not connect to server: timeout expired", EvidenceCategory.DB_CONNECTION_TIMEOUT),
            ("authentication failed for user \"admin\"", EvidenceCategory.AUTHENTICATION_FAILURE),
            ("invalid credentials supplied", EvidenceCategory.AUTHENTICATION_FAILURE),
            ("Error: bind: address already in use", EvidenceCategory.PORT_CONFLICT),
            ("port is already allocated", EvidenceCategory.PORT_CONFLICT),
            (
                "environment variable DATABASE_URL is not set",
                EvidenceCategory.MISSING_ENVIRONMENT_VARIABLE,
            ),
            ("missing required environment variable API_KEY", EvidenceCategory.MISSING_ENVIRONMENT_VARIABLE),
            ('"GET /health HTTP/1.1" 500 128', EvidenceCategory.HTTP_5XX),
            ("upstream responded with 503", EvidenceCategory.HTTP_5XX),
            ("Out of memory: Killed process 1234 (node)", EvidenceCategory.OOM),
            ("cannot allocate memory", EvidenceCategory.OOM),
            ("write failed: no space left on device", EvidenceCategory.DISK_PRESSURE),
            ("disk quota exceeded", EvidenceCategory.DISK_PRESSURE),
            ("connect: network is unreachable", EvidenceCategory.NETWORK_FAILURE),
            ("no route to host", EvidenceCategory.NETWORK_FAILURE),
            (
                "upstream connect error or disconnect/reset before headers",
                EvidenceCategory.DEPENDENCY_UNAVAILABLE,
            ),
            ("503 Service Unavailable from payments-api", EvidenceCategory.DEPENDENCY_UNAVAILABLE),
            ("Unhandled exception in worker thread", EvidenceCategory.GENERIC_ERROR),
            ("Traceback (most recent call last):", EvidenceCategory.GENERIC_ERROR),
        ],
    )
    def test_line_classified_as_expected_category(self, line, expected_category):
        match = classify_line(line)
        assert match is not None, f"expected {expected_category} to match {line!r}, matched nothing"
        assert match.category is expected_category


class TestInnocentLinesMatchNothing:
    @pytest.mark.parametrize(
        "line",
        [
            "service started successfully on port 8080",
            "listening on 0.0.0.0:5432",
            "GET /health HTTP/1.1\" 200 12",
            "worker 3 ready",
            "connection established",
        ],
    )
    def test_ordinary_line_produces_no_category(self, line):
        assert classify_line(line) is None


class TestCategoriesAreNotMergedAcrossPatterns:
    def test_db_connection_timeout_and_dependency_unavailable_stay_distinct(self):
        db_match = classify_line("connection terminated unexpectedly")
        dep_match = classify_line("upstream connect error or disconnect/reset before headers")
        assert db_match.category is EvidenceCategory.DB_CONNECTION_TIMEOUT
        assert dep_match.category is EvidenceCategory.DEPENDENCY_UNAVAILABLE
        assert db_match.category is not dep_match.category

    def test_oom_and_disk_pressure_stay_distinct(self):
        oom_match = classify_line("out of memory")
        disk_match = classify_line("no space left on device")
        assert oom_match.category is EvidenceCategory.OOM
        assert disk_match.category is EvidenceCategory.DISK_PRESSURE

    def test_generic_error_never_wins_over_a_more_specific_category(self):
        # Contains both "error"-shaped language AND a specific db-timeout
        # phrase -- the specific category must win (checked first).
        match = classify_line("error: connection terminated unexpectedly")
        assert match.category is EvidenceCategory.DB_CONNECTION_TIMEOUT


class TestPatternLibraryShape:
    def test_generic_error_is_last_precedence_wise(self):
        assert DEFAULT_PATTERNS[-1].category is EvidenceCategory.GENERIC_ERROR

    def test_every_pattern_has_at_least_one_compiled_regex(self):
        for pattern in DEFAULT_PATTERNS:
            assert len(pattern.patterns) >= 1

    def test_every_category_used_exactly_once_in_default_patterns(self):
        # container_restart / container_unhealthy are Docker-fact-derived,
        # not log patterns -- deliberately absent here.
        categories = [p.category for p in DEFAULT_PATTERNS]
        assert len(categories) == len(set(categories))
        assert EvidenceCategory.CONTAINER_RESTART not in categories
        assert EvidenceCategory.CONTAINER_UNHEALTHY not in categories

    def test_severities_are_valid_evidence_severity_members(self):
        for pattern in DEFAULT_PATTERNS:
            assert isinstance(pattern.severity, EvidenceSeverity)
