#!/usr/bin/env bash
#
# Provision an EC2 instance with an Elastic IP for use as a SOCKS5 proxy.
# Routes outbound WeCom API calls through a fixed IP.
#
# Prerequisites:
#   - AWS CLI v2 configured with credentials (aws configure)
#   - Permissions: ec2:* (RunInstances, AllocateAddress, CreateSecurityGroup, etc.)
#
# Usage:
#   ./setup-proxy.sh [--region ap-southeast-1] [--key-name rc-proxy-key]
#
# Outputs:
#   - Elastic IP address
#   - Instance ID
#   - SSH key saved to ~/.ssh/<key-name>.pem
#
# Idempotent: re-running skips resources that already exist (matched by Name tag).
#

set -euo pipefail

# --- Defaults ---
REGION="${AWS_DEFAULT_REGION:-ap-southeast-1}"
KEY_NAME="rc-proxy-key"
SG_NAME="rc-proxy-sg"
INSTANCE_NAME="rc-proxy"
INSTANCE_TYPE="t3.micro"
TAG_KEY="Project"
TAG_VALUE="remote-control-proxy"

# --- Parse args ---
while [[ $# -gt 0 ]]; do
    case "$1" in
        --region) REGION="$2"; shift 2 ;;
        --key-name) KEY_NAME="$2"; shift 2 ;;
        *) echo "Unknown arg: $1"; exit 1 ;;
    esac
done

AWS="aws --region $REGION --output text"
KEY_FILE="$HOME/.ssh/${KEY_NAME}.pem"

echo "=== Remote Control — Proxy EC2 Setup ==="
echo "  Region:    $REGION"
echo "  Key name:  $KEY_NAME"
echo ""

# --- 1. SSH Key Pair ---
echo "[1/5] SSH key pair..."
if $AWS ec2 describe-key-pairs --key-names "$KEY_NAME" --query "KeyPairs[0].KeyPairId" 2>/dev/null; then
    echo "  Key pair '$KEY_NAME' already exists."
else
    echo "  Creating key pair '$KEY_NAME'..."
    $AWS ec2 create-key-pair --key-name "$KEY_NAME" --query "KeyMaterial" > "$KEY_FILE"
    chmod 600 "$KEY_FILE"
    echo "  Saved private key to $KEY_FILE"
fi

# --- 2. Security Group ---
echo "[2/5] Security group..."
SG_ID=$($AWS ec2 describe-security-groups \
    --filters "Name=group-name,Values=$SG_NAME" \
    --query "SecurityGroups[0].GroupId" 2>/dev/null || echo "None")

if [ "$SG_ID" = "None" ] || [ -z "$SG_ID" ]; then
    VPC_ID=$($AWS ec2 describe-vpcs --filters "Name=is-default,Values=true" --query "Vpcs[0].VpcId")
    echo "  Creating security group in VPC $VPC_ID..."
    SG_ID=$($AWS ec2 create-security-group \
        --group-name "$SG_NAME" \
        --description "SSH access for remote-control proxy" \
        --vpc-id "$VPC_ID")
    $AWS ec2 authorize-security-group-ingress \
        --group-id "$SG_ID" \
        --protocol tcp --port 22 --cidr 0.0.0.0/0 > /dev/null
    $AWS ec2 create-tags --resources "$SG_ID" \
        --tags "Key=$TAG_KEY,Value=$TAG_VALUE" > /dev/null
    echo "  Created security group: $SG_ID"
else
    echo "  Security group already exists: $SG_ID"
fi

# --- 3. EC2 Instance ---
echo "[3/5] EC2 instance..."
INSTANCE_ID=$($AWS ec2 describe-instances \
    --filters "Name=tag:Name,Values=$INSTANCE_NAME" "Name=instance-state-name,Values=running,stopped" \
    --query "Reservations[0].Instances[0].InstanceId" 2>/dev/null || echo "None")

