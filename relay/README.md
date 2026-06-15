# Legacy AWS Relay (DEPRECATED — removed)

> **This AWS relay (API Gateway + Lambda + DynamoDB) has been replaced by the
> self-hosted relay.** It was flagged by AppSec finding `APIGAuthenticationCheck`
> for unauthenticated public POST endpoints (`/messages/fetch` drained the queue
> with no auth; `/callback` stored any body without verifying the WeCom signature).
>
> **Use the self-hosted relay instead:** see
> [docs/self-hosted-relay.md](../docs/self-hosted-relay.md),
> [docs/security.md](../docs/security.md), and
> [ADR 0001](../docs/architecture-decisions/0001-self-hosted-relay.md).
>
> This directory and `lambda_function.py` are retained **only** so existing
> deployments can tear the old stack down.

## Tearing down the legacy stack

After cutting over to the self-hosted relay and waiting out the old DynamoDB
7-day TTL (so any in-flight messages drain), delete the AWS resources:

```bash
./scripts/setup-relay.sh --teardown
```

This deletes, in order: API Gateway → Lambda → IAM role (`wecom-relay-lambda-role`)
→ DynamoDB table (`wecom_relay_messages`).

To verify the flagged endpoints are gone (expect a connection failure / 404):

```bash
curl -sS -o /dev/null -w "%{http_code}\n" -X POST \
  https://<old-api-id>.execute-api.ap-southeast-1.amazonaws.com/messages/fetch
```

Do **not** click "Request Verification Of Fix" in the AppSec ticket — the teardown
removes the flagged API Gateway entirely, so the finding cannot re-trigger.

## What the old stack was

For historical reference, the legacy relay consisted of:

- **API Gateway** (HTTP API) — routes `GET/POST /callback`, `GET/POST /callback/{agent_id}`, `POST /messages/fetch`
- **Lambda** `wecom-relay` (Python 3.11) — `relay/lambda_function.py`, pass-through store/fetch
- **DynamoDB** `wecom_relay_messages` — raw encrypted messages, 7-day TTL, `seq-index` GSI
- **IAM role** `wecom-relay-lambda-role` — Lambda execution + DynamoDB access

The self-hosted relay (`src/remote_control/relay/`) replaces all of the above with
a single aiohttp + SQLite process on the always-on EC2 box, adds authentication on
both endpoints, and restricts inbound access to WeCom IP ranges.
