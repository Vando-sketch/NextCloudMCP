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
    # `defaults` is a plain dict[str, <union of all the value types above>],
    # so mypy can't verify the **-unpacked kwargs against Settings' distinct
    # per-field types (a TypedDict would fix this, but isn't worth it for a
    # test-only helper with a single call site pattern).
    return Settings(**defaults)  # type: ignore[arg-type]


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


# --- NEXTCLOUD_HTTP_TIMEOUT_SECONDS (A2) ---


def test_caldav_timeout_seconds_defaults_to_30():
    settings = _settings()
    assert settings.caldav_timeout_seconds == 30


def test_caldav_timeout_seconds_accepts_custom_value():
    settings = _settings(caldav_timeout_seconds=5)
    assert settings.caldav_timeout_seconds == 5


def _set_required_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NEXTCLOUD_CALDAV_URL", "https://cloud.example.com/remote.php/dav/")
    monkeypatch.setenv("NEXTCLOUD_USERNAME", "testuser")
    monkeypatch.setenv("NEXTCLOUD_APP_PASSWORD", "testpass")
    monkeypatch.setenv("PUBLIC_BASE_URL", "http://127.0.0.1:8000")


def test_from_env_default_caldav_timeout_seconds(monkeypatch: pytest.MonkeyPatch):
    _set_required_env(monkeypatch)
    monkeypatch.delenv("NEXTCLOUD_HTTP_TIMEOUT_SECONDS", raising=False)

    settings = Settings.from_env()
    assert settings.caldav_timeout_seconds == 30


def test_from_env_reads_caldav_timeout_seconds(monkeypatch: pytest.MonkeyPatch):
    _set_required_env(monkeypatch)
    monkeypatch.setenv("NEXTCLOUD_HTTP_TIMEOUT_SECONDS", "45")

    settings = Settings.from_env()
    assert settings.caldav_timeout_seconds == 45


def test_from_env_rejects_non_integer_caldav_timeout_seconds(monkeypatch: pytest.MonkeyPatch):
    _set_required_env(monkeypatch)
    monkeypatch.setenv("NEXTCLOUD_HTTP_TIMEOUT_SECONDS", "not-a-number")

    with pytest.raises(ConfigError, match="NEXTCLOUD_HTTP_TIMEOUT_SECONDS"):
        Settings.from_env()


# --- Settings.from_env(): missing required vars (E1) ---


@pytest.mark.parametrize(
    "missing_var",
    [
        "NEXTCLOUD_CALDAV_URL",
        "NEXTCLOUD_USERNAME",
        "NEXTCLOUD_APP_PASSWORD",
        "PUBLIC_BASE_URL",
    ],
)
def test_from_env_raises_on_each_missing_required_var(
    monkeypatch: pytest.MonkeyPatch, missing_var: str
):
    _set_required_env(monkeypatch)
    monkeypatch.delenv(missing_var, raising=False)

    with pytest.raises(ConfigError, match=missing_var):
        Settings.from_env()


def test_from_env_raises_on_blank_required_var(monkeypatch: pytest.MonkeyPatch):
    # A whitespace-only value must be treated the same as a missing one.
    _set_required_env(monkeypatch)
    monkeypatch.setenv("NEXTCLOUD_USERNAME", "   ")

    with pytest.raises(ConfigError, match="NEXTCLOUD_USERNAME"):
        Settings.from_env()


# --- Settings.from_env(): MCP_PORT (E1) ---


def test_from_env_default_port(monkeypatch: pytest.MonkeyPatch):
    _set_required_env(monkeypatch)
    monkeypatch.delenv("MCP_PORT", raising=False)

    settings = Settings.from_env()
    assert settings.port == 8000


def test_from_env_reads_custom_port(monkeypatch: pytest.MonkeyPatch):
    _set_required_env(monkeypatch)
    monkeypatch.setenv("MCP_PORT", "9090")

    settings = Settings.from_env()
    assert settings.port == 9090