if [ "$INSTANCE_ID" = "None" ] || [ -z "$INSTANCE_ID" ]; then
    # Get latest Amazon Linux 2023 AMI
    AMI_ID=$($AWS ec2 describe-images \
        --owners amazon \
        --filters "Name=name,Values=al2023-ami-2023.*-x86_64" "Name=state,Values=available" \
        --query "sort_by(Images, &CreationDate)[-1].ImageId")
    echo "  Launching $INSTANCE_TYPE (AMI: $AMI_ID)..."
    INSTANCE_ID=$($AWS ec2 run-instances \
        --image-id "$AMI_ID" \
        --instance-type "$INSTANCE_TYPE" \
        --key-name "$KEY_NAME" \
        --security-group-ids "$SG_ID" \
        --tag-specifications "ResourceType=instance,Tags=[{Key=Name,Value=$INSTANCE_NAME},{Key=$TAG_KEY,Value=$TAG_VALUE}]" \
        --query "Instances[0].InstanceId")
    echo "  Waiting for instance to be running..."
    aws --region "$REGION" ec2 wait instance-running --instance-ids "$INSTANCE_ID"
    echo "  Instance running: $INSTANCE_ID"
else
    echo "  Instance already exists: $INSTANCE_ID"
    # Ensure it's running
    STATE=$($AWS ec2 describe-instances --instance-ids "$INSTANCE_ID" --query "Reservations[0].Instances[0].State.Name")
    if [ "$STATE" = "stopped" ]; then
        echo "  Starting stopped instance..."
        $AWS ec2 start-instances --instance-ids "$INSTANCE_ID" > /dev/null
        aws --region "$REGION" ec2 wait instance-running --instance-ids "$INSTANCE_ID"
    fi
fi

# --- 4. Elastic IP ---
echo "[4/5] Elastic IP..."
EIP_ALLOC=$($AWS ec2 describe-addresses \
    --filters "Name=tag:$TAG_KEY,Values=$TAG_VALUE" \
    --query "Addresses[0].AllocationId" 2>/dev/null || echo "None")

if [ "$EIP_ALLOC" = "None" ] || [ -z "$EIP_ALLOC" ]; then
    echo "  Allocating Elastic IP..."
    EIP_ALLOC=$($AWS ec2 allocate-address --domain vpc --query "AllocationId")
    $AWS ec2 create-tags --resources "$EIP_ALLOC" \
        --tags "Key=$TAG_KEY,Value=$TAG_VALUE" "Key=Name,Value=$INSTANCE_NAME" > /dev/null
fi

# Associate with instance
CURRENT_ASSOC=$($AWS ec2 describe-addresses \
    --allocation-ids "$EIP_ALLOC" \
    --query "Addresses[0].InstanceId" 2>/dev/null || echo "None")

if [ "$CURRENT_ASSOC" != "$INSTANCE_ID" ]; then
    echo "  Associating Elastic IP with instance..."
    $AWS ec2 associate-address --instance-id "$INSTANCE_ID" --allocation-id "$EIP_ALLOC" > /dev/null
fi

ELASTIC_IP=$($AWS ec2 describe-addresses --allocation-ids "$EIP_ALLOC" --query "Addresses[0].PublicIp")
echo "  Elastic IP: $ELASTIC_IP"

# --- 5. Summary ---
echo ""
echo "[5/5] Done!"
echo ""
echo "=== Proxy Infrastructure ==="
echo "  Instance ID:  $INSTANCE_ID"
echo "  Elastic IP:   $ELASTIC_IP"
echo "  SSH key:      $KEY_FILE"
echo "  Region:       $REGION"
echo ""
echo "Test SSH connection:"
echo "  ssh -i $KEY_FILE ec2-user@$ELASTIC_IP"
echo ""
echo "Start SOCKS5 tunnel:"
echo "  ./scripts/start-proxy-tunnel.sh $ELASTIC_IP 1080 $KEY_FILE"
echo ""
echo "Register $ELASTIC_IP as trusted IP in WeCom admin console."
