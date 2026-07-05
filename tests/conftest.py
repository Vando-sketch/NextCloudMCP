"""Shared pytest fixtures."""

from __future__ import annotations

import pytest

from nextcloud_task_mcp.config import Settings


@pytest.fixture
def settings() -> Settings:
    """A Settings instance with dummy values, no environment variables required."""
    return Settings(
        caldav_url="https://cloud.example.com/remote.php/dav/",
        caldav_username="testuser",
        caldav_password="testpass",
        auth_token="testtoken",
        host="127.0.0.1",
        port=8000,
    )
