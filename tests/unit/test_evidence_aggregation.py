"""Tests for argus.evidence.aggregator: signature normalization and
time-bucketed aggregation."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from argus.domain.models import EvidenceCategory, EvidenceSeverity
from argus.evidence.aggregator import (
    ClassifiedLine,
    aggregate_classified_lines,
    normalize_signature,
)

UTC = timezone.utc
T0 = datetime(2026, 8, 22, 12, 0, 0, tzinfo=UTC)


def make_line(offset_seconds: float, text: str, category=EvidenceCategory.DB_CONNECTION_TIMEOUT):
    return ClassifiedLine(
        observed_at=T0 + timedelta(seconds=offset_seconds),
        category=category,
        severity=EvidenceSeverity.HIGH,
        redacted_text=text,
    )


class TestNormalizeSignature:
    def test_digit_runs_collapse(self):
        assert normalize_signature("timed out after 30s") == normalize_signature("timed out after 45s")

    def test_hex_ids_collapse(self):
        a = normalize_signature("request 4f3a9b2c1d0e5f6a7b8c9d0e failed")
        b = normalize_signature("request 00112233445566778899aabb failed")
        assert a == b

    def test_case_insensitive(self):
        assert normalize_signature("Connection TIMEOUT") == normalize_signature("connection timeout")

    def test_distinct_messages_stay_distinct(self):
        assert normalize_signature("connection timeout") != normalize_signature("authentication failed")

    def test_whitespace_collapses(self):
        assert normalize_signature("a   b\tc") == normalize_signature("a b c")


class TestAggregationBasic:
    def test_ten_repeated_matching_lines_become_one_signal_count_ten(self):
        lines = [make_line(i * 2, "connection timeout after 30s") for i in range(10)]
        candidates = aggregate_classified_lines(lines)
        assert len(candidates) == 1
        assert candidates[0].count == 10
        assert candidates[0].first_seen_at == T0
        assert candidates[0].last_seen_at == T0 + timedelta(seconds=18)

    def test_sample_is_the_first_lines_text(self):
        lines = [make_line(0, "first message"), make_line(1, "first message")]
        candidates = aggregate_classified_lines(lines)
        assert candidates[0].sample == "first message"

    def test_single_line_produces_count_one(self):
        candidates = aggregate_classified_lines([make_line(0, "only one")])
        assert len(candidates) == 1
        assert candidates[0].count == 1


class TestDifferentCategoriesNotMerged:
    def test_different_categories_produce_separate_candidates(self):
        lines = [
            make_line(0, "connection timeout", category=EvidenceCategory.DB_CONNECTION_TIMEOUT),
            make_line(1, "out of memory", category=EvidenceCategory.OOM),
        ]
        candidates = aggregate_classified_lines(lines)
        assert len(candidates) == 2
        categories = {c.category for c in candidates}
        assert categories == {EvidenceCategory.DB_CONNECTION_TIMEOUT, EvidenceCategory.OOM}

    def test_different_signatures_same_category_produce_separate_candidates(self):
        lines = [
            make_line(0, "connection timeout"),
            make_line(1, "authentication failed"),  # different text, same category label passed in
        ]
        candidates = aggregate_classified_lines(lines)
        assert len(candidates) == 2


class TestTimeWindowSplitting:
    def test_lines_outside_window_become_separate_signals(self):
        lines = [make_line(0, "oom killed"), make_line(1000, "oom killed")]
        candidates = aggregate_classified_lines(lines, aggregation_window_seconds=300)
        assert len(candidates) == 2
        assert candidates[0].count == 1
        assert candidates[1].count == 1

    def test_lines_inside_window_stay_together(self):
        lines = [make_line(0, "oom killed"), make_line(299, "oom killed")]
        candidates = aggregate_classified_lines(lines, aggregation_window_seconds=300)
        assert len(candidates) == 1
        assert candidates[0].count == 2

    def test_bucket_boundary_is_measured_from_the_buckets_own_first_line_not_a_sliding_gap(self):
        # Three lines: 0s, 200s, 400s -- window 300s. The boundary check is
        # always "how far is this line from the CURRENT bucket's first
        # line", never a line-to-line gap -- so 200s joins the bucket
        # that started at 0s (200-0=200<=300), but 400s does not
        # (400-0=400>300), even though 400s is only 200s after the 200s
        # line itself. The bucket anchor never shifts mid-bucket.
        lines = [make_line(0, "x"), make_line(200, "x"), make_line(400, "x")]
        candidates = aggregate_classified_lines(lines, aggregation_window_seconds=300)
        assert len(candidates) == 2
        assert candidates[0].count == 2  # 0s and 200s together (200-0=200 <= 300)
        assert candidates[1].count == 1  # 400s starts a new bucket (400-0=400 > 300)

    def test_many_lines_spanning_multiple_windows_split_into_multiple_buckets(self):
        lines = [make_line(i * 100, "recurring") for i in range(10)]  # 0, 100, ..., 900
        candidates = aggregate_classified_lines(lines, aggregation_window_seconds=250)
        # Bucket boundaries: [0,100,200](first=0,300>250 closes before 300)
        # -> new bucket at 300: [300,400,500](600-300=300>250 closes) ->
        # new bucket at 600: [600,700,800](900-600=300>250 closes) -> new
        # bucket at 900: [900]
        assert sum(c.count for c in candidates) == 10
        assert len(candidates) == 4
