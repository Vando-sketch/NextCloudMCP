# Deployment guide

This guide covers the intended production setup: the server running on an Ubuntu LXC
container, reachable from the **public internet** over a [Tailscale](https://tailscale.com)
Funnel, with `tailscale funnel` terminating TLS in front of it.

```
Claude (custom connector, cloud-side)
        │  HTTPS (Tailscale Funnel cert)
        ▼
tailscale funnel  ──►  nextcloud-organizer-mcp (127.0.0.1:8000, plain HTTP)
                              │  HTTPS + app password
                              ▼
                      Nextcloud (CalDAV)
```

> Prefer containers? See [Running in Docker](docker.md) for the published image and a
> compose file; the rest of this guide (Funnel, connecting Claude, token management)
> applies to it as well.

The server itself never handles TLS. Unlike a plain `tailscale serve` setup, **Funnel
exposes the server to the entire internet**, not just your tailnet - this is required
because Claude's connector performs the OAuth flow (and later, tool calls) from
Anthropic's servers, which cannot reach into a private tailnet. The server's own
authentication (OAuth 2.1 via `PersonalAuthProvider`, see the
[Authentication](authentication.md)) is what protects it now that network-level
isolation from Tailscale is gone for this service.

## 1. Install on the container

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

# Must match the public Funnel URL exactly (scheme + host), set up in step 4.
PUBLIC_BASE_URL=https://<hostname>.<tailnet>.ts.net

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
sudo systemctl restart nextcloud-organizer-mcp
```

## 4b. Alternative: Cloudflare Tunnel

> **Verification status: partially tested - feedback wanted.** A *named* tunnel with a
> stable hostname has **not** been tested end to end by the maintainers: no domain on
> Cloudflare was available. What *was* tested is listed under
> [What has and has not been tested](#what-has-and-has-not-been-tested). If you run
> this recipe, please report back (what worked, what did not, `cloudflared` version,
> Cloudflare plan and security settings) in a
> [client compatibility report](https://github.com/Vando-sketch/Nextcloud-Organizer-MCP/issues/new/choose)
> or on issue [#71](https://github.com/Vando-sketch/Nextcloud-Organizer-MCP/issues/71).

Use this instead of step 4 when you cannot open inbound ports (for example behind CGNAT)
or do not want to use Tailscale. [`cloudflared`](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/)
opens an **outbound** connection to Cloudflare, and Cloudflare serves a public HTTPS
hostname that it forwards through that connection to the server.

```
Claude (custom connector, cloud-side)
        │  HTTPS (Cloudflare certificate)
        ▼
Cloudflare edge  ◄── outbound tunnel ──  cloudflared  ──►  nextcloud-organizer-mcp
                                                          (127.0.0.1:8000, plain HTTP)
```

Steps 1-3 (install, configure, systemd service) stay the same. Only the public URL
differs.

**Requirements:** a domain whose DNS is on Cloudflare (a free plan is enough). A named
tunnel needs a hostname under your own zone. Cloudflare's random `trycloudflare.com`
quick tunnels are for experiments only (see below).

### Set up a named tunnel

1. In the [Cloudflare Zero Trust dashboard](https://one.dash.cloudflare.com) open
   **Networks → Tunnels → Create a tunnel**, choose **Cloudflared**, and name it.
2. On your server, install `cloudflared` from
   [Cloudflare's package repository](https://pkg.cloudflare.com/) and run the install
   command the dashboard shows for your OS. It registers a `cloudflared` systemd service
   that starts on boot with your tunnel token.
3. Add a **Published application route**:
   - Subdomain and domain: pick the domain **from the dropdown**, for example
     `organizer.example.com`.
   - Service type `HTTP`, URL `127.0.0.1:8000`. Use `127.0.0.1` rather than `localhost`,
     because the server only binds IPv4 loopback.
4. In `/etc/nextcloud-organizer-mcp.env` set the public URL to exactly that hostname
   (scheme and host, no path, no trailing slash) and restart the server:

   ```bash
   PUBLIC_BASE_URL=https://organizer.example.com
   MCP_OAUTH_PASSWORD=<long random value>
   MCP_OAUTH_STATE_DIR=/var/lib/nextcloud-organizer-mcp/oauth-state
   MCP_HOST=127.0.0.1
   MCP_PORT=8000
   ```

   ```bash
   sudo systemctl restart nextcloud-organizer-mcp
   ```

5. Add the connector in Claude with the URL `https://organizer.example.com/mcp`
   (see [Connect Claude](#5-connect-claude)).

The tunnel token is a credential: anyone holding it can run a connector for your tunnel.
If it leaks, delete the tunnel in the dashboard and create a new one.

### What becomes public

The whole server becomes reachable from the internet at your hostname, exactly as with
Funnel. Cloudflare does not add authentication on its own here. The server's OAuth 2.1
flow and the `MCP_OAUTH_PASSWORD` consent page are the protection, which is why the server
refuses to start without the password on a non-localhost `PUBLIC_BASE_URL`. Do not put
**Cloudflare Access** in front of the hostname: Claude's connector cannot complete an
Access login, so the connection is expected to fail (untested). Keep the server bound to
`127.0.0.1` so the tunnel is the only way in.

### Client addresses and the consent rate limit

The consent page limits failed password attempts per client IP. Uvicorn takes that
address from `X-Forwarded-For`, but only when the connection comes from a trusted
address, and by default that is `127.0.0.1` only (`FORWARDED_ALLOW_IPS`).

- **`cloudflared` on the same host as the server (this recipe):** works as is. The
  server sees the real client address.
- **`cloudflared` on another host or in a container:** every request arrives from the
  tunnel host's address, so all visitors share one rate-limit bucket. Ten wrong attempts
  from anyone would then lock the owner out of the consent page too. Set
  `FORWARDED_ALLOW_IPS` to the address `cloudflared` connects from, for example
  `FORWARDED_ALLOW_IPS=172.18.0.2`. Do not set it to `*` while the server is reachable
  by other means than the tunnel, because any client could then forge its address. This
  setting was checked in the Uvicorn source only, not run against a remote
  `cloudflared`.

### Quick tunnels are not a production setup

`cloudflared tunnel --url http://127.0.0.1:8000` needs no account and prints a random
`https://<words>.trycloudflare.com` URL, but the URL changes every time the process
restarts. `PUBLIC_BASE_URL` and the Claude connector would have to change with it.
Cloudflare also states that quick tunnels do not support Server-Sent Events. Use one only
to try the flow for a few minutes, never as a lasting setup.

### What has and has not been tested

Tested on 2026-09-30 with `nextcloud-organizer-mcp` 0.2.1, `cloudflared` 2026.9.3 on
Debian 12 (Linux amd64) and a Cloudflare **free** plan with Bot Fight Mode off. The tests
used a **quick tunnel** and a scripted OAuth client (Python `httpx`), not a real Claude
client, against a real Nextcloud instance:

| Check | Result |
|---|---|
| OAuth discovery (`/.well-known/oauth-protected-resource/mcp`, `/.well-known/oauth-authorization-server`) advertises the public `https://` URLs | Passed |
| `POST /mcp` without a token returns `401` with `WWW-Authenticate: Bearer resource_metadata=...` | Passed |
| Dynamic Client Registration, `/authorize`, consent page, `/token` with PKCE | Passed |
| Read-only tool call (`list_task_lists`) over `POST /mcp` against real Nextcloud | Passed |
| Issued token still valid after two server restarts (persistent `MCP_OAUTH_STATE_DIR`) | Passed |
| Cloudflare challenge or HTML page in front of any OAuth or MCP endpoint | None seen (free plan, Bot Fight Mode off, default settings, scripted client) |
| Forged `X-Forwarded-For` and `X-Real-IP` do not bypass the consent rate limit; the server logged the real client address | Passed |
| A client-sent `CF-Connecting-IP` header | Rejected by Cloudflare itself with `403 error code: 1000` before it reaches the server |
| Standalone `GET /mcp` event stream | **Not delivered** through the quick tunnel: no headers for 15 s, no `: ping` events in 200 s. The server sends a keep-alive comment every 15 s when reached directly. Request/response calls were not affected. |

Not tested: a **named tunnel** with a stable hostname (only its connection and
dashboard-delivered route config were observed), the real Claude.ai or Claude Desktop
connector, Cloudflare Access, **Bot Fight Mode or Super Bot Fight Mode enabled**, WAF
rules, caching rules, tool calls that run longer than 100 seconds, and `cloudflared` on a
separate host. Cloudflare closes idle proxied connections after about 100 seconds, which
the server's 15-second keep-alive should stay under, but this was not confirmed through a
named tunnel.

### Cloudflare settings to check if something fails

- **Bot Fight Mode / Super Bot Fight Mode** can serve a JavaScript challenge that a
  non-browser client such as Claude's connector cannot solve. Turn it off for the zone, or
  on a paid plan exclude the OAuth and MCP paths with a WAF skip rule. This is untested.
- **Cloudflare Access** in front of the hostname breaks the connector (see above).
- **Caching:** do not add cache rules for this hostname. Discovery and token responses
  must never be cached. The default showed `cf-cache-status: DYNAMIC`.

## 5. Connect Claude

### Claude.ai (web) — syncs to mobile automatically

**Settings → Connectors → Add custom connector**, URL:
`https://<hostname>.<tailnet>.ts.net/mcp`. Leave any Client ID/Secret fields blank -
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
      "args": ["-y", "mcp-remote", "https://<hostname>.<tailnet>.ts.net/mcp"]
    }
  }
}
```

Requires Node.js.

### Claude Code

```bash
claude mcp add nextcloud-organizer-mcp --transport http "https://<hostname>.<tailnet>.ts.net/mcp"
```

### Claude mobile (iOS/Android)

Add the connector on claude.ai web (above) - it syncs to mobile automatically. Connectors
can't be added directly from the mobile app.

## Local-only use (no public URL)

If the client runs on the same machine as the server, you can skip the public URL, TLS
and the consent password entirely. This works for clients that connect from your own
machine (Claude Code, Claude Desktop through the `mcp-remote` bridge, MCP Inspector). It
does **not** work for the Claude.ai web or mobile connector, see
[below](#why-a-cloud-hosted-connector-cannot-use-it).

### Configure

```bash
NEXTCLOUD_BASE_URL=https://cloud.example.com
NEXTCLOUD_USERNAME=<nextcloud user>
NEXTCLOUD_APP_PASSWORD=<app password>

