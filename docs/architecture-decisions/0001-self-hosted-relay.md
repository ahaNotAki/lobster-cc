# ADR 0001: Self-Hosted Relay (replace AWS API Gateway + Lambda + DynamoDB)

## Status
Accepted — 2026-06-15

## Context
An AppSec scan filed finding `APIGAuthenticationCheck` against two unauthenticated
API Gateway POST endpoints:

- `POST /messages/fetch` — returned the buffered message queue with no auth; anyone
  with the URL could drain it.
- `POST /callback` — the Lambda stored any posted body to DynamoDB **without
  verifying the WeCom signature**, an open write / DoS vector.

We were required to remove API Gateway and DynamoDB.

WeCom callback delivery semantics (official doc, `developer.work.weixin.qq.com/document/path/90930`):
5-second response deadline per attempt, **3 retries** on connection-failure/timeout
only, a short and undocumented retry window, dropped events **permanently lost**,
and **no fetch-later API**. Official guidance: do not hard-depend on callbacks.

## Decision
Adopt **Option C**: a small self-hosted aiohttp relay on the existing always-on
EC2 box (Elastic IP `18.142.75.174`). It buffers raw callbacks in short-TTL SQLite
and serves them to the local poller over an authenticated `/messages/fetch`. The
`/callback` endpoint verifies the WeCom signature **and** a 5-minute timestamp
freshness window. The EC2 security group opens the relay port only to WeCom IP
ranges (never `0.0.0.0/0`).

Two defense layers: (1) IP allowlist at the network boundary, (2) cryptographic
auth (WeCom signature on `/callback`, Bearer token on `/messages/fetch`).

## Rejected: Option A (reverse SSH tunnel, no buffer)
A reverse SSH tunnel from the desktop to EC2, processing callbacks inline with no
store, would also remove all managed AWS services. **Rejected** because of the
retry policy above: if the desktop or tunnel is down for even a few seconds during
WeCom's short retry window, the message is permanently lost. The cloud buffer is
the entire reason the relay exists.

## Consequences
- **Freshness is enforced at the relay ingress ONLY**, never on the local dispatch
  path. Enforcing it locally would discard messages legitimately buffered during a
  local outage longer than the freshness window — reintroducing the exact loss
  problem this design avoids. The relay sits where WeCom delivers within seconds,
  so freshness there is correct; the local poller relies on signature verification
  plus monotonic cursor/seq de-duplication (already implemented).
- We still operate a relay process, but on infrastructure we control, with a far
  smaller blast radius than a 7-day DynamoDB table.
- TLS is deferred to Phase 2 (see `docs/security.md`). WeCom payloads are AES-CBC
  encrypted + HMAC-signed + timestamp-bound, and WeCom officially permits plain
  HTTP on custom ports.
