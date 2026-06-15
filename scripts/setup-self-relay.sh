#!/usr/bin/env bash
#
# Provision and deploy the self-hosted WeCom relay onto an existing EC2 box.
# Replaces the AWS API Gateway + Lambda + DynamoDB relay (AppSec finding
# APIGAuthenticationCheck).
#
# What it does:
#   1. Opens the relay port in the EC2 security group RESTRICTED to the WeCom
#      callback IP ranges you pass via --wecom-ips (NEVER 0.0.0.0/0).
#   2. Runs scripts/audit-sg.sh as a gate (fails if port would be 0.0.0.0/0).
#   3. Copies relay code + installs/starts the systemd unit on the EC2 host.
#
# Get the WeCom callback IP ranges from the getcallbackip API:
#   https://developer.work.weixin.qq.com/document/path/90930
#
# Usage:
#   ./scripts/setup-self-relay.sh \
#       --host ec2-user@<elastic-ip> --sg-id sg-xxxx \
#       --relay-port 8443 --fetch-token <secret> \
#       --wecom-ips "1.2.3.0/24,5.6.7.8/32" \
#       --ssh-key ~/.ssh/rc-proxy-key.pem [--region ap-southeast-1] [--dry-run]
#
set -euo pipefail

REGION="${AWS_DEFAULT_REGION:-ap-southeast-1}"
HOST=""; SG_ID=""; RELAY_PORT="8443"; FETCH_TOKEN=""; WECOM_IPS=""
WECOM_TOKEN=""; WECOM_AES_KEY=""; AGENT_CONFIGS=""
SSH_KEY="$HOME/.ssh/rc-proxy-key.pem"; DRY_RUN=false
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

while [[ $# -gt 0 ]]; do
    case "$1" in
        --host) HOST="$2"; shift 2 ;;
        --sg-id) SG_ID="$2"; shift 2 ;;
        --relay-port) RELAY_PORT="$2"; shift 2 ;;
        --fetch-token) FETCH_TOKEN="$2"; shift 2 ;;
        --wecom-ips) WECOM_IPS="$2"; shift 2 ;;
        --wecom-token) WECOM_TOKEN="$2"; shift 2 ;;
        --wecom-aes-key) WECOM_AES_KEY="$2"; shift 2 ;;
        --agent-configs) AGENT_CONFIGS="$2"; shift 2 ;;
        --ssh-key) SSH_KEY="$2"; shift 2 ;;
        --region) REGION="$2"; shift 2 ;;
        --dry-run) DRY_RUN=true; shift ;;
        *) echo "Unknown arg: $1"; exit 2 ;;
    esac
done

for req in HOST SG_ID FETCH_TOKEN WECOM_IPS; do
    if [ -z "${!req}" ]; then
        echo "ERROR: --$(echo "$req" | tr '[:upper:]' '[:lower:]' | tr '_' '-') required"
        exit 2
    fi
done

# Per-agent WeCom creds: either single-agent (--wecom-token + --wecom-aes-key)
# or multi-agent (--agent-configs JSON). At least one path is required for the
# relay to verify callbacks.
if [ -z "$AGENT_CONFIGS" ] && { [ -z "$WECOM_TOKEN" ] || [ -z "$WECOM_AES_KEY" ]; }; then
    echo "ERROR: provide either --agent-configs JSON (multi-agent) or both"
    echo "       --wecom-token and --wecom-aes-key (single-agent)."
    exit 2
fi

# Run a command from its argv array — no eval, no word-splitting, injection-safe.
run() { if [ "$DRY_RUN" = true ]; then echo "  + $*"; else "$@"; fi; }

# Validate a CIDR is well-formed (IPv4/IPv6 + prefix) before it reaches AWS.
valid_cidr() {
    echo "$1" | grep -Eq '^([0-9]{1,3}\.){3}[0-9]{1,3}/[0-9]{1,2}$|^[0-9a-fA-F:]+/[0-9]{1,3}$'
}

echo "=== Self-Hosted Relay Setup ==="
echo "  Host: $HOST   SG: $SG_ID   Port: $RELAY_PORT   Region: $REGION"
echo ""

