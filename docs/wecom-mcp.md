# WeCom MCP Server

An MCP (Model Context Protocol) server that lets Claude Code send messages, images, and files to users via WeCom (企业微信).

## Why

When Claude Code runs as a scheduled task (via `claude-code-scheduler` or OS cron), it runs outside the Remote Control executor pipeline. Without this MCP server, Claude has no way to send results back to the user. With it, Claude can call `send_wecom_message` to deliver results directly.

This also enables Claude to send files on demand — if a user asks "send me output.png", Claude can use `send_wecom_file` or `send_wecom_image` to deliver it.

## Installation

### Automatic (via Remote Control server)

The Remote Control server automatically generates `.mcp.json` in the working directory on startup. No manual setup needed if you're running the server. The `.mcp.json` includes two MCP servers:

- **`wecom`** — message, image, and file sending tools (this server)
- **`agent-profile`** — agent self-configuration tools (`get_agent_config`, `set_agent_config`, `list_agent_config`, `reset_agent_config`) for tuning output style, model selection, notifications, and custom commands via `.agent-profile.yaml`

Both are registered automatically so all Claude processes (including scheduler-spawned ones) have access.

### Manual

Add to your project's `.mcp.json` (or `~/.claude/mcp.json` for global access):

```json
{
  "mcpServers": {
    "wecom": {
      "command": "python",
      "args": ["-m", "remote_control.mcp.wecom_server"],
      "env": {
        "WECOM_CORP_ID": "<your_corp_id>",
        "WECOM_AGENT_ID": "<your_agent_id>",
        "WECOM_SECRET": "<your_agent_secret>",
        "WECOM_PROXY": ""
      }
    }
  }
}
```

**Prerequisites:**
- The `remote-control` package must be installed (`pip install -e .` from the repo root)
- The `python` in `command` must be the one with `remote-control` installed (use absolute path if needed, e.g., `/path/to/.venv/bin/python`)

## Available Tools

| Tool | Parameters | Description |
|------|-----------|-------------|
| `send_wecom_message` | `user_id`, `content` | Send a text message to a WeCom user |
| `send_wecom_image` | `user_id`, `file_path` | Upload and send an image (png, jpg, gif, etc.) |
| `send_wecom_file` | `user_id`, `file_path` | Upload and send any file (pdf, csv, zip, etc.) |

## Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `WECOM_CORP_ID` | Yes | WeCom enterprise corp ID |
| `WECOM_AGENT_ID` | Yes | WeCom application agent ID |
| `WECOM_SECRET` | Yes | WeCom application secret |
| `WECOM_PROXY` | No | SOCKS5 proxy URL (e.g., `socks5://127.0.0.1:1080`) |

## Usage Examples

Once installed, Claude Code can use the tools directly:

```
User: run the tests and send me the results
Claude: [runs tests, then calls send_wecom_message(user_id="user1", content="All 234 tests passed.")]

User: generate a chart of sales data and send it to me
Claude: [creates chart.png, then calls send_wecom_image(user_id="user1", file_path="/path/to/chart.png")]

User: send me the report.pdf
Claude: [calls send_wecom_file(user_id="user1", file_path="/path/to/report.pdf")]
```

For scheduled tasks, the prompt should include instructions to send results back:
```
"Every day at 9am, run the tests and send results to user_id='YourUserID' using send_wecom_message."
```

## Troubleshooting

**"WECOM_CORP_ID must be set"** — Check that `.mcp.json` has the correct env vars. If auto-generated, restart the Remote Control server.

**"Failed to get access token"** — Verify WECOM_SECRET is correct. Check network connectivity (proxy if needed).

**"WeCom send failed"** — Check the error code in the response. Common: 60020 (invalid user_id), 41004 (invalid media_id).

**MCP server not found** — Ensure `remote-control` is installed in the Python environment referenced by `.mcp.json`. Use `python -m remote_control.mcp.wecom_server` to test manually.
