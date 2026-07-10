# Vendored from https://github.com/crumrine/fastmcp-personal-auth
# (commit as of 2026-07-08, file personal_auth.py), with five local security
# patches (see "LOCAL PATCHES" below) - otherwise unmodified.
#
# fastmcp-personal-auth is not published as an installable package - its own
# README instructs consumers to copy this module directly into their project.
# It is included here verbatim (plus the documented patches) rather than
# reformatted so that future updates can be diffed against upstream.
#
# LOCAL PATCHES (patches 1-2 applied 2026-07-09, patches 3-5 applied
# 2026-07-10), all confirmed by live reproduction against a running instance,
# not just code review:
#
# 1. PersonalAuthProvider.authorize() had an "auto-approve for allowed
#    redirect domains" fallback in its password check that unconditionally
#    evaluated True, because authorize() already returns earlier (via
#    _is_redirect_allowed's raise) whenever that same condition is False.
#    This made MCP_OAUTH_PASSWORD a no-op: any password (or none at all) was
#    accepted as long as the redirect domain matched the allow-list. Combined
#    with Dynamic Client Registration being open by design, this let an
#    anonymous script (no browser, no real claude.ai session) self-issue a
#    valid access token against a publicly-reachable deployment. The dead
#    fallback has been removed so the password, when configured, is actually
#    enforced.
#
# 2. The password check accepted the password via either the `scope` or
#    `state` OAuth parameter. Unlike `state`, `scope` is persisted verbatim
#    onto the issued access token AND refresh token (and echoed back in the
#    /token response body), meaning a password sent this way would end up
#    durably written to disk in oauth_tokens.json for the life of the token,
#    multiplying at-rest plaintext copies of the one secret this deployment
#    relies on. The `scope` channel has been removed; only `state` (never
#    persisted) is checked now.
#
# 3. oauth_tokens.json holds plaintext bearer and refresh tokens, but was
#    written with whatever the process umask left it at - commonly
#    world-readable - and the state dir was created without an explicit mode.
#    The state dir is now created (and chmod'd - Path.mkdir(mode=...) is
#    masked by the umask and, unlike chmod, does not fix an already-existing
#    directory's permissions) as 0o700, and oauth_tokens.json is now written
#    via os.open(..., 0o600) so it's never briefly or permanently
#    group/world-readable.
#
# 4. Refresh tokens were minted with `expires_at=None` - i.e. they never
#    expire, so a single leak of oauth_tokens.json grants indefinite access
#    even after the leak is noticed (D5). A new constructor parameter,
#    `refresh_token_expiry_seconds` (default 180 days;
#    `PersonalAuthProvider(..., refresh_token_expiry_seconds=None)` restores
#    the original never-expires behavior for anyone who wants it), is now
#    applied when a refresh token is first issued in
#    `exchange_authorization_code`.
#
#    Investigation of the base class before patching: `InMemoryOAuthProvider
#    .load_refresh_token` (inherited unmodified here) already correctly
#    enforces whatever `expires_at` is set on a `RefreshToken` - it checks
#    `expires_at is not None and expires_at < time.time()` and revokes the
#    pair, returning None - so once a refresh token actually carries an
#    `expires_at`, expiry is enforced with no override needed there.
#
#    However, `InMemoryOAuthProvider.exchange_refresh_token` (the token
#    *rotation* path, used every time a refresh token is redeemed) mints its
#    *replacement* refresh token using that module's own
#    `DEFAULT_REFRESH_TOKEN_EXPIRY_SECONDS = None` constant, which is not
#    parameterized and can't be overridden by a subclass short of
#    reimplementing the method - so without further changes, the very first
#    refresh token would expire on schedule but every token it rotates into
#    would silently revert to "never expires", defeating the bound. This
#    class now overrides `exchange_refresh_token` to call `super()` (so the
#    rotation, scope validation, and old-token revocation logic stays
#    upstream's) and then re-stamp the newly-issued refresh token's
#    `expires_at` from `self.refresh_token_expiry_seconds` before persisting
#    state.
#
# 5. The MCP_OAUTH_PASSWORD gate expected the OAuth *client* to embed the
#    password in the `state` parameter of /authorize. Live test against
#    production claude.ai (2026-07-10, real "Add custom connector" flow, the
#    /authorize request captured in the browser's DevTools network tab):
#    Claude sends its own randomly generated CSRF token as `state`
#    (e.g. "AfGKaeD8ijS45GgSdUH0KLgD0AAitxmZJozNMHVOTLo") and its connector
#    UI has no input that could influence it - so the gate denied every
#    legitimate authorization and the connector could never be set up. (It
#    did fail closed: an availability bug, not a bypass.) The `state` check
#    was unfixable rather than tunable, so it has been *replaced* by an
#    interactive consent page:
#
#      - authorize() no longer inspects `state`. When a password is
#        configured, it stashes the validated request (client + params) in
#        memory under a cryptographically random single-use pending key
#        (secrets.token_urlsafe(32), 10-minute TTL) and returns the URL of
#        GET /consent?pending=<key>. The MCP SDK's AuthorizationHandler
#        302-redirects the user's browser there (its contract is "provider
#        returns the next URL to redirect to" - it cannot serve HTML from
#        /authorize itself, which is why the form lives on its own route).
#      - GET /consent renders a password form (pending key as hidden field);
#        POST /consent verifies the password with secrets.compare_digest
#        (constant-time - the old `in` substring test was never a suitable
#        password comparison), then completes the original authorization via
#        the upstream super().authorize() path and 302s back to the client's
#        redirect_uri with the code and the client's own `state` intact.
#      - The form is a publicly reachable password oracle (the old design,
#        whatever else was wrong with it, never exposed one), so submissions
#        are rate-limited: max 5 wrong attempts per pending key (then the key
#        is invalidated and the flow must be restarted) and max 10 failures
#        per client IP per 15 minutes (then hard 429, even with the correct
#        password). Failed attempts do NOT consume the pending key below the
#        limit. Form data is never logged and never echoed into responses
#        (this deployment already disables Uvicorn's access log - see
#        README > Authentication - and the consent handlers keep that
#        guarantee: nothing the user types leaves the comparison).
#      - The routes are contributed by overriding FastMCP's
#        OAuthProvider.get_routes() hook, which the framework calls to
#        collect the auth router - OAuth discovery (/.well-known/*),
#        /register (DCR) and /token are untouched.
#
# MIT License
#
# Copyright (c) 2026 Brian Crumrine
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.

