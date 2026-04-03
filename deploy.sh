#!/usr/bin/env bash
#
# Deploy remote_control to a remote SSH machine.
#
# Usage:
#   ./deploy.sh user@host [/remote/path] [--proxy-ip ELASTIC_IP --proxy-key /path/to/key.pem]
#
# - Syncs the project to the same directory structure on the remote machine
# - Creates the directory if it doesn't exist
# - Installs/updates dependencies in a virtualenv
# - Optionally ensures the SOCKS5 proxy tunnel is running for fixed outbound IP
# - Starts the application (kills any existing instance first)
# - Reports status back
#
# Default remote path: same as local project path
#
# Proxy flags (optional):
#   --proxy-ip    Elastic IP of the proxy EC2 instance
#   --proxy-key   Path to SSH key for the proxy EC2 (local path; copied to remote)
#   --proxy-port  Local SOCKS5 port on the remote host (default: 1080)
#
# If --proxy-ip is set, the script will:
#   1. Check/create the EC2 proxy infra (runs scripts/setup-proxy.sh locally)
#   2. Copy the SSH key to the remote deploy host
#   3. Ensure the proxy tunnel is running on the remote host
#

set -euo pipefail

# --- Parse arguments ---
LOCAL_DIR="$(cd "$(dirname "$0")" && pwd)"
SSH_TARGET=""
REMOTE_DIR=""
PROXY_IP=""
PROXY_KEY=""
PROXY_PORT="1080"

while [[ $# -gt 0 ]]; do
    case "$1" in
        --proxy-ip)   PROXY_IP="$2"; shift 2 ;;
        --proxy-key)  PROXY_KEY="$2"; shift 2 ;;
        --proxy-port) PROXY_PORT="$2"; shift 2 ;;
        -*)           echo "Unknown flag: $1"; exit 1 ;;
        *)
            if [ -z "$SSH_TARGET" ]; then
                SSH_TARGET="$1"
            elif [ -z "$REMOTE_DIR" ]; then
                REMOTE_DIR="$1"
            else
                echo "Unexpected arg: $1"; exit 1
            fi
            shift
            ;;
    esac
done

SSH_TARGET="${SSH_TARGET:?Usage: ./deploy.sh user@host [/remote/path] [--proxy-ip IP --proxy-key KEY]}"
REMOTE_DIR="${REMOTE_DIR:-$LOCAL_DIR}"
PID_FILE="$REMOTE_DIR/.remote_control.pid"
LOG_FILE="$REMOTE_DIR/remote_control.log"
TUNNEL_PID_FILE="$REMOTE_DIR/.proxy_tunnel.pid"

# Count total steps
TOTAL_STEPS=5
if [ -n "$PROXY_IP" ]; then
    TOTAL_STEPS=6
fi
STEP=0
next_step() { STEP=$((STEP + 1)); echo "[$STEP/$TOTAL_STEPS] $1"; }

echo "=== Deploy remote_control ==="
echo "  Local:  $LOCAL_DIR"
echo "  Remote: $SSH_TARGET:$REMOTE_DIR"
if [ -n "$PROXY_IP" ]; then
    echo "  Proxy:  $PROXY_IP (SOCKS5 port $PROXY_PORT)"
fi
echo ""

# --- Create remote directory ---
next_step "Creating remote directory..."
ssh "$SSH_TARGET" "mkdir -p '$REMOTE_DIR'"

# --- Sync files ---
next_step "Syncing files..."
rsync -avz --delete \
    --exclude '.venv/' \
    --exclude '__pycache__/' \
    --exclude '*.pyc' \
    --exclude '.pytest_cache/' \
    --exclude '*.egg-info/' \
    --exclude '*.db' \
    --exclude '*.db-wal' \
    --exclude '*.db-shm' \
    --exclude '.git/' \
    --exclude '.remote_control.pid' \
    --exclude '.proxy_tunnel.pid' \
    --exclude 'remote_control.log' \
    --exclude 'config.yaml' \
    --exclude '.dashboard-workstations.json' \
    --exclude '.dashboard-tabs.json' \
    --exclude '.agent-profile.yaml' \
    --exclude '.agent-profile-history/' \
    "$LOCAL_DIR/" "$SSH_TARGET:$REMOTE_DIR/"

