#!/usr/bin/env bash
#
# lobster-cc — One-command AWS infrastructure setup.
#
# Deploys everything you need to run lobster-cc:
#   1. Relay stack (Lambda + API Gateway + DynamoDB)
#   2. [Optional] EC2 proxy with Elastic IP (for WeCom IP whitelist)
#
# Prerequisites:
#   - AWS CLI v2 configured with credentials (aws configure)
#   - pip (for packaging Lambda dependencies)
#
# Usage:
#   ./scripts/setup.sh --token TOKEN --aes-key KEY             # relay only
#   ./scripts/setup.sh --token TOKEN --aes-key KEY --proxy     # relay + EC2 proxy
#
# All flags:
#   --token TOKEN         WeCom callback verification token (required first time)
#   --aes-key KEY         WeCom callback AES encoding key (required first time)
#   --agent-configs JSON  Multi-agent config (optional)
#   --region REGION       AWS region (default: ap-southeast-1)
#   --ttl-days N          Message retention days (default: 7)
#   --proxy               Also provision EC2 proxy with Elastic IP
#   --proxy-key-name NAME SSH key pair name (default: rc-proxy-key)
#   --update-code         Only update Lambda code (skip all infra)
#   --teardown            Delete ALL resources (relay + proxy)
#
# Idempotent: re-running skips existing resources.
#

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# ── Defaults ──────────────────────────────────────
REGION="${AWS_DEFAULT_REGION:-ap-southeast-1}"
WECOM_TOKEN=""
WECOM_AES_KEY=""
AGENT_CONFIGS=""
TTL_DAYS="7"
WITH_PROXY=false
PROXY_KEY_NAME="rc-proxy-key"
UPDATE_CODE_ONLY=false
TEARDOWN=false

# ── Relay resource names ──────────────────────────
TABLE_NAME="wecom_relay_messages"
FUNCTION_NAME="wecom-relay"
ROLE_NAME="wecom-relay-lambda-role"
API_NAME="wecom-relay"

# ── Proxy resource names ──────────────────────────
PROXY_SG_NAME="rc-proxy-sg"
PROXY_INSTANCE_NAME="rc-proxy"
PROXY_INSTANCE_TYPE="t3.micro"
PROXY_TAG_KEY="Project"
PROXY_TAG_VALUE="remote-control-proxy"

# ── Parse args ────────────────────────────────────
while [[ $# -gt 0 ]]; do
    case "$1" in
        --token)          WECOM_TOKEN="$2"; shift 2 ;;
        --aes-key)        WECOM_AES_KEY="$2"; shift 2 ;;
        --agent-configs)  AGENT_CONFIGS="$2"; shift 2 ;;
        --region)         REGION="$2"; shift 2 ;;
        --ttl-days)       TTL_DAYS="$2"; shift 2 ;;
        --proxy)          WITH_PROXY=true; shift ;;
        --proxy-key-name) PROXY_KEY_NAME="$2"; shift 2 ;;
        --update-code)    UPDATE_CODE_ONLY=true; shift ;;
        --teardown)       TEARDOWN=true; shift ;;
        -h|--help)
            sed -n '2,/^$/{ s/^# //; s/^#//; p }' "$0"
            exit 0 ;;
        -*)  echo "Unknown flag: $1"; exit 1 ;;
        *)   echo "Unexpected arg: $1"; exit 1 ;;
    esac
done

AWS="aws --region $REGION --output text"
RELAY_DIR="$(cd "$SCRIPT_DIR/../relay" && pwd)"
KEY_FILE="$HOME/.ssh/${PROXY_KEY_NAME}.pem"