echo "[1/4] Opening relay port to WeCom IP ranges (not 0.0.0.0/0)..."
IFS=',' read -ra CIDRS <<< "$WECOM_IPS"
for cidr in "${CIDRS[@]}"; do
    cidr="$(echo "$cidr" | xargs)"
    if [ "$cidr" = "0.0.0.0/0" ] || [ "$cidr" = "::/0" ]; then
        echo "  REFUSED: will not open the relay port to $cidr"
        exit 1
    fi
    if ! valid_cidr "$cidr"; then
        echo "  REFUSED: '$cidr' is not a valid CIDR"
        exit 1
    fi
    echo "  Authorizing $cidr -> tcp/$RELAY_PORT"
    run aws --region "$REGION" --output text ec2 authorize-security-group-ingress \
        --group-id "$SG_ID" --protocol tcp --port "$RELAY_PORT" --cidr "$cidr" 2>/dev/null || true
done

echo "[2/4] Running SG audit gate..."
if [ "$DRY_RUN" = false ]; then
    "$SCRIPT_DIR/audit-sg.sh" --sg-id "$SG_ID" --relay-port "$RELAY_PORT" --region "$REGION"
else
    echo "  + (skipped in dry-run)"
fi

echo "[3/4] Deploying relay code + systemd unit to $HOST..."
# Only the non-secret port is substituted into the (world-readable) unit file.
# Secrets go into a 0600 EnvironmentFile written separately below.
SVC=$(sed -e "s|__RELAY_PORT__|$RELAY_PORT|" "$SCRIPT_DIR/templates/lobster-relay.service")
# Build the 0600 env file content (secrets). printf %q-safe via a heredoc on the host.
ENV_CONTENT="RELAY_FETCH_TOKEN=${FETCH_TOKEN}
WECOM_TOKEN=${WECOM_TOKEN}
WECOM_AES_KEY=${WECOM_AES_KEY}
AGENT_CONFIGS=${AGENT_CONFIGS}"
if [ "$DRY_RUN" = true ]; then
    echo "  + create lobster-relay user + dirs (/opt/lobster-relay, /var/lib/lobster-relay, /etc/lobster-relay)"
    echo "  + write 0600 /etc/lobster-relay/relay.env (RELAY_FETCH_TOKEN, WECOM_TOKEN, WECOM_AES_KEY, AGENT_CONFIGS)"
    echo "  + rsync relay code to $HOST:/opt/lobster-relay"
    echo "  + verify python deps (aiohttp, pycryptodome, defusedxml) present"
    echo "  + install systemd unit lobster-relay.service and enable --now"
else
    ssh -i "$SSH_KEY" "$HOST" "sudo useradd -r -s /usr/sbin/nologin lobster-relay 2>/dev/null || true; \
        sudo mkdir -p /opt/lobster-relay /var/lib/lobster-relay /etc/lobster-relay; \
        sudo chown lobster-relay:lobster-relay /var/lib/lobster-relay; \
        sudo chmod 700 /var/lib/lobster-relay"
    # Write the secret env file with 0600 perms owned by lobster-relay.
    printf '%s\n' "$ENV_CONTENT" | ssh -i "$SSH_KEY" "$HOST" \
        "sudo tee /etc/lobster-relay/relay.env > /dev/null && \
         sudo chown lobster-relay:lobster-relay /etc/lobster-relay/relay.env && \
         sudo chmod 600 /etc/lobster-relay/relay.env"
    rsync -az -e "ssh -i $SSH_KEY" \
        "$PROJECT_DIR/src/remote_control" "$HOST:/tmp/lobster-relay-src/"
    ssh -i "$SSH_KEY" "$HOST" "sudo rm -rf /opt/lobster-relay/remote_control; \
        sudo cp -r /tmp/lobster-relay-src/remote_control /opt/lobster-relay/"
    # Install deps; fail loudly if they can't be made present (avoids a silent crash-loop).
    ssh -i "$SSH_KEY" "$HOST" "sudo pip3 install --quiet aiohttp pycryptodome defusedxml || true; \
        python3 -c 'import aiohttp, Crypto.Cipher, defusedxml' || { \
            echo 'ERROR: relay Python deps (aiohttp/pycryptodome/defusedxml) missing on host'; exit 1; }"
    echo "$SVC" | ssh -i "$SSH_KEY" "$HOST" "sudo tee /etc/systemd/system/lobster-relay.service > /dev/null"
    ssh -i "$SSH_KEY" "$HOST" "sudo systemctl daemon-reload && sudo systemctl enable --now lobster-relay"
fi

echo "[4/4] Done."
echo ""
echo "  WeCom callback URL:  http://<elastic-ip>:$RELAY_PORT/callback/<agent_id>"
echo "  Local config.yaml:"
echo "    wecom:"
echo "      mode: \"relay\""
echo "      relay_url: \"http://<elastic-ip>:$RELAY_PORT\""
echo "      relay_token: \"$FETCH_TOKEN\""
