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
