"""Unit-level tests for PersonalAuthProvider's token lifecycle (E2, E3) and its
bounded refresh-token expiry (D5).

Unlike tests/test_auth.py (which drives the flow through the real ASGI app,
since that's the only way to exercise `/authorize`'s redirect-based error
handling), these tests instantiate `PersonalAuthProvider` directly and call
its async methods (`authorize`, `exchange_authorization_code`,
`exchange_refresh_token`, `load_access_token`, `load_refresh_token`,
`revoke_token`) the way the framework does internally - see
tests/conftest.py's `register_oauth_client` / `authorize_and_get_code` /
`issue_token` helpers, reused here and in tests/test_admin.py.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path

import pytest
from conftest import (
    TEST_OAUTH_PASSWORD,
    authorize_and_get_code,
    issue_token,
    register_oauth_client,
    run_async,
)
from mcp.server.auth.provider import AuthorizeError, TokenError

from nextcloud_task_mcp.personal_auth import PersonalAuthProvider


def _provider(tmp_path: Path, **overrides) -> PersonalAuthProvider:
    defaults: dict = dict(
        base_url="https://test.example.com",
        password=TEST_OAUTH_PASSWORD,
        state_dir=str(tmp_path / "oauth-state"),
    )
    defaults.update(overrides)
    return PersonalAuthProvider(**defaults)


# --- Authorization-code exchange happy path (E2) ---


def test_exchange_authorization_code_returns_access_and_refresh_token(tmp_path):
    async def scenario():
        provider = _provider(tmp_path, access_token_expiry_seconds=3600)
        client, token = await issue_token(provider, state=TEST_OAUTH_PASSWORD)

        assert token.access_token
        assert token.refresh_token
        assert token.token_type == "Bearer"
        assert token.expires_in == 3600

        loaded = await provider.load_access_token(token.access_token)
        assert loaded is not None
        assert loaded.token == token.access_token
        assert loaded.client_id == client.client_id

    run_async(scenario())


# --- Replay protection (E2) ---


def test_replayed_authorization_code_is_rejected(tmp_path):
    async def scenario():
        provider = _provider(tmp_path)
        client = await register_oauth_client(provider)
        auth_code = await authorize_and_get_code(provider, client, state=TEST_OAUTH_PASSWORD)

        await provider.exchange_authorization_code(client, auth_code)

        with pytest.raises(TokenError) as exc_info:
            await provider.exchange_authorization_code(client, auth_code)
        assert exc_info.value.error == "invalid_grant"

    run_async(scenario())


# --- Access-token expiry (E2) ---


def test_expired_access_token_is_rejected_by_load_access_token(tmp_path):
    async def scenario():
        # A negative expiry means `expires_at` is already in the past the
        # instant the token is minted - no time-travel/monkeypatching needed.
        provider = _provider(tmp_path, access_token_expiry_seconds=-1)
        _, token = await issue_token(provider, state=TEST_OAUTH_PASSWORD)

        assert await provider.load_access_token(token.access_token) is None
        # InMemoryOAuthProvider.load_access_token also cleans up expired
        # tokens on the way out.
        assert token.access_token not in provider.access_tokens

    run_async(scenario())


def test_non_expired_access_token_is_accepted(tmp_path):
    async def scenario():
        provider = _provider(tmp_path, access_token_expiry_seconds=3600)
        _, token = await issue_token(provider, state=TEST_OAUTH_PASSWORD)

        assert await provider.load_access_token(token.access_token) is not None

    run_async(scenario())


# --- Refresh-token exchange (E2) ---


def test_refresh_token_exchange_returns_fresh_access_token(tmp_path):
    async def scenario():
        provider = _provider(tmp_path)
        client, token = await issue_token(provider, state=TEST_OAUTH_PASSWORD)
        assert token.refresh_token is not None
        refresh_token: str = token.refresh_token

        loaded_refresh = await provider.load_refresh_token(client, refresh_token)
        assert loaded_refresh is not None

        new_token = await provider.exchange_refresh_token(client, loaded_refresh, scopes=[])

        assert new_token.access_token != token.access_token
        assert await provider.load_access_token(new_token.access_token) is not None
        # Rotation revokes the old access token (upstream InMemoryOAuthProvider
        # behavior, unmodified by any local patch).
        assert await provider.load_access_token(token.access_token) is None

    run_async(scenario())


# --- Revocation (E2) ---


def test_revoke_token_invalidates_access_token(tmp_path):
    async def scenario():
        provider = _provider(tmp_path)
        _, token = await issue_token(provider, state=TEST_OAUTH_PASSWORD)

        access_token_obj = provider.access_tokens[token.access_token]
        await provider.revoke_token(access_token_obj)

        assert await provider.load_access_token(token.access_token) is None

    run_async(scenario())


def test_revoke_token_invalidates_paired_refresh_token(tmp_path):
    async def scenario():
        provider = _provider(tmp_path)
        client, token = await issue_token(provider, state=TEST_OAUTH_PASSWORD)
        assert token.refresh_token is not None
        refresh_token: str = token.refresh_token

        access_token_obj = provider.access_tokens[token.access_token]
        await provider.revoke_token(access_token_obj)

        assert await provider.load_refresh_token(client, refresh_token) is None

    run_async(scenario())


# --- Persistence across provider instances (E3) ---


def test_state_survives_a_second_provider_on_the_same_state_dir(tmp_path):
    async def scenario():
        state_dir = tmp_path / "oauth-state"
        provider1 = _provider(tmp_path, state_dir=str(state_dir))
        client, token = await issue_token(provider1, state=TEST_OAUTH_PASSWORD)
        assert token.refresh_token is not None
        refresh_token: str = token.refresh_token

        provider2 = _provider(tmp_path, state_dir=str(state_dir))

        assert client.client_id in provider2.clients
        loaded = await provider2.load_access_token(token.access_token)
        assert loaded is not None
        assert loaded.client_id == client.client_id

        loaded_refresh = await provider2.load_refresh_token(client, refresh_token)
        assert loaded_refresh is not None

        # a2r/r2a maps also survive.
        assert provider2._access_to_refresh_map[token.access_token] == refresh_token
        assert provider2._refresh_to_access_map[refresh_token] == token.access_token

    run_async(scenario())


def test_corrupt_state_file_logs_warning_and_starts_empty(tmp_path, caplog):
    state_dir = tmp_path / "oauth-state"
    state_dir.mkdir(parents=True)
    (state_dir / "oauth_tokens.json").write_text("{ this is not valid json !!")

    with caplog.at_level(logging.WARNING, logger="personal-auth"):
        provider = _provider(tmp_path, state_dir=str(state_dir))

    assert provider.clients == {}
    assert provider.access_tokens == {}
    assert provider.refresh_tokens == {}
    assert any("Failed to load OAuth state" in record.message for record in caplog.records)


def test_provider_starts_empty_when_state_file_absent(tmp_path):
    provider = _provider(tmp_path, state_dir=str(tmp_path / "does-not-exist-yet"))
    assert provider.clients == {}
    assert provider.access_tokens == {}
    assert provider.refresh_tokens == {}


# --- Bounded refresh-token lifetime (D5, personal_auth.py LOCAL PATCH 4) ---


def test_refresh_token_gets_bounded_expiry_by_default(tmp_path):
    async def scenario():
        before = int(time.time())
        provider = _provider(tmp_path, refresh_token_expiry_seconds=1000)
        _, token = await issue_token(provider, state=TEST_OAUTH_PASSWORD)
        assert token.refresh_token is not None

        refresh_obj = provider.refresh_tokens[token.refresh_token]
        assert refresh_obj.expires_at is not None
        assert before + 1000 <= refresh_obj.expires_at <= int(time.time()) + 1000 + 2

    run_async(scenario())


def test_refresh_token_expiry_seconds_none_keeps_unbounded_refresh_token(tmp_path):
    async def scenario():
        provider = _provider(tmp_path, refresh_token_expiry_seconds=None)
        _, token = await issue_token(provider, state=TEST_OAUTH_PASSWORD)
        assert token.refresh_token is not None

        refresh_obj = provider.refresh_tokens[token.refresh_token]
        assert refresh_obj.expires_at is None

    run_async(scenario())


def test_expired_refresh_token_is_rejected_by_load_refresh_token(tmp_path):
    # Confirms the finding recorded in personal_auth.py's LOCAL PATCH 4 note:
    # InMemoryOAuthProvider.load_refresh_token (inherited unmodified) already
    # enforces whatever expires_at a RefreshToken carries - no override of
    # load_refresh_token itself was needed, only of the rotation path in
    # exchange_refresh_token (covered separately below).
    async def scenario():
        provider = _provider(tmp_path, refresh_token_expiry_seconds=-1)
        client, token = await issue_token(provider, state=TEST_OAUTH_PASSWORD)
        assert token.refresh_token is not None
        refresh_token: str = token.refresh_token

        assert await provider.load_refresh_token(client, refresh_token) is None
        assert refresh_token not in provider.refresh_tokens

    run_async(scenario())


def test_refresh_token_rotation_preserves_bounded_expiry(tmp_path):
    # Regression test for the specific gap LOCAL PATCH 4 closes:
    # InMemoryOAuthProvider.exchange_refresh_token mints its *replacement*
    # refresh token with expires_at=None unconditionally (it uses its own
    # module-level DEFAULT_REFRESH_TOKEN_EXPIRY_SECONDS=None, not this
    # class's refresh_token_expiry_seconds) - without PersonalAuthProvider's
    # override re-stamping it, a bounded lifetime would only ever apply to
    # the very first refresh token, and every rotation would silently regain
    # an unbounded one.
    async def scenario():
        provider = _provider(tmp_path, refresh_token_expiry_seconds=1000)
        client, token = await issue_token(provider, state=TEST_OAUTH_PASSWORD)
        assert token.refresh_token is not None

        loaded_refresh = await provider.load_refresh_token(client, token.refresh_token)
        assert loaded_refresh is not None
        new_token = await provider.exchange_refresh_token(client, loaded_refresh, scopes=[])
        assert new_token.refresh_token is not None

        rotated_refresh_obj = provider.refresh_tokens[new_token.refresh_token]
        assert rotated_refresh_obj.expires_at is not None
        assert rotated_refresh_obj.expires_at <= int(time.time()) + 1000 + 2

    run_async(scenario())


def test_default_refresh_token_expiry_is_180_days(tmp_path):
    from nextcloud_task_mcp.personal_auth import DEFAULT_REFRESH_TOKEN_EXPIRY

    assert DEFAULT_REFRESH_TOKEN_EXPIRY == 180 * 24 * 60 * 60

    async def scenario():
        before = int(time.time())
        provider = _provider(tmp_path)  # uses the constructor default
        _, token = await issue_token(provider, state=TEST_OAUTH_PASSWORD)
        assert token.refresh_token is not None

        refresh_obj = provider.refresh_tokens[token.refresh_token]
        assert refresh_obj.expires_at is not None
        assert before + DEFAULT_REFRESH_TOKEN_EXPIRY <= refresh_obj.expires_at

    run_async(scenario())


# --- Password check: substring semantics pinned intentionally (D6) ---


def test_password_check_accepts_exact_state_match(tmp_path):
    async def scenario():
        provider = _provider(tmp_path)
        client = await register_oauth_client(provider)
        code = await authorize_and_get_code(provider, client, state=TEST_OAUTH_PASSWORD)
        assert code is not None

    run_async(scenario())


def test_password_check_accepts_password_as_substring_of_larger_state(tmp_path):
    # PersonalAuthProvider checks `self.password in params.state` (a substring
    # test), not equality. This is *intentional*, not a bug: claude.ai - not
    # this project - controls what value ends up in the OAuth `state`
    # parameter (it may prefix/suffix it with its own opaque data), so an
    # equality check would risk rejecting a legitimate connector flow. See
    # D2/D6 in docs/improvement-plan.md. This test pins that behavior so any
    # future change to the check (e.g. tightening to equality, or to a
    # constant-time comparison that still allows extra state) is a deliberate
    # decision, not an accidental regression.
    async def scenario():
        provider = _provider(tmp_path)
        client = await register_oauth_client(provider)
        state = f"claude-opaque-prefix-{TEST_OAUTH_PASSWORD}-claude-opaque-suffix"
        code = await authorize_and_get_code(provider, client, state=state)
        assert code is not None

    run_async(scenario())


def test_password_check_rejects_missing_state(tmp_path):
    async def scenario():
        provider = _provider(tmp_path)
        client = await register_oauth_client(provider)
        with pytest.raises(AuthorizeError):
            await authorize_and_get_code(provider, client, state=None)

    run_async(scenario())


def test_password_check_rejects_state_not_containing_password(tmp_path):
    async def scenario():
        provider = _provider(tmp_path)
        client = await register_oauth_client(provider)
        with pytest.raises(AuthorizeError):
            await authorize_and_get_code(provider, client, state="totally-unrelated-value")

    run_async(scenario())


def test_password_check_rejects_state_that_is_a_substring_of_the_password(tmp_path):
    # The converse of the pinned substring behavior above: a `state` that is
    # only a *prefix/substring* of the real password (rather than containing
    # it in full) must still be rejected - `password in state`, not
    # `state in password` or a fuzzy/partial match.
    async def scenario():
        provider = _provider(tmp_path)
        client = await register_oauth_client(provider)

        with pytest.raises(AuthorizeError):
            await authorize_and_get_code(
                provider, client, state=TEST_OAUTH_PASSWORD[: len(TEST_OAUTH_PASSWORD) // 2]
            )

    run_async(scenario())