def test_from_env_rejects_non_integer_port(monkeypatch: pytest.MonkeyPatch):
    _set_required_env(monkeypatch)
    monkeypatch.setenv("MCP_PORT", "not-a-port")

    with pytest.raises(ConfigError, match="MCP_PORT"):
        Settings.from_env()


# --- Settings.from_env(): MCP_OAUTH_ACCESS_TOKEN_EXPIRY_SECONDS (E1) ---


def test_from_env_default_oauth_access_token_expiry_seconds(monkeypatch: pytest.MonkeyPatch):
    _set_required_env(monkeypatch)
    monkeypatch.delenv("MCP_OAUTH_ACCESS_TOKEN_EXPIRY_SECONDS", raising=False)

    settings = Settings.from_env()
    assert settings.oauth_access_token_expiry_seconds == 30 * 24 * 60 * 60


def test_from_env_reads_custom_oauth_access_token_expiry_seconds(monkeypatch: pytest.MonkeyPatch):
    _set_required_env(monkeypatch)
    monkeypatch.setenv("MCP_OAUTH_ACCESS_TOKEN_EXPIRY_SECONDS", "3600")

    settings = Settings.from_env()
    assert settings.oauth_access_token_expiry_seconds == 3600


def test_from_env_rejects_non_integer_oauth_access_token_expiry_seconds(
    monkeypatch: pytest.MonkeyPatch,
):
    _set_required_env(monkeypatch)
    monkeypatch.setenv("MCP_OAUTH_ACCESS_TOKEN_EXPIRY_SECONDS", "forever")

    with pytest.raises(ConfigError, match="MCP_OAUTH_ACCESS_TOKEN_EXPIRY_SECONDS"):
        Settings.from_env()


# --- Settings.from_env(): MCP_OAUTH_REFRESH_TOKEN_EXPIRY_SECONDS (D5) ---


def test_from_env_default_oauth_refresh_token_expiry_seconds(monkeypatch: pytest.MonkeyPatch):
    _set_required_env(monkeypatch)
    monkeypatch.delenv("MCP_OAUTH_REFRESH_TOKEN_EXPIRY_SECONDS", raising=False)

    settings = Settings.from_env()
    assert settings.oauth_refresh_token_expiry_seconds == 180 * 24 * 60 * 60


def test_from_env_reads_custom_oauth_refresh_token_expiry_seconds(monkeypatch: pytest.MonkeyPatch):
    _set_required_env(monkeypatch)
    monkeypatch.setenv("MCP_OAUTH_REFRESH_TOKEN_EXPIRY_SECONDS", "3600")

    settings = Settings.from_env()
    assert settings.oauth_refresh_token_expiry_seconds == 3600


def test_from_env_rejects_non_integer_oauth_refresh_token_expiry_seconds(
    monkeypatch: pytest.MonkeyPatch,
):
    _set_required_env(monkeypatch)
    monkeypatch.setenv("MCP_OAUTH_REFRESH_TOKEN_EXPIRY_SECONDS", "forever")

    with pytest.raises(ConfigError, match="MCP_OAUTH_REFRESH_TOKEN_EXPIRY_SECONDS"):
        Settings.from_env()


def test_default_oauth_refresh_token_expiry_seconds_on_settings_dataclass():
    # The dataclass default (used when Settings is constructed directly, not
    # via from_env - e.g. in tests) must match from_env's default too.
    settings = _settings()
    assert settings.oauth_refresh_token_expiry_seconds == 180 * 24 * 60 * 60


# --- Settings.from_env(): MCP_OAUTH_ALLOWED_REDIRECT_DOMAINS CSV parsing (E1) ---


def test_from_env_default_allowed_redirect_domains_is_none(monkeypatch: pytest.MonkeyPatch):
    _set_required_env(monkeypatch)
    monkeypatch.delenv("MCP_OAUTH_ALLOWED_REDIRECT_DOMAINS", raising=False)

    settings = Settings.from_env()
    assert settings.oauth_allowed_redirect_domains is None