PUBLIC_BASE_URL=http://127.0.0.1:8000
MCP_HOST=127.0.0.1
MCP_PORT=8000
# MCP_OAUTH_PASSWORD is not needed here
MCP_OAUTH_STATE_DIR=$HOME/.local/state/nextcloud-organizer-mcp/oauth-state
```

The server does not read a `.env` file by itself; export the variables first:

```bash
set -a; . ./.env; set +a
uv run --no-dev nextcloud-organizer-mcp     # MCP endpoint: http://127.0.0.1:8000/mcp
```

`MCP_OAUTH_PASSWORD` may be left out **only** when `PUBLIC_BASE_URL` points at
`localhost`, `127.0.0.1` or `::1` **and** `MCP_HOST` is one of those as well. This does
not switch OAuth off: clients still discover the server, register themselves
(Dynamic Client Registration), and exchange PKCE codes for tokens, and `/mcp` still
returns `401` without a valid token. Only the interactive consent page is skipped, so the
authorization completes without a prompt. Any process on the machine that can reach the
port can therefore obtain a token, so use this only on a machine you trust.

The check is enforced at startup. With no password, all of these refuse to start with
`MCP_OAUTH_PASSWORD is required when PUBLIC_BASE_URL is not localhost or MCP_HOST is not
a local bind address`: `MCP_HOST=0.0.0.0`, `MCP_HOST=<a LAN address>`, a public
`PUBLIC_BASE_URL`, and a LAN-address `PUBLIC_BASE_URL`. Set a password to lift the
restriction for that case.

### Connect a local client

**Claude Desktop** (through [`mcp-remote`](https://github.com/geelen/mcp-remote), needs
Node.js). Use the absolute path to `npx`, because GUI apps do not inherit your shell
`PATH` (`which npx`):

```json
{
  "mcpServers": {
    "nextcloud-organizer-mcp": {
      "command": "/opt/homebrew/bin/npx",
      "args": ["-y", "mcp-remote", "http://127.0.0.1:8000/mcp"]
    }
  }
}
```

The first start opens your browser for the authorization step, which completes
immediately. `mcp-remote` stores its tokens under `~/.mcp-auth/`.

**Claude Code:**

```bash
claude mcp add nextcloud-organizer-mcp --transport http http://127.0.0.1:8000/mcp
```

Then run `/mcp` inside Claude Code and authenticate the server once.

### Redirect domains

The OAuth redirect goes to the client's own loopback callback, and the server checks
its host against `MCP_OAUTH_ALLOWED_REDIRECT_DOMAINS`. When you leave that unset and
`PUBLIC_BASE_URL` is local, the default list is `claude.ai`, `claude.com` and
`localhost`. That covers clients that use `http://localhost:<port>/...` callbacks,
including `mcp-remote` (`http://localhost:<port>/oauth/callback`). A client that
registers `http://127.0.0.1:<port>/...` instead is rejected with
`Redirect URI domain not allowed`; add `127.0.0.1` to
`MCP_OAUTH_ALLOWED_REDIRECT_DOMAINS` for such a client.

