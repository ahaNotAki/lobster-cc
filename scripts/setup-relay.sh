#!/usr/bin/env bash
#
# Deploy the WeCom relay stack: DynamoDB + IAM + Lambda + API Gateway.
# One command, fully idempotent — re-running skips existing resources.
#
# Prerequisites:
#   - AWS CLI v2 configured with credentials (aws configure)
#   - pip (for packaging pycryptodome for Lambda)
#
# Usage:
#   ./scripts/setup-relay.sh --token YOUR_TOKEN --aes-key YOUR_AES_KEY [OPTIONS]
#
# Options:
#   --token         WeCom callback verification token (required for first deploy)
#   --aes-key       WeCom callback AES encoding key (required for first deploy)
#   --agent-configs Multi-agent JSON (optional, e.g. '{"1000002":{"token":"x","aes_key":"y"}}')
#   --region        AWS region (default: ap-southeast-1)
#   --ttl-days      Message retention in days (default: 7)
#   --update-code   Only update Lambda code (skip infra creation)
#   --teardown      Delete all relay resources
#
# What gets created:
#   - DynamoDB table:     wecom_relay_messages (with GSI + TTL)
#   - IAM role:           wecom-relay-lambda-role
#   - Lambda function:    wecom-relay (Python 3.11, 128MB, 30s)
#   - API Gateway:        wecom-relay (HTTP API with 5 routes)
#

set -euo pipefail

# --- Defaults ---
REGION="${AWS_DEFAULT_REGION:-ap-southeast-1}"
TABLE_NAME="wecom_relay_messages"
FUNCTION_NAME="wecom-relay"
ROLE_NAME="wecom-relay-lambda-role"
API_NAME="wecom-relay"
TTL_DAYS="7"
WECOM_TOKEN=""
WECOM_AES_KEY=""
AGENT_CONFIGS=""
UPDATE_CODE_ONLY=false
TEARDOWN=false

# Locate relay/ directory relative to this script
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
RELAY_DIR="$PROJECT_DIR/relay"

# --- Parse args ---
while [[ $# -gt 0 ]]; do
    case "$1" in
        --token)         WECOM_TOKEN="$2"; shift 2 ;;
        --aes-key)       WECOM_AES_KEY="$2"; shift 2 ;;
        --agent-configs) AGENT_CONFIGS="$2"; shift 2 ;;
        --region)        REGION="$2"; shift 2 ;;
        --ttl-days)      TTL_DAYS="$2"; shift 2 ;;
        --update-code)   UPDATE_CODE_ONLY=true; shift ;;
        --teardown)      TEARDOWN=true; shift ;;
        -*)              echo "Unknown flag: $1"; exit 1 ;;
        *)               echo "Unexpected arg: $1"; exit 1 ;;
    esac
done

AWS="aws --region $REGION --output text"

# ────────────────────────────────────────────────
# TEARDOWN
# ────────────────────────────────────────────────
if [ "$TEARDOWN" = true ]; then
    echo "=== Teardown Relay Resources ==="
    echo "  Region: $REGION"
    echo ""

    # Delete API Gateway
    API_ID=$($AWS apigatewayv2 get-apis --query "Items[?Name=='$API_NAME'].ApiId | [0]" 2>/dev/null || echo "None")
    if [ "$API_ID" != "None" ] && [ -n "$API_ID" ]; then
        echo "  Deleting API Gateway ($API_ID)..."
        $AWS apigatewayv2 delete-api --api-id "$API_ID" > /dev/null
    else
        echo "  API Gateway not found, skipping."
    fi

    # Delete Lambda
    if $AWS lambda get-function --function-name "$FUNCTION_NAME" --query "Configuration.FunctionName" &>/dev/null; then
        echo "  Deleting Lambda ($FUNCTION_NAME)..."
        $AWS lambda delete-function --function-name "$FUNCTION_NAME" > /dev/null
    else
        echo "  Lambda not found, skipping."
    fi

    # Delete IAM role
    if aws iam get-role --role-name "$ROLE_NAME" --query "Role.RoleName" &>/dev/null 2>&1; then
        echo "  Deleting IAM role ($ROLE_NAME)..."
        aws iam detach-role-policy --role-name "$ROLE_NAME" \
            --policy-arn arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole 2>/dev/null || true
        aws iam delete-role-policy --role-name "$ROLE_NAME" --policy-name DynamoDBAccess 2>/dev/null || true
        aws iam delete-role --role-name "$ROLE_NAME" > /dev/null
    else
        echo "  IAM role not found, skipping."
    fi

    # Delete DynamoDB table
    if $AWS dynamodb describe-table --table-name "$TABLE_NAME" --query "Table.TableName" &>/dev/null; then
        echo "  Deleting DynamoDB table ($TABLE_NAME)..."
        $AWS dynamodb delete-table --table-name "$TABLE_NAME" > /dev/null
    else
        echo "  DynamoDB table not found, skipping."
    fi

    echo ""
    echo "=== Teardown complete ==="
    exit 0
