#!/usr/bin/env bash
#
# Pre-deploy security gate: fail if the relay port or SOCKS port 1080 is
# exposed to 0.0.0.0/0 in the given security group.
#
# Usage:
#   ./scripts/audit-sg.sh --sg-id sg-xxxx [--relay-port 8443] [--region ap-southeast-1]
#
set -euo pipefail

REGION="${AWS_DEFAULT_REGION:-ap-southeast-1}"
SG_ID=""
RELAY_PORT="8443"
SOCKS_PORT="1080"

while [[ $# -gt 0 ]]; do
    case "$1" in
        --sg-id)      SG_ID="$2"; shift 2 ;;
        --relay-port) RELAY_PORT="$2"; shift 2 ;;
        --region)     REGION="$2"; shift 2 ;;
        *) echo "Unknown arg: $1"; exit 2 ;;
    esac
done

[ -z "$SG_ID" ] && { echo "ERROR: --sg-id required"; exit 2; }

PERMS=$(aws --region "$REGION" ec2 describe-security-groups \
    --group-ids "$SG_ID" \
    --query "SecurityGroups[0].IpPermissions" --output json)

FAIL=0
check_port() {
    local port="$1" label="$2"
    local open
    open=$(echo "$PERMS" | PORT="$port" python3 -c "
import sys, json, os
perms = json.load(sys.stdin)
port = int(os.environ['PORT'])
bad = False
for p in perms:
    fr, to = p.get('FromPort'), p.get('ToPort')
    if fr is None or to is None:
        continue
    if fr <= port <= to:
        for r in p.get('IpRanges', []):
            if r.get('CidrIp') == '0.0.0.0/0':
                bad = True
print('OPEN' if bad else 'OK')
")
    if [ "$open" = "OPEN" ]; then
        echo "  FAIL: $label (port $port) is open to 0.0.0.0/0"
        FAIL=1
    else
        echo "  OK:   $label (port $port) is not open to 0.0.0.0/0"
    fi
}

echo "=== Security Group Audit ($SG_ID) ==="
check_port "$RELAY_PORT" "relay callback port"
check_port "$SOCKS_PORT" "SOCKS proxy port"

if [ "$FAIL" -ne 0 ]; then
    echo ""
    echo "AUDIT FAILED — refusing to proceed. Restrict the offending port to WeCom IP ranges."
    exit 1
fi
echo "Audit passed."
