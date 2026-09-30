# Deployment verification

Every deployment recipe is verified with the same shared checklist. It separates
repeatable automated checks (local fixtures with a mocked Nextcloud) from a real
external-client and Nextcloud verification, and it records setups nobody has
tested. The shared automation work is tracked in
[#75](https://github.com/Vando-sketch/Nextcloud-Organizer-MCP/issues/75).

- [Automated checks](#automated-checks): what CI proves, and what it cannot.
- [Shared checklist](#shared-checklist): the nine steps for any recipe.
- [Recipe variants](#recipe-variants): the differences for Caddy or another
  proxy, Cloudflare Tunnel, Tailscale Funnel, local HTTP and stdio, each with a
  fixed record template.
- [Evidence matrix](#evidence-matrix): what is verified, by which method.

## Automated checks

```bash
# Real Uvicorn forwarded-header trust checks; no Caddy required.
uv run pytest -q tests/test_proxy_deployment.py

# Full local HTTPS proxy checks, with an installed Caddy binary.
RUN_PROXY_TESTS=1 uv run pytest -q tests/test_proxy_deployment.py

# Or select a particular binary explicitly.
RUN_PROXY_TESTS=1 CADDY_BIN=/path/to/caddy uv run pytest -q tests/test_proxy_deployment.py
```

These checks use a local Caddy process (the pinned version in the CI
`caddy-proxy` job) running the [example Caddyfile](../examples/Caddyfile) with a
disposable TLS certificate. The Nextcloud service is mocked. With
`RUN_PROXY_TESTS=1` they cover:

| Area | Check |
|---|---|
| Advertised URLs and discovery | Authorization-server and protected-resource metadata, and the `401` challenge, carry the public HTTPS URLs. |
| Routing | `/.well-known/*`, `/register`, `/authorize`, `/consent` (GET and POST), `/token` (code and refresh grants) and `/mcp` all reach MCP through one hostname. |
| Consent and redirects | Wrong password is rejected, the right one redirects to the client callback with `state`. A redirect domain outside the allow-list is denied at `/authorize` without a consent page or code, and an unregistered `redirect_uri` is rejected. |
| MCP requests | Initialize, `tools/list` and a read-only `list_task_lists` call over Streamable HTTP. |
| Streaming | A progress event arrives through the proxy while the tool call is still running, and the idle stream survives a pause. The standalone `GET /mcp` event stream opens. |
| Restart | After the backend restarts on the same port and `MCP_OAUTH_STATE_DIR`, the proxy reaches it again, the issued token still works, and the stale MCP session gets `404` until the client initializes a new one. |
| Client IPs | Uvicorn honors forwarded client addresses only from the configured peers (both directions tested without Caddy). Caddy discards spoofed `X-Forwarded-For`, `X-Real-IP` and `Forwarded` values, so they cannot reset the consent rate limit. |
| Logs | Caddy's default error logger, filtered as documented, drops request URIs and `Referer` values. |

They do **not** establish compatibility with a public certificate, another
proxy (nginx, Traefik, Cloudflare, Tailscale), an external MCP client, real
Nextcloud, proxy or CDN idle deadlines longer than the test's pause, or
several genuinely distinct public source addresses. Those need the manual
checklist below. Keep automated fixtures and real-deployment results in
separate rows of the [evidence matrix](#evidence-matrix).

## Shared checklist

For a public recipe, run these checks from outside the server's network against
the documented hostname; [recipe variants](#recipe-variants) list what changes
for other setups. Use disposable authorization attempts for the negative tests;
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

## Recipe variants

Each variant starts from the [shared checklist](#shared-checklist) and lists
only what differs. Fill in the [record template](#record-template) for every
run, and mark steps that do not apply as "n/a" rather than deleting them.

### Caddy and other existing reverse proxies

Recipe: [4a](deployment.md#4a-expose-via-caddy-or-an-existing-proxy). Run all
nine steps from an external network. Record the proxy name and version, every
hop (CDN, second proxy) with its trust setting, `FORWARDED_ALLOW_IPS`, and the
redacted virtual host. Steps 6 and 7 are the ones a local fixture cannot cover:
use a real public certificate, two distinct public source addresses, and idle
and response deadlines inherited from an existing configuration. nginx and
Traefik have no tested configuration; a run against one must record its
buffering and timeout directives before a copyable recipe is published.

### Cloudflare Tunnel

Recipe: [4c](deployment.md#4c-alternative-cloudflare-tunnel). Use a **named**
tunnel with a hostname in your own zone (quick tunnels do not stream events).
Run all nine steps, and additionally record: `cloudflared` version and whether
it runs on the MCP host, the Cloudflare plan, Bot Fight Mode / Super Bot Fight
Mode, Access, WAF and cache rules affecting the hostname, and whether
`FORWARDED_ALLOW_IPS` had to change. In step 6, keep a call open longer than
100 seconds and record whether the client reconnects. In step 7, Cloudflare
rejects a client-sent `CF-Connecting-IP` itself; verify the rate-limit address
with the tunnel's real topology.

### Tailscale Funnel

Recipe: [4b](deployment.md#4b-expose-via-tailscale-funnel). Run steps 1-5 and 8
from an external network (a phone off Wi-Fi works), and record the Funnel
status output and the client. Client address handling behind Funnel has not been
checked: if you run step 7, record which address the server sees and whether
`FORWARDED_ALLOW_IPS` had to change. In step 6, record whether the client reconnects after
`tailscale funnel` restarts.

### Local HTTP (no public URL)

Recipe: [Local-only use](deployment.md#local-only-use-no-public-url). Run the
checklist on the client machine. Steps 2 (DNS/TLS), 6 (proxy deadlines) and 7
(client IPs) are n/a. Step 3 covers `http://127.0.0.1:8000` discovery, step 4
the consent-free authorization (verify it redirects to an allowed redirect
domain such as `localhost`), and step 8 the token surviving a restart. Also
confirm startup is refused when `MCP_HOST` is not loopback and no password is
set.

### Native stdio

Recipe: [Native stdio transport](deployment.md#native-stdio-transport). No
URL, proxy or OAuth: steps 2-4 and 6-7 are n/a. Record the client and its
configuration (without the app password), then check that the client lists the
tools (step 1 plus tool discovery), a read-only call returns real Nextcloud
data (step 5), stdout carries only protocol messages and stderr logs contain
no credentials (step 9), and the server process exits when the client closes
(step 8 equivalent). Every client session starts its own process.

## Record template

Copy this block into the issue, pull request or a
[client compatibility report](https://github.com/Vando-sketch/Nextcloud-Organizer-MCP/issues/new/choose).
Do not include secrets or private Nextcloud content.

```text
Recipe:            Caddy | other proxy | Cloudflare Tunnel | Funnel | local HTTP | stdio
Date / tester:
Server:            version or commit, FastMCP, MCP SDK, Python, OS
Client:            name and version (or web client and test date)
Proxy / tunnel:    name, version, hops, trust setting, redacted configuration
Nextcloud:         version
Discovery:         pass | fail | n/a  (step 2-3, evidence)
OAuth consent:     pass | fail | n/a  (step 4, incl. wrong password rejected)
Read-only call:    pass | fail        (step 5, tool name)
Streaming:         pass | fail | n/a  (step 6, duration, reconnect behavior)
Client IP limits:  pass | fail | n/a  (step 7, two source addresses, spoofing)
Restart:           pass | fail        (step 8, token and reconnect behavior)
Logs:              pass | fail | n/a  (step 9)
Untested / notes:
```

## Evidence matrix

"Automated" means the local fixture in `tests/` with a mocked Nextcloud.
"Manual" means a person ran the shared checklist against the named setup.
A blank or "Pending" cell means nothing has been verified. Do not treat an
automated result as evidence for the manual columns.

| Recipe | Automated | Manual: external client | Manual: real Nextcloud read-only call | Manual: restart / reconnect | Manual: distinct client IPs | Manual: streaming |
|---|---|---|---|---|---|---|
| Caddy | Passed (local Caddy 2.11.4, CI job `caddy-proxy`) | Pending | Pending | Pending | Pending | Pending |
| nginx / Traefik / other proxy | None | Pending | Pending | Pending | Pending | Pending |
| Cloudflare Tunnel | None | Pending: no named tunnel or real Claude client | Passed 2026-09-30 through a quick tunnel with a scripted client | Passed 2026-09-30 (two server restarts, quick tunnel) | Forged headers did not bypass the limit (quick tunnel, one source address) | Failed on quick tunnel (standalone `GET /mcp` not delivered); named tunnel pending |
| Tailscale Funnel | Shared OAuth tests only | Claude.ai connector and consent flow tested per the README; not recorded with this template | Pending | Pending | Pending | Pending |
| Local HTTP | Shared OAuth tests and startup rules | `mcp-remote` 0.14.3 passed 2026-09-30; Claude Code 2.1.285 reached "Needs authentication", interactive login not run | Passed 2026-09-30 (`mcp-remote`) | Passed 2026-09-30 (`mcp-remote` reconnected after a restart) | n/a | n/a |
| stdio | Passed (`tests/test_stdio.py`, real process over pipes) | Not run for Claude Desktop, Claude Code or MCP Inspector | Passed (opt-in `tests/test_integration.py`) | n/a | n/a | n/a |

Details for the manual rows are in [deployment.md](deployment.md): Cloudflare
under "What has and has not been tested", local HTTP under "Verified setup".

## Verification record

### Local automated Caddy setup, 2026-09-30

- macOS 26.6.2 (arm64), Python 3.12.13, project 0.2.1 plus the changes of #75.
- Caddy 2.11.4, FastMCP 4.0.10, MCP SDK 2.2.0, Uvicorn 0.50.0.
- Client: the test suite's JSON-RPC/OAuth harness using HTTPX 0.28.1.
- Configuration: [examples/Caddyfile](../examples/Caddyfile), adapted to temporary
  loopback ports and a disposable localhost certificate trusted only by the test
  client. Automatic certificate issuance and the Caddy admin API are disabled
  for the test. MCP trusts only `127.0.0.1`; Caddy uses isolated temporary storage.
- Nextcloud: mocked `CalDavService`; no real Nextcloud data or credentials used.
- Result: all nine proxy tests passed, five consecutive runs of the file included.
  The full suite with `RUN_PROXY_TESTS=1` passed 1,244 tests with 19
  real-Nextcloud integration tests skipped and 95% coverage. Ruff lint/format
  checks and `mypy src tests` also passed. The Linux CI job runs the same file.
  A mutation check confirmed the streaming test fails when Caddy buffers
  responses (`response_buffers unlimited`).

The local run verified HTTPS discovery URLs and the unauthenticated MCP challenge,
registration, consent form routing, wrong-password rejection, PKCE token exchange,
refresh, and an authenticated `list_task_lists` call returning an SSE response.
It verified redirect allow-list denial, incremental SSE delivery through the
proxy with a two-second idle pause, the standalone event stream, and recovery
after a backend restart (`502` while down, persisted token accepted, stale
session `404`). It also verified separate consent budgets for client addresses
forwarded by a trusted peer, rejection of forwarding from an untrusted peer, and
Caddy's removal of spoofed forwarding headers. These are transport and trust
checks; they do not establish behavior under a public proxy's idle deadline,
long-lived stream duration, or a real client's reconnect logic.

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
| Public streaming and restart/reconnect behavior | Pending | Local automation verifies incremental delivery and restart against a local Caddy; run the checklist against the intended deployment and record its duration. |

When completing the live verification, replace the relevant pending entries
with a test date, tested versions, setup and concrete result. A passing local
test run alone does not satisfy the real-deployment acceptance criteria of
[#70](https://github.com/Vando-sketch/Nextcloud-Organizer-MCP/issues/70).
