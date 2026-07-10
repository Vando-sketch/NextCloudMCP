"""Operator CLI for inspecting and revoking OAuth tokens (D5).

`PersonalAuthProvider` (see nextcloud_task_mcp.personal_auth, vendored) has no
built-in operator-facing revocation path: once a client is authorized, the
only way to invalidate its tokens was to delete the whole state file (losing
every other client's session too) or edit oauth_tokens.json by hand. This
module gives an operator a small, dependency-free way to list issued tokens
and revoke one (and its paired access/refresh token) without either of those.

Deliberately NOT vendored: it doesn't touch PersonalAuthProvider's internals
or import fastmcp/mcp at all, only stdlib (argparse + json) - it reads and
rewrites oauth_tokens.json directly, using the exact schema
`PersonalAuthProvider._save_state`/`_load_state` (personal_auth.py) produce
and consume: a JSON object with "clients", "access_tokens", "refresh_tokens",
"a2r" (access-token -> refresh-token) and "r2a" (refresh-token -> access-token)
keys. If that schema changes upstream, this module needs to change with it -
there is no shared code path enforcing they stay in sync.

Usage:
    nextcloud-task-mcp-admin list
    nextcloud-task-mcp-admin revoke <token-or-prefix>
    nextcloud-task-mcp-admin --state-dir /var/lib/nextcloud-task-mcp/oauth-state list
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import sys
from pathlib import Path
from typing import Any

#: Mirrors personal_auth.DEFAULT_STATE_DIR - duplicated rather than imported
#: so this module stays a plain stdlib script, independent of the vendored
#: file and everything it (transitively) imports (fastmcp, mcp).
DEFAULT_STATE_DIR = ".oauth-state"
STATE_FILENAME = "oauth_tokens.json"

_EMPTY_STATE: dict[str, dict[str, Any]] = {
    "clients": {},
    "access_tokens": {},
    "refresh_tokens": {},
    "a2r": {},
    "r2a": {},
}


def state_file_path(state_dir: str) -> Path:
    """The oauth_tokens.json path for a given state directory."""
    return Path(state_dir) / STATE_FILENAME


def load_state(state_file: Path) -> dict[str, Any]:
    """Load and lightly normalize oauth_tokens.json.

    Returns an empty-but-well-formed state dict if the file doesn't exist yet
    (nothing has been issued) rather than raising, so `list`/`revoke` behave
    predictably on a fresh deployment.
    """
    if not state_file.exists():
        return {key: dict(value) for key, value in _EMPTY_STATE.items()}
    data = json.loads(state_file.read_text())
    for key, default in _EMPTY_STATE.items():
        data.setdefault(key, dict(default))
    return data


def save_state(state_file: Path, data: dict[str, Any]) -> None:
    """Write oauth_tokens.json back with 0o600 permissions - it holds
    plaintext bearer/refresh tokens, matching PersonalAuthProvider's own
    _save_state (personal_auth.py LOCAL PATCH 3)."""
    state_file.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    # mkdir's mode= is masked by the umask and is a no-op if the directory
    # already existed (the common case here - PersonalAuthProvider normally
    # created it first), so chmod explicitly too, mirroring personal_auth.py
    # LOCAL PATCH 3.
    os.chmod(state_file.parent, 0o700)
    fd = os.open(state_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(json.dumps(data, indent=2))
    os.chmod(state_file, 0o600)


def _truncate(token: str, length: int = 16) -> str:
    return token if len(token) <= length else f"{token[:length]}..."


def _format_expiry(expires_at: float | int | None) -> str:
    if expires_at is None:
        return "never"
    return datetime.datetime.fromtimestamp(expires_at, tz=datetime.timezone.utc).isoformat()


def _format_token_line(prefix: str, token: str, info: dict[str, Any]) -> str:
    client_id = info.get("client_id", "?")
    expiry = _format_expiry(info.get("expires_at"))
    return f"  [{prefix}] {_truncate(token)}  client={client_id}  expires={expiry}"


def cmd_list(args: argparse.Namespace) -> int:
    state_file = state_file_path(args.state_dir)
    data = load_state(state_file)
    access_tokens: dict[str, Any] = data["access_tokens"]
    refresh_tokens: dict[str, Any] = data["refresh_tokens"]

    print(f"State file: {state_file}")
    if not access_tokens and not refresh_tokens:
        print("No tokens issued.")
        return 0

    print(f"\nAccess tokens ({len(access_tokens)}):")
    for token, info in access_tokens.items():
        print(_format_token_line("access", token, info))

    print(f"\nRefresh tokens ({len(refresh_tokens)}):")
    for token, info in refresh_tokens.items():
        print(_format_token_line("refresh", token, info))

    return 0


def cmd_revoke(args: argparse.Namespace) -> int:
    state_file = state_file_path(args.state_dir)
    data = load_state(state_file)
    prefix: str = args.token_prefix

    access_tokens: dict[str, Any] = data["access_tokens"]
    refresh_tokens: dict[str, Any] = data["refresh_tokens"]
    a2r: dict[str, str] = data["a2r"]
    r2a: dict[str, str] = data["r2a"]

    matched_access = [token for token in access_tokens if token.startswith(prefix)]
    matched_refresh = [token for token in refresh_tokens if token.startswith(prefix)]

    if not matched_access and not matched_refresh:
        print(f"No token matching prefix {prefix!r} found in {state_file}", file=sys.stderr)
        return 1

    removed: list[str] = []

    for access_token in matched_access:
        del access_tokens[access_token]
        removed.append(access_token)
        paired_refresh = a2r.pop(access_token, None)
        if paired_refresh is not None:
            refresh_tokens.pop(paired_refresh, None)
            r2a.pop(paired_refresh, None)
            removed.append(paired_refresh)

    for refresh_token in matched_refresh:
        if refresh_token not in refresh_tokens:
            continue  # already removed above via its paired access token
        del refresh_tokens[refresh_token]
        removed.append(refresh_token)
        paired_access = r2a.pop(refresh_token, None)
        if paired_access is not None:
            access_tokens.pop(paired_access, None)
            a2r.pop(paired_access, None)
            removed.append(paired_access)

    save_state(state_file, data)

    for token in removed:
        print(f"Revoked {_truncate(token)}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="nextcloud-task-mcp-admin",
        description=(
            "Operator CLI for inspecting and revoking OAuth tokens issued by "
            "nextcloud-task-mcp's PersonalAuthProvider."
        ),
    )
    parser.add_argument(
        "--state-dir",
        default=os.environ.get("MCP_OAUTH_STATE_DIR", DEFAULT_STATE_DIR),
        help=(
            "Directory holding oauth_tokens.json. Defaults to "
            "$MCP_OAUTH_STATE_DIR, or "
            f"{DEFAULT_STATE_DIR!r} if that's unset."
        ),
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    list_parser = subparsers.add_parser("list", help="List issued access/refresh tokens.")
    list_parser.set_defaults(func=cmd_list)

    revoke_parser = subparsers.add_parser(
        "revoke", help="Revoke a token (and its paired access/refresh token) by prefix."
    )
    revoke_parser.add_argument(
        "token_prefix",
        help=(
            "Prefix (or the full value) of the access or refresh token to "
            "revoke, as printed by `list`."
        ),
    )
    revoke_parser.set_defaults(func=cmd_revoke)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
