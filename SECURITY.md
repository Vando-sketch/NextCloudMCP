# Security Policy

nextcloud-organizer-mcp holds credentials for a Nextcloud account (an app
password) and, in its intended deployment, is reachable from the public
internet behind an OAuth 2.1 gate. Security reports are taken seriously and
are very welcome.

## Supported versions

Only the latest release receives security fixes. Please make sure you can
reproduce an issue on the latest release (or on `main`) before reporting it.

| Version         | Supported |
| --------------- | --------- |
| latest `0.x`    | Yes       |
| older releases  | No        |

## Reporting a vulnerability

**Please do not report security vulnerabilities through public GitHub issues,
discussions or pull requests.**

Report them privately via GitHub's
[private vulnerability reporting](https://github.com/Vando-sketch/nextcloud-organizer-mcp/security/advisories/new)
("Security" tab → "Report a vulnerability"). This keeps the report confidential
between you and the maintainer until a fix is published.

Please include as much of the following as you can:

- The type of issue (e.g. authentication bypass, token leakage, SSRF,
  iCalendar injection, denial of service).
- The affected version or commit, and the relevant configuration
  (environment variables with secrets redacted).
- Step-by-step instructions to reproduce, ideally with a proof of concept.
- The impact: what an attacker can achieve and under which preconditions.

## What to expect

This is a volunteer-maintained project, so the timelines below are goals, not
guarantees:

- **Acknowledgement** within 5 days.
- **Initial assessment** (confirmed / not a vulnerability / need more
  information) within 14 days.
- **Fix and advisory** for confirmed issues as soon as practical, typically
  within 90 days. You will be kept informed of progress.

Once a fix is released, a GitHub Security Advisory is published (with a CVE
where appropriate). Reporters are credited in the advisory unless they prefer
to stay anonymous.

Please give us a reasonable chance to fix the issue before disclosing it
publicly, and do not access, modify or delete data that isn't yours while
testing — use your own Nextcloud instance and your own deployment.

## Scope

In scope — vulnerabilities in the code in this repository, for example:

- Bypassing the OAuth consent password (`MCP_OAUTH_PASSWORD`) or obtaining an
  access token without it.
- Leaking access/refresh tokens, the consent password or the Nextcloud app
  password (in responses, error messages or logs).
- Reading or modifying Nextcloud data outside what an authenticated MCP client
  is meant to reach.
- Weaknesses in the vendored `PersonalAuthProvider`
  (`src/nextcloud_organizer_mcp/personal_auth.py`) as used by this project.

Out of scope:

- Vulnerabilities in Nextcloud itself, FastMCP, the `caldav` library or other
  dependencies — please report those upstream (a note to us is still
  appreciated if this project is affected).
- Issues that require an already-authenticated MCP client or access to the
  server's host, environment file or OAuth state directory.
- Deployments that ignore the documented hardening, e.g. a public server
  without `MCP_OAUTH_PASSWORD`, `NEXTCLOUD_ALLOW_INSECURE_HTTP=1` in
  production, or a world-readable environment file.
- Behaviour of the MCP client or LLM using the server (e.g. prompt injection
  that makes a model call a tool the user authorised it to call).

## Hardening your deployment

See the [Authentication](README.md#authentication) section of the README and
the [deployment guide](docs/deployment.md). In short: always set a long random
`MCP_OAUTH_PASSWORD`, use a dedicated Nextcloud app password (never your
account password), keep the server bound to `127.0.0.1` behind a TLS-
terminating proxy, keep the environment file and `MCP_OAUTH_STATE_DIR`
readable only by the service user, and stay on the latest release.
