# AWS Relay Resources

All resources created for the WeCom relay endpoint.

## Quick Deploy (Recommended)

One script deploys everything using AWS CLI — no extra tools needed:

```bash
./scripts/setup-relay.sh --token YOUR_WECOM_TOKEN --aes-key YOUR_WECOM_AES_KEY
```

Options:
| Flag | Description |
|------|-------------|
| `--token` | WeCom callback verification token (required on first run) |
| `--aes-key` | WeCom AES encoding key (required on first run) |
| `--agent-configs` | Multi-agent JSON, e.g. `'{"1000002":{"token":"x","aes_key":"y"}}'` |
| `--region` | AWS region (default: `ap-southeast-1`) |
| `--ttl-days` | Message retention days (default: 7) |
| `--update-code` | Only update Lambda code (skip infra) |
| `--teardown` | Delete all relay resources |

The script is fully idempotent — re-running skips existing resources.

## Alternative: Deploy with SAM

Deploy the entire relay stack (Lambda + API Gateway + DynamoDB) in one command using [AWS SAM CLI](https://docs.aws.amazon.com/serverless-application-model/latest/developerguide/install-sam-cli.html).

### Prerequisites

- AWS SAM CLI installed (`brew install aws-sam-cli` or [other methods](https://docs.aws.amazon.com/serverless-application-model/latest/developerguide/install-sam-cli.html))
- AWS credentials configured (`aws configure` or environment variables)
- Docker installed (for `sam build --use-container` to compile pycryptodome for Linux)

### Deploy

```bash
cd relay/

# Build (compiles pycryptodome in a Lambda-compatible container)
sam build

# Deploy (first time — prompts for WeComToken and WeComAesKey)
sam deploy --guided

# Subsequent deploys (uses saved samconfig.toml)
sam deploy
```

On first `sam deploy --guided`, you will be prompted for:

| Parameter | Description |
|-----------|-------------|
| `WeComToken` | WeCom verification token (hidden input) |
| `WeComAesKey` | WeCom AES encoding key (hidden input) |
| `AgentConfigs` | Multi-agent JSON (optional, leave empty for single-agent) |
| `TtlDays` | Message retention days (default: 7) |

The deploy output prints the `ApiEndpoint` — use this as `relay_url` in your local `config.yaml` and as the WeCom callback URL.

### Update Lambda Code Only

```bash
cd relay/
sam build && sam deploy
```

### Teardown

```bash
cd relay/
sam delete
```

### samconfig.toml

The included `samconfig.toml` provides sensible defaults (region `ap-southeast-1`, stack name `wecom-relay`, container build). Override parameters at deploy time:

```bash
sam deploy --parameter-overrides "WeComToken=xxx WeComAesKey=yyy AgentConfigs={...}"
```

---

## Account & Region

| Property | Value |
|----------|-------|
| AWS Account | `<your-aws-account-id>` |
| Region | `ap-southeast-1` (Singapore) |
| Profile | `<your-aws-profile>` |

## API Gateway (HTTP API)

| Property | Value |
|----------|-------|
| API Name | `wecom-relay` |
| API ID | `<your-api-id>` |
| Endpoint | `https://<your-api-id>.execute-api.ap-southeast-1.amazonaws.com` |
| Protocol | HTTP |
| Stage | `$default` (auto-deploy) |

### Routes

| Method | Path | Target |
|--------|------|--------|
| GET | `/callback` | Lambda `wecom-relay` (legacy single-agent) |
| POST | `/callback` | Lambda `wecom-relay` (legacy single-agent) |
| GET | `/callback/{agent_id}` | Lambda `wecom-relay` (multi-agent) |
| POST | `/callback/{agent_id}` | Lambda `wecom-relay` (multi-agent) |
| POST | `/messages/fetch` | Lambda `wecom-relay` |

### Integration

| Property | Value |
|----------|-------|
| Integration ID | `<your-integration-id>` |
| Type | `AWS_PROXY` |
| Payload Format | `2.0` |

## Lambda Function

| Property | Value |
|----------|-------|
| Function Name | `wecom-relay` |
| ARN | `arn:aws:lambda:ap-southeast-1:<your-aws-account-id>:function:wecom-relay` |
| Runtime | Python 3.11 |
| Handler | `lambda_function.lambda_handler` |
| Memory | 128 MB |
| Timeout | 30s |
| Architecture | x86_64 |

### Environment Variables

| Variable | Description |
|----------|-------------|
| `TABLE_NAME` | DynamoDB table name (`wecom_relay_messages`) |
| `WECOM_TOKEN` | Legacy single-agent WeCom token (fallback) |
| `WECOM_AES_KEY` | Legacy single-agent AES key (fallback) |
| `TTL_DAYS` | Message TTL in days (default: `7`) |
| `AGENT_CONFIGS` | Multi-agent JSON: `{"<agent-id-1>":{"token":"...","aes_key":"..."},"<agent-id-2>":{...}}` |

### Dependencies

- `pycryptodome` — packaged in deployment zip (Linux x86_64 build)
- `boto3` — provided by Lambda runtime

### Source Code

- `relay/lambda_function.py` — Single file, 3 handlers

## IAM Role

| Property | Value |
|----------|-------|
| Role Name | `wecom-relay-lambda-role` |
| ARN | `arn:aws:iam::<your-aws-account-id>:role/wecom-relay-lambda-role` |

### Attached Policies

| Policy | Type |
|--------|------|
| `AWSLambdaBasicExecutionRole` | AWS managed (CloudWatch Logs) |
| `DynamoDBAccess` | Inline — PutItem, UpdateItem, Query, GetItem on `wecom_relay_messages` table + indexes |

### API Gateway Permission

| Statement ID | Principal | Action |
|--------------|-----------|--------|
| `ApiGatewayInvoke` | `apigateway.amazonaws.com` | `lambda:InvokeFunction` |

## DynamoDB Table

| Property | Value |
|----------|-------|
| Table Name | `wecom_relay_messages` |
| ARN | `arn:aws:dynamodb:ap-southeast-1:<your-aws-account-id>:table/wecom_relay_messages` |
| Billing Mode | Provisioned (5 RCU / 5 WCU) |
| TTL | Enabled on `ttl` attribute |

### Schema

| Attribute | Type | Key |
|-----------|------|-----|
| `msg_id` | String | Partition Key (PK) |

### Global Secondary Index: `seq-index`

| Attribute | Type | Key |
|-----------|------|-----|
| `gsi_pk` | String | Partition Key (always `"msg"`) |
| `seq` | Number | Sort Key |

Projection: ALL

### Item Structure

```json
{
    "msg_id": "uuid-string",
    "seq": 1,
    "gsi_pk": "msg",
    "query_params": {
        "msg_signature": "...",
        "timestamp": "...",
        "nonce": "..."
    },
    "agent_id": "1000002",
    "body": "<xml><Encrypt>...</Encrypt></xml>",
    "created_at": "2026-03-07T12:00:00Z",
    "ttl": 1741700000
}
```

Special item `msg_id = "__counter__"` holds the atomic sequence counter (no `gsi_pk`, so excluded from GSI).

## URLs for Configuration

### WeCom Admin Console (Callback URLs)

```
# Per-agent (recommended for multi-agent):
https://<your-api-id>.execute-api.ap-southeast-1.amazonaws.com/callback/<agent-id-1>
https://<your-api-id>.execute-api.ap-southeast-1.amazonaws.com/callback/<agent-id-2>

# Legacy (single-agent):
https://<your-api-id>.execute-api.ap-southeast-1.amazonaws.com/callback
```

### Local Server Config (relay mode)

```yaml
wecom:
  mode: "relay"
  relay_url: "https://<your-api-id>.execute-api.ap-southeast-1.amazonaws.com"
  relay_poll_interval_seconds: 5.0
```

## Updating the Lambda

```bash
# Rebuild and deploy
cd /tmp && rm -rf lambda_package && mkdir lambda_package
pip install --platform manylinux2014_x86_64 --only-binary=:all: --target /tmp/lambda_package pycryptodome
cp relay/lambda_function.py /tmp/lambda_package/
cd /tmp/lambda_package && zip -r /tmp/wecom_relay.zip .

aws lambda update-function-code \
  --region ap-southeast-1 \
  --function-name wecom-relay \
  --zip-file fileb:///tmp/wecom_relay.zip
```

## Updating Environment Variables

```bash
# Use a JSON file for complex env vars (AGENT_CONFIGS contains nested JSON):
cat > /tmp/lambda-env.json << 'EOF'
{"Variables": {
  "TABLE_NAME": "wecom_relay_messages",
  "WECOM_TOKEN": "legacy_token",
  "WECOM_AES_KEY": "legacy_aes_key",
  "TTL_DAYS": "7",
  "AGENT_CONFIGS": "{\"<agent-id-1>\":{\"token\":\"...\",\"aes_key\":\"...\"},\"<agent-id-2>\":{\"token\":\"...\",\"aes_key\":\"...\"}}"
}}
EOF
aws lambda update-function-configuration \
  --region ap-southeast-1 \
  --function-name wecom-relay \
  --environment file:///tmp/lambda-env.json
```

## Teardown (if needed)

```bash
REGION=ap-southeast-1

# Delete API Gateway
aws apigatewayv2 delete-api --region $REGION --api-id <your-api-id>

# Delete Lambda
aws lambda delete-function --region $REGION --function-name wecom-relay

# Delete DynamoDB table
aws dynamodb delete-table --region $REGION --table-name wecom_relay_messages

# Delete IAM role (detach policies first)
aws iam detach-role-policy --role-name wecom-relay-lambda-role \
  --policy-arn arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole
aws iam delete-role-policy --role-name wecom-relay-lambda-role --policy-name DynamoDBAccess
aws iam delete-role --role-name wecom-relay-lambda-role
```
