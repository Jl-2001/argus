"""Tests for argus.evidence.redaction.

Every one of Milestone 10's explicitly-named secret shapes gets its own
test, plus a test that ordinary, non-secret log content survives
untouched -- redaction must never mangle innocent text.
"""

from __future__ import annotations

from argus.evidence.redaction import redact_secrets


class TestBearerToken:
    def test_bearer_token_is_masked(self):
        line = "Authorization: Bearer abc123.def456-XYZ_token"
        redacted = redact_secrets(line)
        assert "abc123" not in redacted
        assert "[REDACTED]" in redacted

    def test_bearer_prefix_survives_for_context(self):
        redacted = redact_secrets("Authorization: Bearer sometoken123")
        assert redacted.startswith("Authorization: ")


class TestJWT:
    def test_jwt_shaped_string_is_masked(self):
        jwt = (
            "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0."
            "dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PYVsLquNPYJI"
        )
        line = f"session token={jwt} accepted"
        redacted = redact_secrets(line)
        assert jwt not in redacted
        assert "[REDACTED]" in redacted

    def test_jwt_inside_cookie_header_is_masked(self):
        jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0In0.abcdefghijklmnopqrstuvwxyz012345"
        redacted = redact_secrets(f"Cookie: session={jwt}")
        assert jwt not in redacted


class TestAWSAccessKey:
    def test_akia_key_is_masked(self):
        redacted = redact_secrets("using AWS key AKIAABCDEFGHIJKLMNOP for upload")
        assert "AKIAABCDEFGHIJKLMNOP" not in redacted
        assert "[REDACTED]" in redacted

    def test_asia_temporary_key_is_masked(self):
        redacted = redact_secrets("temp credentials ASIAABCDEFGHIJKLMNOP in use")
        assert "ASIAABCDEFGHIJKLMNOP" not in redacted


class TestPasswordAndSecretKeyValue:
    def test_password_equals_is_masked(self):
        redacted = redact_secrets("connecting with password=hunter2")
        assert "hunter2" not in redacted
        assert "password=[REDACTED]" in redacted

    def test_secret_colon_is_masked(self):
        redacted = redact_secrets("config secret: topsecretvalue123")
        assert "topsecretvalue123" not in redacted

    def test_token_query_style_is_masked(self):
        redacted = redact_secrets("GET /api?token=abcdef123456 200")
        assert "abcdef123456" not in redacted

    def test_api_key_header_style_is_masked(self):
        redacted = redact_secrets("X-Api-Key: sk_live_abcdefghijklmnop")
        assert "sk_live_abcdefghijklmnop" not in redacted

    def test_access_key_is_masked(self):
        redacted = redact_secrets("access_key=AKIAXXXXXXXXXXXXXXXX")
        assert "AKIAXXXXXXXXXXXXXXXX" not in redacted


class TestDatabaseUrlWithCredentials:
    def test_postgres_url_credentials_masked_host_preserved(self):
        redacted = redact_secrets("connecting to postgres://myuser:supersecret123@db.internal:5432/appdb")
        assert "supersecret123" not in redacted
        assert "myuser" not in redacted
        assert "db.internal:5432/appdb" in redacted  # host/path -- not secret -- stays legible

    def test_mongodb_srv_url_credentials_masked(self):
        redacted = redact_secrets("mongodb+srv://admin:p@ssW0rd@cluster0.example.mongodb.net/mydb")
        assert "p@ssW0rd" not in redacted
        assert "cluster0.example.mongodb.net" in redacted


class TestInnocentStringsUnaffected:
    def test_plain_startup_line_is_unchanged(self):
        line = "service started successfully on port 8080"
        assert redact_secrets(line) == line

    def test_plain_error_line_without_secrets_is_unchanged(self):
        line = "ERROR: could not resolve hostname db.internal"
        assert redact_secrets(line) == line

    def test_numbers_and_normal_punctuation_survive(self):
        line = "request completed in 42ms, status=200, retries=0"
        assert redact_secrets(line) == line

    def test_idempotent_on_already_redacted_text(self):
        once = redact_secrets("password=hunter2")
        twice = redact_secrets(once)
        assert once == twice
