# Security Model — WeCom Callback

This document describes the authentication and trust model for the WeCom
callback handling. See also [ADR 0002](architecture-decisions/0002-inline-callback-processing.md).

## Public path

```
WeCom  ──HTTP POST──►  EC2:80  ──reverse SSH tunnel──►  desktop:8080  ──►  WeComGateway
                                                                              │
                                                                              ▼
                                                                          executor → Claude Code
```

The lobster-cc server is exposed to the public internet on EC2:80 via the
existing `rc-dashboard-tunnel.service` (autossh reverse tunnel). The same path
that serves the dashboard UI now also handles `POST /wecom/callback/{agent_id}`.

## Authentication on `/wecom/callback/{agent_id}`

`WeComGateway.handle_message` enforces, in order, before any work is enqueued:

1. **WeCom signature**: SHA1 of the sorted `token,timestamp,nonce,encrypt`.
   The `token` is a shared secret with WeCom. Forgery without it is
   computationally infeasible. Invalid signature → `403 invalid signature`.
2. **Timestamp freshness**: reject if `|now - timestamp| > 300s`. Replay
   protection — WeCom delivers within seconds, so any older delivery indicates
   replay. Stale → `403 stale`.
3. **Decryption**: AES-CBC with the agent's `EncodingAESKey`. An eavesdropper
   on the wire sees only ciphertext.

The raw XML body is parsed with `defusedxml` to mitigate entity-expansion /
XXE attacks — necessary because parsing precedes signature verification (the
`<Encrypt>` field must be extracted before it can be verified).

GET (URL verification) follows the same signature path with the `echostr` query
parameter.

## Plain HTTP rationale

Callbacks travel as plain HTTP from WeCom to EC2:80, then through the
encrypted SSH tunnel to the desktop. This is acceptable because each WeCom
payload is independently AES-encrypted, HMAC-signed (via the shared `token`
under SHA1), and timestamp-bound — eavesdropping yields nothing useful and
forgery is blocked at the gateway.

WeCom officially permits HTTP on custom ports ([dev doc 90238](https://developer.work.weixin.qq.com/document/path/90238): "支持http或https协议").

If a future compliance requirement mandates TLS to the EC2 box, terminate it
with nginx + Let's Encrypt on a public DNS name, or via Cloudflare in front.
The gateway-side checks remain unchanged.

## SOCKS proxy port

The EC2 box also runs an outbound SOCKS5 proxy (port 1080) for fixed-IP WeCom
API calls. Its security group rules are managed manually; do not expose 1080
to `0.0.0.0/0` (it would become an open proxy).
