# Authentication

The server authenticates MCP clients with **OAuth 2.1** (Dynamic Client Registration +
PKCE), via [`PersonalAuthProvider`](https://github.com/crumrine/fastmcp-personal-auth) -
vendored into [`src/nextcloud_organizer_mcp/personal_auth.py`](../src/nextcloud_organizer_mcp/personal_auth.py)
since it ships as a single file to copy in, not an installable package. There is no
static bearer token to configure.

This exists because Claude's connector UI (web, mobile, Desktop, Cowork) only exposes
OAuth fields for custom connectors - it has no field for a raw static token. OAuth is
also what makes the server usable from Claude mobile at all, since mobile has no config
file to hand-edit.

How it's secured, since anyone on the internet can reach the OAuth discovery and
registration endpoints once the server is public:

- **Dynamic Client Registration is intentionally open** (`/register` accepts any client) -
  this is required for Claude.ai's connector flow and is not itself a security boundary.
- **The redirect-domain allow-list is *not*, by itself, a security boundary.** A script
  never has to actually control a listed domain (e.g. `claude.ai`) to pass this check -
  it only has to *claim* a matching `redirect_uri` when calling `/authorize`, and the
  authorization code comes back directly in that same HTTP response. Configurable via
  `MCP_OAUTH_ALLOWED_REDIRECT_DOMAINS`; when unset and `PUBLIC_BASE_URL` isn't local, the
  server also drops `localhost` from the vendored default allow-list (a `localhost`
  entry can never be reached by a real OAuth redirect on a public deployment anyway) -
  but don't rely on this list alone either way.
- **`MCP_OAUTH_PASSWORD` is the actual security gate**, and is required (the server
  refuses to start without it) whenever `PUBLIC_BASE_URL` isn't `localhost`/`127.0.0.1`,
  or `MCP_HOST` is bound to a non-local address (e.g. `0.0.0.0` - a stale localhost
  `PUBLIC_BASE_URL` with a `0.0.0.0` bind is a common Docker misconfiguration).
  Without it, anyone who can reach the server can self-issue a valid access token. It is
  enforced by an interactive **consent page**: `/authorize` parks the request under a
  cryptographically random, single-use pending key (10-minute TTL) and redirects the
  browser to `/consent`, which asks for the password before any authorization code is
  minted. The comparison is constant-time (`secrets.compare_digest`), and the form is
  rate-limited (max 5 wrong attempts per pending key, max 10 failures per client IP per
  15 minutes) since it is a publicly reachable password prompt. The placeholder value
  shipped (commented out) in `.env.example` is rejected outright if left in place.
- **Access tokens are opaque random strings** (not JWTs with inspectable claims) and are
  persisted to `MCP_OAUTH_STATE_DIR` (default `.oauth-state/oauth_tokens.json`, gitignored)
  so they survive server restarts.
- The `/mcp` endpoint itself rejects any request without a valid `Authorization: Bearer
  <access-token>` header before any tool or CalDAV logic runs.
- The server disables Uvicorn's default HTTP access log (`uvicorn_config={"access_log":
  False}` in `server.py`). The password itself only ever travels in the POST body of the
  `/consent` form, which Uvicorn never logs - but the default access-log format records
  full request paths *including query strings*, which for `/consent` carry the
  single-use pending keys that gate authorization, so the access log stays off. The
  consent handlers themselves never log or echo submitted form data anywhere either.
- Behind a reverse proxy, the consent rate limit relies on a trusted client address.
  Configure the proxy to discard spoofed forwarding headers and Uvicorn to trust
  only the proxy peer. Audit the proxy's own logging for OAuth query parameters;
  disabling Uvicorn access logs does not disable proxy logs. See the
  [Caddy deployment recipe](deployment.md#4a-expose-via-caddy-or-an-existing-proxy).

**Local security patches.** The vendored `PersonalAuthProvider` carries five fixes for
upstream issues found while building this integration, all confirmed by live
reproduction against a running instance, not just by reading the code - see the "LOCAL
PATCHES" note at the top of [`personal_auth.py`](../src/nextcloud_organizer_mcp/personal_auth.py)
for the full log. The most consequential: upstream's password check had a dead-code
fallback that accepted *any* password (or none) as long as the redirect domain matched
the allow-list, and its whole delivery mechanism - expecting the OAuth client to embed
the password in the `state`/`scope` parameters - turned out to be unworkable against
real Claude clients (see below), so it was replaced by the interactive consent page.

**Why a consent page (confirmed 2026-07-10).** Upstream's design expected Claude to
somehow send your password in the OAuth `state` parameter of the `/authorize` request.
A live test against production claude.ai (real "Add custom connector" flow, `/authorize`
request captured in the browser's DevTools network tab) confirmed that can never happen:
`state` carries Claude's own randomly generated CSRF token, and the connector UI has no
field that could influence it. The gate therefore denied every legitimate authorization
- fail-closed, so no exposure, but the connector could not be set up at all. The consent
page replaces it: you now type the password into a form served by this server during the
OAuth flow, which is what upstream's `state` trick was trying to approximate.

## Registering the connector in Claude

Once the server is running and reachable at `PUBLIC_BASE_URL` (see the
[deployment guide](deployment.md) for Caddy, an existing proxy, or Tailscale Funnel):

1. In Claude.ai (or Cowork/Desktop): **Settings → Connectors → Add custom connector**.
2. **URL:** `<PUBLIC_BASE_URL>/mcp`, e.g. `https://your-host.your-tailnet.ts.net/mcp`.
3. Leave any Client ID / Client Secret fields blank - Dynamic Client Registration handles
   this automatically; there's nothing to copy from the server.
4. Save. Claude opens the OAuth authorization flow in a browser, which lands on this
   server's consent page - enter your `MCP_OAUTH_PASSWORD` there and the connector is
   authenticated (synced automatically to Claude mobile).

Claude Desktop (no native remote-connector UI yet) instead uses the
[`mcp-remote`](https://github.com/geelen/mcp-remote) bridge in `claude_desktop_config.json`
- see the [deployment guide](deployment.md#5-connect-claude) for the exact config.