def test_from_env_parses_single_domain(monkeypatch: pytest.MonkeyPatch):
    _set_required_env(monkeypatch)
    monkeypatch.setenv("MCP_OAUTH_ALLOWED_REDIRECT_DOMAINS", "claude.ai")

    settings = Settings.from_env()
    assert settings.oauth_allowed_redirect_domains == ["claude.ai"]


def test_from_env_parses_multiple_domains_and_strips_whitespace(monkeypatch: pytest.MonkeyPatch):
    _set_required_env(monkeypatch)
    monkeypatch.setenv("MCP_OAUTH_ALLOWED_REDIRECT_DOMAINS", " claude.ai , claude.com ,example.org")

    settings = Settings.from_env()
    assert settings.oauth_allowed_redirect_domains == ["claude.ai", "claude.com", "example.org"]


def test_from_env_drops_empty_entries_in_domain_csv(monkeypatch: pytest.MonkeyPatch):
    _set_required_env(monkeypatch)
    monkeypatch.setenv("MCP_OAUTH_ALLOWED_REDIRECT_DOMAINS", "claude.ai,,  ,claude.com")

    settings = Settings.from_env()
    assert settings.oauth_allowed_redirect_domains == ["claude.ai", "claude.com"]


def test_from_env_empty_string_domain_csv_yields_empty_list_not_none(
    monkeypatch: pytest.MonkeyPatch,
):
    # An explicitly-set-but-empty env var is a deliberate "no domains allowed",
    # distinct from "not set at all" (which yields None / the vendored default).
    _set_required_env(monkeypatch)
    monkeypatch.setenv("MCP_OAUTH_ALLOWED_REDIRECT_DOMAINS", "")

    settings = Settings.from_env()
    assert settings.oauth_allowed_redirect_domains == []


# --- Settings.from_env(): remaining defaults (E1) ---


def test_from_env_defaults(monkeypatch: pytest.MonkeyPatch):
    _set_required_env(monkeypatch)
    for var in (
        "MCP_OAUTH_PASSWORD",
        "MCP_OAUTH_STATE_DIR",
        "MCP_HOST",
        "NEXTCLOUD_ALLOW_INSECURE_HTTP",
    ):
        monkeypatch.delenv(var, raising=False)

    settings = Settings.from_env()
    assert settings.oauth_password is None
    assert settings.oauth_state_dir == ".oauth-state"
    assert settings.host == "127.0.0.1"
    assert settings.allow_insecure_http is False


def test_from_env_reads_oauth_password_and_strips_whitespace(monkeypatch: pytest.MonkeyPatch):
    _set_required_env(monkeypatch)
    monkeypatch.setenv("MCP_OAUTH_PASSWORD", "  a-real-secret  ")

    settings = Settings.from_env()
    assert settings.oauth_password == "a-real-secret"


def test_from_env_blank_oauth_password_is_none(monkeypatch: pytest.MonkeyPatch):
    _set_required_env(monkeypatch)
    monkeypatch.setenv("MCP_OAUTH_PASSWORD", "   ")

    settings = Settings.from_env()
    assert settings.oauth_password is None


def test_from_env_reads_allow_insecure_http_flag(monkeypatch: pytest.MonkeyPatch):
    _set_required_env(monkeypatch)
    monkeypatch.setenv("NEXTCLOUD_ALLOW_INSECURE_HTTP", "1")

    settings = Settings.from_env()
    assert settings.allow_insecure_http is True


def test_from_env_custom_state_dir_and_host(monkeypatch: pytest.MonkeyPatch):
    _set_required_env(monkeypatch)
    monkeypatch.setenv("MCP_OAUTH_STATE_DIR", "/tmp/custom-state")
    monkeypatch.setenv("MCP_HOST", "0.0.0.0")
    monkeypatch.setenv("MCP_OAUTH_PASSWORD", "a-real-secret")

    settings = Settings.from_env()
    assert settings.oauth_state_dir == "/tmp/custom-state"
    assert settings.host == "0.0.0.0"
