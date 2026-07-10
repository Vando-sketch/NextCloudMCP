# Deployment guide

This guide covers the intended production setup: the server running on an Ubuntu LXC
container, reachable from the **public internet** over a [Tailscale](https://tailscale.com)
Funnel, with `tailscale funnel` terminating TLS in front of it.

```
Claude (custom connector, cloud-side)
        │  HTTPS (Tailscale Funnel cert)
        ▼
tailscale funnel  ──►  nextcloud-task-mcp (127.0.0.1:8000, plain HTTP)
                              │  HTTPS + app password
                              ▼
                      Nextcloud (CalDAV)
```

The server itself never handles TLS. Unlike a plain `tailscale serve` setup, **Funnel
exposes the server to the entire internet**, not just your tailnet - this is required
because Claude's connector performs the OAuth flow (and later, tool calls) from
Anthropic's servers, which cannot reach into a private tailnet. The server's own
authentication (OAuth 2.1 via `PersonalAuthProvider`, see the
[README](../README.md#authentication)) is what protects it now that network-level
isolation from Tailscale is gone for this service.

## 1. Install on the container

```bash
# as a dedicated user, e.g. "mcp"
curl -LsSf https://astral.sh/uv/install.sh | sh   # install uv if not present
git clone https://github.com/<your-user>/NextCloudTaskMCP.git
cd NextCloudTaskMCP
uv sync --locked --no-dev
```

`--no-dev` skips pytest/ruff, which aren't needed at runtime.

## 2. Configure

Create `/etc/nextcloud-task-mcp.env` (root-owned, mode `600` — it contains secrets):

```bash
NEXTCLOUD_CALDAV_URL=https://cloud.example.com/remote.php/dav/
NEXTCLOUD_USERNAME=<nextcloud user>
NEXTCLOUD_APP_PASSWORD=<app password from Settings -> Security>

# Must match the public Funnel URL exactly (scheme + host), set up in step 4.
PUBLIC_BASE_URL=https://<hostname>.<tailnet>.ts.net

# Required for any non-localhost PUBLIC_BASE_URL, or if MCP_HOST below is
# bound to a non-local address - the server refuses to start without it in
# either case. This is the actual security gate on the OAuth /authorize step
# now that the server is reachable from the public internet; the
# redirect-domain allow-list alone does not stop a scripted client from
# self-issuing a token. See README > Authentication.
MCP_OAUTH_PASSWORD=<long random value>

# OAuth client/token state persists here across restarts - must be writable
# by the "mcp" user. Matches the systemd StateDirectory set up below.
MCP_OAUTH_STATE_DIR=/var/lib/nextcloud-task-mcp/oauth-state

MCP_HOST=127.0.0.1
MCP_PORT=8000
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
> "Registrierung beim Anmeldedienst fehlgeschlagen" (fail-closed - no exposure, but no
> way to ever register the connector either).
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
> [README > Authentication](../README.md#authentication)) - that now guards the pending
> keys in `/consent` query strings rather than the password itself.

## 3. systemd service

`/etc/systemd/system/nextcloud-task-mcp.service`:

```ini
[Unit]
Description=Nextcloud Task MCP server
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=mcp
WorkingDirectory=/home/mcp/NextCloudTaskMCP
EnvironmentFile=/etc/nextcloud-task-mcp.env
ExecStart=/home/mcp/.local/bin/uv run --no-dev nextcloud-task-mcp
Restart=on-failure
RestartSec=5

# basic hardening, cheap and non-intrusive
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=read-only
ReadWritePaths=/home/mcp/NextCloudTaskMCP/.venv
# Owned by "mcp", auto-created at /var/lib/nextcloud-task-mcp, writable even
# under ProtectSystem=strict. Holds the persisted OAuth client/token state.
StateDirectory=nextcloud-task-mcp

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now nextcloud-task-mcp
sudo systemctl status nextcloud-task-mcp
```

## 4. Expose via Tailscale Funnel

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
sudo systemctl restart nextcloud-task-mcp
```

## 5. Connect Claude

### Claude.ai (web) — syncs to mobile automatically

**Settings → Connectors → Add custom connector**, URL:
`https://<hostname>.<tailnet>.ts.net/mcp`. Leave any Client ID/Secret fields blank -
Dynamic Client Registration handles that. Approve the OAuth prompt that opens in your
browser. See the [README](../README.md#registering-the-connector-in-claude) for details.

### Claude Desktop

Claude Desktop's remote-connector support goes through the
[`mcp-remote`](https://github.com/geelen/mcp-remote) bridge, which handles the OAuth flow
locally (opens a browser for one-time auth). Add to `claude_desktop_config.json`
(`~/Library/Application Support/Claude/` on macOS):

```json
{
  "mcpServers": {
    "nextcloud-task-mcp": {
      "command": "npx",
      "args": ["-y", "mcp-remote", "https://<hostname>.<tailnet>.ts.net/mcp"]
    }
  }
}
```

Requires Node.js.

### Claude Code

```bash
claude mcp add nextcloud-task-mcp --transport http "https://<hostname>.<tailnet>.ts.net/mcp"
```

### Claude mobile (iOS/Android)

Add the connector on claude.ai web (above) - it syncs to mobile automatically. Connectors
can't be added directly from the mobile app.

## Managing issued OAuth tokens

`oauth_tokens.json` (in `MCP_OAUTH_STATE_DIR`) accumulates one access/refresh token pair
per authorized client for as long as they stay valid - by default, access tokens expire
after `MCP_OAUTH_ACCESS_TOKEN_EXPIRY_SECONDS` (30 days) and refresh tokens after
`MCP_OAUTH_REFRESH_TOKEN_EXPIRY_SECONDS` (180 days, see `.env.example`), but there is no
built-in way to end a session early - e.g. after a lost device, or to confirm what's
actually been issued. `nextcloud-task-mcp-admin` (installed alongside the server by `uv
sync`) reads and edits `oauth_tokens.json` directly, without needing the server running:

```bash
# List every issued access/refresh token (truncated - full values are never printed),
# its client_id, and expiry. Defaults to $MCP_OAUTH_STATE_DIR, or .oauth-state/ if unset.
nextcloud-task-mcp-admin --state-dir /var/lib/nextcloud-task-mcp/oauth-state list

# Revoke one token (and its paired access/refresh token) by prefix, as shown by `list`.
# Claude will need to reconnect the connector (re-run the OAuth flow) afterwards.
nextcloud-task-mcp-admin --state-dir /var/lib/nextcloud-task-mcp/oauth-state revoke pat_a1b2c3
```

Stop the `nextcloud-task-mcp` service first if you want to be certain there's no
in-flight write racing the CLI's edit (both write the same file); the CLI itself always
rewrites `oauth_tokens.json` atomically-enough for a single-operator workflow (open,
truncate, write, `chmod 0600`), matching the permissions `PersonalAuthProvider` itself
uses.

## Updating

```bash
cd ~/NextCloudTaskMCP
git pull
uv sync --locked --no-dev
sudo systemctl restart nextcloud-task-mcp
```

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| Claude.ai says "error connecting" while adding the connector | `PUBLIC_BASE_URL` doesn't exactly match the Funnel URL (scheme/host mismatch breaks OAuth discovery); or the server isn't reachable from the internet yet (check `tailscale funnel status`) |
| OAuth prompt appears but authorization fails | `MCP_OAUTH_PASSWORD` wasn't satisfied - it's checked against the `state` OAuth parameter, which Claude's client generates and this project cannot verify it can supply. The server deliberately does **not** log request query strings (that would log the password itself - see README > Authentication), so debug this by inspecting the actual `/authorize` request Claude's browser flow makes (e.g. browser dev tools network tab) rather than server logs. Or: the redirect domain isn't in `MCP_OAUTH_ALLOWED_REDIRECT_DOMAINS` (only relevant if you changed the default) |
| Service fails to start: `MCP_OAUTH_PASSWORD is required...` | `PUBLIC_BASE_URL` isn't localhost and `MCP_OAUTH_PASSWORD` is unset - this is enforced deliberately, set the password (step 2) |
| `401` calling `/mcp` after Claude was previously connected | Access token expired or was revoked; disconnect and reconnect the connector in Claude to re-run the OAuth flow |
| OAuth state lost after a restart | `MCP_OAUTH_STATE_DIR` isn't pointing at a persistent, writable path - confirm the systemd `StateDirectory` is set and matches |
| "Nextcloud rejected the CalDAV credentials" | Wrong username or expired/revoked app password |
| "Could not reach the Nextcloud server" | Nextcloud down, or the container can't resolve/route to it |
| `tailscale funnel` refuses to start | Funnel not enabled for this node in the tailnet admin console (Settings → Funnel) |

Server logs: `journalctl -u nextcloud-task-mcp -f`. Unexpected internal errors are logged
there with full tracebacks, while the MCP client only ever sees a short generic message.
