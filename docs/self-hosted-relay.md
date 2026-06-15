# Self-Hosted Relay — Deployment & Operations

The self-hosted relay replaces the AWS API Gateway + Lambda + DynamoDB stack
(see [ADR 0001](architecture-decisions/0001-self-hosted-relay.md)). It runs as a
small aiohttp process on the always-on EC2 box, buffers raw WeCom callbacks in
short-TTL SQLite, and serves them to the local lobster server's poller over an
authenticated endpoint.

## Architecture

```
┌──────────┐  callback  ┌──────────────────────┐  poll(Bearer)  ┌─────────────────┐
│  You on  │───────────►│  EC2: lobster-relay   │◄───────────────│  lobster-cc     │
│  WeCom   │            │  aiohttp + SQLite     │───────────────►│  server (local) │
│  📱      │◄───────────│  /callback /fetch     │                │  Claude Code ←──┤
└──────────┘  reply     │  (SG → WeCom IPs only)│                │  Dashboard   ←──┤
                        └──────────────────────┘                └─────────────────┘
```

1. WeCom pushes the encrypted callback to `http://<elastic-ip>:<port>/callback/<agent_id>`.
2. The relay verifies the WeCom signature + 5-min timestamp freshness, then stores
   the raw encrypted body in SQLite (WAL, short TTL).
3. The local server polls `POST /messages/fetch` with `Authorization: Bearer <token>`,
   decrypts locally, and runs the task through Claude Code.
4. All decryption happens locally. The relay never sees plaintext.

## Prerequisites

- The existing EC2 box with an Elastic IP and your SSH key (see `docs/aws-proxy.md`).
- Python 3.11+ on the EC2 box.
- The relay's inbound port (default `8443`) and the WeCom callback IP ranges.

## Get the WeCom callback IP ranges

WeCom publishes the source IPs it delivers callbacks from via the `getcallbackip`
API. Fetch them (with a valid access token) and pass the CIDRs to the setup script:

```
GET https://qyapi.weixin.qq.com/cgi-bin/getcallbackip?access_token=ACCESS_TOKEN
```

Re-check quarterly — the ranges can change. Update the SG rules if they do.

## Deploy — one command (recommended)

`scripts/deploy-self-relay.sh` orchestrates the whole cutover and reads **all real
credentials from the remote host's `config.yaml`** — nothing secret is typed on
the command line or stored in this repo. It pulls each agent's
corp_id/secret/token/encoding_aes_key/relay_token off the remote config, derives
the WeCom callback IP ranges itself (via `getcallbackip`), provisions the relay,
health-checks it, prints the exact WeCom admin-console URL(s) to register, and
optionally restarts the local server.

Prerequisite: each relay-mode agent in the remote `config.yaml` already has a
`relay_token` set (the relay's Bearer secret — generate once with
`python3 -c "import secrets; print(secrets.token_hex(32))"`).

```bash
./scripts/deploy-self-relay.sh \
    --host ec2-user@<deploy-host> --remote-dir /home/you/lobster-cc \
    --relay-host ec2-user@<relay-elastic-ip> --sg-id sg-xxxx \
    --relay-port 8443 --ssh-key ~/.ssh/rc-proxy-key.pem \
    [--restart-local] [--dry-run]
```

`--host`/`--remote-dir` is the box running lobster-cc (where `config.yaml` lives);
`--relay-host` is the box that will run the relay — often the same EC2 box (pass
the same SSH target for both). Single- vs multi-agent is detected from the config
automatically. Use `--dry-run` to preview; add `--restart-local` to restart the
remote `lobster-cc` after you confirm the WeCom URL is registered.

## Deploy — manual (single step)

If you prefer to run just the relay provisioning yourself, call the underlying
script directly. Secrets can be passed as flags **or** via environment variables
(`RELAY_FETCH_TOKEN`, `WECOM_TOKEN`, `WECOM_AES_KEY`, `AGENT_CONFIGS`) to keep them
out of `ps`:

```bash
FETCH_TOKEN=$(python3 -c "import secrets; print(secrets.token_hex(32))")

# Single-agent:
RELAY_FETCH_TOKEN="$FETCH_TOKEN" WECOM_TOKEN="<token>" WECOM_AES_KEY="<aes>" \
./scripts/setup-self-relay.sh \
    --host ec2-user@18.142.75.174 --sg-id sg-xxxxxxxx --relay-port 8443 \
    --wecom-ips "<cidr1>,<cidr2>,..." --ssh-key ~/.ssh/rc-proxy-key.pem

# Multi-agent: pass per-agent creds as JSON instead of WECOM_TOKEN/WECOM_AES_KEY:
RELAY_FETCH_TOKEN="$FETCH_TOKEN" \
AGENT_CONFIGS='{"1000002":{"token":"t2","aes_key":"k2"},"1000003":{"token":"t3","aes_key":"k3"}}' \
./scripts/setup-self-relay.sh \
    --host ec2-user@18.142.75.174 --sg-id sg-xxxxxxxx --relay-port 8443 \
    --wecom-ips "<cidr1>,<cidr2>" --ssh-key ~/.ssh/rc-proxy-key.pem
```

