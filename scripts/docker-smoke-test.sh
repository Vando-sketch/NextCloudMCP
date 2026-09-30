#!/usr/bin/env bash
# Smoke test for the Docker image. Needs no Nextcloud: the server connects to it
# lazily, so a dummy NEXTCLOUD_BASE_URL is enough to start and to probe the
# unauthenticated endpoints. Used by the "docker" job in .github/workflows/ci.yml.
#
#   scripts/docker-smoke-test.sh <image>
set -euo pipefail

image="${1:?usage: $0 <image>}"
suffix="$$"
container="nom-smoke-${suffix}"
volume="nom-smoke-state-${suffix}"
port="${SMOKE_PORT:-18000}"
password="smoke-$(head -c 16 /dev/urandom | od -An -tx1 | tr -d ' \n')"
app_password="app-$(head -c 16 /dev/urandom | od -An -tx1 | tr -d ' \n')"

cleanup() {
  docker rm -f "$container" >/dev/null 2>&1 || true
  docker volume rm "$volume" >/dev/null 2>&1 || true
}
trap cleanup EXIT

fail() {
  echo "FAIL: $*" >&2
  docker logs "$container" 2>&1 | tail -n 30 >&2 || true
  exit 1
}

run_server() {
  docker run -d --name "$container" \
    -e NEXTCLOUD_BASE_URL=https://cloud.invalid \
    -e NEXTCLOUD_USERNAME=smoke \
    -e NEXTCLOUD_APP_PASSWORD="$app_password" \
    -e PUBLIC_BASE_URL=https://mcp.example.com \
    -e MCP_OAUTH_PASSWORD="$password" \
    -p "127.0.0.1:${port}:8000" \
    -v "${volume}:/data" \
    "$image" >/dev/null
}

wait_healthy() {
  for _ in $(seq 1 30); do
    status="$(docker inspect --format '{{.State.Health.Status}}' "$container")"
    [ "$status" = healthy ] && return 0
    [ "$(docker inspect --format '{{.State.Running}}' "$container")" = true ] || fail "container exited"
    sleep 2
  done
  fail "container did not become healthy (last status: ${status})"
}

echo "1. Refuses to start without MCP_OAUTH_PASSWORD (0.0.0.0 bind)"
if output="$(docker run --rm \
  -e NEXTCLOUD_BASE_URL=https://cloud.invalid \
  -e NEXTCLOUD_USERNAME=smoke \
  -e NEXTCLOUD_APP_PASSWORD="$app_password" \
  -e PUBLIC_BASE_URL=https://mcp.example.com \
  "$image" 2>&1)"; then
  fail "server started without MCP_OAUTH_PASSWORD"
fi
grep -q MCP_OAUTH_PASSWORD <<<"$output" || fail "startup error does not mention MCP_OAUTH_PASSWORD: $output"

echo "2. Starts and reports healthy"
run_server
wait_healthy

echo "3. Public discovery endpoint returns 200, /mcp without a token returns 401"
code="$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:${port}/.well-known/oauth-authorization-server")"
[ "$code" = 200 ] || fail "discovery endpoint returned ${code}"
code="$(curl -s -o /dev/null -w '%{http_code}' -X POST "http://127.0.0.1:${port}/mcp")"
[ "$code" = 401 ] || fail "/mcp without a token returned ${code}"

echo "4. Runs as a non-root user"
uid="$(docker exec "$container" id -u)"
[ "$uid" = 10001 ] || fail "container runs as uid ${uid}"

echo "5. Secrets are not in the image history or the logs"
if docker history --no-trunc "$image" | grep -qE "${password}|${app_password}"; then
  fail "a secret appears in the image history"
fi
if docker logs "$container" 2>&1 | grep -qE "${password}|${app_password}"; then
  fail "a secret appears in the container logs"
fi

echo "6. OAuth state survives re-creating the container"
client_id="$(curl -s -X POST "http://127.0.0.1:${port}/register" \
  -H 'content-type: application/json' \
  -d '{"client_name":"smoke","redirect_uris":["https://claude.ai/api/mcp/auth_callback"],"grant_types":["authorization_code","refresh_token"],"response_types":["code"],"token_endpoint_auth_method":"none"}' \
  | python3 -c 'import json, sys; print(json.load(sys.stdin)["client_id"])')"
[ -n "$client_id" ] || fail "client registration returned no client_id"
docker rm -f "$container" >/dev/null
run_server
wait_healthy
docker exec "$container" grep -q "$client_id" /data/oauth_tokens.json \
  || fail "registered client is gone after re-creating the container"

echo "7. Admin CLI works through docker exec"
docker exec "$container" nextcloud-organizer-mcp-admin --state-dir /data list >/dev/null \
  || fail "nextcloud-organizer-mcp-admin failed"

echo "Docker smoke test passed."
