"""Milestone 16 -- `argus agents` / `argus agents inspect` /
`argus agents add`. Invoked through `main.main()`, exactly like every
other command's own test file."""

from __future__ import annotations

import contextlib
import io
import json
from pathlib import Path

from argus.cli import main as main_module
from argus.store.database import open_database
from argus.store.repository import Repository


def run_cli(db_path: Path, *args: str) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = main_module.main(["--database", str(db_path), *args])
    return code, out.getvalue(), err.getvalue()


class TestAgentsAdd:
    def test_registers_a_host_and_prints_the_token_once(self, tmp_path):
        db_path = tmp_path / "a.db"
        code, out, _ = run_cli(db_path, "agents", "add", "dell-latitude-5400", "--name", "Ubuntu Dell")
        assert code == 0
        assert "ARGUS_AGENT_TOKEN=" in out
        assert "ARGUS_AGENT_ID=" in out
        assert "ARGUS_HOST_KEY=dell-latitude-5400" in out

    def test_token_is_stored_only_as_a_hash(self, tmp_path):
        db_path = tmp_path / "a.db"
        code, out, _ = run_cli(db_path, "agents", "add", "dell", "--name", "Dell")
        token_line = next(line for line in out.splitlines() if line.strip().startswith("ARGUS_AGENT_TOKEN="))
        plaintext_token = token_line.split("=", 1)[1]

        conn = open_database(db_path)
        try:
            host = Repository(conn).get_host_by_key("dell")
        finally:
            conn.close()
        assert host is not None
        assert host.agent_token_hash != plaintext_token
        assert plaintext_token not in (host.agent_token_hash or "")

    def test_duplicate_host_key_fails_cleanly(self, tmp_path):
        db_path = tmp_path / "a.db"
        run_cli(db_path, "agents", "add", "dell", "--name", "Dell")
        code, out, _ = run_cli(db_path, "agents", "add", "dell", "--name", "Dell Again")
        assert code == 1
        assert "already registered" in out.lower() or "could not register" in out.lower()


class TestAgentsList:
    def test_lists_local_and_registered_hosts(self, tmp_path):
        db_path = tmp_path / "a.db"
        run_cli(db_path, "agents", "add", "dell", "--name", "Ubuntu Dell")
        code, out, _ = run_cli(db_path, "agents")
        assert code == 0
        assert "Ubuntu Dell" in out
        assert "Local Host" in out

    def test_json_output_never_includes_the_token(self, tmp_path):
        db_path = tmp_path / "a.db"
        run_cli(db_path, "agents", "add", "dell", "--name", "Ubuntu Dell")
        code, out, _ = run_cli(db_path, "agents", "--json")
        payload = json.loads(out)
        assert "hosts" in payload
        text = out
        assert "agent_token_hash" not in text
        assert "token" not in text.lower()


class TestAgentsInspect:
    def test_inspect_known_host(self, tmp_path):
        db_path = tmp_path / "a.db"
        run_cli(db_path, "agents", "add", "dell", "--name", "Ubuntu Dell")
        code, out, _ = run_cli(db_path, "agents", "inspect", "dell")
        assert code == 0
        assert "Ubuntu Dell" in out

    def test_inspect_unknown_host_fails_cleanly(self, tmp_path):
        db_path = tmp_path / "a.db"
        code, out, _ = run_cli(db_path, "agents", "inspect", "nonexistent")
        assert code == 1
        assert "no host found" in out.lower()
