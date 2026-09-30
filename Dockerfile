# syntax=docker/dockerfile:1

# ---- Build stage: resolve and install the locked dependencies with uv. ----
FROM python:3.12-slim AS builder

COPY --from=ghcr.io/astral-sh/uv:0.11 /uv /usr/local/bin/uv

ENV UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/app/.venv

WORKDIR /app

# Dependencies first, so this layer is cached until pyproject.toml/uv.lock change.
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-editable --no-install-project

# Then the project itself. README.md is needed because pyproject.toml uses it as
# the package readme.
COPY README.md ./
COPY src ./src
RUN uv sync --locked --no-dev --no-editable

# ---- Runtime stage: the venv only, no uv, no build tools, no source tree. ----
FROM python:3.12-slim

LABEL org.opencontainers.image.title="nextcloud-organizer-mcp" \
      org.opencontainers.image.description="MCP server for managing Nextcloud tasks, calendars and notes via natural language" \
      org.opencontainers.image.source="https://github.com/Vando-sketch/Nextcloud-Organizer-MCP" \
      org.opencontainers.image.licenses="MIT" \
      io.modelcontextprotocol.server.name="io.github.Vando-sketch/nextcloud-organizer-mcp"

# /data holds the persisted OAuth clients and tokens (oauth_tokens.json). It is
# created here, owned by the runtime user, so a named volume mounted on it
# inherits the right ownership.
RUN groupadd --system --gid 10001 mcp \
    && useradd --system --uid 10001 --gid mcp --no-create-home --shell /usr/sbin/nologin mcp \
    && mkdir /data \
    && chown mcp:mcp /data

COPY --from=builder /app/.venv /app/.venv

# MCP_HOST=0.0.0.0 is a non-local bind, so the server refuses to start without
# MCP_OAUTH_PASSWORD (config.py). No secret is baked into the image: every
# credential is supplied at runtime.
ENV PATH="/app/.venv/bin:${PATH}" \
    PYTHONUNBUFFERED=1 \
    MCP_HOST=0.0.0.0 \
    MCP_PORT=8000 \
    MCP_OAUTH_STATE_DIR=/data

USER 10001:10001
WORKDIR /data
VOLUME /data
EXPOSE 8000

# /mcp answers 401 without a token and urllib raises on 4xx, so probe the public
# OAuth discovery document instead. It needs neither credentials nor Nextcloud.
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD ["python", "-c", "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:' + os.environ.get('MCP_PORT', '8000') + '/.well-known/oauth-authorization-server', timeout=3)"]

ENTRYPOINT ["nextcloud-organizer-mcp"]
