"""Unit tests for Settings' OAuth-password-required-for-public-deployments rule."""

from __future__ import annotations

import pytest

from nextcloud_task_mcp.config import ConfigError, Settings


def _settings(**overrides) -> Settings:
    defaults = dict(
        caldav_url="https://cloud.example.com/remote.php/dav/",
        caldav_username="testuser",
        caldav_password="testpass",
        public_base_url="http://127.0.0.1:8000",
        oauth_password=None,
        oauth_state_dir=".oauth-state-test",
        oauth_allowed_redirect_domains=None,
        oauth_access_token_expiry_seconds=30 * 24 * 60 * 60,
        host="127.0.0.1",
        port=8000,
        allow_insecure_http=False,
    )
    defaults.update(overrides)
    return Settings(**defaults)


def test_local_base_url_does_not_require_password():
    _settings(public_base_url="http://127.0.0.1:8000", oauth_password=None)
    _settings(public_base_url="http://localhost:8000", oauth_password=None)


def test_public_base_url_without_password_is_rejected():
    with pytest.raises(ConfigError, match="MCP_OAUTH_PASSWORD"):
        _settings(public_base_url="https://my-host.my-tailnet.ts.net", oauth_password=None)


def test_public_base_url_with_password_is_accepted():
    _settings(public_base_url="https://my-host.my-tailnet.ts.net", oauth_password="secret")


def test_public_base_url_with_empty_string_password_is_rejected():
    # Regression test: the check must use truthiness, not `is None` - an empty
    # string is not a real password and must not silently satisfy the gate.
    with pytest.raises(ConfigError, match="MCP_OAUTH_PASSWORD"):
        _settings(public_base_url="https://my-host.my-tailnet.ts.net", oauth_password="")


def test_placeholder_password_is_rejected_even_when_local():
    # D1: the exact placeholder shipped (commented out) in .env.example must never
    # be accepted, regardless of host - a copy-paste deploy must not silently run
    # with a password that is public knowledge.
    with pytest.raises(ConfigError, match="placeholder"):
        _settings(
            public_base_url="http://127.0.0.1:8000",
            host="127.0.0.1",
            oauth_password="change-me-to-a-long-random-password",
        )


def test_placeholder_password_is_rejected_when_public():
    with pytest.raises(ConfigError, match="placeholder"):
        _settings(
            public_base_url="https://my-host.my-tailnet.ts.net",
            oauth_password="change-me-to-a-long-random-password",
        )


def test_password_required_when_bind_host_is_0_0_0_0_even_with_local_public_base_url():
    # D3: MCP_HOST=0.0.0.0 with a stale localhost PUBLIC_BASE_URL is a common
    # Docker mistake - the previous gate only looked at PUBLIC_BASE_URL and missed
    # this case entirely.
    with pytest.raises(ConfigError, match="MCP_OAUTH_PASSWORD"):
        _settings(
            public_base_url="http://127.0.0.1:8000",
            host="0.0.0.0",
            oauth_password=None,
        )


def test_password_not_required_when_both_public_base_url_and_host_are_local():
    _settings(public_base_url="http://127.0.0.1:8000", host="127.0.0.1", oauth_password=None)
    _settings(public_base_url="http://localhost:8000", host="localhost", oauth_password=None)


def test_password_not_required_when_bind_host_is_empty():
    # An empty MCP_HOST isn't a real-world value, but must not be treated as a
    # non-local bind (that would demand a password nobody asked for locally).
    _settings(public_base_url="http://127.0.0.1:8000", host="", oauth_password=None)


def test_password_required_when_bind_host_is_public_even_with_local_public_base_url():
    with pytest.raises(ConfigError, match="MCP_OAUTH_PASSWORD"):
        _settings(
            public_base_url="http://127.0.0.1:8000",
            host="203.0.113.5",
            oauth_password=None,
        )


# --- NEXTCLOUD_CALDAV_URL scheme enforcement (D8) ---


def test_http_caldav_url_rejected_for_non_local_host():
    with pytest.raises(ConfigError, match="https"):
        _settings(caldav_url="http://cloud.example.com/remote.php/dav/")


def test_http_caldav_url_allowed_for_localhost():
    _settings(caldav_url="http://localhost:8080/remote.php/dav/")
    _settings(caldav_url="http://127.0.0.1:8080/remote.php/dav/")
    _settings(caldav_url="http://[::1]:8080/remote.php/dav/")


def test_http_caldav_url_allowed_with_escape_hatch():
    _settings(
        caldav_url="http://cloud.example.com/remote.php/dav/",
        allow_insecure_http=True,
    )


def test_https_caldav_url_always_allowed():
    _settings(caldav_url="https://cloud.example.com/remote.php/dav/", allow_insecure_http=False)
