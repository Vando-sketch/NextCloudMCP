"""Unit tests for Settings' OAuth-password-required-for-public-deployments rule."""

from __future__ import annotations

import pytest

from nextcloud_organizer_mcp.config import ConfigError, Settings

#: Every variable `Settings.from_env()` (and `default_timezone_from_env`) reads.
_CONFIG_ENV_VARS = (
    "NEXTCLOUD_USERNAME",
    "NEXTCLOUD_APP_PASSWORD",
    "NEXTCLOUD_BASE_URL",
    "NEXTCLOUD_CALDAV_URL",
    "NEXTCLOUD_ALLOW_INSECURE_HTTP",
    "NEXTCLOUD_HTTP_TIMEOUT_SECONDS",
    "PUBLIC_BASE_URL",
    "MCP_HOST",
    "MCP_PORT",
    "MCP_DEFAULT_TIMEZONE",
    "MCP_OAUTH_PASSWORD",
    "MCP_OAUTH_STATE_DIR",
    "MCP_OAUTH_ACCESS_TOKEN_EXPIRY_SECONDS",
    "MCP_OAUTH_REFRESH_TOKEN_EXPIRY_SECONDS",
    "MCP_OAUTH_ALLOWED_REDIRECT_DOMAINS",
)


@pytest.fixture(autouse=True)
def _clean_config_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Start every test from an empty configuration environment.

    Without this, a developer's real NEXTCLOUD_*/MCP_* variables (or a stray
    .env exported into the shell) silently change what the tests that only
    set *some* variables observe.
    """
    for var in _CONFIG_ENV_VARS:
        monkeypatch.delenv(var, raising=False)


def _settings(**overrides) -> Settings:
    defaults = dict(
        caldav_url="https://cloud.example.com/remote.php/dav/",
        caldav_username="testuser",
        caldav_password="testpass",
        notes_base_url="https://cloud.example.com",
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


# --- NEXTCLOUD_BASE_URL scheme enforcement (mirrors NEXTCLOUD_CALDAV_URL/D8) ---


def test_http_notes_base_url_rejected_for_non_local_host():
    with pytest.raises(ConfigError, match="https"):
        _settings(notes_base_url="http://cloud.example.com")


def test_http_notes_base_url_allowed_for_localhost():
    _settings(notes_base_url="http://localhost:8080")
    _settings(notes_base_url="http://127.0.0.1:8080")
    _settings(notes_base_url="http://[::1]:8080")


def test_http_notes_base_url_allowed_with_escape_hatch():
    _settings(notes_base_url="http://cloud.example.com", allow_insecure_http=True)


def test_https_notes_base_url_always_allowed():
    _settings(notes_base_url="https://cloud.example.com", allow_insecure_http=False)


# --- NEXTCLOUD_HTTP_TIMEOUT_SECONDS (A2) ---


def test_caldav_timeout_seconds_defaults_to_30():
    settings = _settings()
    assert settings.caldav_timeout_seconds == 30


def test_caldav_timeout_seconds_accepts_custom_value():
    settings = _settings(caldav_timeout_seconds=5)
    assert settings.caldav_timeout_seconds == 5


def _set_required_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NEXTCLOUD_USERNAME", "testuser")
    monkeypatch.setenv("NEXTCLOUD_APP_PASSWORD", "testpass")
    monkeypatch.setenv("NEXTCLOUD_BASE_URL", "https://cloud.example.com")
    monkeypatch.setenv("PUBLIC_BASE_URL", "http://127.0.0.1:8000")


# --- Settings.from_env(): missing required vars (E1) ---


@pytest.mark.parametrize(
    "missing_var",
    [
        "NEXTCLOUD_USERNAME",
        "NEXTCLOUD_APP_PASSWORD",
        "NEXTCLOUD_BASE_URL",
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


# --- Settings.from_env(): optional NEXTCLOUD_CALDAV_URL derivation ---


@pytest.mark.parametrize(
    ("base_url", "expected_caldav_url"),
    [
        ("https://cloud.example.com", "https://cloud.example.com/remote.php/dav/"),
        ("https://cloud.example.com/", "https://cloud.example.com/remote.php/dav/"),
        ("https://example.com/nextcloud", "https://example.com/nextcloud/remote.php/dav/"),
        ("https://example.com/nextcloud/", "https://example.com/nextcloud/remote.php/dav/"),
    ],
)
def test_from_env_derives_caldav_url_when_unset(
    monkeypatch: pytest.MonkeyPatch, base_url: str, expected_caldav_url: str
):
    _set_required_env(monkeypatch)
    monkeypatch.delenv("NEXTCLOUD_CALDAV_URL", raising=False)
    monkeypatch.setenv("NEXTCLOUD_BASE_URL", base_url)

    settings = Settings.from_env()
    assert settings.caldav_url == expected_caldav_url


@pytest.mark.parametrize("empty_val", ["", "   "])
def test_from_env_empty_caldav_url_behaves_like_unset(
    monkeypatch: pytest.MonkeyPatch, empty_val: str
):
    _set_required_env(monkeypatch)
    monkeypatch.setenv("NEXTCLOUD_CALDAV_URL", empty_val)
    monkeypatch.setenv("NEXTCLOUD_BASE_URL", "https://cloud.example.com")

    settings = Settings.from_env()
    assert settings.caldav_url == "https://cloud.example.com/remote.php/dav/"


def test_from_env_explicit_caldav_url_used_verbatim(monkeypatch: pytest.MonkeyPatch):
    _set_required_env(monkeypatch)
    monkeypatch.setenv("NEXTCLOUD_CALDAV_URL", "https://dav.different-host.org/dav/")
    monkeypatch.setenv("NEXTCLOUD_BASE_URL", "https://cloud.example.com")

    settings = Settings.from_env()
    assert settings.caldav_url == "https://dav.different-host.org/dav/"


def test_from_env_http_base_url_derived_caldav_url_fails_https_check(
    monkeypatch: pytest.MonkeyPatch,
):
    _set_required_env(monkeypatch)
    monkeypatch.delenv("NEXTCLOUD_CALDAV_URL", raising=False)
    monkeypatch.setenv("NEXTCLOUD_BASE_URL", "http://cloud.example.com")

    with pytest.raises(ConfigError, match="https"):
        Settings.from_env()


def test_from_env_http_base_url_error_names_the_variable_that_is_actually_set(
    monkeypatch: pytest.MonkeyPatch,
):
    """The message must point at NEXTCLOUD_BASE_URL, not the derived variable.

    With the scheme checks in the other order, an operator who never set
    NEXTCLOUD_CALDAV_URL would be told to fix it - a variable that is nowhere
    in their environment.
    """
    _set_required_env(monkeypatch)
    monkeypatch.delenv("NEXTCLOUD_CALDAV_URL", raising=False)
    monkeypatch.setenv("NEXTCLOUD_BASE_URL", "http://cloud.example.com")

    with pytest.raises(ConfigError, match="NEXTCLOUD_BASE_URL must use https"):
        Settings.from_env()


@pytest.mark.parametrize(
    "base_url",
    [
        "https://cloud.example.com/nextcloud?ref=1",
        "https://cloud.example.com#anchor",
    ],
)
def test_from_env_rejects_base_url_with_query_or_fragment(
    monkeypatch: pytest.MonkeyPatch, base_url: str
):
    """Appending the DAV path to such a URL yields a nonsense endpoint.

    It would pass every scheme and host check, so the server would start
    happily and only fail on the first CalDAV request.
    """
    _set_required_env(monkeypatch)
    monkeypatch.delenv("NEXTCLOUD_CALDAV_URL", raising=False)
    monkeypatch.setenv("NEXTCLOUD_BASE_URL", base_url)

    with pytest.raises(ConfigError, match="query string or fragment"):
        Settings.from_env()


def test_both_urls_insecure_reports_the_base_url_first(monkeypatch: pytest.MonkeyPatch):
    """Documents the trade-off of checking the base URL first.

    With both set to http://, the operator now hears about NEXTCLOUD_BASE_URL
    rather than NEXTCLOUD_CALDAV_URL. Both are wrong and both need fixing, and
    the base URL is the one that can no longer be omitted.
    """
    _set_required_env(monkeypatch)
    monkeypatch.setenv("NEXTCLOUD_BASE_URL", "http://cloud.example.com")
    monkeypatch.setenv("NEXTCLOUD_CALDAV_URL", "http://cloud.example.com/remote.php/dav/")

    with pytest.raises(ConfigError, match="NEXTCLOUD_BASE_URL must use https"):
        Settings.from_env()


def test_explicit_http_caldav_url_still_names_caldav_url(monkeypatch: pytest.MonkeyPatch):
    """An explicitly set insecure CalDAV URL is still reported as such."""
    _set_required_env(monkeypatch)
    monkeypatch.setenv("NEXTCLOUD_BASE_URL", "https://cloud.example.com")
    monkeypatch.setenv("NEXTCLOUD_CALDAV_URL", "http://dav.example.com/remote.php/dav/")

    with pytest.raises(ConfigError, match="NEXTCLOUD_CALDAV_URL must use https"):
        Settings.from_env()


def test_from_env_http_base_url_derived_caldav_url_allowed_with_insecure_flag(
    monkeypatch: pytest.MonkeyPatch,
):
    _set_required_env(monkeypatch)
    monkeypatch.delenv("NEXTCLOUD_CALDAV_URL", raising=False)
    monkeypatch.setenv("NEXTCLOUD_BASE_URL", "http://cloud.example.com")
    monkeypatch.setenv("NEXTCLOUD_ALLOW_INSECURE_HTTP", "1")

    settings = Settings.from_env()
    assert settings.caldav_url == "http://cloud.example.com/remote.php/dav/"


def test_from_env_http_base_url_derived_caldav_url_allowed_for_localhost(
    monkeypatch: pytest.MonkeyPatch,
):
    _set_required_env(monkeypatch)
    monkeypatch.delenv("NEXTCLOUD_CALDAV_URL", raising=False)
    monkeypatch.setenv("NEXTCLOUD_BASE_URL", "http://localhost:8080")

    settings = Settings.from_env()
    assert settings.caldav_url == "http://localhost:8080/remote.php/dav/"


def test_from_env_reads_notes_base_url(monkeypatch: pytest.MonkeyPatch):
    _set_required_env(monkeypatch)
    monkeypatch.setenv("NEXTCLOUD_BASE_URL", "https://cloud.example.org")

    settings = Settings.from_env()
    assert settings.notes_base_url == "https://cloud.example.org"


def test_from_env_raises_on_blank_required_var(monkeypatch: pytest.MonkeyPatch):
    # A whitespace-only value must be treated the same as a missing one.
    _set_required_env(monkeypatch)
    monkeypatch.setenv("NEXTCLOUD_USERNAME", "   ")

    with pytest.raises(ConfigError, match="NEXTCLOUD_USERNAME"):
        Settings.from_env()


# --- Settings.from_env(): integer-valued variables (E1, D5, A2) ---

_INT_ENV_VARS = [
    # (env var, Settings attribute, default when unset, custom value)
    ("NEXTCLOUD_HTTP_TIMEOUT_SECONDS", "caldav_timeout_seconds", 30, 45),
    ("MCP_PORT", "port", 8000, 9090),
    (
        "MCP_OAUTH_ACCESS_TOKEN_EXPIRY_SECONDS",
        "oauth_access_token_expiry_seconds",
        30 * 24 * 60 * 60,
        3600,
    ),
    (
        "MCP_OAUTH_REFRESH_TOKEN_EXPIRY_SECONDS",
        "oauth_refresh_token_expiry_seconds",
        180 * 24 * 60 * 60,
        3600,
    ),
]


@pytest.mark.parametrize(("var", "attr", "default", "_custom"), _INT_ENV_VARS)
def test_from_env_integer_var_default_when_unset(
    monkeypatch: pytest.MonkeyPatch, var: str, attr: str, default: int, _custom: int
):
    _set_required_env(monkeypatch)
    monkeypatch.delenv(var, raising=False)

    assert getattr(Settings.from_env(), attr) == default


@pytest.mark.parametrize(("var", "attr", "_default", "custom"), _INT_ENV_VARS)
def test_from_env_integer_var_reads_custom_value(
    monkeypatch: pytest.MonkeyPatch, var: str, attr: str, _default: int, custom: int
):
    _set_required_env(monkeypatch)
    monkeypatch.setenv(var, str(custom))

    assert getattr(Settings.from_env(), attr) == custom


@pytest.mark.parametrize("var", [entry[0] for entry in _INT_ENV_VARS])
def test_from_env_rejects_non_integer_value(monkeypatch: pytest.MonkeyPatch, var: str):
    _set_required_env(monkeypatch)
    monkeypatch.setenv(var, "not-a-number")

    with pytest.raises(ConfigError, match=var):
        Settings.from_env()


def test_default_oauth_refresh_token_expiry_seconds_on_settings_dataclass():
    # The dataclass default (used when Settings is constructed directly, not
    # via from_env - e.g. in tests) must match from_env's default too.
    settings = _settings()
    assert settings.oauth_refresh_token_expiry_seconds == 180 * 24 * 60 * 60


# --- Settings.from_env(): MCP_OAUTH_ALLOWED_REDIRECT_DOMAINS CSV parsing (E1) ---


def test_from_env_default_allowed_redirect_domains_is_none(monkeypatch: pytest.MonkeyPatch):
    _set_required_env(monkeypatch)

    settings = Settings.from_env()
    assert settings.oauth_allowed_redirect_domains is None


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("claude.ai", ["claude.ai"]),
        (" claude.ai , claude.com ,example.org", ["claude.ai", "claude.com", "example.org"]),
        ("claude.ai,,  ,claude.com", ["claude.ai", "claude.com"]),
    ],
    ids=["single", "strips-whitespace", "drops-empty-entries"],
)
def test_from_env_parses_redirect_domain_csv(
    monkeypatch: pytest.MonkeyPatch, raw: str, expected: list[str]
):
    _set_required_env(monkeypatch)
    monkeypatch.setenv("MCP_OAUTH_ALLOWED_REDIRECT_DOMAINS", raw)

    settings = Settings.from_env()
    assert settings.oauth_allowed_redirect_domains == expected


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


def test_default_timezone_defaults_to_europe_berlin(monkeypatch: pytest.MonkeyPatch):
    _set_required_env(monkeypatch)
    monkeypatch.delenv("MCP_DEFAULT_TIMEZONE", raising=False)
    settings = Settings.from_env()
    assert settings.default_timezone == "Europe/Berlin"


def test_default_timezone_env_var_override(monkeypatch: pytest.MonkeyPatch):
    _set_required_env(monkeypatch)
    monkeypatch.setenv("MCP_DEFAULT_TIMEZONE", "America/New_York")
    settings = Settings.from_env()
    assert settings.default_timezone == "America/New_York"


def test_invalid_default_timezone_raises_config_error(monkeypatch: pytest.MonkeyPatch):
    _set_required_env(monkeypatch)
    monkeypatch.setenv("MCP_DEFAULT_TIMEZONE", "Invalid/Timezone_Name")
    with pytest.raises(ConfigError, match="MCP_DEFAULT_TIMEZONE.*Invalid/Timezone_Name"):
        Settings.from_env()


# --- MCP_TRANSPORT: http (default) vs stdio ---


def test_transport_defaults_to_http():
    assert _settings().transport == "http"


def test_from_env_transport_defaults_to_http(monkeypatch: pytest.MonkeyPatch):
    _set_required_env(monkeypatch)
    monkeypatch.delenv("MCP_TRANSPORT", raising=False)
    assert Settings.from_env().transport == "http"


@pytest.mark.parametrize("value", ["", "  "])
def test_from_env_empty_transport_means_http(monkeypatch: pytest.MonkeyPatch, value: str):
    _set_required_env(monkeypatch)
    monkeypatch.setenv("MCP_TRANSPORT", value)
    assert Settings.from_env().transport == "http"


def test_from_env_reads_stdio_transport_case_insensitively(monkeypatch: pytest.MonkeyPatch):
    _set_required_env(monkeypatch)
    monkeypatch.setenv("MCP_TRANSPORT", " STDIO ")
    assert Settings.from_env().transport == "stdio"


def test_from_env_rejects_unknown_transport(monkeypatch: pytest.MonkeyPatch):
    _set_required_env(monkeypatch)
    monkeypatch.setenv("MCP_TRANSPORT", "sse")
    with pytest.raises(ConfigError, match="MCP_TRANSPORT.*'sse'"):
        Settings.from_env()


def test_from_env_stdio_needs_only_the_nextcloud_variables(monkeypatch: pytest.MonkeyPatch):
    for name in ("PUBLIC_BASE_URL", "MCP_OAUTH_PASSWORD", "MCP_OAUTH_STATE_DIR", "MCP_HOST"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("MCP_TRANSPORT", "stdio")
    monkeypatch.setenv("NEXTCLOUD_USERNAME", "testuser")
    monkeypatch.setenv("NEXTCLOUD_APP_PASSWORD", "testpass")
    monkeypatch.setenv("NEXTCLOUD_BASE_URL", "https://cloud.example.com")

    settings = Settings.from_env()

    assert settings.transport == "stdio"
    assert settings.public_base_url == ""


def test_from_env_http_still_requires_public_base_url(monkeypatch: pytest.MonkeyPatch):
    _set_required_env(monkeypatch)
    monkeypatch.setenv("MCP_TRANSPORT", "http")
    monkeypatch.delenv("PUBLIC_BASE_URL")
    with pytest.raises(ConfigError, match="PUBLIC_BASE_URL"):
        Settings.from_env()


def test_stdio_does_not_require_oauth_password_or_public_base_url():
    _settings(transport="stdio", public_base_url="", oauth_password=None, host="0.0.0.0")


def test_stdio_still_rejects_cleartext_nextcloud_url():
    with pytest.raises(ConfigError, match="NEXTCLOUD_BASE_URL must use https://"):
        _settings(transport="stdio", public_base_url="", notes_base_url="http://cloud.example.com")


def test_http_with_empty_public_base_url_is_rejected():
    with pytest.raises(ConfigError, match="PUBLIC_BASE_URL"):
        _settings(transport="http", public_base_url="")
