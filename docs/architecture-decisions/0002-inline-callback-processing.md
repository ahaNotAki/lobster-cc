# ADR 0002: Inline Callback Processing (collapse relay into lobster-cc server)

## Status
Accepted — 2026-06-22.
**Supersedes** [ADR 0001](0001-self-hosted-relay.md).

## Context
[ADR 0001](0001-self-hosted-relay.md) introduced a self-hosted relay (aiohttp +
SQLite on the always-on EC2 box) to buffer WeCom callbacks during local outages,
replacing the AppSec-flagged AWS API Gateway + Lambda + DynamoDB stack.

After running ADR 0001 in production for a short period and reviewing the
deployment, three observations changed the calculus:

1. **The dashboard reverse tunnel is already production-stable.** The
   `rc-dashboard-tunnel.service` autossh tunnel forwards EC2's public port 80 →
   the desktop's lobster-cc server on `localhost:8080`. It's been live for
   months, recovers from EC2 reboots in <30s, and uses the exact same
   infrastructure pattern as the SOCKS proxy tunnel.

2. **The relay's buffer protected message *receipt* but not user *experience*.**
   If the desktop is down, the relay can verify+store callbacks, but it can't
   execute Claude Code or send WeCom replies. The user sees no service
   regardless of whether the message was buffered. The buffer only saves
   delivery during a *brief* desktop outage that recovers within WeCom's retry
   window — the same gap autossh's <30s recovery already covers.

3. **The relay added meaningful operational complexity.** A separate systemd
   unit, a `0600` `EnvironmentFile` for secrets, an SG-rule audit gate, a Bearer
   token shared between the poller and the relay, a separate logrotate config,
   the cursor-stale-after-migration class of bugs, and ~600 lines of relay code
   to maintain. None of which exists if the gateway processes callbacks inline.

## Decision
Collapse the relay into the lobster-cc server itself. WeCom POSTs directly to
`http://<elastic-ip>/wecom/callback/{agent_id}` via the existing
`rc-dashboard-tunnel.service`. The `CallbackSource` (already in the codebase
since day one) handles signature + 5-min timestamp freshness verification
inline via `WeComGateway` and dispatches into the existing executor pipeline.
No cloud buffer.

## Consequences

### Positive
- **One fewer distributed service.** `lobster-relay` systemd unit, secrets file,
  monitoring cron, deploy/audit/monitor scripts, the entire `relay/` Python
  package, and the `RelayPollingSource` poller all deleted.
- **One fewer shared secret.** The Bearer `relay_token` for `/messages/fetch`
  is gone (no fetch endpoint exists anymore).
- **Simpler config schema.** `WeComConfig` no longer carries `mode`,
  `relay_url`, `relay_token`, `relay_poll_interval_seconds`. Just credentials
  and an optional `proxy`.
- **Simpler cutover model.** Deploy code, switch one URL in WeCom admin
  console, restart server. No SG rule plumbing, no separate provisioning step.
- **Security posture preserved.** WeCom signature verification + 5-min
  freshness window are still enforced, just at `WeComGateway.handle_message`
  instead of the standalone relay. The AWS API Gateway authentication finding
  remains closed (no public unauthenticated endpoint exists).

### Negative
- **Message loss during sustained desktop outages.** If the desktop is down
  for longer than WeCom's 3-retry window (~15s), incoming callbacks are
  permanently lost. Brief flaps (process restart, tunnel reconnect) are
  covered by autossh (<30s recovery, well within most retry windows).
- This trade-off is acceptable for the project's single-user use case (no SLA,
  no compliance bar, retry-by-resending is trivial). It would be unacceptable
  for a multi-tenant service — restoring ADR 0001's relay buffer is the path
  back if requirements change.

### Neutral
- **Network exposure changes:** the relay had its port restricted to WeCom IPs
  (network-layer ACL); the new design serves on EC2:80 to the open internet
  (the same way the dashboard already does). Cryptographic auth (signature +
  freshness) was always the primary control — the IP allowlist was
  defense-in-depth, not the load-bearing layer. The new design keeps the
  primary control, drops the auxiliary one.

## Implementation Notes
- `WeComGateway.handle_message` enforces a 300s timestamp freshness check
  immediately after signature verification. Replay protection moves from the
  relay to the gateway (the new trust boundary).
- WeCom callback URL: `http://<elastic-ip>/wecom/callback/{agent_id}`
  (under `/wecom/` — matches the path `CallbackSource` registers; avoids
  namespace pollution as the lobster-cc app grows).
- `WeComConfig` uses `extra="ignore"` so leftover legacy fields
  (`mode`, `relay_url`, `relay_token`, `relay_poll_interval_seconds`) in an
  in-the-wild config don't crash the upgraded server during cutover.
- Removed: `src/remote_control/relay/`, `RelayPollingSource`, the relay
  deployment scripts, `tests/test_relay_app.py`, `tests/test_audit_sg.py`,
  `tests/test_message_source.py`, the `lobster-relay` systemd unit, the SG
  rules on :8443.
- Kept: `defusedxml` dep — `WeComGateway` parses attacker-controlled XML
  before signature verification can complete (the `<Encrypt>` field needs
  extraction first), so the same hardening applies.

## Why no local SQLite buffer in lobster-cc?
The relay's buffer protected against (a) temporary poller-server gap and
(b) executor backlog. Of these, (b) is already solved by the existing executor
task queue, and (a) doesn't exist in this design (no poller). Adding a local
buffer between the gateway and the executor would solve a problem that doesn't
exist. If durable mid-task crash recovery becomes a need, the existing `tasks`
table in SQLite (where each task is recorded on creation) is the right place
to add it — the gateway already enqueues to that table before responding.
