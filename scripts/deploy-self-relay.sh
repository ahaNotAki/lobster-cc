#!/usr/bin/env bash
#
# Orchestrate the full self-hosted-relay cutover, end to end.
#
# Reads ALL real credentials from the REMOTE host's config.yaml — this open-source
# package contains no secrets, and secrets are never passed on the command line
# (they would leak into `ps`). The orchestrator pulls corp_id/secret/token/
# encoding_aes_key/relay_token per agent off the remote config, derives the WeCom
# callback IP ranges via the getcallbackip API, provisions the relay's SG rule +
# systemd service, and (optionally) repoints the local server to the new relay.
#
# What it does, in order:
#   1. Read agents (+ relay_token, relay_url) from <remote>:<remote-dir>/config.yaml
#   2. Derive WeCom callback IP CIDRs via getcallbackip (using corp_id/secret)
#   3. Provision/refresh the relay: SG → WeCom IPs only, code + systemd on EC2
#      (secrets passed to setup-self-relay.sh via env, never argv)
#   4. Health-check the relay
#   5. Print the exact WeCom admin-console URL(s) to register (manual, one-time)
#   6. On --restart-local: restart the remote lobster-cc service so the poller
#      picks up the new relay_url/relay_token
#
# Usage:
#   ./scripts/deploy-self-relay.sh \
#       --host ec2-user@<deploy-host> --remote-dir /path/to/lobster-cc \
#       --relay-host ec2-user@<relay-elastic-ip> --sg-id sg-xxxx \
#       [--relay-port 8443] [--ssh-key ~/.ssh/rc-proxy-key.pem] \
#       [--region ap-southeast-1] [--restart-local] [--dry-run]
#
# Notes:
#   - --host / --remote-dir point at the box running lobster-cc (where config.yaml
#     lives). --relay-host points at the box that will run the relay. They are
#     often the SAME EC2 box (then pass the same SSH target for both).
#   - Registering the callback URL in the WeCom admin console cannot be automated
#     (no API); the script prints the exact URL(s) and waits for confirmation
#     before restarting the local server (unless --dry-run).
#
set -euo pipefail

REGION="${AWS_DEFAULT_REGION:-ap-southeast-1}"
HOST=""; REMOTE_DIR=""; RELAY_HOST=""; SG_ID=""; RELAY_PORT="8443"
SSH_KEY="$HOME/.ssh/rc-proxy-key.pem"; RESTART_LOCAL=false; DRY_RUN=false
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

while [[ $# -gt 0 ]]; do
    case "$1" in
        --host) HOST="$2"; shift 2 ;;
        --remote-dir) REMOTE_DIR="$2"; shift 2 ;;
        --relay-host) RELAY_HOST="$2"; shift 2 ;;
        --sg-id) SG_ID="$2"; shift 2 ;;
        --relay-port) RELAY_PORT="$2"; shift 2 ;;
        --ssh-key) SSH_KEY="$2"; shift 2 ;;
        --region) REGION="$2"; shift 2 ;;
        --restart-local) RESTART_LOCAL=true; shift ;;
        --dry-run) DRY_RUN=true; shift ;;
        *) echo "Unknown arg: $1"; exit 2 ;;
    esac
done

for req in HOST REMOTE_DIR RELAY_HOST SG_ID; do
    if [ -z "${!req}" ]; then
        echo "ERROR: --$(echo "$req" | tr '[:upper:]' '[:lower:]' | tr '_' '-') is required"
        exit 2
    fi
done

RELAY_IP="${RELAY_HOST##*@}"

echo "=== Self-Hosted Relay — Full Cutover ==="
echo "  lobster-cc host: $HOST:$REMOTE_DIR"
echo "  relay host:      $RELAY_HOST  (port $RELAY_PORT, sg $SG_ID)"
echo "  region:          $REGION"
echo ""

