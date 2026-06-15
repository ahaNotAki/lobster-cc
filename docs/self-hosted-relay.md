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

## Deploy

```bash
# Generate a Bearer secret for /messages/fetch
FETCH_TOKEN=$(python3 -c "import secrets; print(secrets.token_hex(32))")

./scripts/setup-self-relay.sh \
    --host ec2-user@18.142.75.174 \
    --sg-id sg-xxxxxxxx \
    --relay-port 8443 \
    --fetch-token "$FETCH_TOKEN" \
    --wecom-ips "<cidr1>,<cidr2>,..." \
    --ssh-key ~/.ssh/rc-proxy-key.pem \
    --region ap-southeast-1
```

What it does:
1. Opens the relay port in the SG restricted to the given WeCom CIDRs (refuses `0.0.0.0/0`).
2. Runs `scripts/audit-sg.sh` as a gate (fails if the relay port or SOCKS 1080 is open to the world).
3. Creates a non-root `lobster-relay` user, copies relay code to `/opt/lobster-relay`,
   installs and starts the `lobster-relay` systemd unit (`Restart=always`).

Use `--dry-run` to preview the actions without touching AWS or the host.

## Local config

```yaml
wecom:
  mode: "relay"
  relay_url: "http://18.142.75.174:8443"
  relay_token: "<the FETCH_TOKEN from above>"
```

## Cutover (zero message loss)

1. Deploy the self-hosted relay (above).
2. **Stop the local server** — prevents split-brain where new messages go to the
   new relay while the poller still reads the old one.
3. Repoint the WeCom admin-console callback URL to
   `http://<elastic-ip>:<port>/callback/<agent_id>`. WeCom issues a GET verify;
   the relay must be running.
4. Update local `config.yaml` (`relay_url` + `relay_token`), restart the server.
5. Send a WeCom message end-to-end; confirm a reply.
6. **Wait out the old DynamoDB 7-day TTL** so any in-flight messages drain. Keep
   the old AWS relay ~1 week as rollback.
7. Tear down the legacy AWS stack: `./scripts/setup-relay.sh --teardown`
   (deletes API Gateway + Lambda + IAM role + DynamoDB).

Rollback: revert the WeCom callback URL + local `config.yaml` to the old relay;
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
- `rejected_count` — cumulative `403`s (bad signature / stale / unknown agent). A
  spike suggests an attack or a config drift.

### Monitoring cron
Run `scripts/relay-monitor.sh` every minute and route its alert output to WeCom:
```cron
* * * * * /opt/lobster-relay/scripts/relay-monitor.sh --url http://127.0.0.1:8443 --max-queue 100 || /path/to/notify-wecom.sh
```
Exit codes: `0` healthy, `1` unreachable, `2` threshold breached. The systemd unit
also has `OnFailure=lobster-relay-alert@%n.service` for service-crash alerts (wire
that unit to your notifier).

## Token rotation

See [docs/security.md](security.md#bearer-token-rotation-manual-phase-1).
