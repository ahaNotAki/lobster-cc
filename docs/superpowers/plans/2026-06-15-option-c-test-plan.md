# Option C — Test Plan

Companion to `2026-06-15-option-c-self-hosted-relay.md`. Defines what "all tests pass" means for sign-off. Every behavior below has a corresponding test written **before** its implementation (TDD).

## Test layers

| Layer | Tool | Scope | Runs in CI |
|-------|------|-------|------------|
| Unit — relay app | pytest + aiohttp TestClient | RelayStore, RelayConfig, handlers, auth, freshness | yes |
| Unit — local poller | pytest + mocked httpx | Bearer header sent / omitted | yes |
| Unit — config | pytest | `relay_token` field | yes |
| Script gate | pytest driving bash + fake `aws` | audit-sg.sh pass/fail logic | yes |
| Script syntax | `bash -n` | setup-self-relay.sh, relay-monitor.sh, audit-sg.sh | manual |
| Script smoke | bash --dry-run / unreachable URL | setup-self-relay.sh dry-run, relay-monitor.sh unreachable | manual |
| Regression | pytest | full existing suite unchanged | yes |
| Live cutover | manual runbook | e2e WeCom message round-trip on EC2 | manual, post-approval |

## Acceptance criteria (all must hold)

### A. Security finding resolved
- [ ] `/callback` returns **403** for: bad signature, stale timestamp (>300s), empty body, unknown agent. (`test_callback_rejects_*`, `test_callback_empty_body_rejected`)
- [ ] `/callback` returns **200 "success"** + persists only for a valid signed+fresh message. (`test_callback_stores_valid_message`)
- [ ] `/messages/fetch` returns **401** without a Bearer token and with a wrong token. (`test_fetch_requires_bearer_token`, `test_fetch_rejects_wrong_token`)
- [ ] `/messages/fetch` returns buffered messages only with the correct Bearer token. (`test_fetch_returns_buffered_messages`)
- [ ] `audit-sg.sh` exits non-zero if the relay port OR SOCKS 1080 is open to `0.0.0.0/0`; exits 0 when both restricted. (`test_audit_*`)
- [ ] `setup-self-relay.sh` refuses a `0.0.0.0/0` entry in `--wecom-ips`. (dry-run smoke)

### B. Buffering guarantee preserved (the reason for Option C)
- [ ] No timestamp-freshness check exists on the local dispatch path — verified by code review of `message_source.py` (freshness only in `relay/app.py`). Existing `test_relay_source_dispatch_encrypted_message` must still pass with an old timestamp (`1234567890`), proving buffered/old messages still dispatch locally.
- [ ] `RelayStore.fetch` returns messages in monotonic `seq` order after a cursor. (`test_relay_store_fetch_after_cursor`, `test_relay_store_fetch_respects_limit`)
- [ ] `RelayStore.purge_expired` removes only rows older than TTL. (`test_relay_store_purges_expired`)

### C. Correctness / durability
- [ ] `RelayStore` round-trips body + query_params faithfully (signature survives storage). (`test_relay_store_put_and_fetch`)
- [ ] `RelayConfig.from_env` requires `RELAY_FETCH_TOKEN`, supports single + multi-agent creds. (`test_relay_config_*`)
- [ ] `/health` returns status, queue_depth, rejected_count. (`test_health_endpoint`)
- [ ] Purge background task is registered when `run_purge=True`. (`test_purge_task_registered`)

### D. Local integration
- [ ] `RelayPollingSource` sends `Authorization: Bearer <relay_token>` when configured. (`test_relay_source_sends_bearer_token`)
- [ ] No `Authorization` header when `relay_token` empty (backwards compatible). (`test_relay_source_no_auth_header_without_token`)
- [ ] `WeComConfig.relay_token` defaults to `""` and accepts a value. (`test_wecom_config_relay_token_*`)

### E. No regressions
- [ ] `python -m pytest tests/ -v` — full suite green, including all pre-existing `test_message_source.py`, `test_config.py`, `test_server.py`, `test_store.py`, `test_cli_init.py`.
- [ ] `ruff check src/ tests/` clean.

## Commands

```bash
# New relay unit tests
python -m pytest tests/test_relay_app.py -v

# Local poller auth
python -m pytest tests/test_message_source.py -k bearer -v

# Config
python -m pytest tests/test_config.py -k relay_token -v

# Script gate
python -m pytest tests/test_audit_sg.py -v

# Full suite (the gate for "all test plan passed")
python -m pytest tests/ -v

# Lint
ruff check src/ tests/

# Script syntax + smoke (manual)
bash -n scripts/setup-self-relay.sh scripts/audit-sg.sh scripts/relay-monitor.sh
bash scripts/setup-self-relay.sh --host ec2-user@1.2.3.4 --sg-id sg-x --fetch-token t --wecom-ips "1.2.3.0/24" --dry-run
bash scripts/relay-monitor.sh --url http://127.0.0.1:1; echo "exit=$?"
```

## Out of scope for automated tests (validated manually post-approval)
- Live AWS SG mutation, EC2 scp/systemd install (`setup-self-relay.sh` real run)
- Live AWS teardown (`setup-relay.sh --teardown`)
- WeCom admin-console URL re-registration + e2e round-trip
- systemd OnFailure alert delivery
