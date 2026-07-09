"""Shared pytest fixtures."""

from __future__ import annotations

import pytest

from nextcloud_task_mcp.config import Settings

#: Matches the value baked into the `settings` fixture below - tests that need
#: to exercise the OAuth password gate reference this directly.
TEST_OAUTH_PASSWORD = "test-oauth-password"


@pytest.fixture
def settings(tmp_path) -> Settings:
    """A Settings instance with dummy values, no environment variables required.

    `oauth_state_dir` points at a per-test tmp_path so PersonalAuthProvider's
    token persistence never touches the repo or leaks state between tests.
    `public_base_url` is deliberately non-local (mirrors a real deployment) so
    `oauth_password` must be set too - Settings enforces that pairing itself.
    """
    return Settings(
        caldav_url="https://cloud.example.com/remote.php/dav/",
        caldav_username="testuser",
        caldav_password="testpass",
        public_base_url="https://test.example.com",
        oauth_password=TEST_OAUTH_PASSWORD,
        oauth_state_dir=str(tmp_path / "oauth-state"),
        oauth_allowed_redirect_domains=None,
        oauth_access_token_expiry_seconds=30 * 24 * 60 * 60,
        host="127.0.0.1",
        port=8000,
    )