fi

# ────────────────────────────────────────────────
# DEPLOY
# ────────────────────────────────────────────────
echo "=== lobster-cc — Relay Setup ==="
echo "  Region: $REGION"
echo ""

ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)

# ---- Helper: package Lambda zip ----
package_lambda() {
    echo "  Packaging Lambda..."
    local TMP_DIR
    TMP_DIR=$(mktemp -d)
    pip install --quiet --platform manylinux2014_x86_64 --only-binary=:all: \
        --target "$TMP_DIR" pycryptodome > /dev/null
    cp "$RELAY_DIR/lambda_function.py" "$TMP_DIR/"
    (cd "$TMP_DIR" && zip -qr /tmp/wecom_relay.zip .)
    rm -rf "$TMP_DIR"
    echo "  Package ready: /tmp/wecom_relay.zip"
}

# ────────────────────────────────────────────────
# UPDATE CODE ONLY
# ────────────────────────────────────────────────
if [ "$UPDATE_CODE_ONLY" = true ]; then
    echo "[1/1] Updating Lambda code..."
    package_lambda
    $AWS lambda update-function-code \
        --function-name "$FUNCTION_NAME" \
        --zip-file fileb:///tmp/wecom_relay.zip > /dev/null
    echo "  Lambda code updated."
    echo ""
    echo "=== Update complete ==="
    exit 0
fi

# ────────────────────────────────────────────────
# FULL SETUP (idempotent)
# ────────────────────────────────────────────────
TOTAL_STEPS=5
STEP=0
next_step() { STEP=$((STEP + 1)); echo "[$STEP/$TOTAL_STEPS] $1"; }

# --- 1. DynamoDB Table ---
next_step "DynamoDB table..."
if $AWS dynamodb describe-table --table-name "$TABLE_NAME" --query "Table.TableName" &>/dev/null; then
    echo "  Table '$TABLE_NAME' already exists."
else
    echo "  Creating table '$TABLE_NAME'..."
    $AWS dynamodb create-table \
        --table-name "$TABLE_NAME" \
        --attribute-definitions \
            AttributeName=msg_id,AttributeType=S \
            AttributeName=gsi_pk,AttributeType=S \
            AttributeName=seq,AttributeType=N \
        --key-schema AttributeName=msg_id,KeyType=HASH \
        --global-secondary-indexes '[{
            "IndexName":"seq-index",
            "KeySchema":[{"AttributeName":"gsi_pk","KeyType":"HASH"},{"AttributeName":"seq","KeyType":"RANGE"}],
            "Projection":{"ProjectionType":"ALL"},
            "ProvisionedThroughput":{"ReadCapacityUnits":5,"WriteCapacityUnits":5}
        }]' \
        --provisioned-throughput ReadCapacityUnits=5,WriteCapacityUnits=5 > /dev/null

    echo "  Waiting for table to be active..."
    aws --region "$REGION" dynamodb wait table-exists --table-name "$TABLE_NAME"

    echo "  Enabling TTL on 'ttl' attribute..."
    $AWS dynamodb update-time-to-live \
        --table-name "$TABLE_NAME" \
        --time-to-live-specification "Enabled=true,AttributeName=ttl" > /dev/null

    echo "  Table created with GSI and TTL."
fi

# --- 2. IAM Role ---
next_step "IAM role..."
if aws iam get-role --role-name "$ROLE_NAME" --query "Role.Arn" --output text &>/dev/null 2>&1; then
    ROLE_ARN=$(aws iam get-role --role-name "$ROLE_NAME" --query "Role.Arn" --output text)
    echo "  Role '$ROLE_NAME' already exists: $ROLE_ARN"