# ══════════════════════════════════════════════════
# TEARDOWN
# ══════════════════════════════════════════════════
if [ "$TEARDOWN" = true ]; then
    echo "╔══════════════════════════════════════════╗"
    echo "║  lobster-cc — Teardown All Resources     ║"
    echo "╚══════════════════════════════════════════╝"
    echo "  Region: $REGION"
    echo ""

    # ── Relay teardown ──
    echo "── Relay ──────────────────────────────────"

    API_ID=$($AWS apigatewayv2 get-apis --query "Items[?Name=='$API_NAME'].ApiId | [0]" 2>/dev/null || echo "None")
    if [ "$API_ID" != "None" ] && [ -n "$API_ID" ]; then
        echo "  Deleting API Gateway ($API_ID)..."
        $AWS apigatewayv2 delete-api --api-id "$API_ID" > /dev/null
    else echo "  API Gateway: not found"; fi

    if $AWS lambda get-function --function-name "$FUNCTION_NAME" --query "Configuration.FunctionName" &>/dev/null; then
        echo "  Deleting Lambda ($FUNCTION_NAME)..."
        $AWS lambda delete-function --function-name "$FUNCTION_NAME" > /dev/null
    else echo "  Lambda: not found"; fi

    if aws iam get-role --role-name "$ROLE_NAME" --query "Role.RoleName" &>/dev/null 2>&1; then
        echo "  Deleting IAM role ($ROLE_NAME)..."
        aws iam detach-role-policy --role-name "$ROLE_NAME" \
            --policy-arn arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole 2>/dev/null || true
        aws iam delete-role-policy --role-name "$ROLE_NAME" --policy-name DynamoDBAccess 2>/dev/null || true
        aws iam delete-role --role-name "$ROLE_NAME" > /dev/null
    else echo "  IAM role: not found"; fi

    if $AWS dynamodb describe-table --table-name "$TABLE_NAME" --query "Table.TableName" &>/dev/null; then
        echo "  Deleting DynamoDB table ($TABLE_NAME)..."
        $AWS dynamodb delete-table --table-name "$TABLE_NAME" > /dev/null
    else echo "  DynamoDB table: not found"; fi

    # ── Proxy teardown ──
    echo ""
    echo "── Proxy ──────────────────────────────────"

    INSTANCE_ID=$($AWS ec2 describe-instances \
        --filters "Name=tag:Name,Values=$PROXY_INSTANCE_NAME" "Name=instance-state-name,Values=running,stopped" \
        --query "Reservations[0].Instances[0].InstanceId" 2>/dev/null || echo "None")
    if [ "$INSTANCE_ID" != "None" ] && [ -n "$INSTANCE_ID" ]; then
        echo "  Terminating EC2 instance ($INSTANCE_ID)..."
        $AWS ec2 terminate-instances --instance-ids "$INSTANCE_ID" > /dev/null
    else echo "  EC2 instance: not found"; fi

    EIP_ALLOC=$($AWS ec2 describe-addresses \
        --filters "Name=tag:$PROXY_TAG_KEY,Values=$PROXY_TAG_VALUE" \
        --query "Addresses[0].AllocationId" 2>/dev/null || echo "None")
    if [ "$EIP_ALLOC" != "None" ] && [ -n "$EIP_ALLOC" ]; then
        # Wait for instance to terminate before releasing EIP
        if [ "$INSTANCE_ID" != "None" ] && [ -n "$INSTANCE_ID" ]; then
            echo "  Waiting for instance to terminate..."
            aws --region "$REGION" ec2 wait instance-terminated --instance-ids "$INSTANCE_ID" 2>/dev/null || true
        fi
        echo "  Releasing Elastic IP ($EIP_ALLOC)..."
        $AWS ec2 release-address --allocation-id "$EIP_ALLOC" > /dev/null 2>&1 || true
    else echo "  Elastic IP: not found"; fi

    SG_ID=$($AWS ec2 describe-security-groups \
        --filters "Name=group-name,Values=$PROXY_SG_NAME" \
        --query "SecurityGroups[0].GroupId" 2>/dev/null || echo "None")
    if [ "$SG_ID" != "None" ] && [ -n "$SG_ID" ]; then
        echo "  Deleting security group ($SG_ID)..."
        # May need to wait for ENI detachment
        sleep 5
        $AWS ec2 delete-security-group --group-id "$SG_ID" > /dev/null 2>&1 || \
            echo "  (security group still in use — will be cleaned up after instance fully terminates)"
    else echo "  Security group: not found"; fi

    if $AWS ec2 describe-key-pairs --key-names "$PROXY_KEY_NAME" --query "KeyPairs[0].KeyPairId" &>/dev/null; then
        echo "  Deleting key pair ($PROXY_KEY_NAME)..."
        $AWS ec2 delete-key-pair --key-name "$PROXY_KEY_NAME" > /dev/null
        [ -f "$KEY_FILE" ] && rm -f "$KEY_FILE" && echo "  Removed $KEY_FILE"
    else echo "  Key pair: not found"; fi

    echo ""
    echo "Teardown complete."
    exit 0
