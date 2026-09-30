# Deployment verification

Use this checklist for Caddy and any existing reverse proxy. It separates
repeatable local checks from a real public client/Nextcloud deployment. The
shared automation work is tracked in
[#75](https://github.com/Vando-sketch/Nextcloud-Organizer-MCP/issues/75).

## Automated checks

```bash
# Real Uvicorn forwarded-header trust checks; no Caddy required.
uv run pytest -q tests/test_proxy_deployment.py

# Full local HTTPS proxy checks, with an installed Caddy binary.
RUN_PROXY_TESTS=1 uv run pytest -q tests/test_proxy_deployment.py

# Or select a particular binary explicitly.
RUN_PROXY_TESTS=1 CADDY_BIN=/path/to/caddy uv run pytest -q tests/test_proxy_deployment.py
```

These checks use a local Caddy process and disposable TLS certificates. The
Nextcloud service is mocked; they do not establish compatibility with a
public certificate, an external MCP client, or a real Nextcloud server.
See the test assertions and CI job for the current automated coverage.

## Public deployment checklist

Run these checks from outside the server's network against the documented
hostname. Use disposable authorization attempts for the negative tests;
consent failures deliberately consume the per-IP rate-limit budget. Never
include passwords, tokens, authorization codes, consent pending keys, or
private Nextcloud content in a verification report.

1. **Record the setup.** Note the project commit/package version, FastMCP and
   MCP SDK versions, proxy version (`caddy version` for Caddy), client name and
   version (or web client and test date), Nextcloud version, operating system,
   and each proxy hop. Record a redacted configuration and whether the backend
   and proxy share the host network.
2. **DNS and TLS.** Check A and AAAA records from the external network. Fetch
   `https://organizer.example.com/.well-known/oauth-authorization-server`
   without disabling certificate validation. Expect JSON with public HTTPS
   issuer and endpoint URLs on the MCP hostname. Check that HTTP redirects
   to HTTPS and that `cloud.example.com` still serves Nextcloud.
3. **Protected-resource discovery.** Send an unauthenticated POST to `/mcp`.
   Expect `401` and a `WWW-Authenticate` challenge. Fetch its
   `resource_metadata` URL (currently
   `/.well-known/oauth-protected-resource/mcp`). Verify the resource and
   authorization-server URLs use the public hostname, without backend IPs
   or an unexpected path prefix.
4. **Client and OAuth consent.** Add
   `https://organizer.example.com/mcp` to the external client's connector
   configuration. Allow dynamic client registration, then complete the
   browser consent page with `MCP_OAUTH_PASSWORD`. Check that a wrong password
   is rejected and that the configured password completes a fresh attempt.
   Verify the final callback reaches the client's allowed redirect domain.
5. **Read-only Nextcloud call.** Through the authenticated client, call
   `list_task_lists` or `list_calendars`. Compare its result with the real
   Nextcloud instance. Record success and the tool name without publishing
   private list/calendar names. Listing available tools alone does not verify
   the Nextcloud connection.
6. **Streaming.** Establish an authenticated MCP stream through the public
   proxy. Confirm incremental SSE delivery rather than delivery only when
   the response closes; keep the connection open beyond any inherited short
   proxy deadline. Record the duration, any configured timeout and whether
   the client reconnects after a proxy reload or server restart.
7. **Client IP and spoofing.** From two genuinely distinct source IPs, confirm
   exhausting the first IP's consent failure budget does not block the
   second. From the blocked source, change a supplied `X-Forwarded-For` value
   and confirm it cannot restore access. Check the backend's trusted proxy
   peer and every upstream hop. Repeat these checks after changing proxy
   trust settings. Clients sharing a NAT address naturally share the IP
   budget.
8. **Persistence.** Restart MCP with the same `MCP_OAUTH_STATE_DIR`. Reconnect
   using the previously issued credentials and perform another read-only
   call. Restart interrupts in-flight requests and pending consent attempts;
   the client may need to create a fresh transport session.
9. **Logs.** Inspect proxy/CDN and application logs after the disposable OAuth
   flow. Confirm sensitive query values, authorization headers, credentials
   and consent form bodies are absent or redacted. Review imported proxy
   logging configuration as well as the new virtual host. Trigger an upstream
   failure with disposable URI and `Referer` marker values and inspect runtime
   error logs too: disabling site access logs does not disable these logs.
   Verify separate access log encoders and upstream/CDN outputs independently.
   Keep debug/trace logging disabled during the OAuth checks.

## Verification record

### Local automated setup, 2026-09-30

- macOS 26.6.2 (arm64), Python 3.12.13, project 0.2.1 with these changes.
- Caddy 2.11.4, FastMCP 4.0.10, MCP SDK 2.2.0, Uvicorn 0.50.0.
- Client: the test suite's JSON-RPC/OAuth harness using HTTPX 0.28.1.
- Configuration: [examples/Caddyfile](../examples/Caddyfile), adapted to temporary
  loopback ports and a disposable localhost certificate trusted only by the test
  client. Automatic certificate issuance and the Caddy admin API are disabled
  for the test. MCP trusts only `127.0.0.1`; Caddy uses isolated temporary storage.
- Nextcloud: mocked `CalDavService`; no real Nextcloud data or credentials used.
- Result: all five proxy tests passed. The full suite with `RUN_PROXY_TESTS=1`
  passed 1,219 tests with 18 real-Nextcloud integration tests skipped and 95.38%
  coverage. Ruff lint/format checks and mypy also passed. The added Linux CI
  job has not yet been run on GitHub for this change.

The local run verified HTTPS discovery URLs and the unauthenticated MCP challenge,
registration, consent form routing, wrong-password rejection, PKCE token exchange,
refresh, and an authenticated `list_task_lists` call returning an SSE response.
It also verified separate consent budgets for client addresses forwarded by a
trusted peer, rejection of forwarding from an untrusted peer, and Caddy's removal
of spoofed forwarding headers. These are transport and trust checks; they do not
establish delayed incremental delivery, long-lived stream duration, or restart
behavior through a public proxy.

An upstream-failure probe confirmed that Caddy's unfiltered runtime error logs
include request URI and `Referer` values. The documented filter removed disposable
OAuth markers from both fields while preserving the `502` error log.

### Public deployment

| Scope | Status | Evidence |
|---|---|---|
| Public Caddy certificate and external-client discovery | Pending | No publicly reachable Caddy test deployment is available. |
| External client registration and consent through Caddy | Pending | Existing Claude/Funnel compatibility does not verify this proxy recipe. |
| Read-only call against real Nextcloud through Caddy | Pending | Local proxy tests use a mocked Nextcloud service. |
| Client IP limits from distinct public source addresses | Pending | Local automation verifies trust behavior; public proxy hops still need this check. |
| Public streaming and restart/reconnect behavior | Pending | Run the checklist against the intended deployment and record its duration. |

When completing the live verification, replace the relevant pending entries
with a test date, tested versions, setup and concrete result. A passing local
test run alone does not satisfy the real-deployment acceptance criteria of
[#70](https://github.com/Vando-sketch/Nextcloud-Organizer-MCP/issues/70).