"""
FastMCP Personal Auth Provider

A drop-in OAuth 2.1 auth provider for FastMCP that works with Claude.ai,
Claude mobile, Claude Desktop, and Claude Code — no external identity
provider required.

Usage:
    from fastmcp import FastMCP
    from personal_auth import PersonalAuthProvider

    auth = PersonalAuthProvider(
        base_url="https://your-domain.com",
        password="your-secret-password",
        allowed_redirect_domains=["claude.ai", "claude.com", "localhost"],
    )

    mcp = FastMCP(name="my-server", auth=auth)

    @mcp.tool
    def hello() -> str:
        return "Hello, world!"

    mcp.run(transport="streamable-http", host="0.0.0.0", port=8050)
"""

import json
import os
import secrets
import time
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response
from starlette.routing import Route

from fastmcp.server.auth.providers.in_memory import InMemoryOAuthProvider
from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    AuthorizeError,
    RefreshToken,
    TokenError,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from mcp.server.auth.settings import ClientRegistrationOptions

logger = logging.getLogger("personal-auth")

DEFAULT_ACCESS_TOKEN_EXPIRY = 30 * 24 * 60 * 60  # 30 days
DEFAULT_REFRESH_TOKEN_EXPIRY = 180 * 24 * 60 * 60  # 180 days (LOCAL PATCH 4)
DEFAULT_STATE_DIR = ".oauth-state"

# Consent-page limits (LOCAL PATCH 5). The pending TTL only needs to cover
# "browser opens, human types a password once"; the rate limits bound how fast
# the publicly reachable form can be used to guess MCP_OAUTH_PASSWORD.
CONSENT_PENDING_TTL_SECONDS = 10 * 60
CONSENT_MAX_ATTEMPTS_PER_KEY = 5
CONSENT_MAX_FAILURES_PER_IP = 10
CONSENT_FAILURE_WINDOW_SECONDS = 15 * 60