else
    echo "  Creating IAM role '$ROLE_NAME'..."
    ROLE_ARN=$(aws iam create-role \
        --role-name "$ROLE_NAME" \
        --assume-role-policy-document '{
            "Version":"2012-10-17",
            "Statement":[{
                "Effect":"Allow",
                "Principal":{"Service":"lambda.amazonaws.com"},
                "Action":"sts:AssumeRole"
            }]
        }' --query "Role.Arn" --output text)

    aws iam attach-role-policy --role-name "$ROLE_NAME" \
        --policy-arn arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole

    aws iam put-role-policy --role-name "$ROLE_NAME" \
        --policy-name DynamoDBAccess \
        --policy-document "{
            \"Version\":\"2012-10-17\",
            \"Statement\":[{
                \"Effect\":\"Allow\",
                \"Action\":[\"dynamodb:PutItem\",\"dynamodb:UpdateItem\",\"dynamodb:Query\",\"dynamodb:GetItem\"],
                \"Resource\":[
                    \"arn:aws:dynamodb:$REGION:$ACCOUNT_ID:table/$TABLE_NAME\",
                    \"arn:aws:dynamodb:$REGION:$ACCOUNT_ID:table/$TABLE_NAME/index/*\"
                ]
            }]
        }"

    echo "  Role created: $ROLE_ARN"
    echo "  Waiting 10s for IAM propagation..."
    sleep 10
fi

# --- 3. Lambda Function ---
next_step "Lambda function..."
if $AWS lambda get-function --function-name "$FUNCTION_NAME" --query "Configuration.FunctionName" &>/dev/null; then
    echo "  Lambda '$FUNCTION_NAME' already exists. Updating code..."
    package_lambda
    $AWS lambda update-function-code \
        --function-name "$FUNCTION_NAME" \
        --zip-file fileb:///tmp/wecom_relay.zip > /dev/null

    # Update env vars if token/aes-key provided
    if [ -n "$WECOM_TOKEN" ] || [ -n "$WECOM_AES_KEY" ] || [ -n "$AGENT_CONFIGS" ]; then
        echo "  Updating environment variables..."
        ENV_JSON="{\"Variables\":{\"TABLE_NAME\":\"$TABLE_NAME\",\"TTL_DAYS\":\"$TTL_DAYS\""
        [ -n "$WECOM_TOKEN" ] && ENV_JSON="$ENV_JSON,\"WECOM_TOKEN\":\"$WECOM_TOKEN\""
        [ -n "$WECOM_AES_KEY" ] && ENV_JSON="$ENV_JSON,\"WECOM_AES_KEY\":\"$WECOM_AES_KEY\""
        [ -n "$AGENT_CONFIGS" ] && ENV_JSON="$ENV_JSON,\"AGENT_CONFIGS\":$(echo "$AGENT_CONFIGS" | python3 -c 'import sys,json; print(json.dumps(sys.stdin.read().strip()))')"
        ENV_JSON="$ENV_JSON}}"

        $AWS lambda update-function-configuration \
            --function-name "$FUNCTION_NAME" \
            --environment "$ENV_JSON" > /dev/null
    fi
    echo "  Lambda updated."
else
    if [ -z "$WECOM_TOKEN" ] || [ -z "$WECOM_AES_KEY" ]; then
        echo ""
        echo "  ERROR: --token and --aes-key are required for first-time Lambda creation."
        echo "  Usage: $0 --token YOUR_TOKEN --aes-key YOUR_AES_KEY"
        exit 1
    fi

    package_lambda

    echo "  Creating Lambda '$FUNCTION_NAME'..."
    ROLE_ARN="arn:aws:iam::${ACCOUNT_ID}:role/$ROLE_NAME"

    ENV_JSON="{\"Variables\":{\"TABLE_NAME\":\"$TABLE_NAME\",\"WECOM_TOKEN\":\"$WECOM_TOKEN\",\"WECOM_AES_KEY\":\"$WECOM_AES_KEY\",\"TTL_DAYS\":\"$TTL_DAYS\""
    [ -n "$AGENT_CONFIGS" ] && ENV_JSON="$ENV_JSON,\"AGENT_CONFIGS\":$(echo "$AGENT_CONFIGS" | python3 -c 'import sys,json; print(json.dumps(sys.stdin.read().strip()))')"
    ENV_JSON="$ENV_JSON}}"

    $AWS lambda create-function \
        --function-name "$FUNCTION_NAME" \
        --runtime python3.11 \
        --handler lambda_function.lambda_handler \
        --role "$ROLE_ARN" \
        --zip-file fileb:///tmp/wecom_relay.zip \
        --timeout 30 --memory-size 128 \
        --environment "$ENV_JSON" > /dev/null

    echo "  Waiting for Lambda to be active..."
    aws --region "$REGION" lambda wait function-active-v2 --function-name "$FUNCTION_NAME"
    echo "  Lambda created."
