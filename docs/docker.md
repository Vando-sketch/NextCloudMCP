# Running in Docker

A container image is published to GitHub Container Registry for `linux/amd64` and
`linux/arm64`:

```
ghcr.io/vando-sketch/nextcloud-organizer-mcp
```

Tags: `vX.Y.Z` (an exact release), `X.Y` (the latest patch of a minor release) and
`latest`. Pin `vX.Y.Z` if you want updates to be a deliberate step.

The image contains the server only. It speaks plain HTTP on port 8000 and does not
handle TLS, so you still need something in front of it that serves your
`PUBLIC_BASE_URL` over HTTPS (see [TLS](#tls)).

## Quick start with Docker Compose

You need Docker with Compose 2.24 or newer, a Nextcloud with the Calendar/Tasks and Notes
apps, and a Nextcloud app password (**Settings → Security → Devices & sessions**, never
your account password).

```bash
curl -LO https://raw.githubusercontent.com/Vando-sketch/Nextcloud-Organizer-MCP/main/compose.yaml
curl -L https://raw.githubusercontent.com/Vando-sketch/Nextcloud-Organizer-MCP/main/.env.example -o .env
```

Edit `.env`: set `NEXTCLOUD_BASE_URL`, `NEXTCLOUD_USERNAME`, `NEXTCLOUD_APP_PASSWORD`,
`PUBLIC_BASE_URL` (the public HTTPS URL clients use) and `MCP_OAUTH_PASSWORD` (uncomment
it; generate a value with `python3 -c "import secrets; print(secrets.token_urlsafe(24))"`).
Then:

```bash
docker compose up -d
docker compose logs -f
```

Compose refuses to start and names the missing variable if a required one is unset. The
server listens on `127.0.0.1:8000` of the Docker host; set `HOST_PORT` in `.env` to use
another port. Every setting documented in [`.env.example`](../.env.example) works in
`.env`, except the three the container fixes for you: `MCP_HOST`, `MCP_PORT` and
`MCP_OAUTH_STATE_DIR`.

Then add the connector in Claude as described in
[Authentication](authentication.md#registering-the-connector-in-claude): URL
`<PUBLIC_BASE_URL>/mcp`, and enter `MCP_OAUTH_PASSWORD` on the consent page.

## Quick start with `docker run`

```bash
docker run -d --name nextcloud-organizer-mcp --restart unless-stopped \
  -p 127.0.0.1:8000:8000 \
  -v nextcloud-organizer-mcp-state:/data \
  -e NEXTCLOUD_BASE_URL=https://cloud.example.com \
  -e NEXTCLOUD_USERNAME=your-username \
  -e NEXTCLOUD_APP_PASSWORD=your-app-password \
  -e PUBLIC_BASE_URL=https://mcp.example.com \
  -e MCP_OAUTH_PASSWORD=a-long-random-password \
  ghcr.io/vando-sketch/nextcloud-organizer-mcp:latest
```

Prefer `--env-file .env` to `-e` flags for the secrets, so they stay out of your shell
history.

## Things to know

**A password is always required.** Inside the container the server binds `0.0.0.0`, which
counts as a non-local bind, so it refuses to start without `MCP_OAUTH_PASSWORD` even if
`PUBLIC_BASE_URL` is `http://127.0.0.1:8000`. That is intended: see
[Authentication](authentication.md).

**Persisting OAuth state.** Client registrations and tokens (`oauth_tokens.json`) live in
`/data`. Mount a volume there, as both commands above do; without it, every connected
client has to re-authorize each time the container is re-created. A named volume works out
of the box. A bind mount (`-v ./state:/data`) is created root-owned by Docker, and the
server runs as uid `10001`, so give it away first:

```bash
mkdir state && sudo chown 10001:10001 state
```

**Secrets.** The image contains no credentials; everything is passed at runtime. The
server does not log them and the HTTP access log stays disabled. Anyone who can run
`docker inspect` on the host can still read the container's environment, as with any
Docker deployment.

**Non-root.** The process runs as uid `10001`.

**Health check.** The image reports healthy once `/.well-known/oauth-authorization-server`
answers. That check needs no credentials and does not contact Nextcloud, so a healthy
container can still have a wrong app password; check the logs if tool calls fail.

## TLS

The server does not terminate TLS, and `PUBLIC_BASE_URL` must match the URL clients use,
scheme included. Publish the port on the Docker host (loopback by default) and put one of
these in front:

- **Tailscale Funnel** on the host, as in the [deployment guide](deployment.md#4-expose-via-tailscale-funnel):
  `tailscale funnel --bg 8000`.
- **A reverse proxy** you already run (Caddy, nginx, Traefik) proxying your HTTPS host name
  to `127.0.0.1:8000`. Caddy needs one line: `mcp.example.com { reverse_proxy 127.0.0.1:8000 }`.
- **A tunnel** such as Cloudflare Tunnel pointing at `http://127.0.0.1:8000`.

If the proxy runs in another container on the same Compose network, drop the `ports:`
entry and proxy to `nextcloud-organizer-mcp:8000` instead.

## Managing issued tokens

The admin CLI is in the image:

```bash
docker compose exec nextcloud-organizer-mcp nextcloud-organizer-mcp-admin --state-dir /data list
docker compose exec nextcloud-organizer-mcp nextcloud-organizer-mcp-admin --state-dir /data revoke pat_a1b2c3
```

With `docker run`, use `docker exec nextcloud-organizer-mcp …` instead. See
[Managing issued OAuth tokens](deployment.md#managing-issued-oauth-tokens).

## Updating

```bash
docker compose pull
docker compose up -d
```

For `docker run`: `docker pull` the new tag, then `docker rm -f` and run the command
again. The volume keeps the OAuth state, so connected clients stay connected.

## Building the image yourself

```bash
docker build -t nextcloud-organizer-mcp .
```

The build installs the exact versions in `uv.lock` into a virtual environment in a first
stage and copies only that into the final image.