fi

# ══════════════════════════════════════════════════
# DEPLOY
# ══════════════════════════════════════════════════
PHASE_COUNT=5
[ "$WITH_PROXY" = true ] && PHASE_COUNT=10

echo "╔══════════════════════════════════════════╗"
echo "║  lobster-cc — AWS Infrastructure Setup   ║"
echo "╚══════════════════════════════════════════╝"
echo "  Region:  $REGION"
echo "  Relay:   yes"
echo "  Proxy:   $WITH_PROXY"
echo ""

ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
STEP=0
next_step() { STEP=$((STEP + 1)); echo "[$STEP/$PHASE_COUNT] $1"; }

# ── Helper: package Lambda zip ────────────────────
package_lambda() {
    echo "  Packaging Lambda..."
    local TMP_DIR
    TMP_DIR=$(mktemp -d)
    pip install --quiet --platform manylinux2014_x86_64 --only-binary=:all: \
        --target "$TMP_DIR" pycryptodome > /dev/null
    cp "$RELAY_DIR/lambda_function.py" "$TMP_DIR/"
    (cd "$TMP_DIR" && zip -qr /tmp/wecom_relay.zip .)
    rm -rf "$TMP_DIR"
}

# ── Helper: build env JSON ────────────────────────
build_env_json() {
    local ENV_JSON="{\"Variables\":{\"TABLE_NAME\":\"$TABLE_NAME\",\"TTL_DAYS\":\"$TTL_DAYS\""
    [ -n "$WECOM_TOKEN" ]   && ENV_JSON="$ENV_JSON,\"WECOM_TOKEN\":\"$WECOM_TOKEN\""
    [ -n "$WECOM_AES_KEY" ] && ENV_JSON="$ENV_JSON,\"WECOM_AES_KEY\":\"$WECOM_AES_KEY\""
    [ -n "$AGENT_CONFIGS" ] && ENV_JSON="$ENV_JSON,\"AGENT_CONFIGS\":$(echo "$AGENT_CONFIGS" | python3 -c 'import sys,json; print(json.dumps(sys.stdin.read().strip()))')"
    ENV_JSON="$ENV_JSON}}"
    echo "$ENV_JSON"
}

# ══════════════════════════════════════════════════
# UPDATE CODE ONLY
# ══════════════════════════════════════════════════
if [ "$UPDATE_CODE_ONLY" = true ]; then
    echo "[1/1] Updating Lambda code..."
    package_lambda
    $AWS lambda update-function-code \
        --function-name "$FUNCTION_NAME" \
        --zip-file fileb:///tmp/wecom_relay.zip > /dev/null
    echo "  Done."
    exit 0
fi

# ══════════════════════════════════════════════════
# PHASE 1: RELAY (steps 1-5)
# ══════════════════════════════════════════════════
echo "── Relay Stack ────────────────────────────"
echo ""

# --- 1. DynamoDB ---
next_step "DynamoDB table..."
if $AWS dynamodb describe-table --table-name "$TABLE_NAME" --query "Table.TableName" &>/dev/null; then
    echo "  Already exists."
