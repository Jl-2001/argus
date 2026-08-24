"""Secret redaction -- applied to every log sample before it is ever
persisted, matched against a pattern, or returned by the CLI.

This is deliberately **not** claimed to be complete DLP (data-loss
prevention). It is a reasonable, deterministic, pre-storage redaction
pass over the specific secret shapes explicitly called out for
Milestone 10: bearer tokens, JWTs, AWS-style access keys, and
``key=value``/URL-embedded credentials. Anything that doesn't match one
of these explicit shapes is left as-is -- this module does not attempt
to guess whether an arbitrary string "looks secret enough".

Redaction always runs *before* pattern matching and *before* any sample
text is stored -- ``argus.evidence.collector`` never holds an
unredacted line past the point this function is called. There is no
code path anywhere in this package that persists, logs, or returns a
line that hasn't already been through ``redact_secrets``.
"""

from __future__ import annotations

import re

__all__ = ["redact_secrets"]

_MASK = "[REDACTED]"

# Order matters: broader shapes (a full credentialed URL) are redacted
# before the narrower generic key=value rule gets a chance to only catch
# half of it and leave a dangling fragment.
_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    # postgres://user:pass@host, mongodb+srv://user:pass@host, etc. --
    # only the credentials portion is replaced, so the host/scheme (not
    # secret) stays legible for a human reading the evidence sample.
    (
        "credentialed_url",
        re.compile(r"(?P<scheme>[A-Za-z][A-Za-z0-9+.\-]*://)[^\s/:@]+:[^\s/@]+@"),
    ),
    # RFC 6750 Bearer tokens: "Bearer <token>" (Authorization headers,
    # log lines echoing outgoing/incoming requests, ...).
    ("bearer_token", re.compile(r"\bBearer\s+[A-Za-z0-9\-_.=]+")),
    # JWT-shaped strings: three base64url segments separated by dots.
    # Matched independently of "Bearer" since JWTs also appear as plain
    # cookie/query values.
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{4,}\.[A-Za-z0-9_-]{4,}\.[A-Za-z0-9_-]{4,}\b")),
    # AWS access key ids -- a fixed, recognizable prefix + 16 alnum chars.
    ("aws_access_key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    # key=value / key: value style secrets -- password, secret, token,
    # api_key, access_key, etc. Redacts only the value, keeps the key
    # name so the sample stays informative ("password=[REDACTED]").
    (
        "key_value_secret",
        re.compile(
            r"(?i)\b(password|passwd|pwd|secret|token|api[_-]?key|access[_-]?key|auth[_-]?key)"
            r"([\"']?\s*[:=]\s*[\"']?)([^\s\"'&,;]+)"
        ),
    ),
)


def redact_secrets(text: str) -> str:
    """Return ``text`` with every recognized secret shape masked.

    Idempotent and order-sensitive: applying it twice never un-redacts
    or double-mangles an already-redacted string, since ``[REDACTED]``
    itself matches none of the rules above.
    """

    redacted = text

    name, pattern = _RULES[0]
    redacted = pattern.sub(lambda m: f"{m.group('scheme')}{_MASK}@", redacted)

    for name, pattern in _RULES[1:3]:
        redacted = pattern.sub(_MASK, redacted)

    name, pattern = _RULES[3]
    redacted = pattern.sub(_MASK, redacted)

    name, pattern = _RULES[4]
    redacted = pattern.sub(lambda m: f"{m.group(1)}{m.group(2)}{_MASK}", redacted)

    return redacted
