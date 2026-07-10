"""Environment-based configuration for the server."""

from __future__ import annotations

import os
from dataclasses import dataclass
from urllib.parse import urlparse

_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}

# The exact placeholder shipped (commented out) in .env.example. If this literal
# string ever ends up as the configured MCP_OAUTH_PASSWORD, someone copy-pasted
# the example without generating a real secret - reject it unconditionally
# (independent of the public/local host gate below), so it can never
# accidentally satisfy that gate simply by being non-empty. (D1)
_PLACEHOLDER_OAUTH_PASSWORD = "change-me-to-a-long-random-password"


class ConfigError(RuntimeError):
    """Raised when required environment variables are missing or invalid."""


def is_local_hostname(hostname: str | None) -> bool:
    """True if `hostname` is a loopback/local address (localhost/127.0.0.1/::1).

    `None` (e.g. from an unparseable URL) is treated as NOT local - fail safe,
    so a malformed URL can't accidentally bypass a security gate that only
    relaxes for local addresses.
    """
    return hostname in _LOCAL_HOSTS


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
    allow_insecure_http: bool = False
    caldav_timeout_seconds: int = 30
    # Bounds how long a leaked oauth_tokens.json grants indefinite access for
    # (D5) - PersonalAuthProvider previously minted refresh tokens with
    # expires_at=None (never expire); see personal_auth.py LOCAL PATCH 4.
    oauth_refresh_token_expiry_seconds: int = 180 * 24 * 60 * 60

    def __post_init__(self) -> None:
        if self.oauth_password == _PLACEHOLDER_OAUTH_PASSWORD:
            raise ConfigError(
                "MCP_OAUTH_PASSWORD is set to the placeholder value from "
                "'.env.example' ('change-me-to-a-long-random-password') - generate a "
                "real secret instead, e.g. with: "
                'python3 -c "import secrets; print(secrets.token_urlsafe(24))". '
                "See docs/deployment.md."
            )

        # PersonalAuthProvider's /authorize has no login/consent step of its own -
        # the redirect-domain allow-list alone does not stop a scripted client from
        # registering itself and self-issuing a valid access token (it never needs
        # to actually control the redirect domain, only claim one that's on the
        # list). Once this server is reachable from anywhere but localhost - either
        # because PUBLIC_BASE_URL says so, or because it's actually bound to a
        # non-local address (MCP_HOST=0.0.0.0 with a stale localhost
        # PUBLIC_BASE_URL is a common Docker mistake, D3) - the password is the
        # only real gate, so it must be set. Enforced here (not just in from_env)
        # so it holds regardless of how Settings is constructed.
        public_base_is_local = is_local_hostname(urlparse(self.public_base_url).hostname)
        # An empty bind host isn't a value we expect in practice, but there's no
        # reason to treat it as a non-local bind - unlike PUBLIC_BASE_URL above,
        # failing safe here would just demand a password nobody asked for. The
        # dangerous cases (0.0.0.0, an explicit external interface) are still
        # non-local and still caught.
        bind_is_local = not self.host or self.host in _LOCAL_HOSTS
        if not (public_base_is_local and bind_is_local) and not self.oauth_password:
            raise ConfigError(
                "MCP_OAUTH_PASSWORD is required when PUBLIC_BASE_URL is not localhost "
                "or MCP_HOST is not a local bind address - without it, anyone who can "
                "reach this server can self-issue a valid OAuth access token. See "
                "docs/deployment.md."
            )

        # A http:// NEXTCLOUD_CALDAV_URL sends the Nextcloud app password in
        # cleartext Basic Auth on every request. Require https:// unless the URL
        # genuinely points at a local address, or the operator has explicitly
        # opted in via NEXTCLOUD_ALLOW_INSECURE_HTTP=1 (local testing only). (D8)
        parsed_caldav = urlparse(self.caldav_url)
        caldav_is_https = parsed_caldav.scheme == "https"
        caldav_is_local = is_local_hostname(parsed_caldav.hostname)
        if not caldav_is_https and not (caldav_is_local or self.allow_insecure_http):
            raise ConfigError(
                "NEXTCLOUD_CALDAV_URL must use https:// - a http:// URL sends the "
                "Nextcloud app password in cleartext Basic Auth. Use an https:// URL, "
                "or set NEXTCLOUD_ALLOW_INSECURE_HTTP=1 if NEXTCLOUD_CALDAV_URL "
                "genuinely points at a local address (localhost/127.0.0.1/::1) for "
                "testing."
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

        refresh_expiry_raw = os.environ.get(
            "MCP_OAUTH_REFRESH_TOKEN_EXPIRY_SECONDS", str(180 * 24 * 60 * 60)
        )
        try:
            oauth_refresh_token_expiry_seconds = int(refresh_expiry_raw)
        except ValueError as exc:
            raise ConfigError(
                f"MCP_OAUTH_REFRESH_TOKEN_EXPIRY_SECONDS must be an integer, got: "
                f"{refresh_expiry_raw!r}"
            ) from exc

        allowed_domains_raw = os.environ.get("MCP_OAUTH_ALLOWED_REDIRECT_DOMAINS")
        oauth_allowed_redirect_domains = (
            [domain.strip() for domain in allowed_domains_raw.split(",") if domain.strip()]
            if allowed_domains_raw is not None
            else None
        )

        allow_insecure_http = os.environ.get("NEXTCLOUD_ALLOW_INSECURE_HTTP", "").strip() == "1"

        timeout_raw = os.environ.get("NEXTCLOUD_HTTP_TIMEOUT_SECONDS", "30")
        try:
            caldav_timeout_seconds = int(timeout_raw)
        except ValueError as exc:
            raise ConfigError(
                f"NEXTCLOUD_HTTP_TIMEOUT_SECONDS must be an integer, got: {timeout_raw!r}"
            ) from exc

        return cls(
            caldav_url=require("NEXTCLOUD_CALDAV_URL"),
            caldav_username=require("NEXTCLOUD_USERNAME"),
            caldav_password=require("NEXTCLOUD_APP_PASSWORD"),
            public_base_url=require("PUBLIC_BASE_URL"),
            oauth_password=os.environ.get("MCP_OAUTH_PASSWORD", "").strip() or None,
            oauth_state_dir=os.environ.get("MCP_OAUTH_STATE_DIR", ".oauth-state"),
            oauth_allowed_redirect_domains=oauth_allowed_redirect_domains,
            oauth_access_token_expiry_seconds=oauth_access_token_expiry_seconds,
            oauth_refresh_token_expiry_seconds=oauth_refresh_token_expiry_seconds,
            host=os.environ.get("MCP_HOST", "127.0.0.1"),
            port=port,
            allow_insecure_http=allow_insecure_http,
            caldav_timeout_seconds=caldav_timeout_seconds,
        )