else
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
    aws --region "$REGION" dynamodb wait table-exists --table-name "$TABLE_NAME"
    $AWS dynamodb update-time-to-live \
        --table-name "$TABLE_NAME" \
        --time-to-live-specification "Enabled=true,AttributeName=ttl" > /dev/null
    echo "  Created with GSI and TTL."
fi

# --- 2. IAM Role ---
next_step "IAM role..."
if aws iam get-role --role-name "$ROLE_NAME" --query "Role.Arn" --output text &>/dev/null 2>&1; then
    ROLE_ARN=$(aws iam get-role --role-name "$ROLE_NAME" --query "Role.Arn" --output text)
    echo "  Already exists."
else
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
    echo "  Created. Waiting 10s for IAM propagation..."
    sleep 10
fi

# --- 3. Lambda ---
next_step "Lambda function..."
package_lambda
if $AWS lambda get-function --function-name "$FUNCTION_NAME" --query "Configuration.FunctionName" &>/dev/null; then
    echo "  Already exists. Updating code..."
    $AWS lambda update-function-code \
        --function-name "$FUNCTION_NAME" \
        --zip-file fileb:///tmp/wecom_relay.zip > /dev/null
    if [ -n "$WECOM_TOKEN" ] || [ -n "$WECOM_AES_KEY" ] || [ -n "$AGENT_CONFIGS" ]; then
        echo "  Updating environment variables..."
        $AWS lambda update-function-configuration \
            --function-name "$FUNCTION_NAME" \
            --environment "$(build_env_json)" > /dev/null
    fi
else
    if [ -z "$WECOM_TOKEN" ] || [ -z "$WECOM_AES_KEY" ]; then
        echo ""
        echo "  ERROR: --token and --aes-key required for first-time setup."
        exit 1
    fi
    ROLE_ARN="arn:aws:iam::${ACCOUNT_ID}:role/$ROLE_NAME"
    $AWS lambda create-function \
        --function-name "$FUNCTION_NAME" \
        --runtime python3.11 \
        --handler lambda_function.lambda_handler \
        --role "$ROLE_ARN" \
        --zip-file fileb:///tmp/wecom_relay.zip \
        --timeout 30 --memory-size 128 \
        --environment "$(build_env_json)" > /dev/null
    aws --region "$REGION" lambda wait function-active-v2 --function-name "$FUNCTION_NAME"
    echo "  Created."
fi
LAMBDA_ARN="arn:aws:lambda:$REGION:$ACCOUNT_ID:function:$FUNCTION_NAME"

# --- 4. API Gateway ---
next_step "API Gateway..."
API_ID=$($AWS apigatewayv2 get-apis --query "Items[?Name=='$API_NAME'].ApiId | [0]" 2>/dev/null || echo "None")
if [ "$API_ID" != "None" ] && [ -n "$API_ID" ]; then
    echo "  Already exists: $API_ID"
else
    API_ID=$($AWS apigatewayv2 create-api --name "$API_NAME" --protocol-type HTTP --query ApiId)
    INTEG_ID=$($AWS apigatewayv2 create-integration \
        --api-id "$API_ID" --integration-type AWS_PROXY \
        --integration-uri "$LAMBDA_ARN" --payload-format-version "2.0" \
        --query IntegrationId)
    for RK in "GET /callback" "POST /callback" \
              "GET /callback/{agent_id}" "POST /callback/{agent_id}" \
              "POST /messages/fetch"; do
        $AWS apigatewayv2 create-route --api-id "$API_ID" \
            --route-key "$RK" --target "integrations/$INTEG_ID" > /dev/null
    done
    $AWS apigatewayv2 create-stage --api-id "$API_ID" --stage-name '$default' --auto-deploy > /dev/null
    $AWS lambda add-permission --function-name "$FUNCTION_NAME" \
        --statement-id ApiGatewayInvoke --action lambda:InvokeFunction \
        --principal apigateway.amazonaws.com \
        --source-arn "arn:aws:execute-api:$REGION:$ACCOUNT_ID:$API_ID/*" > /dev/null
    echo "  Created with 5 routes."
