"""Environment-based configuration for the server."""

from __future__ import annotations

import os
from dataclasses import dataclass
from urllib.parse import urlparse

_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}


class ConfigError(RuntimeError):
    """Raised when required environment variables are missing or invalid."""


@dataclass(frozen=True)
class Settings:
    """Runtime configuration, always read from environment variables."""

    caldav_url: str
    caldav_username: str
    caldav_password: str
    public_base_url: str
    oauth_password: str | None
    oauth_state_dir: str
    oauth_allowed_redirect_domains: list[str] | None
    oauth_access_token_expiry_seconds: int
    host: str
    port: int

    def __post_init__(self) -> None:
        # PersonalAuthProvider's /authorize has no login/consent step of its own -
        # the redirect-domain allow-list alone does not stop a scripted client from
        # registering itself and self-issuing a valid access token (it never needs
        # to actually control the redirect domain, only claim one that's on the
        # list). Once this server is reachable from anywhere but localhost, the
        # password is the only real gate, so it must be set. Enforced here (not
        # just in from_env) so it holds regardless of how Settings is constructed.
        host = urlparse(self.public_base_url).hostname
        if host not in _LOCAL_HOSTS and not self.oauth_password:
            raise ConfigError(
                "MCP_OAUTH_PASSWORD is required when PUBLIC_BASE_URL is not localhost - "
                "without it, anyone who can reach this server can self-issue a valid "
                "OAuth access token. See docs/deployment.md."
            )

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

        expiry_raw = os.environ.get("MCP_OAUTH_ACCESS_TOKEN_EXPIRY_SECONDS", str(30 * 24 * 60 * 60))
        try:
            oauth_access_token_expiry_seconds = int(expiry_raw)
        except ValueError as exc:
            raise ConfigError(
                f"MCP_OAUTH_ACCESS_TOKEN_EXPIRY_SECONDS must be an integer, got: {expiry_raw!r}"
            ) from exc

        allowed_domains_raw = os.environ.get("MCP_OAUTH_ALLOWED_REDIRECT_DOMAINS")
        oauth_allowed_redirect_domains = (
            [domain.strip() for domain in allowed_domains_raw.split(",") if domain.strip()]
            if allowed_domains_raw is not None
            else None
        )

        return cls(
            caldav_url=require("NEXTCLOUD_CALDAV_URL"),
            caldav_username=require("NEXTCLOUD_USERNAME"),
            caldav_password=require("NEXTCLOUD_APP_PASSWORD"),
            public_base_url=require("PUBLIC_BASE_URL"),
            oauth_password=os.environ.get("MCP_OAUTH_PASSWORD", "").strip() or None,
            oauth_state_dir=os.environ.get("MCP_OAUTH_STATE_DIR", ".oauth-state"),
            oauth_allowed_redirect_domains=oauth_allowed_redirect_domains,
            oauth_access_token_expiry_seconds=oauth_access_token_expiry_seconds,
            host=os.environ.get("MCP_HOST", "127.0.0.1"),
            port=port,
        )
