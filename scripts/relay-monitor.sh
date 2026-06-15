#!/usr/bin/env bash
#
# Health-check the self-hosted relay and alert (stdout + exit code) on problems.
# Intended to run via cron every minute. Wire the alert lines to a WeCom-sending
# hook (e.g. pipe non-zero exits to the lobster wecom MCP / a notify script).
#
# Usage:
#   ./scripts/relay-monitor.sh --url http://127.0.0.1:8443 \
#       [--max-queue 100] [--max-rejected 600]
#
# Exit codes: 0 healthy, 1 unreachable, 2 threshold breached.
#
set -euo pipefail

URL="http://127.0.0.1:8443"
MAX_QUEUE=100
MAX_REJECTED=600   # cumulative rejected_count ceiling; tune per deployment

while [[ $# -gt 0 ]]; do
    case "$1" in
        --url) URL="$2"; shift 2 ;;
        --max-queue) MAX_QUEUE="$2"; shift 2 ;;
        --max-rejected) MAX_REJECTED="$2"; shift 2 ;;
        *) echo "Unknown arg: $1"; exit 2 ;;
    esac
done

RESP=$(curl -sf --max-time 5 "$URL/health" 2>/dev/null) || {
    echo "ALERT: relay unreachable at $URL/health"
    exit 1
}

QD=$(echo "$RESP" | python3 -c "import sys,json; print(json.load(sys.stdin).get('queue_depth',0))")
RJ=$(echo "$RESP" | python3 -c "import sys,json; print(json.load(sys.stdin).get('rejected_count',0))")

if [ "$QD" -gt "$MAX_QUEUE" ]; then
    echo "ALERT: relay queue depth $QD exceeds $MAX_QUEUE (local poller may be down)"
    exit 2
fi
if [ "$RJ" -gt "$MAX_REJECTED" ]; then
    echo "ALERT: relay rejected_count $RJ exceeds $MAX_REJECTED (possible attack / config drift)"
    exit 2
fi
echo "OK: queue_depth=$QD rejected_count=$RJ"
