# Deployment guide

Run the server on a Linux host with systemd, then choose a public HTTPS entry
point: [Caddy or your existing Nextcloud proxy](#4a-expose-via-caddy-or-an-existing-proxy),
or [Tailscale Funnel](#4b-expose-via-tailscale-funnel). Both terminate TLS and
forward requests to the server's local HTTP port.

```text
Clients ── HTTPS ──► existing reverse proxy
                         ├─ cloud.example.com     ──► Nextcloud
                         └─ organizer.example.com ──► MCP (127.0.0.1:8000)
                                                        │
                                  Nextcloud ◄── HTTPS + app password
```

A cloud-hosted MCP client needs an endpoint reachable from the public internet.
The server's [OAuth authentication](authentication.md) protects that endpoint.
Plain `tailscale serve` only reaches your tailnet; Funnel makes it public.

The Caddy recipe and its verification checklist are documented below. See the
[verification record](deployment-verification.md) for the distinction between
local automated proxy checks and a real public client/Nextcloud deployment.

## 1. Install on the host

```bash
# as a dedicated user, e.g. "mcp"
curl -LsSf https://astral.sh/uv/install.sh | sh   # install uv if not present
git clone https://github.com/<your-user>/nextcloud-organizer-mcp.git
cd nextcloud-organizer-mcp
uv sync --locked --no-dev
```

`--no-dev` skips pytest/ruff, which aren't needed at runtime.

## 2. Configure

Create `/etc/nextcloud-organizer-mcp.env` (root-owned, mode `600` — it contains secrets):

```bash
NEXTCLOUD_BASE_URL=https://cloud.example.com
NEXTCLOUD_USERNAME=<nextcloud user>
NEXTCLOUD_APP_PASSWORD=<app password from Settings -> Security>

# Optional. Only needed when the CalDAV endpoint is not <base>/remote.php/dav/
# (both URLs must point at the same Nextcloud instance).
# NEXTCLOUD_CALDAV_URL=https://cloud.example.com/remote.php/dav/

# Must match the public HTTPS origin exactly (scheme + host), set up in step 4.
# For Funnel use https://<hostname>.<tailnet>.ts.net instead.
PUBLIC_BASE_URL=https://organizer.example.com

# Required for any non-localhost PUBLIC_BASE_URL, or if MCP_HOST below is
# bound to a non-local address - the server refuses to start without it in
# either case. This is the actual security gate on the OAuth /authorize step
# now that the server is reachable from the public internet; the
# redirect-domain allow-list alone does not stop a scripted client from
# self-issuing a token. See docs/authentication.md.
MCP_OAUTH_PASSWORD=<long random value>

# OAuth client/token state persists here across restarts - must be writable
# by the "mcp" user. Matches the systemd StateDirectory set up below.
MCP_OAUTH_STATE_DIR=/var/lib/nextcloud-organizer-mcp/oauth-state

MCP_HOST=127.0.0.1
MCP_PORT=8000

# Uvicorn trusts forwarded headers only from this local proxy peer.
# This matches the Caddy upstream 127.0.0.1:8000 below.
FORWARDED_ALLOW_IPS=127.0.0.1
```

Generate the OAuth password with:

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(24))"
```

> **How the password gate works (D2 - resolved 2026-07-10).** Earlier revisions checked
> `MCP_OAUTH_PASSWORD` against the OAuth `state` query parameter of the `/authorize`
> request and flagged it as an unverified assumption whether claude.ai could ever
> deliver the password that way. That assumption was then tested against production
> claude.ai (real **Settings → Connectors → Add custom connector** flow, `/authorize`
> request captured in the browser's DevTools network tab) and **confirmed false**:
> `state` carries Claude's own randomly generated CSRF token
> (e.g. `state=AfGKaeD8ijS45GgSdUH0KLgD0AAitxmZJozNMHVOTLo`), the connector UI has no
> input that could influence it, and every legitimate authorization was denied with
> "Registration with the authentication service failed" (fail-closed - no exposure, but
> no way to ever register the connector either).
>
> The `state` check has been replaced by an **interactive consent page** (LOCAL PATCH 5
> in `personal_auth.py`): `/authorize` parks the validated OAuth request in memory under
> a cryptographically random single-use pending key (10-minute TTL) and 302-redirects
> the browser to `GET /consent`, which serves a password form. `POST /consent` verifies
> the password in constant time (`secrets.compare_digest`), then mints the authorization
> code and redirects back to Claude's `redirect_uri` with the code and Claude's own
> `state` intact. During connector setup you will therefore see a password prompt served
> by this server - enter `MCP_OAUTH_PASSWORD` there.
>
> Because the form is a publicly reachable password prompt, it is rate-limited: max 5
> wrong attempts per pending key (then the key is invalidated and the flow must be
> restarted from Claude), max 10 failures per client IP per 15 minutes (then a hard
> `429`, even with the correct password). Submitted form data is never logged and never
> echoed into responses, and the server keeps Uvicorn's HTTP access log disabled (see
> [Authentication](authentication.md)) - that now guards the pending
> keys in `/consent` query strings rather than the password itself.

## 3. systemd service

`/etc/systemd/system/nextcloud-organizer-mcp.service`:

```ini
[Unit]
Description=Organizer MCP for Nextcloud
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=mcp
WorkingDirectory=/home/mcp/nextcloud-organizer-mcp
EnvironmentFile=/etc/nextcloud-organizer-mcp.env
ExecStart=/home/mcp/.local/bin/uv run --no-dev nextcloud-organizer-mcp
Restart=on-failure
RestartSec=5

# basic hardening, cheap and non-intrusive
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=read-only
ReadWritePaths=/home/mcp/nextcloud-organizer-mcp/.venv
# Owned by "mcp", auto-created at /var/lib/nextcloud-organizer-mcp, writable even
# under ProtectSystem=strict. Holds the persisted OAuth client/token state.
StateDirectory=nextcloud-organizer-mcp

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now nextcloud-organizer-mcp
sudo systemctl status nextcloud-organizer-mcp
```

## 4a. Expose via Caddy or an existing proxy

### Caddy on the same host

Use a dedicated hostname such as `organizer.example.com`, alongside the existing
`cloud.example.com` Nextcloud site. Caddy and MCP must share the host network
for `127.0.0.1:8000` to reach MCP. Container networking is a separate deployment
choice; see [#64](https://github.com/Vando-sketch/Nextcloud-Organizer-MCP/issues/64).

Point the new hostname's DNS A/AAAA records at the proxy. For this default
recipe, allow inbound TCP ports 80 and 443 to Caddy and keep its certificate
storage writable and persistent. Caddy provisions and renews public
certificates and redirects HTTP to HTTPS.
[Caddy automatic HTTPS documentation](https://caddyserver.com/docs/automatic-https)

Add the site block and runtime log filter to `/etc/caddy/Caddyfile` (also
available as [examples/Caddyfile](../examples/Caddyfile)). If the file already
has a global options block, merge these `log default` settings into it; Caddy
accepts only one global block. Preserve existing default logging settings
when adding the filter.

```caddyfile
{
    log default {
        format filter {
            wrap json
            fields {
                request>uri delete
                request>headers>Referer delete
            }
        }
    }
}

organizer.example.com {
    reverse_proxy 127.0.0.1:8000
}
```

Keep your existing Nextcloud site block and its backend configuration. If
another proxy already owns ports 80/443, add the MCP hostname there using the
adaptation guidance below, or plan a migration before starting Caddy.

Validate the complete configuration and reload the existing Caddy service:

```bash
sudo caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile
sudo systemctl reload caddy
sudo systemctl restart nextcloud-organizer-mcp
```

Set `PUBLIC_BASE_URL=https://organizer.example.com`, without `/mcp`, in step 2.
The connector URL is **`https://organizer.example.com/mcp`**.

### Routes, client IPs and streaming

Route the **whole hostname**, preserving paths, query strings, methods and bodies.
The server serves `/mcp`, `/.well-known/oauth-authorization-server`, OAuth
protected-resource discovery at `/.well-known/oauth-protected-resource/mcp`
(use the URL in `/mcp`'s `WWW-Authenticate` challenge), `/register`,
`/authorize`, `/consent` (GET and POST), and `/token`.
Proxying only `/mcp` leaves discovery and consent unreachable. Use a subdomain;
subpath deployment is tracked separately in
[#73](https://github.com/Vando-sketch/Nextcloud-Organizer-MCP/issues/73).

Caddy preserves `Host`, sets `X-Forwarded-For`, `X-Forwarded-Proto`, and
`X-Forwarded-Host`, and ignores client-supplied values for those forwarded
headers by default. It immediately flushes `text/event-stream` responses;
`stream_timeout` has no default limit. The recipe needs no custom header or
buffering directives.
[Caddy reverse proxy documentation](https://caddyserver.com/docs/caddyfile/directives/reverse_proxy)

`FORWARDED_ALLOW_IPS=127.0.0.1` is Uvicorn's environment setting for the trusted
proxy peer. It allows the local Caddy connection to supply the original client
IP and HTTPS scheme. Consent's per-IP rate limit uses that client address.
Keep MCP bound to loopback and trust only the actual proxy peer; never set
`FORWARDED_ALLOW_IPS=*` for an exposed backend.
[Uvicorn settings](https://uvicorn.dev/settings/#http)

For a CDN or another proxy in front of Caddy, configure trust at every hop for
the actual upstream proxy addresses and repeat the client-IP checks below.
Do not broadly trust private networks as a substitute for identifying the proxy.

### Reuse another existing Nextcloud proxy

Add a virtual host for `organizer.example.com` with your proxy's normal public
TLS configuration and an HTTP upstream of `127.0.0.1:8000`. Preserve the entire
hostname's routes and original host, forward the HTTPS scheme, and replace
untrusted client-supplied forwarding headers with the actual client address.
Set `FORWARDED_ALLOW_IPS` to the peer address MCP really sees if it differs from
the same-host loopback example, and restrict network access to that backend.

Check response buffering and any request/response or idle deadlines inherited
from your existing configuration. MCP uses Streamable HTTP, including SSE;
responses must reach the client incrementally and long-lived streams must not
be cut off by a short proxy deadline. Test through every proxy hop using the
[shared checklist](deployment-verification.md). nginx/Traefik configurations
remain unverified here; verify them with the shared checklist before publishing
copyable recipes.

### Protect OAuth request parameters in proxy logs

The example does not enable site HTTP access logging. Caddy still emits runtime
errors: a failed upstream request can produce a `502` error log containing the
request URI and `Referer` header. Either may contain `/consent?pending=...` or
other OAuth parameters. The global default log filter removes these fields
while retaining the error itself. This filter applies to default runtime logs
across all sites, so review its effect on your existing Nextcloud logging.
[Caddy global logging options](https://caddyserver.com/docs/caddyfile/options#log)

Audit imported snippets, separately configured access log outputs, upstream
proxies and CDNs. Apply equivalent filtering to every output that records
request details; a default runtime log filter alone does not configure separate
access log encoders. Caddy's default sensitive-header redaction does not remove
sensitive URL query parameters.
[Caddy log documentation](https://caddyserver.com/docs/caddyfile/directives/log)

OAuth authorization URLs contain `state` and redirect parameters; consent URLs
contain a single-use authorization key. Authorization codes, bearer tokens,
passwords and form bodies also belong outside logs. Keep debug/trace logging
disabled during OAuth flows. Verify logging with disposable marker values for
both successful requests and upstream failures, including a `Referer` carrying
a consent URL.

## 4b. Expose via Tailscale Funnel

```bash
sudo tailscale funnel --bg 8000
```

This publishes `https://<hostname>.<tailnet>.ts.net/` (TLS certificate managed by
Tailscale) to the **public internet** and proxies it to `127.0.0.1:8000`. Check with:

```bash
tailscale funnel status
```

Funnel must be enabled for this node in your tailnet's admin console first
(**Settings → Funnel** at [login.tailscale.com](https://login.tailscale.com)) - unlike
`tailscale serve`, it's not on by default. If `tailscale funnel` refuses to start, this is
almost always why.

Make sure `PUBLIC_BASE_URL` in step 2 matches this URL exactly, then (re)start the
service so it picks up the value:

```bash
sudo systemctl restart nextcloud-organizer-mcp
```

## 5. Connect Claude

### Claude.ai (web) — syncs to mobile automatically

**Settings → Connectors → Add custom connector**, URL:
`https://organizer.example.com/mcp` for Caddy, or
`https://<hostname>.<tailnet>.ts.net/mcp` for Funnel. Leave any Client ID/Secret fields blank -
Dynamic Client Registration handles that. Approve the OAuth prompt that opens in your
browser. See the [Authentication](authentication.md#registering-the-connector-in-claude) for details.

### Claude Desktop

Claude Desktop's remote-connector support goes through the
[`mcp-remote`](https://github.com/geelen/mcp-remote) bridge, which handles the OAuth flow
locally (opens a browser for one-time auth). Add to `claude_desktop_config.json`
(`~/Library/Application Support/Claude/` on macOS):

```json
{
  "mcpServers": {
    "nextcloud-organizer-mcp": {
      "command": "npx",
      "args": ["-y", "mcp-remote", "https://organizer.example.com/mcp"]
    }
  }
}
```

Requires Node.js. For Funnel, replace the hostname with your public Funnel hostname.

### Claude Code

```bash
claude mcp add nextcloud-organizer-mcp --transport http "https://organizer.example.com/mcp"
```

For Funnel, use `https://<hostname>.<tailnet>.ts.net/mcp` instead.

### Claude mobile (iOS/Android)

Add the connector on claude.ai web (above) - it syncs to mobile automatically. Connectors
can't be added directly from the mobile app.

## Managing issued OAuth tokens

`oauth_tokens.json` (in `MCP_OAUTH_STATE_DIR`) accumulates one access/refresh token pair
per authorized client for as long as they stay valid - by default, access tokens expire
after `MCP_OAUTH_ACCESS_TOKEN_EXPIRY_SECONDS` (30 days) and refresh tokens after
`MCP_OAUTH_REFRESH_TOKEN_EXPIRY_SECONDS` (180 days, see `.env.example`), but there is no
built-in way to end a session early - e.g. after a lost device, or to confirm what's
actually been issued. `nextcloud-organizer-mcp-admin` (installed alongside the server by `uv
sync`) reads and edits `oauth_tokens.json` directly, without needing the server running:

```bash
# List every issued access/refresh token (truncated - full values are never printed),
# its client_id, and expiry. Defaults to $MCP_OAUTH_STATE_DIR, or .oauth-state/ if unset.
nextcloud-organizer-mcp-admin --state-dir /var/lib/nextcloud-organizer-mcp/oauth-state list

# Revoke one token (and its paired access/refresh token) by prefix, as shown by `list`.
# Claude will need to reconnect the connector (re-run the OAuth flow) afterwards.
nextcloud-organizer-mcp-admin --state-dir /var/lib/nextcloud-organizer-mcp/oauth-state revoke pat_a1b2c3
```

Stop the `nextcloud-organizer-mcp` service first if you want to be certain there's no
in-flight write racing the CLI's edit (both write the same file); the CLI itself always
rewrites `oauth_tokens.json` atomically-enough for a single-operator workflow (open,
truncate, write, `chmod 0600`), matching the permissions `PersonalAuthProvider` itself
uses.

## Updating

```bash
cd ~/nextcloud-organizer-mcp
git pull
uv sync --locked --no-dev
sudo systemctl restart nextcloud-organizer-mcp
```

## Migrating from nextcloud-task-mcp

The project used to be called `nextcloud-task-mcp`. The old
`nextcloud-task-mcp` and `nextcloud-task-mcp-admin` commands are still
installed as deprecated aliases, so an existing deployment keeps running after
a plain `git pull && uv sync --locked --no-dev && sudo systemctl restart
nextcloud-task-mcp`. To move it over to the new names (the OAuth state is kept,
so Claude does not need to reconnect):

```bash
sudo systemctl disable --now nextcloud-task-mcp

# Env file and persisted OAuth state
sudo mv /etc/nextcloud-task-mcp.env /etc/nextcloud-organizer-mcp.env
sudo mv /var/lib/nextcloud-task-mcp /var/lib/nextcloud-organizer-mcp
sudo sed -i 's#/var/lib/nextcloud-task-mcp#/var/lib/nextcloud-organizer-mcp#' \
    /etc/nextcloud-organizer-mcp.env

# systemd unit: rename it, then point it at the new names
sudo mv /etc/systemd/system/nextcloud-task-mcp.service \
    /etc/systemd/system/nextcloud-organizer-mcp.service
sudo sed -i 's/nextcloud-task-mcp/nextcloud-organizer-mcp/g' \
    /etc/systemd/system/nextcloud-organizer-mcp.service
sudo systemctl daemon-reload
sudo systemctl enable --now nextcloud-organizer-mcp
journalctl -u nextcloud-organizer-mcp -n 20
```

The unit's `WorkingDirectory` and `ReadWritePaths` still point at the old
checkout directory; that keeps working. To rename the checkout as well, `mv`
it and update both lines before the `daemon-reload`. If the GitHub repository was
renamed, `git remote set-url origin` to the new URL (GitHub also redirects the
old one).

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| Claude.ai says "error connecting" while adding the connector | `PUBLIC_BASE_URL` doesn't exactly match the public HTTPS origin; or the endpoint isn't publicly reachable. Check DNS/TLS and proxy routing, or `tailscale funnel status` for Funnel. |
| TLS certificate error or connection timeout | Check the hostname's A and AAAA records, inbound ports 80/443, certificate issuance logs and client trust. A stale AAAA record can send clients to the wrong host. |
| Discovery or consent returns `404`, or Nextcloud HTML | The proxy routes only `/mcp`, strips a path prefix, or selects the Nextcloud virtual host. Route all paths for the dedicated MCP hostname. |
| Streams stall or disconnect through the proxy | Check inherited buffering and response/idle deadlines at every proxy hop; run the streaming checklist. |
| All users share a consent `429`, or changing `X-Forwarded-For` bypasses it | Review forwarded-header trust: MCP must trust the actual proxy peer and the proxy must replace untrusted incoming headers. Repeat the two-client and spoofing checks. |
| OAuth prompt appears but authorization fails | The consent page rejected the submitted `MCP_OAUTH_PASSWORD`; retry the form with the configured password. If authorization fails before the consent page appears, check that the redirect domain is in `MCP_OAUTH_ALLOWED_REDIRECT_DOMAINS` (only relevant if you changed the default). See [Authentication](authentication.md) for the consent flow. |
| Service fails to start: `MCP_OAUTH_PASSWORD is required...` | `PUBLIC_BASE_URL` isn't localhost and `MCP_OAUTH_PASSWORD` is unset - this is enforced deliberately, set the password (step 2) |
| `401` calling `/mcp` after Claude was previously connected | Access token expired or was revoked; disconnect and reconnect the connector in Claude to re-run the OAuth flow |
| OAuth state lost after a restart | `MCP_OAUTH_STATE_DIR` isn't pointing at a persistent, writable path - confirm the systemd `StateDirectory` is set and matches |
| "Nextcloud rejected the CalDAV credentials" | Wrong username or expired/revoked app password |
| "Could not reach the Nextcloud server" | Nextcloud down, or the container can't resolve/route to it |
| `tailscale funnel` refuses to start | Funnel not enabled for this node in the tailnet admin console (Settings → Funnel) |

Server logs: `journalctl -u nextcloud-organizer-mcp -f`. Unexpected internal errors are logged
there with full tracebacks, while the MCP client only ever sees a short generic message.
