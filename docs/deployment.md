# Deployment guide

This guide covers the intended production setup: the server running on an Ubuntu LXC
container, reachable only over a private [Tailscale](https://tailscale.com) network, with
`tailscale serve` terminating TLS in front of it.

```
Claude (custom connector)
        │  HTTPS (Tailscale cert)
        ▼
tailscale serve  ──►  nextcloud-task-mcp (127.0.0.1:8000, plain HTTP)
                              │  HTTPS + app password
                              ▼
                      Nextcloud (CalDAV)
```

The server itself never handles TLS and never listens on a public interface. Its only
own protection is the static bearer token — appropriate here because the transport
security and network access control are provided by Tailscale.

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
MCP_AUTH_TOKEN=<long random token>
MCP_HOST=127.0.0.1
MCP_PORT=8000
```

Generate the token with:

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(32))"
```

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

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now nextcloud-task-mcp
sudo systemctl status nextcloud-task-mcp
```

## 4. Expose via Tailscale

```bash
sudo tailscale serve --bg 8000
```

This publishes `https://<hostname>.<tailnet>.ts.net/` (TLS certificate managed by
Tailscale) and proxies it to `127.0.0.1:8000`. Only devices in your tailnet can reach it.
Check with:

```bash
tailscale serve status
```

## 5. Connect Claude

Add a custom connector with:

- **URL:** `https://<hostname>.<tailnet>.ts.net/mcp` (note the `/mcp` path)
- **Authentication:** Bearer token, the value of `MCP_AUTH_TOKEN`

The device running the Claude client must be part of the same tailnet (or the request
must be routed through one that is).

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
| Claude gets "Missing or invalid bearer token" | Connector token doesn't match `MCP_AUTH_TOKEN` on the server |
| "Nextcloud rejected the CalDAV credentials" | Wrong username or expired/revoked app password |
| "Could not reach the Nextcloud server" | Nextcloud down, or the container can't resolve/route to it |
| Connector can't connect at all | `tailscale serve` not running, wrong `/mcp` path, or client device not in the tailnet |

Server logs: `journalctl -u nextcloud-task-mcp -f`. Unexpected internal errors are logged
there with full tracebacks, while the MCP client only ever sees a short generic message.