@dataclass
class _PendingAuthorization:
    """A validated /authorize request parked while the user completes the
    consent form (LOCAL PATCH 5)."""

    client: OAuthClientInformationFull
    params: AuthorizationParams
    expires_at: float
    attempts: int = 0


class PersonalAuthProvider(InMemoryOAuthProvider):
    """OAuth 2.1 provider for personal/small-team MCP servers.

    Fills the gap between FastMCP's InMemoryOAuthProvider (test-only, no
    persistence, no security) and OAuthProxy (requires Google/GitHub/Auth0).

    Features:
    - Dynamic Client Registration (DCR) for Claude.ai compatibility
    - PKCE support (handled by FastMCP framework)
    - Restrict /authorize to approved redirect domains only
    - Optional password gate on authorization
    - Token persistence to a JSON file (survives restarts)
    - Configurable token expiry (default 30 days)
    """

    def __init__(
        self,
        base_url: str,
        password: Optional[str] = None,
        allowed_redirect_domains: Optional[list[str]] = None,
        access_token_expiry_seconds: int = DEFAULT_ACCESS_TOKEN_EXPIRY,
        refresh_token_expiry_seconds: Optional[int] = DEFAULT_REFRESH_TOKEN_EXPIRY,
        state_dir: Optional[str] = None,
    ):
        """
        Args:
            base_url: Public URL of this server (e.g. "https://my-server.example.com")
            password: Optional password required to authorize. If None, authorization
                      is gated only by allowed_redirect_domains.
            allowed_redirect_domains: List of domains allowed in OAuth redirect URIs.
                Defaults to ["claude.ai", "claude.com", "localhost"]. Set to None
                to allow all domains (not recommended for public servers).
            access_token_expiry_seconds: How long access tokens last. Default 30 days.
            refresh_token_expiry_seconds: How long issued refresh tokens remain
                valid, bounding how long a leaked oauth_tokens.json grants access
                for (D5, LOCAL PATCH 4). Default 180 days. Pass None to restore
                the original upstream behavior of refresh tokens that never
                expire.
            state_dir: Directory for persisting OAuth state. Default ".oauth-state".
        """
        super().__init__(
            base_url=base_url,
            client_registration_options=ClientRegistrationOptions(enabled=True),
        )

        self.password = password
        self.allowed_redirect_domains = allowed_redirect_domains if allowed_redirect_domains is not None else [
            "claude.ai", "claude.com", "localhost"
        ]
        self.access_token_expiry_seconds = access_token_expiry_seconds
        self.refresh_token_expiry_seconds = refresh_token_expiry_seconds
        # Parked /authorize requests awaiting the consent form, and recent
        # failed form submissions per client IP (LOCAL PATCH 5). Both are
        # deliberately in-memory only: a restart just means re-running the
        # connector flow.
        self._pending_authorizations: dict[str, _PendingAuthorization] = {}
        self._consent_failures: dict[str, list[float]] = {}

        self._state_dir = Path(state_dir or DEFAULT_STATE_DIR)
        self._state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        # mkdir's mode= is masked by the umask and won't fix an already-existing
        # dir, so chmod explicitly too (LOCAL PATCH 3).
        os.chmod(self._state_dir, 0o700)
        self._load_state()

    # --- State persistence ---

    def _state_file(self) -> Path:
        return self._state_dir / "oauth_tokens.json"

    def _load_state(self):
        f = self._state_file()
        if not f.exists():
            return
        try:
            data = json.loads(f.read_text())
            for k, v in data.get("clients", {}).items():
                self.clients[k] = OAuthClientInformationFull(**v)
            for k, v in data.get("access_tokens", {}).items():
                self.access_tokens[k] = AccessToken(**v)
            for k, v in data.get("refresh_tokens", {}).items():
                self.refresh_tokens[k] = RefreshToken(**v)
            self._access_to_refresh_map = data.get("a2r", {})
            self._refresh_to_access_map = data.get("r2a", {})
            logger.info(
                f"Loaded OAuth state: {len(self.clients)} clients, "
                f"{len(self.access_tokens)} access tokens"
            )
        except Exception as e:
            logger.warning(f"Failed to load OAuth state from {f}: {e}")

    def _save_state(self):
        def serialize(obj):
            if hasattr(obj, "model_dump"):
                return obj.model_dump(mode="json")
            return {
                "token": obj.token, "client_id": obj.client_id,
                "scopes": obj.scopes, "expires_at": obj.expires_at,
            }

        data = {
            "clients": {k: v.model_dump(mode="json") for k, v in self.clients.items()},
            "access_tokens": {k: serialize(v) for k, v in self.access_tokens.items()},
            "refresh_tokens": {k: serialize(v) for k, v in self.refresh_tokens.items()},
            "a2r": self._access_to_refresh_map,
            "r2a": self._refresh_to_access_map,
        }
        # oauth_tokens.json holds plaintext bearer/refresh tokens - open with
        # 0o600 up front instead of write_text()'s default-umask permissions
        # (LOCAL PATCH 3).
        fd = os.open(self._state_file(), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(json.dumps(data, indent=2))

    # --- Authorization gate ---

    def _is_redirect_allowed(self, redirect_uri: str) -> bool:
        if self.allowed_redirect_domains is None:
            return True
        try:
            host = urlparse(redirect_uri).hostname or ""
            return any(
                host == domain or host.endswith(f".{domain}")
                for domain in self.allowed_redirect_domains
            )
        except Exception:
            return False

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        await super().register_client(client_info)
        self._save_state()

    async def authorize(
        self, client: OAuthClientInformationFull, params: AuthorizationParams
    ) -> str:
        redirect = str(params.redirect_uri) if params.redirect_uri else ""

        # Check redirect domain
        if not self._is_redirect_allowed(redirect):
            raise AuthorizeError(
                error="access_denied",
                error_description="Redirect URI domain not allowed.",
            )

        if not self.password:
            return await self._complete_authorization(client, params)

        # Password configured: park the request and send the user's browser to
        # the consent form instead of minting a code here. The framework's
        # AuthorizationHandler turns whatever URL we return into a 302, so
        # this is the supported way to insert an interactive step (LOCAL
        # PATCH 5 - replaces the old `state`-carries-the-password check, which
        # real claude.ai can never satisfy: it sends its own CSRF token as
        # `state`).
        self._prune_pending()
        pending_key = secrets.token_urlsafe(32)
        self._pending_authorizations[pending_key] = _PendingAuthorization(
            client=client,
            params=params,
            expires_at=time.time() + CONSENT_PENDING_TTL_SECONDS,
        )
        return f"{str(self.base_url).rstrip('/')}/consent?pending={pending_key}"

    async def _complete_authorization(
        self, client: OAuthClientInformationFull, params: AuthorizationParams
    ) -> str:
        """Mint the authorization code via the upstream flow and return the
        client redirect URL (code + the client's own `state` echoed back)."""
        result = await super().authorize(client, params)
        self._save_state()
        return result

    # --- Consent page (LOCAL PATCH 5) ---

    def _prune_pending(self) -> None:
        now = time.time()
        expired = [k for k, v in self._pending_authorizations.items() if v.expires_at < now]
        for key in expired:
            del self._pending_authorizations[key]

    def _recent_ip_failures(self, ip: str) -> list[float]:
        """Failed consent submissions from `ip` within the rate-limit window
        (pruning older entries as a side effect)."""
        cutoff = time.time() - CONSENT_FAILURE_WINDOW_SECONDS
        recent = [stamp for stamp in self._consent_failures.get(ip, []) if stamp > cutoff]
        if recent:
            self._consent_failures[ip] = recent
        else:
            self._consent_failures.pop(ip, None)
        return recent

    @staticmethod
    def _client_ip(request: Request) -> str:
        return request.client.host if request.client else "unknown"

    _CONSENT_HEADERS = {
        # The pending key rides in a query string / hidden form field - keep
        # it out of caches and referrer headers.
        "Cache-Control": "no-store",
        "Referrer-Policy": "no-referrer",
    }

    def _consent_form_html(self, pending_key: str, error: Optional[str] = None) -> str:
        # No escaping needed: `pending_key` comes from secrets.token_urlsafe
        # (URL-safe base64 alphabet only) and `error` is one of our own static
        # strings. Nothing the user submitted is ever reflected back.
        error_html = f'<p class="error">{error}</p>' if error else ""
        return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex">
<title>Authorize access</title>
<style>
  body {{ font-family: system-ui, sans-serif; display: flex; justify-content: center;
         align-items: center; min-height: 100vh; margin: 0; background: #f4f4f5; }}
  main {{ background: #fff; padding: 2rem; border-radius: 8px; max-width: 24rem;
          width: 100%; box-shadow: 0 1px 4px rgba(0, 0, 0, 0.15); }}
  h1 {{ font-size: 1.25rem; margin-top: 0; }}
  input[type=password] {{ width: 100%; padding: 0.5rem; margin: 0.25rem 0 1rem;
                          box-sizing: border-box; }}
  button {{ padding: 0.5rem 1.5rem; }}
  .error {{ color: #b00020; }}
</style>
</head>
<body>
<main>
<h1>Authorize access</h1>
<p>A client is requesting access to this MCP server. Enter the server
password (<code>MCP_OAUTH_PASSWORD</code>) to approve it.</p>
{error_html}
<form method="POST" action="/consent">
<input type="hidden" name="pending" value="{pending_key}">
<label for="password">Password</label>
<input type="password" id="password" name="password"
       autocomplete="current-password" autofocus required>
<button type="submit">Authorize</button>
</form>
</main>
</body>
</html>"""

    def _consent_error_page(self, message: str, status_code: int) -> Response:
        return HTMLResponse(
            f"""<!doctype html>
<html lang="en">
<head><meta charset="utf-8"><meta name="robots" content="noindex">
<title>Authorization failed</title></head>
<body style="font-family: system-ui, sans-serif; max-width: 32rem; margin: 4rem auto;">
<h1 style="font-size: 1.25rem;">Authorization failed</h1>
<p>{message}</p>
</body>
</html>""",
            status_code=status_code,
            headers=self._CONSENT_HEADERS,
        )

    _EXPIRED_MESSAGE = (
        "This authorization link is invalid or has expired. "
        "Start the connection again from your MCP client."
    )

    async def render_consent_form(self, request: Request) -> Response:
        """GET /consent - show the password form for a parked authorization."""
        self._prune_pending()
        pending_key = request.query_params.get("pending", "")
        if pending_key not in self._pending_authorizations:
            return self._consent_error_page(self._EXPIRED_MESSAGE, status_code=400)
        return HTMLResponse(
            self._consent_form_html(pending_key), headers=self._CONSENT_HEADERS
        )

    async def handle_consent_submission(self, request: Request) -> Response:
        """POST /consent - verify the password, then finish the parked
        authorization and redirect back to the OAuth client.

        Never log or echo the submitted form data anywhere in here: the whole
        point of this deployment's logging setup (access log disabled, see
        README > Authentication) is that MCP_OAUTH_PASSWORD ends up in no log
        file.
        """
        self._prune_pending()
        form = await request.form()
        pending_key = form.get("pending")
        submitted = form.get("password")
        pending_key = pending_key if isinstance(pending_key, str) else ""
        submitted = submitted if isinstance(submitted, str) else ""

        # Per-IP limit first: once an IP is over budget it gets a hard reject
        # regardless of key validity - or password correctness, since an
        # attacker at the limit must not learn whether attempt N+1 would have
        # been the right guess.
        ip = self._client_ip(request)
        if len(self._recent_ip_failures(ip)) >= CONSENT_MAX_FAILURES_PER_IP:
            logger.warning("Consent form rate limit hit for IP %s", ip)
            return self._consent_error_page(
                "Too many failed attempts. Try again later.", status_code=429
            )

        entry = self._pending_authorizations.get(pending_key)
        if entry is None:
            return self._consent_error_page(self._EXPIRED_MESSAGE, status_code=400)

        password_ok = bool(self.password) and secrets.compare_digest(
            submitted.encode("utf-8"), self.password.encode("utf-8")
        )
        if not password_ok:
            entry.attempts += 1
            self._consent_failures.setdefault(ip, []).append(time.time())
            logger.info(
                "Consent form: wrong password (attempt %d/%d for this key)",
                entry.attempts,
                CONSENT_MAX_ATTEMPTS_PER_KEY,
            )
            if entry.attempts >= CONSENT_MAX_ATTEMPTS_PER_KEY:
                del self._pending_authorizations[pending_key]
                return self._consent_error_page(
                    "Too many failed attempts for this authorization. "
                    "Start the connection again from your MCP client.",
                    status_code=403,
                )
            return HTMLResponse(
                self._consent_form_html(pending_key, error="Wrong password. Try again."),
                status_code=401,
                headers=self._CONSENT_HEADERS,
            )

        # Success: the key is single-use.
        del self._pending_authorizations[pending_key]
        try:
            redirect_url = await self._complete_authorization(entry.client, entry.params)
        except AuthorizeError:
            # e.g. the client was dropped between /authorize and the form
            # submission. Static message only - no exception detail.
            return self._consent_error_page(
                "The authorization request could not be completed. "
                "Start the connection again from your MCP client.",
                status_code=400,
            )
        return RedirectResponse(
            redirect_url, status_code=302, headers={"Cache-Control": "no-store"}
        )

    def get_routes(self, mcp_path: Optional[str] = None) -> list[Route]:
        # FastMCP calls this hook to collect the auth router; appending here
        # adds the consent routes without touching the SDK-provided
        # /authorize, /token, /register or /.well-known/* routes (LOCAL
        # PATCH 5).
        routes = super().get_routes(mcp_path)
        routes.append(Route("/consent", endpoint=self.render_consent_form, methods=["GET"]))
        routes.append(
            Route("/consent", endpoint=self.handle_consent_submission, methods=["POST"])
        )
        return routes

    # --- Token exchange with configurable expiry ---

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode
    ) -> OAuthToken:
        if authorization_code.code not in self.auth_codes:
            raise TokenError("invalid_grant", "Authorization code not found or already used.")

        del self.auth_codes[authorization_code.code]

        access_token_value = f"pat_{secrets.token_hex(32)}"
        refresh_token_value = f"prt_{secrets.token_hex(32)}"
        access_token_expires_at = int(time.time() + self.access_token_expiry_seconds)

        if client.client_id is None:
            raise TokenError("invalid_client", "Client ID is required")

        self.access_tokens[access_token_value] = AccessToken(
            token=access_token_value,
            client_id=client.client_id,
            scopes=authorization_code.scopes,
            expires_at=access_token_expires_at,
        )
        # Bounded by default (D5, LOCAL PATCH 4) - only truly `None` (never
        # expires) if the operator explicitly configured it that way.
        refresh_token_expires_at = (
            int(time.time() + self.refresh_token_expiry_seconds)
            if self.refresh_token_expiry_seconds is not None
            else None
        )
        self.refresh_tokens[refresh_token_value] = RefreshToken(
            token=refresh_token_value,
            client_id=client.client_id,
            scopes=authorization_code.scopes,
            expires_at=refresh_token_expires_at,
        )

        self._access_to_refresh_map[access_token_value] = refresh_token_value
        self._refresh_to_access_map[refresh_token_value] = access_token_value
        self._save_state()

        return OAuthToken(
            access_token=access_token_value,
            token_type="Bearer",
            expires_in=self.access_token_expiry_seconds,
            refresh_token=refresh_token_value,
            scope=" ".join(authorization_code.scopes),
        )

    async def exchange_refresh_token(self, client, refresh_token, scopes):
        # See LOCAL PATCH 4 above: the base implementation always mints the
        # *replacement* refresh token (rotation) with expires_at=None,
        # regardless of what this class's refresh_token_expiry_seconds says -
        # re-stamp it here so a bounded lifetime actually survives rotation.
        result = await super().exchange_refresh_token(client, refresh_token, scopes)
        if self.refresh_token_expiry_seconds is not None and result.refresh_token is not None:
            new_refresh_token = self.refresh_tokens.get(result.refresh_token)
            if new_refresh_token is not None:
                new_refresh_token.expires_at = int(
                    time.time() + self.refresh_token_expiry_seconds
                )
        self._save_state()
        return result

    async def revoke_token(self, token):
        await super().revoke_token(token)
        self._save_state()
