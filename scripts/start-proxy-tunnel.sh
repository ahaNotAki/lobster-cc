#!/usr/bin/env bash
#
# Start a persistent SOCKS5 proxy tunnel to an EC2 instance.
# Used to route WeCom API calls through a fixed Elastic IP.
#
# Prerequisites:
#   - autossh (brew install autossh / apt install autossh)
#   - SSH key with access to the EC2 instance
#
# Usage:
#   ./start-proxy-tunnel.sh <ec2-elastic-ip> [socks-port] [ssh-key-path]
#
# The tunnel runs in the foreground. Use systemd or launchd to daemonize.
#

set -euo pipefail

PROXY_HOST="${1:?Usage: $0 <ec2-elastic-ip> [socks-port] [ssh-key-path]}"
SOCKS_PORT="${2:-1080}"
SSH_KEY="${3:-$HOME/.ssh/rc-proxy-key.pem}"

# Check prerequisites
if ! command -v autossh &>/dev/null; then
    echo "ERROR: autossh not found."
    echo "  macOS:  brew install autossh"
    echo "  Linux:  sudo apt install autossh  (or yum install autossh)"
    exit 1
fi

if [ ! -f "$SSH_KEY" ]; then
    echo "ERROR: SSH key not found: $SSH_KEY"
    exit 1
fi

echo "Starting SOCKS5 proxy tunnel..."
echo "  Proxy host: $PROXY_HOST"
echo "  SOCKS port: localhost:$SOCKS_PORT"
echo "  SSH key:    $SSH_KEY"
echo ""
echo "Configure in config.yaml:"
echo "  wecom:"
echo "    proxy: \"socks5://127.0.0.1:$SOCKS_PORT\""
echo ""

# AUTOSSH_GATETIME=0: don't exit if first connection fails quickly (useful on boot)
export AUTOSSH_GATETIME=0

exec autossh -M 0 -N \
    -D "0.0.0.0:$SOCKS_PORT" \
    -o "ServerAliveInterval 30" \
    -o "ServerAliveCountMax 3" \
    -o "StrictHostKeyChecking accept-new" \
    -o "ExitOnForwardFailure yes" \
    -i "$SSH_KEY" \
    "ec2-user@$PROXY_HOST"
