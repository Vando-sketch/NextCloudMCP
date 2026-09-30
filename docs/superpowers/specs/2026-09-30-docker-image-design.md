# Ship a Docker image (issue #64)

Status: design approved 2026-09-30, spec awaiting review.

## Goal

A user with Docker and a Nextcloud instance runs the server with one documented
command or one compose file, without installing Python or uv. The image is
published to GHCR by the release workflow.

## Constraints

- The server binds `MCP_HOST`/`MCP_PORT`. A non-local bind requires
  `MCP_OAUTH_PASSWORD` (`config.py`), so the image binds `0.0.0.0` and the
  compose file makes the password mandatory. This holds even when
  `PUBLIC_BASE_URL` is `http://127.0.0.1:8000`.
- OAuth state (`oauth_tokens.json` in `MCP_OAUTH_STATE_DIR`) must survive
  container restarts. `PersonalAuthProvider` creates the directory `0700` and
  the file `0600`, and `chmod`s the directory, so the directory must be owned
  by the container user.
- The server does no TLS. `PUBLIC_BASE_URL` must be the public HTTPS URL that
  clients use. A reverse proxy or tunnel in front terminates TLS.
- Secrets (`NEXTCLOUD_APP_PASSWORD`, `MCP_OAUTH_PASSWORD`) never appear in the
  image (no `ENV`/`ARG`/build context) or in logs.
- Non-root user, small image.
- CI stays green (ruff, mypy, pytest) and the existing PyPI release job is
  unchanged.

## Decisions

- **Registry: GHCR only**, `ghcr.io/vando-sketch/nextcloud-organizer-mcp`,
  chosen by the maintainer. The release job authenticates with `GITHUB_TOKEN`,
  so no new secrets. No Docker Hub.
- **Build from the repo with uv, not `pip install` from PyPI.** The build uses
  `uv.lock`, so the image is reproducible and does not depend on PyPI
  propagation timing inside the release job.
- **`python:3.12-slim` base, not Alpine or distroless.** Wheels for `lxml` and
  `cryptography` exist for glibc on amd64 and arm64. A shell stays available
  for `docker exec` admin use.
- **Architectures: `linux/amd64` and `linux/arm64`** (Raspberry Pi and Apple
  Silicon hosts are common for self-hosters).
- **No bundled TLS proxy or Tailscale sidecar.** The issue defers non-Tailscale
  setups to a separate deployment issue. `docs/docker.md` names the options.

## Components

### `Dockerfile`

- Builder stage: `python:3.12-slim` with uv copied from
  `ghcr.io/astral-sh/uv`. Runs `uv sync --locked --no-dev --no-editable` into
  `/app/.venv`, with dependency layers cached before the source copy.
- Runtime stage: `python:3.12-slim`. Copies only `/app/.venv`. Creates uid
  `10001` and `/data` owned by it.
- `ENV MCP_HOST=0.0.0.0 MCP_OAUTH_STATE_DIR=/data PATH=/app/.venv/bin:$PATH`,
  `VOLUME /data`, `EXPOSE 8000`, `USER 10001`.
- `ENTRYPOINT ["nextcloud-organizer-mcp"]`. With the venv on `PATH`,
  `docker exec <c> nextcloud-organizer-mcp-admin --state-dir /data list` works.
- `HEALTHCHECK` runs a `python -c` `urllib` request against
  `/.well-known/oauth-authorization-server`. That endpoint is public and
  returns 200. `/mcp` returns 401 and `urllib` raises on 4xx, so it is not
  usable. The server has no dedicated health route and none is added.
- OCI labels: `org.opencontainers.image.source`/`description`/`licenses`, and
  `io.modelcontextprotocol.server.name` set to
  `io.github.Vando-sketch/nextcloud-organizer-mcp` (must equal the `name` in
  `server.json`; used by the MCP Registry to verify image ownership).

### `.dockerignore`

Excludes `.git`, `.env*`, `.oauth-state`, `.venv`, caches, `tests`, `docs`,
`dist`, `graphify-out`. Keeps secrets out of the build context.

### `compose.yaml`

- Service using the GHCR image, `restart: unless-stopped`, `env_file: .env`
  (optional), named volume on `/data`.
