"""Tests for argus.cli.formatting and argus.cli.durations -- pure
string-rendering and input-parsing, no database, no Docker.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from argus.cli.durations import InvalidDurationError, parse_duration
from argus.cli.formatting import EM_DASH, iso, relative_time, render_kv, render_table

UTC = timezone.utc
NOW = datetime(2026, 8, 21, 12, 0, 0, tzinfo=UTC)


class TestRelativeTime:
    def test_none_is_em_dash(self):
        assert relative_time(NOW, None) == EM_DASH

    def test_seconds(self):
        assert relative_time(NOW, NOW - timedelta(seconds=3)) == "3s ago"
        assert relative_time(NOW, NOW - timedelta(seconds=59)) == "59s ago"

    def test_exactly_sixty_seconds_rolls_over_to_minutes(self):
        assert relative_time(NOW, NOW - timedelta(seconds=60)) == "1m ago"

    def test_minutes_boundary(self):
        assert relative_time(NOW, NOW - timedelta(seconds=3599)) == "59m ago"
        assert relative_time(NOW, NOW - timedelta(seconds=3600)) == "1h ago"

    def test_hours_boundary(self):
        assert relative_time(NOW, NOW - timedelta(seconds=86399)) == "23h ago"
        assert relative_time(NOW, NOW - timedelta(seconds=86400)) == "1d ago"

    def test_days(self):
        assert relative_time(NOW, NOW - timedelta(days=9)) == "9d ago"

    def test_future_timestamp_clamps_to_zero_seconds(self):
        assert relative_time(NOW, NOW + timedelta(seconds=30)) == "0s ago"


class TestIso:
    def test_none_is_none(self):
        assert iso(None) is None

    def test_utc_timestamp(self):
        assert iso(NOW) == "2026-08-21T12:00:00+00:00"

    def test_never_a_relative_string(self):
        result = iso(NOW - timedelta(seconds=5))
        assert "ago" not in result


class TestRenderTable:
    def test_columns_align_to_widest_cell(self):
        text = render_table(["NAME", "STATUS"], [["CNSTRCT", "HEALTHY"], ["A", "DEGRADED"]])
        lines = text.splitlines()
        assert lines[0].startswith("NAME     STATUS")  # NAME padded to len("CNSTRCT")
        assert lines[1].startswith("CNSTRCT  HEALTHY")

    def test_last_column_not_padded(self):
        text = render_table(["A", "B"], [["x", "y"]])
        for line in text.splitlines():
            assert not line.endswith(" ")

    def test_show_header_false_omits_header_line(self):
        text = render_table(["", ""], [["a", "b"]], show_header=False)
        assert text == "a  b"

    def test_empty_rows_still_renders_header(self):
        text = render_table(["NAME", "STATUS"], [])
        assert text == "NAME  STATUS"


class TestRenderKv:
    def test_aligns_values_to_fixed_label_width(self):
        text = render_kv([("Status", "HEALTHY"), ("Last success", "3s ago")])
        lines = text.splitlines()
        # both value columns start at the same offset regardless of label length
        assert lines[0].index("HEALTHY") == lines[1].index("3s ago")


class TestParseDuration:
    @pytest.mark.parametrize(
        "text, expected_seconds",
        [
            ("45s", 45),
            ("30m", 30 * 60),
            ("6h", 6 * 3600),
            ("24h", 24 * 3600),
            ("7d", 7 * 86400),
        ],
    )
    def test_valid_durations(self, text, expected_seconds):
        assert parse_duration(text) == timedelta(seconds=expected_seconds)

    @pytest.mark.parametrize(
        "text", ["", "bogus", "-5m", "5", "5x", "5.5h", "0m", "yesterday-ish", "30 m"]
    )
    def test_invalid_durations_raise_clear_error(self, text):
        with pytest.raises(InvalidDurationError):
            parse_duration(text)
