"""Milestone 16 -- `argus.security`: agent token generation/hashing/
comparison."""

from __future__ import annotations

from argus.security import generate_token, hash_token, tokens_match


class TestGenerateToken:
    def test_produces_a_high_entropy_string(self):
        token = generate_token()
        assert isinstance(token, str)
        assert len(token) >= 32

    def test_two_calls_never_collide(self):
        tokens = {generate_token() for _ in range(50)}
        assert len(tokens) == 50


class TestHashToken:
    def test_deterministic(self):
        token = generate_token()
        assert hash_token(token) == hash_token(token)

    def test_different_tokens_hash_differently(self):
        assert hash_token(generate_token()) != hash_token(generate_token())

    def test_hash_is_never_the_plaintext_token(self):
        token = "a-plaintext-example-token"
        assert hash_token(token) != token

    def test_hash_output_looks_like_a_hex_digest(self):
        digest = hash_token(generate_token())
        assert len(digest) == 64  # sha256 hex digest length
        assert all(c in "0123456789abcdef" for c in digest)


class TestTokensMatch:
    def test_matching_hashes_match(self):
        token = generate_token()
        digest = hash_token(token)
        assert tokens_match(digest, digest) is True

    def test_mismatched_hashes_do_not_match(self):
        assert tokens_match(hash_token("a"), hash_token("b")) is False

    def test_never_called_with_or_compares_plaintext_successfully(self):
        # Documentation-level guard: comparing a plaintext token against
        # its own hash must never accidentally succeed.
        token = generate_token()
        assert tokens_match(token, hash_token(token)) is False