You must supply WeCom credentials so the relay can verify callbacks: either
`WECOM_TOKEN` + `WECOM_AES_KEY` (single-agent) **or** `AGENT_CONFIGS` JSON
(multi-agent). The script errors out if neither is given.

What it does:
1. Opens the relay port in the SG restricted to the given WeCom CIDRs (refuses `0.0.0.0/0`, `::/0`, and malformed CIDRs).
2. Runs `scripts/audit-sg.sh` as a gate (fails if the relay port or SOCKS 1080 is open to the world over IPv4 or IPv6).
3. Creates a non-root `lobster-relay` user, writes secrets to a `0600`
   `/etc/lobster-relay/relay.env` (the systemd unit stays secret-free), copies
   relay code to `/opt/lobster-relay`, verifies Python deps, and installs +
   starts the `lobster-relay` systemd unit (`Restart=always`).

Re-running is safe (idempotent): SG rule re-authorization is a no-op, code is
re-synced, and the service is restarted — use it to push code updates.

Use `--dry-run` to preview the actions without touching AWS or the host.

## Local config

```yaml
wecom:
  mode: "relay"
  relay_url: "http://18.142.75.174:8443"
  relay_token: "<the FETCH_TOKEN from above>"
```

## Cutover (zero message loss)

The orchestrator (`deploy-self-relay.sh`) does steps 1–3 and 5; only the WeCom
admin-console registration (step 4) and the config edit are inherently manual.

1. Add `relay_token` to each relay-mode agent in the remote `config.yaml` (the
   relay's Bearer secret).
2. Run the orchestrator (without `--restart-local` for now):
   ```bash
   ./scripts/deploy-self-relay.sh --host ec2-user@<deploy-host> \
       --remote-dir /path/to/lobster-cc --relay-host ec2-user@<relay-ip> \
       --sg-id sg-xxxx --ssh-key ~/.ssh/rc-proxy-key.pem
   ```
   It provisions + health-checks the relay and prints the callback URL(s).
3. **Register the printed callback URL(s)** in the WeCom admin console
   (接收消息 → 设置API接收). WeCom issues a GET verify; the relay is already running.
4. Set `relay_url` to `http://<relay-ip>:<port>` in the remote `config.yaml`.
   (`relay_token` is already there from step 1. **Both are required** — the server
   refuses to start in relay mode without `relay_token`.)
5. Re-run the orchestrator with `--restart-local` (it confirms the URL is
   registered, then restarts `lobster-cc`), or restart manually:
   `ssh <host> 'sudo systemctl restart lobster-cc'`.
6. Send a WeCom message end-to-end; confirm a reply.
7. **Wait out the old DynamoDB 7-day TTL** so any in-flight messages drain. Keep
   the old AWS relay ~1 week as rollback.
8. Tear down the legacy AWS stack: `./scripts/setup-relay.sh --teardown`
   (deletes API Gateway + Lambda + IAM role + DynamoDB).

Rollback: revert the WeCom callback URL + remote `config.yaml` to the old relay;
the old DynamoDB buffer (7-day TTL) still holds messages.

## Operations

### Service management (on EC2)
```bash
systemctl status lobster-relay
sudo systemctl restart lobster-relay
journalctl -u lobster-relay -f
```

### Health endpoint
`GET http://127.0.0.1:8443/health` returns:
```json
{"status": "ok", "queue_depth": 0, "last_callback_ts": 1700000000, "rejected_count": 0}
```
- `queue_depth` — buffered messages not yet drained by the local poller. A rising
  value means the local poller is down.
- `rejected_count` — **cumulative** `403`s since the relay last started (bad
  signature / stale / unknown agent); it resets only on restart. The monitor's
  `--max-rejected` is therefore a total ceiling, not a per-minute rate — set it
  generously (e.g. a few hundred) to allow for normal stray invalid callbacks
  over the service's uptime, or restart-and-watch if you need a true rate.

### Monitoring cron
Run `scripts/relay-monitor.sh` every minute and route its alert output to WeCom:
```cron
* * * * * /opt/lobster-relay/scripts/relay-monitor.sh --url http://127.0.0.1:8443 --max-queue 100 || /path/to/notify-wecom.sh
```
Exit codes: `0` healthy, `1` unreachable, `2` threshold breached. The systemd unit
uses `Restart=always` (with `RestartSec=10`), so a crashed relay auto-recovers in
~10s; the cron monitor is what surfaces a sustained outage or backlog to you.

## Token rotation

See [docs/security.md](security.md#bearer-token-rotation-manual-phase-1).