fi
LAMBDA_ARN="arn:aws:lambda:$REGION:$ACCOUNT_ID:function:$FUNCTION_NAME"

# --- 4. API Gateway ---
next_step "API Gateway..."
API_ID=$($AWS apigatewayv2 get-apis --query "Items[?Name=='$API_NAME'].ApiId | [0]" 2>/dev/null || echo "None")

if [ "$API_ID" != "None" ] && [ -n "$API_ID" ]; then
    echo "  API Gateway '$API_NAME' already exists: $API_ID"
else
    echo "  Creating HTTP API '$API_NAME'..."
    API_ID=$($AWS apigatewayv2 create-api \
        --name "$API_NAME" --protocol-type HTTP \
        --query ApiId)

    # Create integration
    INTEG_ID=$($AWS apigatewayv2 create-integration \
        --api-id "$API_ID" \
        --integration-type AWS_PROXY \
        --integration-uri "$LAMBDA_ARN" \
        --payload-format-version "2.0" \
        --query IntegrationId)

    # Create all 5 routes
    for ROUTE_KEY in "GET /callback" "POST /callback" \
                     "GET /callback/{agent_id}" "POST /callback/{agent_id}" \
                     "POST /messages/fetch"; do
        $AWS apigatewayv2 create-route \
            --api-id "$API_ID" \
            --route-key "$ROUTE_KEY" \
            --target "integrations/$INTEG_ID" > /dev/null
    done

    # Auto-deploy stage
    $AWS apigatewayv2 create-stage \
        --api-id "$API_ID" \
        --stage-name '$default' \
        --auto-deploy > /dev/null

    # Grant API Gateway permission to invoke Lambda
    $AWS lambda add-permission \
        --function-name "$FUNCTION_NAME" \
        --statement-id ApiGatewayInvoke \
        --action lambda:InvokeFunction \
        --principal apigateway.amazonaws.com \
        --source-arn "arn:aws:execute-api:$REGION:$ACCOUNT_ID:$API_ID/*" > /dev/null

    echo "  API Gateway created with 5 routes."
fi

ENDPOINT="https://$API_ID.execute-api.$REGION.amazonaws.com"

# --- 5. Summary ---
next_step "Done!"
echo ""
echo "════════════════════════════════════════════════════════"
echo "  RELAY SETUP COMPLETE"
echo "════════════════════════════════════════════════════════"
echo ""
echo "  Endpoint:   $ENDPOINT"
echo "  API ID:     $API_ID"
echo "  Lambda:     $FUNCTION_NAME"
echo "  DynamoDB:   $TABLE_NAME"
echo "  Region:     $REGION"
echo ""
echo "  Callback URL (single-agent):"
echo "    $ENDPOINT/callback"
echo ""
echo "  Callback URL (multi-agent):"
echo "    $ENDPOINT/callback/<agent_id>"
echo ""
echo "──────────────────────────────────────────────────────"
echo "  NEXT STEPS"
echo "──────────────────────────────────────────────────────"
echo ""
echo "  1. Register the callback URL in WeCom admin console"
echo "     (应用管理 → Your App → 接收消息 → 设置API接收)"
echo ""
echo "  2. Add this to your config.yaml:"
echo ""
echo "     wecom:"
echo "       mode: \"relay\""
echo "       relay_url: \"$ENDPOINT\""
echo ""
echo "  Or run: lobster init"
echo ""
echo "──────────────────────────────────────────────────────"
echo "  MANAGEMENT"
echo "──────────────────────────────────────────────────────"
echo ""
echo "  Update code:   $0 --update-code"
echo "  Teardown:      $0 --teardown"
echo ""
