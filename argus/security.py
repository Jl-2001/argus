"""Agent bearer-token generation/hashing/comparison -- Milestone 16.

A single, tiny, dependency-free module (stdlib ``secrets``/``hashlib``/
``hmac`` only) deliberately kept at the top of the package, sibling to
``argus.domain``, rather than under ``argus.store`` or ``argus.agent``:
it is imported by the control plane's admin bootstrap
(``argus.cli.commands.agents``) *and* its ingestion route
(``argus.api.routes.agents``) *and*, in principle, could be imported by
``argus.agent`` itself one day -- none of those should have to reach
into each other's package just for this.

Never logs, stores, or returns a plaintext token anywhere except the
one deliberate "display it once, right after generation" moment in the
CLI (see ``argus.cli.commands.agents.add``) -- every persisted/compared
value from this module onward is a hash.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets

__all__ = ["generate_token", "hash_token", "tokens_match", "TOKEN_BYTES"]

#: 32 random bytes -> 43 URL-safe base64 characters, ~256 bits of
#: entropy -- comfortably high-entropy for a bearer credential (see the
#: milestone's own "token must be high entropy" requirement), and short
#: enough to type/paste into a `.env` file by hand if needed.
TOKEN_BYTES = 32


def generate_token() -> str:
    """A fresh, cryptographically random agent token. Never derived from
    anything guessable (a host name, a timestamp, a counter) -- pure
    ``secrets.token_urlsafe``, the same primitive Python's own
    documentation recommends for exactly this use case."""

    return secrets.token_urlsafe(TOKEN_BYTES)


def hash_token(token: str) -> str:
    """A stable, one-way digest of ``token`` -- what actually gets
    persisted (``hosts.agent_token_hash``) and compared against on every
    ingest request. Plain SHA-256, not a slow password hash (bcrypt/
    argon2/scrypt): unlike a human-chosen password, this token is
    already maximal-entropy random data, so there is no offline
    dictionary/brute-force attack a slow hash would meaningfully defend
    against here that isn't already defeated by the token's own entropy
    -- see the milestone's own "a simple v1 approach is acceptable" and
    "document mTLS/request signing as future hardening" notes.
    """

    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def tokens_match(candidate_hash: str, stored_hash: str) -> bool:
    """Constant-time comparison of two *already-hashed* values -- never
    called with a plaintext token on either side. Ordinary ``==`` on a
    hex digest is a short-circuiting, length-and-position-dependent
    comparison; ``hmac.compare_digest`` is the standard-library
    primitive for exactly this "don't let response timing leak how much
    of the credential was right" concern."""

    return hmac.compare_digest(candidate_hash, stored_hash)