### Why a cloud-hosted connector cannot use it

Claude.ai (web and mobile) does not connect from your browser or phone. Anthropic's
servers perform the OAuth flow and every tool call. For those servers, `127.0.0.1` is
their own loopback interface, not your machine, so a `PUBLIC_BASE_URL` of
`http://127.0.0.1:8000` can never be reached. A cloud connector needs a public HTTPS URL
and `MCP_OAUTH_PASSWORD`; use the [Tailscale Funnel setup](#4-expose-via-tailscale-funnel)
above.

### Verified setup

Tested on 2026-09-30 against a real Nextcloud (CalDAV over HTTPS, app password), with
the server started as above from this repository:

| Component | Version |
|---|---|
| Server | 0.2.1, FastMCP 4.0.10, Python 3.12.13 |
| `mcp-remote` | 0.14.3 (`npx -y mcp-remote@0.14.3 http://127.0.0.1:8000/mcp`, no `--allow-http` needed for `127.0.0.1`) |
| Node.js | 26.4.0 |
| Claude Code | 2.1.285 (server added and reported `Needs authentication`; interactive `/mcp` login not run) |

With `mcp-remote`: discovery, registration and authorization completed without a consent
page (redirect `http://localhost:12570/oauth/callback`), a `list_task_lists` call returned
the Nextcloud task lists, and after a server restart the client reconnected without
authorizing again (tokens persist in `MCP_OAUTH_STATE_DIR`).

## Native stdio transport (evaluated, not implemented)

The MCP stdio transport would let a client start the server as a child process, with no
port, URL or OAuth. **Decision: worth implementing; tracked in
[#78](https://github.com/Vando-sketch/Nextcloud-Organizer-MCP/issues/78), not part of this guide.**

A throwaway probe showed the existing `build_server(...).run(transport="stdio")` already
answers `initialize` and `tools/list` unchanged. Nothing writes to stdout except protocol
messages, and logging goes to stderr. Authentication is ignored on stdio, and no OAuth
state directory is created.

What it would simplify: no `mcp-remote`/Node.js, no `PUBLIC_BASE_URL`, no
`MCP_OAUTH_STATE_DIR`, no browser step, no port to protect.

What it costs and what an implementation must handle:

- **Secrets.** The Nextcloud app password moves into each client's config (`env` block),
  in plaintext, instead of one `600` env file.
- **Processes.** Every client session starts its own server process. The process must exit
  when stdin closes, and several clients multiply the connections to Nextcloud.
- **Transport selection.** `main()` needs a switch (flag or `MCP_TRANSPORT`), and stdio must
  not require `PUBLIC_BASE_URL` or construct `PersonalAuthProvider`.
- **stdout hygiene.** Stdout must stay clean; keep logging on stderr and add a test.
- **GUI clients** need absolute paths to `uv` in their config.
- **Scope.** stdio does not help Claude.ai web or mobile, which still need the HTTP
  server behind a public URL.

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
| Claude.ai says "error connecting" while adding the connector | `PUBLIC_BASE_URL` doesn't exactly match the Funnel URL (scheme/host mismatch breaks OAuth discovery); or the server isn't reachable from the internet yet (check `tailscale funnel status`) |
| OAuth prompt appears but authorization fails | The consent page rejected the submitted `MCP_OAUTH_PASSWORD`; retry the form with the configured password. If authorization fails before the consent page appears, check that the redirect domain is in `MCP_OAUTH_ALLOWED_REDIRECT_DOMAINS` (only relevant if you changed the default). See [Authentication](authentication.md) for the consent flow. |
| Service fails to start: `MCP_OAUTH_PASSWORD is required...` | `PUBLIC_BASE_URL` isn't localhost and `MCP_OAUTH_PASSWORD` is unset - this is enforced deliberately, set the password (step 2) |
| `401` calling `/mcp` after Claude was previously connected | Access token expired or was revoked; disconnect and reconnect the connector in Claude to re-run the OAuth flow |
| OAuth state lost after a restart | `MCP_OAUTH_STATE_DIR` isn't pointing at a persistent, writable path - confirm the systemd `StateDirectory` is set and matches |
| "Nextcloud rejected the CalDAV credentials" | Wrong username or expired/revoked app password |
| "Could not reach the Nextcloud server" | Nextcloud down, or the container can't resolve/route to it |
| Local setup: `Redirect URI domain not allowed` during authorization | The client registered a `http://127.0.0.1:<port>/...` callback; add `127.0.0.1` to `MCP_OAUTH_ALLOWED_REDIRECT_DOMAINS` (see [Local-only use](#local-only-use-no-public-url)) |
| Local setup: service refuses to start with `MCP_OAUTH_PASSWORD is required...` | `MCP_HOST` is not a loopback address (e.g. `0.0.0.0`) or `PUBLIC_BASE_URL` is not loopback; set a password, or bind to `127.0.0.1` |
| Local setup: `Missing required environment variable` although a `.env` exists | The server does not read `.env` itself; `set -a; . ./.env; set +a` before starting |
| `tailscale funnel` refuses to start | Funnel not enabled for this node in the tailnet admin console (Settings → Funnel) |
| Cloudflare Tunnel: hostname returns Cloudflare error `1033` or `502` | `cloudflared` is not running or not connected (`systemctl status cloudflared`, `journalctl -u cloudflared`), or the route's service URL does not match where the server listens (`127.0.0.1:8000`) |
| Cloudflare Tunnel: the hostname does not resolve | The published route was saved with a typed or placeholder domain instead of one picked from the dropdown; check that the route shows a real hostname under your zone |
| Cloudflare Tunnel: discovery returns HTML, `403` or `503` instead of JSON | A Cloudflare challenge, bot protection or Access policy sits in front of the hostname; check **Security → Events** in the dashboard and the settings above |
| Cloudflare Tunnel: discovery URLs show `http://` or another host | `PUBLIC_BASE_URL` does not exactly match the public `https://` hostname; fix it and restart the server |
| Cloudflare Tunnel: long-running calls or the event stream drop | Cloudflare closes idle connections after about 100 s, and quick tunnels do not stream events; use a named tunnel and check that the client reconnects |
| Cloudflare Tunnel: consent page answers `429` for everyone | `cloudflared` runs on another host, so all clients share one address; set `FORWARDED_ALLOW_IPS` (see above) |

Server logs: `journalctl -u nextcloud-organizer-mcp -f`. Unexpected internal errors are logged
there with full tracebacks, while the MCP client only ever sees a short generic message.
