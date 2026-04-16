#!/usr/bin/env bash
# run-scheduled-task.sh — Execute a scheduled task defined in .schedules/<name>.yaml
#
# Usage:
#   run-scheduled-task.sh <task-name> [--working-dir /path] [--claude /path/to/claude]
#
# Reads .schedules/<task-name>.yaml from the working directory, extracts the
# prompt and config, then runs claude -p with the appropriate flags.
#
# The yaml file should have: name, schedule, enabled, timeout, prompt
# Optional: user_id (for {user_id} substitution), allowed_tools (list), add_dirs (list)
#
# This script is meant to be called from crontab:
#   30 0 * * 1-5 /path/run-scheduled-task.sh stock-morning-brief --working-dir /path/lobster1

set -euo pipefail

TASK_NAME="${1:-}"
WORKING_DIR="."
CLAUDE_CMD=""
NOTIFY_FAIL_SCRIPT=""

shift 2>/dev/null || true
while [[ $# -gt 0 ]]; do
    case "$1" in
        --working-dir) WORKING_DIR="$2"; shift 2 ;;
        --claude) CLAUDE_CMD="$2"; shift 2 ;;
        --notify-fail) NOTIFY_FAIL_SCRIPT="$2"; shift 2 ;;
        *) echo "Unknown option: $1"; exit 1 ;;
    esac
done

if [[ -z "$TASK_NAME" ]]; then
    echo "Usage: $0 <task-name> [--working-dir /path]"
    exit 1
fi

YAML_FILE="$WORKING_DIR/.schedules/${TASK_NAME}.yaml"
if [[ ! -f "$YAML_FILE" ]]; then
    echo "Schedule file not found: $YAML_FILE"
    exit 1
fi

# Parse yaml with python (available on all our hosts)
eval "$(python3 -c "
import yaml, sys, shlex
with open('$YAML_FILE') as f:
    d = yaml.safe_load(f)
print(f'TASK_ENABLED={int(d.get(\"enabled\", True))}')
print(f'TASK_TIMEOUT={d.get(\"timeout\", 600)}')
print(f'TASK_PROMPT={shlex.quote(d.get(\"prompt\", \"\"))}')
print(f'TASK_USER_ID={shlex.quote(d.get(\"user_id\", \"\"))}')
tools = d.get('allowed_tools', [])
if tools:
    print(f'TASK_TOOLS={shlex.quote(\",\".join(tools))}')
else:
    print('TASK_TOOLS=')
add_dirs = d.get('add_dirs', [])
if add_dirs:
    print(f'TASK_ADD_DIRS={shlex.quote(\" \".join(add_dirs))}')
else:
    print('TASK_ADD_DIRS=')
")"

if [[ "$TASK_ENABLED" != "1" ]]; then
    echo "Task '$TASK_NAME' is disabled, skipping."
    exit 0
fi

# Resolve claude command
if [[ -z "$CLAUDE_CMD" ]]; then
    CLAUDE_CMD=$(command -v claude 2>/dev/null || echo "")
    if [[ -z "$CLAUDE_CMD" ]]; then
        echo "claude not found in PATH"
        exit 1
    fi
fi

# Substitute variables in prompt
DATE=$(date +%Y-%m-%d)
PROMPT=$(echo "$TASK_PROMPT" | sed "s/{date}/$DATE/g" | sed "s/{user_id}/$TASK_USER_ID/g")

# Build command
CMD=(timeout "$TASK_TIMEOUT" env -u CLAUDECODE "$CLAUDE_CMD")
CMD+=(--add-dir "$WORKING_DIR")
CMD+=(-p)

if [[ -n "$TASK_TOOLS" ]]; then
    CMD+=(--allowedTools "$TASK_TOOLS")
fi

CMD+=(--dangerously-skip-permissions)
CMD+=(--)
CMD+=("$PROMPT")

# Execute
LOG_DIR="${HOME}/.claude/logs"
mkdir -p "$LOG_DIR"
LOG_FILE="$LOG_DIR/${TASK_NAME}.log"

# Notify the Remote Control server that a cron task is starting
# This makes the task visible on the dashboard and in the task list
RC_PORT="${RC_PORT:-8080}"
TASK_ID=""
if command -v curl &>/dev/null; then
    TASK_ID=$(curl -sf --max-time 5 -X POST "http://127.0.0.1:${RC_PORT}/api/cron/start" \
        -H "Content-Type: application/json" \
        -d "{\"name\": \"${TASK_NAME}\", \"working_dir\": \"${WORKING_DIR}\"}" \
        2>/dev/null | python3 -c "import sys,json;print(json.load(sys.stdin).get('task_id',''))" 2>/dev/null) || true
fi

echo "[$(date)] Running scheduled task: $TASK_NAME (task_id=${TASK_ID:-none})" >> "$LOG_FILE"

# Ensure /api/cron/finish is ALWAYS called, even if claude crashes/times out/gets killed.
# Without this trap, set -e would cause the script to exit before reaching the finish curl.
_finish_cron() {
    local code="${1:-1}"
    if [[ -n "$TASK_ID" ]]; then
        curl -sf --max-time 5 -X POST "http://127.0.0.1:${RC_PORT}/api/cron/finish" \
            -H "Content-Type: application/json" \
            -d "{\"task_id\": \"${TASK_ID}\", \"exit_code\": ${code}}" \
            2>/dev/null || true
    fi
}
trap '_finish_cron 1' EXIT

# cd to working dir so claude picks up the correct per-agent .mcp.json
cd "$WORKING_DIR"

# Run with retry: if first attempt fails, wait and retry once
MAX_ATTEMPTS=2
RETRY_DELAY=30

for attempt in $(seq 1 $MAX_ATTEMPTS); do
    "${CMD[@]}" >> "$LOG_FILE" 2>&1
    EXIT_CODE=$?

    if [[ $EXIT_CODE -eq 0 ]]; then
        break
    fi

    if [[ $attempt -lt $MAX_ATTEMPTS ]]; then
        echo "[$(date)] Attempt $attempt failed (exit code $EXIT_CODE), retrying in ${RETRY_DELAY}s..." >> "$LOG_FILE"
        sleep "$RETRY_DELAY"
    fi
done

# Normal exit: call finish with actual exit code, then disable the trap
_finish_cron "$EXIT_CODE"
trap - EXIT

if [[ $EXIT_CODE -ne 0 ]]; then
    echo "[$(date)] Task $TASK_NAME failed after $MAX_ATTEMPTS attempts (exit code $EXIT_CODE)" >> "$LOG_FILE"
    if [[ -n "$NOTIFY_FAIL_SCRIPT" && -x "$NOTIFY_FAIL_SCRIPT" ]]; then
        "$NOTIFY_FAIL_SCRIPT" "$TASK_NAME" "$LOG_FILE" >> "$LOG_FILE" 2>&1 || true
    fi
fi

exit $EXIT_CODE
