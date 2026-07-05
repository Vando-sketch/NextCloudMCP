"""Environment-based configuration for the server."""

from __future__ import annotations

import os
from dataclasses import dataclass


class ConfigError(RuntimeError):
    """Raised when required environment variables are missing or invalid."""


@dataclass(frozen=True)
class Settings:
    """Runtime configuration, always read from environment variables."""

    caldav_url: str
    caldav_username: str
    caldav_password: str
    auth_token: str
    host: str
    port: int

    @classmethod
    def from_env(cls) -> Settings:
        """Build settings from environment variables, raising ConfigError if invalid."""

        def require(name: str) -> str:
            value = os.environ.get(name, "").strip()
            if not value:
                raise ConfigError(f"Missing required environment variable: {name}")
            return value

        port_raw = os.environ.get("MCP_PORT", "8000")
        try:
            port = int(port_raw)
        except ValueError as exc:
            raise ConfigError(f"MCP_PORT must be an integer, got: {port_raw!r}") from exc

        return cls(
            caldav_url=require("NEXTCLOUD_CALDAV_URL"),
            caldav_username=require("NEXTCLOUD_USERNAME"),
            caldav_password=require("NEXTCLOUD_APP_PASSWORD"),
            auth_token=require("MCP_AUTH_TOKEN"),
            host=os.environ.get("MCP_HOST", "127.0.0.1"),
            port=port,
        )