# ── 1. Read agents + relay_token from the REMOTE config.yaml ──────────────────
echo "[1/6] Reading agent config from $HOST:$REMOTE_DIR/config.yaml ..."
# Emit a small machine-readable bundle from the remote config. We parse it on the
# remote (its python has yaml) and print TSV lines we can read locally. Secrets
# stay on the wire over SSH only; nothing is written to disk locally.
CONFIG_TSV=$(ssh "$HOST" "python3 - <<'PY'
import yaml, json, sys
c = yaml.safe_load(open('$REMOTE_DIR/config.yaml'))
w = c.get('wecom')
agents = w if isinstance(w, list) else [w]
# First non-empty corp_id/secret pair is enough to call getcallbackip.
corp = secret = ''
relay_url = relay_token = ''
multi = {}
for a in agents:
    if not corp:
        corp, secret = a.get('corp_id',''), a.get('secret','')
    relay_url = relay_url or a.get('relay_url','')
    relay_token = relay_token or a.get('relay_token','')
    aid = str(a.get('agent_id',''))
    multi[aid] = {'token': a.get('token',''), 'aes_key': a.get('encoding_aes_key','')}
single = len(agents) == 1
print('CORP\t' + corp)
print('SECRET\t' + secret)
print('RELAY_URL\t' + relay_url)
print('RELAY_TOKEN\t' + relay_token)
print('SINGLE\t' + ('1' if single else '0'))
if single:
    a = agents[0]
    print('WTOKEN\t' + a.get('token',''))
    print('WAES\t' + a.get('encoding_aes_key',''))
    print('AGENT_ID\t' + str(a.get('agent_id','')))
else:
    print('AGENT_CONFIGS\t' + json.dumps(multi))
    print('AGENT_IDS\t' + ','.join(multi.keys()))
PY")

# Print everything after the first tab of the matching KEY line (preserves a
# value that itself contains tabs/spaces, e.g. JSON).
get_field() { printf '%s\n' "$CONFIG_TSV" | awk -F'\t' -v k="$1" '$1==k{print substr($0, index($0,"\t")+1); exit}'; }
CORP=$(get_field CORP)
SECRET=$(get_field SECRET)
RELAY_TOKEN=$(get_field RELAY_TOKEN)
SINGLE=$(get_field SINGLE)
EXISTING_RELAY_URL=$(get_field RELAY_URL)

if [ -z "$CORP" ] || [ -z "$SECRET" ]; then
    echo "  ERROR: could not read corp_id/secret from remote config.yaml"; exit 1
fi
if [ -z "$RELAY_TOKEN" ]; then
    echo "  ERROR: no relay_token in remote config.yaml. Add 'relay_token' to each"
    echo "         relay-mode agent first (the relay's Bearer secret), then re-run."
    exit 1
fi
if [ "$SINGLE" = "1" ]; then
    AGENT_IDS=$(get_field AGENT_ID)
    echo "  Single-agent config (agent $AGENT_IDS)."
else
    AGENT_IDS=$(get_field AGENT_IDS)
    echo "  Multi-agent config (agents: $AGENT_IDS)."
fi

# ── 2. Derive WeCom callback IP ranges (getcallbackip) ────────────────────────
echo "[2/6] Fetching WeCom callback IP ranges (getcallbackip)..."
WECOM_IPS=$(ssh "$HOST" "python3 - <<PY
import urllib.request, json
tok = json.load(urllib.request.urlopen(
    'https://qyapi.weixin.qq.com/cgi-bin/gettoken?corpid=$CORP&corpsecret=$SECRET'))['access_token']
data = json.load(urllib.request.urlopen(
    'https://qyapi.weixin.qq.com/cgi-bin/getcallbackip?access_token=' + tok))
ips = data.get('ip_list', [])
# getcallbackip returns bare IPs; express each as a /32 CIDR for the SG.
print(','.join(ip if '/' in ip else ip + '/32' for ip in ips))
PY")
if [ -z "$WECOM_IPS" ]; then
    echo "  ERROR: getcallbackip returned no IPs (check corp_id/secret/network)."; exit 1
fi
echo "  WeCom callback CIDRs: $WECOM_IPS"

# ── 3. Provision/refresh the relay (secrets via env, not argv) ────────────────
echo "[3/6] Deploying the relay to $RELAY_HOST ..."
RELAY_ENV=(
    "RELAY_FETCH_TOKEN=$RELAY_TOKEN"
)
RELAY_FLAGS=(--host "$RELAY_HOST" --sg-id "$SG_ID" --relay-port "$RELAY_PORT"
             --wecom-ips "$WECOM_IPS" --ssh-key "$SSH_KEY" --region "$REGION")
if [ "$SINGLE" = "1" ]; then
    RELAY_ENV+=("WECOM_TOKEN=$(get_field WTOKEN)" "WECOM_AES_KEY=$(get_field WAES)")
else
    RELAY_ENV+=("AGENT_CONFIGS=$(get_field AGENT_CONFIGS)")
fi
[ "$DRY_RUN" = true ] && RELAY_FLAGS+=(--dry-run)

# Pass secrets through the environment (env -i would drop AWS creds; we just
# prepend our vars), never on the command line.
env "${RELAY_ENV[@]}" "$SCRIPT_DIR/setup-self-relay.sh" "${RELAY_FLAGS[@]}"

# ── 4. Health-check the relay ─────────────────────────────────────────────────
echo "[4/6] Health-checking the relay..."
if [ "$DRY_RUN" = true ]; then
    echo "  + (dry-run) would curl http://127.0.0.1:$RELAY_PORT/health on $RELAY_HOST"
else
    sleep 2
    if ssh -i "$SSH_KEY" "$RELAY_HOST" "curl -sf --max-time 5 http://127.0.0.1:$RELAY_PORT/health >/dev/null"; then
        echo "  Relay /health OK."
    else
        echo "  ERROR: relay /health did not respond. Check: ssh $RELAY_HOST 'journalctl -u lobster-relay -n 50'"
        exit 1
    fi
fi

# ── 5. WeCom admin-console URL(s) to register (manual, one-time) ──────────────
echo "[5/6] Register these callback URL(s) in the WeCom admin console"
echo "       (应用管理 → your app → 接收消息 → 设置API接收):"
if [ "$SINGLE" = "1" ]; then
    echo "         http://$RELAY_IP:$RELAY_PORT/callback/$AGENT_IDS"
else
    IFS=',' read -ra _ids <<< "$AGENT_IDS"
    for aid in "${_ids[@]}"; do
        echo "         http://$RELAY_IP:$RELAY_PORT/callback/$aid"
    done
fi
echo "       (WeCom issues a GET verify on save; the relay is already running.)"
if [ -n "$EXISTING_RELAY_URL" ] && [ "$EXISTING_RELAY_URL" != "http://$RELAY_IP:$RELAY_PORT" ]; then
    echo ""
    echo "  NOTE: remote config.yaml still has relay_url=$EXISTING_RELAY_URL"
    echo "        Update it to http://$RELAY_IP:$RELAY_PORT before/with the restart below."
fi

# ── 6. Restart the local server (optional) ────────────────────────────────────
echo "[6/6] Local server restart..."
if [ "$RESTART_LOCAL" = false ]; then
    echo "  Skipped (no --restart-local). After registering the URL above and"
    echo "  setting relay_url in $HOST:$REMOTE_DIR/config.yaml, restart with:"
    echo "    ssh $HOST 'sudo systemctl restart lobster-cc'"
elif [ "$DRY_RUN" = true ]; then
    echo "  + (dry-run) would prompt to confirm WeCom URL registration, then"
    echo "    restart lobster-cc on $HOST"
else
    read -r -p "  Registered the WeCom URL(s) above? Restart lobster-cc now? [y/N] " _c
    case "$_c" in
        y|Y|yes|YES)
            ssh "$HOST" "sudo systemctl restart lobster-cc && sleep 2 && systemctl is-active lobster-cc"
            echo "  lobster-cc restarted."
            ;;
        *) echo "  Skipped restart. Restart manually when ready." ;;
    esac
fi

echo ""
echo "=== Done. ==="
echo "Next: send a WeCom message end-to-end to confirm. After verifying and"
echo "letting the old DynamoDB 7-day TTL drain, tear down the legacy AWS relay:"
echo "  ./scripts/setup-relay.sh --teardown"
echo "Do NOT click 'Request Verification Of Fix' in the AppSec ticket."
