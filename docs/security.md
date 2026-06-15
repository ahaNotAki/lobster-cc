# Security Model — Self-Hosted Relay

This document describes the authentication and trust model for the self-hosted
WeCom relay that replaced the AWS API Gateway + Lambda + DynamoDB stack (see
[ADR 0001](architecture-decisions/0001-self-hosted-relay.md)).

## Two-layer defense

| Layer | Mechanism | Where |
|-------|-----------|-------|
| 1. Network | EC2 security group opens the relay port **only to WeCom callback IP ranges** (never `0.0.0.0/0`). Enforced by `scripts/setup-self-relay.sh` (refuses `0.0.0.0/0`) and gated by `scripts/audit-sg.sh`. | EC2 SG |
| 2a. `/callback` | WeCom signature verification (SHA1 of the sorted `token,timestamp,nonce,encrypt` — the `token` is the shared secret) **plus** 5-minute timestamp freshness. Invalid/stale/empty/unknown-agent → `403`. The raw XML is parsed with `defusedxml` (entity-expansion / XXE hardening) before verification. | Relay app |
| 2b. `/messages/fetch` | `Authorization: Bearer <fetch_token>` (32-byte random shared secret, compared with `hmac.compare_digest`). Missing/wrong → `401`. | Relay app |

The IP allowlist is the mandatory first layer; cryptographic auth is the second.
Neither alone is the only thing standing between the internet and the relay.

## Timestamp freshness placement

Freshness (reject messages with `|now - timestamp| > 300s`) is enforced **at the
relay ingress only**, never on the local dispatch path.

Rationale: WeCom delivers callbacks to the relay within seconds, so a 5-minute
window is ample real-time replay protection at the trust boundary. The local
server, however, may be down while the relay correctly buffers messages — possibly
for longer than 5 minutes. If the local dispatch path also enforced freshness, it
would discard those buffered messages on recovery, reintroducing the permanent
message-loss problem the relay exists to prevent. The local path instead relies on
signature verification (already present) and monotonic cursor/seq de-duplication.

## Plain HTTP (Phase 1) — rationale

The relay currently accepts plain HTTP. This is acceptable because every WeCom
callback payload is:

- **AES-CBC encrypted** with the agent's `EncodingAESKey` (an eavesdropper sees
  only ciphertext),
- **HMAC-signed** (`msg_signature`) over the encrypted payload (tampering or
  forgery is detected → `403`), and
- **timestamp-bound** (replay beyond 5 minutes is rejected).

WeCom officially permits HTTP on custom ports
(`developer.work.weixin.qq.com/document/path/90238`: "支持http或https协议（为了提高安全性，建议使用https）").

### Phase 2 — adding TLS
For a compliance/policy checkbox, terminate TLS in front of the relay:
- Put nginx on the EC2 box with a Let's Encrypt cert for the box's public DNS name
  (Let's Encrypt will not issue for a bare Elastic IP — a domain is required), or
- Front the relay with a TLS-terminating reverse proxy.
The WeCom callback URL then becomes `https://<name>:<port>/callback/<agent_id>`.

## Bearer token rotation (manual, Phase 1)

The `/messages/fetch` Bearer token is a shared secret stored in two places: the
relay's secrets file (`RELAY_FETCH_TOKEN` in `/etc/lobster-relay/relay.env`, mode
`0600`, owned by `lobster-relay`) on EC2, and the local `config.yaml`
(`wecom.relay_token`). The systemd unit itself is secret-free (world-readable);
all secrets live only in the `0600` `EnvironmentFile`. To rotate:

1. Generate a new secret: `python3 -c "import secrets; print(secrets.token_hex(32))"`
2. On EC2: update `RELAY_FETCH_TOKEN` in `/etc/lobster-relay/relay.env`,
   then `sudo systemctl restart lobster-relay`.
3. Locally: set `wecom.relay_token` in `config.yaml`, restart the lobster server.

Brief overlap is fine: the local poller simply gets `401` until both sides match,
and resumes from its stored cursor once they do — no message loss (the relay keeps
buffering through the rotation).

A `lobster rotate-relay-token` CLI command that automates both sides is a Phase-2
enhancement.

## SOCKS proxy port

The EC2 box also runs an outbound SOCKS5 proxy (port 1080) for fixed-IP WeCom API
calls. `scripts/audit-sg.sh` fails the deploy if port 1080 is exposed to
`0.0.0.0/0`, since that would make it an open proxy.
