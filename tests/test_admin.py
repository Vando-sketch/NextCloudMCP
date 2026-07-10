"""Tests for the nextcloud-task-mcp-admin operator CLI (D5).

`admin.py` is plain project code (unlike personal_auth.py) - normal ruff,
mypy and coverage rules apply. These tests run it against a real
oauth_tokens.json produced by PersonalAuthProvider (via the shared
conftest.py OAuth helpers), not a hand-written fixture, so they also exercise
that the state-file schema `admin.py` assumes actually matches what
PersonalAuthProvider writes.
"""

from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest
from conftest import TEST_OAUTH_PASSWORD, issue_token, run_async

from nextcloud_task_mcp import admin
from nextcloud_task_mcp.personal_auth import PersonalAuthProvider


def _state_dir(tmp_path: Path) -> Path:
    return tmp_path / "oauth-state"


def _seed_one_token(tmp_path: Path) -> tuple[Path, str, str]:
    """Issue one access/refresh token pair via a real provider and return
    (state_dir, access_token, refresh_token)."""

    async def scenario():
        provider = PersonalAuthProvider(
            base_url="https://test.example.com",
            password=TEST_OAUTH_PASSWORD,
            state_dir=str(_state_dir(tmp_path)),
        )
        _, token = await issue_token(provider)
        return token

    token = run_async(scenario())
    assert token.refresh_token is not None
    return _state_dir(tmp_path), token.access_token, token.refresh_token


# --- list ---


def test_list_reports_no_tokens_when_state_file_absent(tmp_path, capsys):
    exit_code = admin.main(["--state-dir", str(_state_dir(tmp_path)), "list"])
    out = capsys.readouterr().out

    assert exit_code == 0
    assert "No tokens issued." in out


def test_list_shows_access_and_refresh_tokens_from_a_real_provider(tmp_path, capsys):
    state_dir, access_token, refresh_token = _seed_one_token(tmp_path)

    exit_code = admin.main(["--state-dir", str(state_dir), "list"])
    out = capsys.readouterr().out

    assert exit_code == 0
    assert "Access tokens (1)" in out
    assert "Refresh tokens (1)" in out
    assert access_token[:16] in out
    assert refresh_token[:16] in out
    # Full tokens must never be printed unabridged - only the truncated prefix.
    assert access_token not in out
    assert refresh_token not in out


# --- revoke ---


def test_revoke_by_access_token_prefix_removes_both_tokens(tmp_path, capsys):
    state_dir, access_token, refresh_token = _seed_one_token(tmp_path)

    exit_code = admin.main(["--state-dir", str(state_dir), "revoke", access_token[:12]])
    out = capsys.readouterr().out

    assert exit_code == 0
    assert "Revoked" in out

    data = json.loads((state_dir / "oauth_tokens.json").read_text())
    assert access_token not in data["access_tokens"]
    assert refresh_token not in data["refresh_tokens"]
    assert access_token not in data["a2r"]
    assert refresh_token not in data["r2a"]


def test_revoke_by_refresh_token_prefix_removes_both_tokens(tmp_path, capsys):
    state_dir, access_token, refresh_token = _seed_one_token(tmp_path)

    exit_code = admin.main(["--state-dir", str(state_dir), "revoke", refresh_token[:12]])
    capsys.readouterr()

    assert exit_code == 0
    data = json.loads((state_dir / "oauth_tokens.json").read_text())
    assert access_token not in data["access_tokens"]
    assert refresh_token not in data["refresh_tokens"]


def test_revoke_unknown_prefix_returns_error_and_leaves_state_untouched(tmp_path, capsys):
    state_dir, access_token, refresh_token = _seed_one_token(tmp_path)
    before = (state_dir / "oauth_tokens.json").read_text()

    exit_code = admin.main(["--state-dir", str(state_dir), "revoke", "not-a-real-prefix"])
    err = capsys.readouterr().err

    assert exit_code == 1
    assert "No token matching prefix" in err
    assert (state_dir / "oauth_tokens.json").read_text() == before


def test_revoke_full_token_value_also_matches(tmp_path, capsys):
    # A prefix search naturally also matches the complete token string.
    state_dir, access_token, _refresh_token = _seed_one_token(tmp_path)

    exit_code = admin.main(["--state-dir", str(state_dir), "revoke", access_token])
    capsys.readouterr()

    assert exit_code == 0
    data = json.loads((state_dir / "oauth_tokens.json").read_text())
    assert access_token not in data["access_tokens"]


def test_revoke_preserves_0o600_state_file_permissions(tmp_path, capsys):
    state_dir, access_token, _refresh_token = _seed_one_token(tmp_path)

    admin.main(["--state-dir", str(state_dir), "revoke", access_token[:12]])
    capsys.readouterr()

    state_file = state_dir / "oauth_tokens.json"
    assert stat.S_IMODE(state_file.stat().st_mode) == 0o600


def test_revoke_does_not_disturb_other_clients_tokens(tmp_path, capsys):
    async def scenario():
        provider = PersonalAuthProvider(
            base_url="https://test.example.com",
            password=TEST_OAUTH_PASSWORD,
            state_dir=str(_state_dir(tmp_path)),
        )
        _, token_a = await issue_token(provider)
        _, token_b = await issue_token(provider)
        return token_a, token_b

    token_a, token_b = run_async(scenario())

    exit_code = admin.main(
        ["--state-dir", str(_state_dir(tmp_path)), "revoke", token_a.access_token[:12]]
    )
    capsys.readouterr()

    assert exit_code == 0
    data = json.loads((_state_dir(tmp_path) / "oauth_tokens.json").read_text())
    assert token_a.access_token not in data["access_tokens"]
    assert token_b.access_token in data["access_tokens"]
    assert token_b.refresh_token in data["refresh_tokens"]


# --- state-dir resolution ---


def test_state_dir_flag_overrides_env_var(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("MCP_OAUTH_STATE_DIR", str(tmp_path / "env-dir"))
    flag_dir = tmp_path / "flag-dir"
    flag_dir.mkdir()

    exit_code = admin.main(["--state-dir", str(flag_dir), "list"])
    out = capsys.readouterr().out

    assert exit_code == 0
    assert str(flag_dir) in out


def test_state_dir_defaults_to_env_var_when_flag_omitted(tmp_path, monkeypatch, capsys):
    env_dir = tmp_path / "env-dir"
    monkeypatch.setenv("MCP_OAUTH_STATE_DIR", str(env_dir))

    exit_code = admin.main(["list"])
    out = capsys.readouterr().out

    assert exit_code == 0
    assert str(env_dir) in out


def test_no_command_is_a_usage_error(capsys):
    with pytest.raises(SystemExit):
        admin.main([])


# --- load_state / save_state helpers directly ---


def test_load_state_of_missing_file_is_well_formed_empty_state(tmp_path):
    data = admin.load_state(admin.state_file_path(str(tmp_path / "nope")))
    assert data == {
        "clients": {},
        "access_tokens": {},
        "refresh_tokens": {},
        "a2r": {},
        "r2a": {},
    }


def test_load_state_fills_in_missing_keys(tmp_path):
    state_file = tmp_path / "oauth_tokens.json"
    state_file.write_text(json.dumps({"access_tokens": {"tok": {"client_id": "c"}}}))

    data = admin.load_state(state_file)
    assert data["access_tokens"] == {"tok": {"client_id": "c"}}
    assert data["refresh_tokens"] == {}
    assert data["a2r"] == {}