- Required values via `${VAR:?message}`: `NEXTCLOUD_BASE_URL`,
  `NEXTCLOUD_USERNAME`, `NEXTCLOUD_APP_PASSWORD`, `PUBLIC_BASE_URL`,
  `MCP_OAUTH_PASSWORD`. Compose fails fast with a clear message rather than the
  container crash-looping.
- Port published as `127.0.0.1:${MCP_PORT:-8000}:8000`. The reverse proxy or
  tunnel runs on the host and terminates TLS. Docs explain how to widen it.

### Release workflow (`.github/workflows/release.yml`)

New `docker` job, independent of the PyPI `build`/`publish` jobs, on
`release: published`:

1. Checkout, verify the tag matches `uv version --short` (same check as the PyPI
   job).
2. `docker/setup-qemu-action`, `docker/setup-buildx-action`,
   `docker/login-action` to `ghcr.io` with `GITHUB_TOKEN`
   (`permissions: contents: read, packages: write`).
3. `docker/metadata-action`: tags `vX.Y.Z`, `X.Y`, `latest`. The `latest` and
   `X.Y` tags apply to non-prerelease releases only.
4. `docker/build-push-action`, platforms `linux/amd64,linux/arm64`, GHA layer
   cache.

### CI

- `ci.yml`: new `docker` job. It builds the image for the runner architecture
  without pushing and smoke-tests it: run with dummy env, wait for the container
  to report healthy, assert `/.well-known/oauth-authorization-server` returns
  200, assert `/mcp` without a token returns 401, assert the process is not
  uid 0, assert the container refuses to start without `MCP_OAUTH_PASSWORD`
  (documents the `0.0.0.0` bind gotcha), and assert the secrets are absent from
  `docker history` and from the container logs.
- `integration.yml`: Docker variant that runs the freshly built image against
  the existing Nextcloud service container and calls a real tool. This
  reuses the existing service-container pattern. It is manual/scheduled like
  the current job, not per-PR.

### Docs

- New `docs/docker.md`: `docker run` one-liner and compose usage; required and
  optional variables (link to `.env.example`); TLS options (reverse proxy,
  tunnel; Tailscale Funnel on the host still works); volume permissions (named
  volume works; bind mounts are root-owned and need `chown 10001`); admin CLI
  through `docker exec`; updating and pinning tags; the `0.0.0.0` +
  `PUBLIC_BASE_URL` gotcha.
- README quick start: Docker block next to the `uv tool install` block.
- `docs/deployment.md`: link near the top.
- `CHANGELOG.md`: Unreleased entry.
- `server.json`: second `packages` entry with `"registryType": "oci"` and
  identifier `ghcr.io/vando-sketch/nextcloud-organizer-mcp:<version>` (format
  confirmed against the MCP Registry package-types docs), streamable-http
  transport, same environment variables. `MCP_HOST` and
  `MCP_OAUTH_STATE_DIR` are preset by the image and not listed. The identifier
  tag is bumped together with `version` on each release.
- `docs/architecture.md`: one line for the Docker files in the module layout.

## Verification

- Local (this Mac has no `docker`): YAML lint, `hadolint`-style review of the
  Dockerfile, `uv sync --locked --no-dev --no-editable` in a scratch venv to
  prove the non-editable install runs both console scripts.
- CI: the smoke test above on every PR touching Docker files or dependencies.
- Acceptance (issue): end to end against a real Nextcloud with a client
  connecting through OAuth. Automated part: the integration variant. Manual
  part, run by the maintainer on the `mcp` host before tagging: pull the built
  image, add it as a Claude custom connector, complete the consent page, call a
  tool, restart the container, confirm no re-authorization is needed.
- Nothing is tagged or published without the maintainer's manual check.

## Out of scope

Docker Hub, a bundled TLS proxy or Tailscale sidecar, Kubernetes/Helm manifests,
a dedicated `/health` route, changes to server code.

## Risks

- The release job cannot be tested without publishing a release. Mitigation:
  the same build runs in CI (single arch, no push); the multi-arch and push
  steps are the only untested part until the first release.
- The first GHCR push creates a private package by default. The maintainer
  must set it public once (documented in the PR description).