fi
ENDPOINT="https://$API_ID.execute-api.$REGION.amazonaws.com"

# --- 5. Relay summary ---
next_step "Relay ready!"
echo ""
echo "  Endpoint: $ENDPOINT"
echo ""

# ══════════════════════════════════════════════════
# PHASE 2: PROXY (steps 6-10, optional)
# ══════════════════════════════════════════════════
ELASTIC_IP=""
if [ "$WITH_PROXY" = true ]; then
    echo "── EC2 Proxy ──────────────────────────────"
    echo ""

    # --- 6. SSH Key Pair ---
    next_step "SSH key pair..."
    if $AWS ec2 describe-key-pairs --key-names "$PROXY_KEY_NAME" --query "KeyPairs[0].KeyPairId" 2>/dev/null; then
        echo "  Already exists."
    else
        $AWS ec2 create-key-pair --key-name "$PROXY_KEY_NAME" --query "KeyMaterial" > "$KEY_FILE"
        chmod 600 "$KEY_FILE"
        echo "  Created. Private key saved to $KEY_FILE"
    fi

    # --- 7. Security Group ---
    next_step "Security group..."
    SG_ID=$($AWS ec2 describe-security-groups \
        --filters "Name=group-name,Values=$PROXY_SG_NAME" \
        --query "SecurityGroups[0].GroupId" 2>/dev/null || echo "None")
    if [ "$SG_ID" = "None" ] || [ -z "$SG_ID" ]; then
        VPC_ID=$($AWS ec2 describe-vpcs --filters "Name=is-default,Values=true" --query "Vpcs[0].VpcId")
        SG_ID=$($AWS ec2 create-security-group \
            --group-name "$PROXY_SG_NAME" \
            --description "SSH access for lobster-cc proxy" \
            --vpc-id "$VPC_ID")
        $AWS ec2 authorize-security-group-ingress \
            --group-id "$SG_ID" --protocol tcp --port 22 --cidr 0.0.0.0/0 > /dev/null
        $AWS ec2 create-tags --resources "$SG_ID" \
            --tags "Key=$PROXY_TAG_KEY,Value=$PROXY_TAG_VALUE" > /dev/null
        echo "  Created: $SG_ID"
    else
        echo "  Already exists: $SG_ID"
    fi

    # --- 8. EC2 Instance ---
    next_step "EC2 instance..."
    INSTANCE_ID=$($AWS ec2 describe-instances \
        --filters "Name=tag:Name,Values=$PROXY_INSTANCE_NAME" "Name=instance-state-name,Values=running,stopped" \
        --query "Reservations[0].Instances[0].InstanceId" 2>/dev/null || echo "None")
    if [ "$INSTANCE_ID" = "None" ] || [ -z "$INSTANCE_ID" ]; then
        AMI_ID=$($AWS ec2 describe-images --owners amazon \
            --filters "Name=name,Values=al2023-ami-2023.*-x86_64" "Name=state,Values=available" \
            --query "sort_by(Images, &CreationDate)[-1].ImageId")
        INSTANCE_ID=$($AWS ec2 run-instances \
            --image-id "$AMI_ID" --instance-type "$PROXY_INSTANCE_TYPE" \
            --key-name "$PROXY_KEY_NAME" --security-group-ids "$SG_ID" \
            --tag-specifications "ResourceType=instance,Tags=[{Key=Name,Value=$PROXY_INSTANCE_NAME},{Key=$PROXY_TAG_KEY,Value=$PROXY_TAG_VALUE}]" \
            --query "Instances[0].InstanceId")
        echo "  Launching... waiting for ready state..."
        aws --region "$REGION" ec2 wait instance-running --instance-ids "$INSTANCE_ID"
        echo "  Running: $INSTANCE_ID"
    else
        echo "  Already exists: $INSTANCE_ID"
        STATE=$($AWS ec2 describe-instances --instance-ids "$INSTANCE_ID" --query "Reservations[0].Instances[0].State.Name")
        if [ "$STATE" = "stopped" ]; then
            echo "  Starting stopped instance..."
            $AWS ec2 start-instances --instance-ids "$INSTANCE_ID" > /dev/null
            aws --region "$REGION" ec2 wait instance-running --instance-ids "$INSTANCE_ID"
        fi
    fi

    # --- 9. Elastic IP ---
    next_step "Elastic IP..."
    EIP_ALLOC=$($AWS ec2 describe-addresses \
        --filters "Name=tag:$PROXY_TAG_KEY,Values=$PROXY_TAG_VALUE" \
        --query "Addresses[0].AllocationId" 2>/dev/null || echo "None")
    if [ "$EIP_ALLOC" = "None" ] || [ -z "$EIP_ALLOC" ]; then
        EIP_ALLOC=$($AWS ec2 allocate-address --domain vpc --query "AllocationId")
        $AWS ec2 create-tags --resources "$EIP_ALLOC" \
            --tags "Key=$PROXY_TAG_KEY,Value=$PROXY_TAG_VALUE" "Key=Name,Value=$PROXY_INSTANCE_NAME" > /dev/null
    fi
    CURRENT_ASSOC=$($AWS ec2 describe-addresses \
        --allocation-ids "$EIP_ALLOC" --query "Addresses[0].InstanceId" 2>/dev/null || echo "None")
    if [ "$CURRENT_ASSOC" != "$INSTANCE_ID" ]; then
        $AWS ec2 associate-address --instance-id "$INSTANCE_ID" --allocation-id "$EIP_ALLOC" > /dev/null
    fi
    ELASTIC_IP=$($AWS ec2 describe-addresses --allocation-ids "$EIP_ALLOC" --query "Addresses[0].PublicIp")

    # --- 10. Proxy summary ---
    next_step "Proxy ready!"
    echo ""
    echo "  Elastic IP:  $ELASTIC_IP"
    echo "  Instance:    $INSTANCE_ID"
    echo "  SSH key:     $KEY_FILE"
    echo ""