# --- Seed per-agent config files (never overwrite existing) ---
ssh "$SSH_TARGET" bash -s <<SEED_SCRIPT
    # Get all working dirs from config
    WORKING_DIRS=\$(python3 -c "
import yaml
c = yaml.safe_load(open('$REMOTE_DIR/config.yaml'))
dirs = set()
dirs.add(c.get('agent',{}).get('default_working_dir',''))
for w in c.get('wecom',[]) if isinstance(c.get('wecom'), list) else [c.get('wecom',{})]:
    wd = w.get('working_dir','')
    if wd: dirs.add(wd)
for d in dirs:
    if d: print(d)
" 2>/dev/null)

    for WD in \$WORKING_DIRS; do
        [ -d "\$WD" ] || mkdir -p "\$WD"

        # Seed .system-prompt.md from example template
        if [ ! -f "\$WD/.system-prompt.md" ] && [ -f "$REMOTE_DIR/scripts/templates/system-prompt-example.md" ]; then
            cp "$REMOTE_DIR/scripts/templates/system-prompt-example.md" "\$WD/.system-prompt.md"
            echo "  Seeded .system-prompt.md to \$WD/"
        fi

        # Seed CLAUDE.md (only if not present — never overwrite agent's manual)
        if [ ! -f "\$WD/CLAUDE.md" ] && [ -f "$REMOTE_DIR/scripts/templates/claude-md-agent.md" ]; then
            cp "$REMOTE_DIR/scripts/templates/claude-md-agent.md" "\$WD/CLAUDE.md"
            echo "  Seeded CLAUDE.md to \$WD/"
        fi

        # Seed .agent-profile.default.yaml (always update — these are factory defaults)
        if [ -f "$REMOTE_DIR/scripts/templates/.agent-profile.default.yaml" ]; then
            cp "$REMOTE_DIR/scripts/templates/.agent-profile.default.yaml" "\$WD/.agent-profile.default.yaml"
        fi

        # Ensure .schedules/ directory exists
        [ -d "\$WD/.schedules" ] || mkdir -p "\$WD/.schedules"
    done
SEED_SCRIPT

# --- Install dependencies ---
next_step "Installing dependencies on remote..."
ssh "$SSH_TARGET" bash -s <<REMOTE_SCRIPT
    cd '$REMOTE_DIR'
    if [ ! -d .venv ]; then
        echo "  Creating virtualenv..."
        python3 -m venv .venv
    fi
    source .venv/bin/activate
    echo "  Installing package..."
    pip install -e '.[dev]' --quiet
REMOTE_SCRIPT

# --- Proxy setup (optional) ---
if [ -n "$PROXY_IP" ]; then
    # Determine proxy key path
    if [ -z "$PROXY_KEY" ]; then
        PROXY_KEY="$HOME/.ssh/rc-proxy-key.pem"
    fi
    if [ ! -f "$PROXY_KEY" ]; then
        echo ""
        echo "  Proxy SSH key not found at $PROXY_KEY"
        echo "  Run scripts/setup-proxy.sh first, or pass --proxy-key /path/to/key.pem"
        exit 1
    fi

    next_step "Setting up proxy tunnel on remote host..."

    # Copy SSH key to remote host
    ssh "$SSH_TARGET" "mkdir -p ~/.ssh && chmod 700 ~/.ssh"
    scp -q "$PROXY_KEY" "$SSH_TARGET:~/.ssh/rc-proxy-key.pem"
    ssh "$SSH_TARGET" "chmod 600 ~/.ssh/rc-proxy-key.pem"
    echo "  SSH key copied to remote host."

    # Ensure autossh is available on remote
    ssh "$SSH_TARGET" bash -s <<'REMOTE_SCRIPT'
    if ! command -v autossh &>/dev/null; then
        echo "  Installing autossh on remote..."
        if command -v apt-get &>/dev/null; then
            sudo apt-get install -y autossh -qq
        elif command -v yum &>/dev/null; then
            sudo yum install -y autossh -q
        elif command -v brew &>/dev/null; then
            brew install autossh
        else
            echo "  ERROR: Cannot install autossh. Please install manually."
            exit 1
        fi
    fi
    echo "  autossh: $(command -v autossh)"
REMOTE_SCRIPT

fi

# --- Extract remote username ---
REMOTE_USER="${SSH_TARGET%%@*}"

# --- Install and start systemd services ---
next_step "Installing and starting services..."

# Transition: clean up old nohup processes (from pre-systemd deploys)
ssh "$SSH_TARGET" bash -s <<REMOTE_SCRIPT
    if [ -f '$PID_FILE' ]; then
        OLD_PID=\$(cat '$PID_FILE')
        if kill -0 "\$OLD_PID" 2>/dev/null; then
            echo "  Stopping old nohup instance (pid=\$OLD_PID)..."
            kill "\$OLD_PID" 2>/dev/null || true
            sleep 2
            kill -9 "\$OLD_PID" 2>/dev/null || true
        fi
        rm -f '$PID_FILE'
    fi
    if [ -f '$TUNNEL_PID_FILE' ]; then
        OLD_PID=\$(cat '$TUNNEL_PID_FILE')
        if kill -0 "\$OLD_PID" 2>/dev/null; then
            echo "  Stopping old nohup tunnel (pid=\$OLD_PID)..."
            kill "\$OLD_PID" 2>/dev/null || true
        fi
        rm -f '$TUNNEL_PID_FILE'
    fi
    ORPHANS=\$(pgrep -f 'claude.*-p.*--output-format' 2>/dev/null || true)
    if [ -n "\$ORPHANS" ]; then
        echo "  Killing orphaned claude processes: \$ORPHANS"
        echo "\$ORPHANS" | xargs kill 2>/dev/null || true
        sleep 2
        echo "\$ORPHANS" | xargs kill -9 2>/dev/null || true
    fi
REMOTE_SCRIPT

# Generate proxy tunnel service (if proxy configured)
if [ -n "$PROXY_IP" ]; then
    ssh "$SSH_TARGET" bash -s <<REMOTE_SCRIPT
    # Accept the EC2 host key upfront
    ssh-keyscan -H '$PROXY_IP' >> ~/.ssh/known_hosts 2>/dev/null || true
    AUTOSSH_BIN=\$(command -v autossh)
    sudo tee /etc/systemd/system/rc-proxy-tunnel.service > /dev/null <<EOF
[Unit]
Description=Lobster CC — WeCom SOCKS5 proxy tunnel
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$REMOTE_USER
Environment=AUTOSSH_GATETIME=0
ExecStart=\$AUTOSSH_BIN -M 0 -N -D 0.0.0.0:$PROXY_PORT -o ServerAliveInterval=30 -o ServerAliveCountMax=3 -o ExitOnForwardFailure=yes -i /home/$REMOTE_USER/.ssh/rc-proxy-key.pem ec2-user@$PROXY_IP
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
    echo "  Installed rc-proxy-tunnel.service"
REMOTE_SCRIPT
    LOBSTER_AFTER="After=network-online.target rc-proxy-tunnel.service"
    LOBSTER_WANTS="Wants=network-online.target rc-proxy-tunnel.service"
else
    LOBSTER_AFTER="After=network-online.target"
    LOBSTER_WANTS="Wants=network-online.target"
fi

# Generate lobster-cc service
ssh "$SSH_TARGET" bash -s <<REMOTE_SCRIPT
    sudo tee /etc/systemd/system/lobster-cc.service > /dev/null <<EOF
[Unit]
Description=Lobster CC — Remote Control for Claude Code
$LOBSTER_AFTER
$LOBSTER_WANTS

[Service]
Type=simple
User=$REMOTE_USER
WorkingDirectory=$REMOTE_DIR
ExecStart=/bin/bash -c 'exec $REMOTE_DIR/.venv/bin/python -m remote_control.main -c config.yaml >> $LOG_FILE 2>&1'
Restart=always
RestartSec=5
Environment=HOME=/home/$REMOTE_USER

[Install]
WantedBy=multi-user.target
EOF
    echo "  Installed lobster-cc.service"
REMOTE_SCRIPT

# Reload and start services
ssh "$SSH_TARGET" bash -s <<REMOTE_SCRIPT
    sudo systemctl daemon-reload

    if [ -f /etc/systemd/system/rc-proxy-tunnel.service ]; then
        sudo systemctl enable rc-proxy-tunnel
        sudo systemctl restart rc-proxy-tunnel
        echo "  rc-proxy-tunnel service started."
    fi

    sudo systemctl enable lobster-cc
    sudo systemctl restart lobster-cc
    sleep 2

    if systemctl is-active --quiet lobster-cc; then
        echo "  lobster-cc service is running."
    else
        echo "  ERROR: lobster-cc failed to start. Recent logs:"
        journalctl -u lobster-cc --no-pager -n 10 2>/dev/null || tail -10 '$LOG_FILE'
        exit 1
    fi
REMOTE_SCRIPT

# --- Report status ---
echo ""
next_step "Status check..."
ssh "$SSH_TARGET" bash -s <<REMOTE_SCRIPT
    echo "  lobster-cc:"
    STATUS=\$(systemctl is-active lobster-cc 2>/dev/null || echo "inactive")
    PID=\$(systemctl show lobster-cc --property=MainPID 2>/dev/null | cut -d= -f2 || echo "?")
    echo "    Status:  \$STATUS (pid=\$PID)"
    echo "    Log:     $LOG_FILE"
    echo "    Config:  $REMOTE_DIR/config.yaml"
    echo ""
    echo "  Last 5 log lines:"
    tail -5 '$LOG_FILE' 2>/dev/null | sed 's/^/    /' || echo "    (no logs yet)"
REMOTE_SCRIPT

if [ -n "$PROXY_IP" ]; then
    ssh "$SSH_TARGET" bash -s <<REMOTE_SCRIPT
    TSTATUS=\$(systemctl is-active rc-proxy-tunnel 2>/dev/null || echo "inactive")
    TPID=\$(systemctl show rc-proxy-tunnel --property=MainPID 2>/dev/null | cut -d= -f2 || echo "?")
    echo "  Proxy:   socks5://127.0.0.1:$PROXY_PORT (\$TSTATUS, pid=\$TPID, via $PROXY_IP)"
REMOTE_SCRIPT
fi

echo ""
echo "=== Deploy complete ==="
echo ""
echo "Useful commands:"
echo "  View logs:    ssh $SSH_TARGET 'tail -f $LOG_FILE'"
echo "  journalctl:   ssh $SSH_TARGET 'journalctl -u lobster-cc -f'"
echo "  Restart:      ssh $SSH_TARGET 'sudo systemctl restart lobster-cc'"
echo "  Stop:         ssh $SSH_TARGET 'sudo systemctl stop lobster-cc'"
echo "  Status:       ssh $SSH_TARGET 'systemctl status lobster-cc'"
if [ -n "$PROXY_IP" ]; then
    echo "  Tunnel logs:  ssh $SSH_TARGET 'journalctl -u rc-proxy-tunnel -f'"
    echo "  Stop tunnel:  ssh $SSH_TARGET 'sudo systemctl stop rc-proxy-tunnel'"
fi
