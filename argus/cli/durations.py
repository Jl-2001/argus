"""The small, fixed --since duration grammar: 30m, 6h, 24h, 7d, ...

Deliberately not a natural-language time parser -- a tiny, explicit
grammar that is easy to validate and produces a clear error for
anything else, rather than guessing at what a user meant.
"""

from __future__ import annotations

import re
from datetime import timedelta

__all__ = ["InvalidDurationError", "parse_duration"]

_PATTERN = re.compile(r"^([1-9][0-9]*)([smhd])$")

_UNIT_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400}


class InvalidDurationError(ValueError):
    """The given text is not a valid duration in the supported grammar."""


def parse_duration(text: str) -> timedelta:
    """Parse e.g. "30m"/"6h"/"24h"/"7d" into a `timedelta`.

    A positive integer immediately followed by exactly one unit letter
    (s/m/h/d) -- nothing else. Raises `InvalidDurationError` (a
    `ValueError`) for anything that doesn't match, including empty
    strings, negative/zero values, decimals, or unsupported units.
    """

    match = _PATTERN.match(text.strip())
    if match is None:
        raise InvalidDurationError(
            f"invalid duration {text!r}; expected a positive integer followed by one of "
            "s/m/h/d, e.g. '30m', '6h', '24h', '7d'"
        )
    value, unit = match.groups()
    return timedelta(seconds=int(value) * _UNIT_SECONDS[unit])