fi

# ══════════════════════════════════════════════════
# FINAL SUMMARY
# ══════════════════════════════════════════════════
echo ""
echo "╔══════════════════════════════════════════════════════╗"
echo "║  SETUP COMPLETE                                      ║"
echo "╠══════════════════════════════════════════════════════╣"
echo "║                                                      ║"
echo "║  Relay endpoint:                                     ║"
echo "║    $ENDPOINT"
echo "║                                                      ║"
if [ -n "$ELASTIC_IP" ]; then
echo "║  Proxy (Elastic IP):                                 ║"
echo "║    $ELASTIC_IP"
echo "║                                                      ║"
fi
echo "╚══════════════════════════════════════════════════════╝"
echo ""
echo "── Next Steps ────────────────────────────────────────"
echo ""
echo "  1. Register callback URL in WeCom admin console:"
echo "     $ENDPOINT/callback"
echo "     (or $ENDPOINT/callback/<agent_id> for multi-agent)"
echo ""
echo "  2. Generate config.yaml:"
echo "     lobster init"
echo ""
echo "     Or add to config.yaml manually:"
echo "       wecom:"
echo "         mode: \"relay\""
echo "         relay_url: \"$ENDPOINT\""
if [ -n "$ELASTIC_IP" ]; then
echo "         proxy: \"socks5://127.0.0.1:1080\""
fi
echo ""
echo "  3. Start the server:"
echo "     lobster -c config.yaml"
echo ""
if [ -n "$ELASTIC_IP" ]; then
echo "  4. Register $ELASTIC_IP as trusted IP in WeCom admin console"
echo ""
echo "  5. Deploy with proxy tunnel:"
echo "     ./deploy.sh user@host /path --proxy-ip $ELASTIC_IP --proxy-key $KEY_FILE"
echo ""
fi
echo "── Management ────────────────────────────────────────"
echo ""
echo "  Update Lambda code:  $0 --update-code"
echo "  Teardown everything: $0 --teardown"
echo ""
