# AWS Fixed IP Proxy

Route outbound WeCom API calls through an EC2 instance with an Elastic IP, so you can register a fixed IP in the WeCom admin console.

## Architecture

```
┌──────────────────┐  SSH SOCKS5 tunnel  ┌──────────────────────┐  HTTPS  ┌──────────────┐
│  Deploy host     │─────────────────────►│  EC2 t3.micro        │────────►│  WeCom API   │
│  (remote_control)│                      │  Elastic IP: x.x.x.x│         │  qyapi...    │
└──────────────────┘                      └──────────────────────┘         └──────────────┘
```

All outbound HTTP requests from `WeComAPI` are routed through a SOCKS5 tunnel to an EC2 instance. WeCom sees the Elastic IP as the source.

## Prerequisites

- **AWS CLI v2** configured with credentials (`aws configure`)
- **autossh** on the deploy host (installed automatically by `deploy.sh` if missing)
- AWS permissions: EC2 full access (RunInstances, AllocateAddress, CreateSecurityGroup, etc.)

## Quick Start

### 1. Create the EC2 proxy

Run from any machine with AWS CLI:

```bash
# Recommended: unified setup (also deploys relay if not already done)
./scripts/setup.sh --proxy

# Or standalone proxy only:
./scripts/setup-proxy.sh
# Optional: --region us-west-2 --key-name my-key
```

This creates:
- t3.micro EC2 instance (free tier eligible)
- Elastic IP associated with the instance
- Security group allowing SSH
- SSH key saved to `~/.ssh/rc-proxy-key.pem`

The script is idempotent — re-running skips existing resources.

### 2. Register the IP in WeCom

Copy the Elastic IP from the setup output and add it as a trusted IP in:
**WeCom Admin Console → App Management → Your App → IP Whitelist**

### 3. Configure `config.yaml` on the deploy host

```yaml
wecom:
  proxy: "socks5://127.0.0.1:1080"
  # ... other wecom settings
```

### 4. Deploy with proxy

```bash
./deploy.sh user@host /remote/path \
    --proxy-ip <elastic-ip> \
    --proxy-key ~/.ssh/rc-proxy-key.pem
```

The deploy script will:
1. Sync code to the remote host
2. Copy the SSH key to the remote host
3. Install autossh if needed
4. Start the SOCKS5 tunnel on the remote host
5. Start the application

## Manual Tunnel Management

### Start tunnel manually on the deploy host

```bash
./scripts/start-proxy-tunnel.sh <elastic-ip> 1080 ~/.ssh/rc-proxy-key.pem
```

### Systemd service (Linux)

The proxy tunnel service is automatically generated and enabled by `deploy.sh`
when using `--proxy-ip`. It auto-starts on boot. Manual management:

```bash
sudo systemctl status rc-proxy-tunnel
sudo systemctl restart rc-proxy-tunnel
journalctl -u rc-proxy-tunnel -f
```

### Check tunnel status

```bash
# Is the systemd service running?
systemctl is-active rc-proxy-tunnel

# Is the SOCKS port listening?
ss -tlnp | grep :1080
```

## How It Works

1. `autossh` maintains a persistent SSH connection to the EC2 instance
2. The `-D 0.0.0.0:1080` flag creates a SOCKS5 proxy on the deploy host
3. `httpx.AsyncClient(proxy="socks5://127.0.0.1:1080")` routes all WeCom API calls through the tunnel
4. Traffic exits from the EC2 instance's Elastic IP
5. If the SSH connection drops, `autossh` reconnects automatically

## Cost

| Resource | Running | Stopped |
|----------|---------|---------|
| t3.micro EC2 | Free tier (12 months), then ~$8.50/mo | $0 |
| Elastic IP | Free (while instance running) | ~$3.65/mo |
| Data transfer | Negligible for API calls | $0 |

## Troubleshooting

**Tunnel won't start:**
- Check SSH connectivity: `ssh -i ~/.ssh/rc-proxy-key.pem ec2-user@<elastic-ip>`
- Check security group allows SSH from the deploy host's IP
- Check key permissions: `chmod 600 ~/.ssh/rc-proxy-key.pem`

**WeCom API still rejected:**
- Verify the Elastic IP is registered in WeCom admin console
- Check the proxy is configured in `config.yaml`
- Test: `curl --socks5 127.0.0.1:1080 https://qyapi.weixin.qq.com/cgi-bin/gettoken?corpid=TEST`

**Tunnel drops frequently:**
- Check EC2 instance status in AWS console
- Review `journalctl -u rc-proxy-tunnel` if using systemd
- Ensure `ServerAliveInterval` is set (already configured in the scripts)

## Cleanup

To remove all proxy resources:

```bash
aws ec2 terminate-instances --instance-ids <instance-id>
aws ec2 release-address --allocation-id <eip-alloc-id>
aws ec2 delete-security-group --group-id <sg-id>
aws ec2 delete-key-pair --key-name rc-proxy-key
rm ~/.ssh/rc-proxy-key.pem
```
