# lobster-cc — Requirements Document

> A simplified OpenClaw-inspired system that uses 企业微信 (WeCom) as the remote control channel and local Claude Code CLI as the AI coding agent.

## 1. Problem Statement

Developers often need to trigger AI-assisted tasks while away from their development machine (e.g., from mobile, during meetings, or on the go). There is no convenient way to instruct a local Claude Code CLI instance to perform tasks remotely and receive feedback on results.

## 2. Goals

- Allow users to send task instructions via 企业微信 messages and have them executed by a local Claude Code CLI instance.
- Report task progress and results back to the user in 企业微信.
- Keep the system simple, self-hosted, and single-user focused.
- Make sure the context is shared across remote conversations for claude.
- Leverage claude code native memory and claude.md to ensure context and knowledge persistent.
- Handle long outputs gracefully (split messages, file uploads) instead of truncating.
- Support multiple WeCom agents in a single server instance.
- Provide easy deployment to remote machines via script.

## 3. Non-Goals (v1)

- ~~Web UI or dashboard.~~ (Implemented in v2: lobster dashboard with real-time monitoring)
- Support for messaging platforms other than 企业微信.
- Support for AI backends other than local Claude Code CLI.
- Complex workflow orchestration (DAGs, dependencies between tasks).

## 4. Architecture Overview

```
┌──────────────┐       ┌──────────────────┐       ┌─────────────────┐
│   User on    │       │   Remote Control  │       │  Claude Code    │
│   企业微信    │◄─────►│   Server (Python) │◄─────►│  CLI (local)    │
│  (Mobile/PC) │       │                   │       │                 │
└──────────────┘       └──────────────────┘       └─────────────────┘
      ▲                        │
      │                        ▼
      │                 ┌──────────────┐
      └─────────────────│  Task Queue  │
                        │  (SQLite)    │
                        └──────────────┘
```

### Components

1. **WeCom Bot Gateway** - Receives messages from 企业微信 API, sends replies back (text, markdown, file, image).
2. **Task Manager** - Manages task lifecycle (queued → running → completed/failed).
3. **Agent Runner** - Spawns and manages Claude Code CLI processes. Uses plain text output. Self-heals session mismatches.
4. **Message Formatter** - Smart long message handling: inline, split, or file upload.

## 5. Functional Requirements

### 5.1 WeCom Integration

| ID | Requirement |
|----|-------------|
| W1 | Support receiving text, image, voice, video, and file messages from 企业微信 application bot (应用消息). Media is downloaded and saved locally. |
| W2 | Support sending text, markdown, file, and image replies back to the user. |
| W3 | Verify 企业微信 callback signatures for security. |
| W4 | Support relay mode: WeCom pushes to an AWS Lambda relay, local server polls the relay. Also support direct callback mode via ngrok. |
| W5 | Support multiple WeCom agents in a single server instance, each with its own message source and executor. |

### 5.2 Task Management

| ID | Requirement |
|----|-------------|
| T1 | Each incoming message creates a task with a unique ID. |
| T2 | Tasks have states: `queued`, `running`, `completed`, `failed`, `cancelled`. |
| T3 | User can query task status by sending a status command (e.g., `/status` or `/status <task_id>`). |
| T4 | User can cancel a running task by sending `/cancel` or `/cancel <task_id>`. |
| T5 | Tasks are persisted in SQLite so they survive server restarts. |
| T6 | Only one task runs at a time per agent (sequential execution queue). |
| T7 | Task timeout: tasks that exceed a configurable time limit are automatically cancelled. |
| T8 | User can clear all task history via `/clear`. |

### 5.3 Claude Code CLI Integration

| ID | Requirement |
|----|-------------|
| C1 | Invoke Claude Code CLI via subprocess (`claude` command). |
| C2 | Pass the user's message as the prompt/instruction to Claude Code. |
| C3 | Specify working directory per session (configurable default, overridable via `/cd <path>`). |
| C4 | Stream Claude Code CLI stdout line-by-line using `--output-format stream-json`. Send real-time progress to user via `StreamHandler` (throttled at configurable interval, rate-limited to 25 msgs/min). |
| C5 | Send periodic progress updates to the user while the task is running (throttled by `progress_interval_seconds`). |
| C6 | Support passing `--allowedTools`, `--model`, `--permission-mode`, and other CLI flags via server configuration. |
| C7 | Respect Claude Code's exit code to determine task success/failure. |
| C8 | Self-heal session mismatches: auto-retry with opposite `--session-id`/`--resume` flag on session errors. |

### 5.4 Command Protocol

Users interact via natural language messages and slash commands:

| Command | Description |
|---------|-------------|
| `<any text>` | Create a new coding task with this instruction. |
| `/status` | Show status of the latest task. |
| `/status <id>` | Show status of a specific task. |
| `/cancel` | Cancel the currently running task. |
| `/cancel <id>` | Cancel a specific task. |
| `/list` | List recent tasks and their states. |
| `/new` | Start a new session (reset context). |
| `/cd <path>` | Change the default working directory. |
| `/cd` | Show the current working directory. |
| `/output <id>` | Get the full output of a completed task. |
| `/clear` | Clear all task history (except running tasks). |
| `/cron add <expr> <msg>` | Schedule a recurring task (5-field cron + message). |
| `/cron list` | List all scheduled tasks. |
| `/cron del <id>` | Delete a scheduled task. |
| `/cron pause <id>` | Pause a scheduled task. |
| `/cron resume <id>` | Resume a paused scheduled task. |
| `/help` | Show available commands. |

### 5.5 Agent Self-Configuration

| ID | Requirement |
|----|-------------|
| SC1 | Agents can read and modify their own behavior profile (`.agent-profile.yaml`) at runtime via MCP tools. |
| SC2 | Profile includes output style (language, format, length), notification preferences, model selection with task-type overrides, memory preferences, and custom commands. |
| SC3 | All profile changes are persisted with an audit trail (timestamped snapshots in `.agent-profile-history/`). |
| SC4 | Profile hot-reloads on file change (mtime-based) without server restart. |
| SC5 | Profiles are schema-enforced (Pydantic validation) — only known keys accepted. |
| SC6 | Full or per-key reset to defaults supported. |

### 5.6 Notifications & Output

| ID | Requirement |
|----|-------------|
| N1 | Notify user when a task starts running. |
| N2 | Send periodic progress updates for long-running tasks (throttled). |
| N3 | Notify user when a task completes, with the full response. |
| N4 | Notify user when a task fails, with error details. |
| N5 | Smart long message handling: short inline, medium split into multiple messages, very long uploaded as `.md` file. Falls back to truncated text on upload failure. |
| N6 | Stream real-time output while task is running (configurable interval, respects WeCom 30 msgs/min rate limit). |

## 6. Non-Functional Requirements

| ID | Requirement |
|----|-------------|
| NF1 | **Language**: Python 3.11+ |
| NF2 | **Dependencies**: Minimize external dependencies. Use `httpx` for HTTP, `sqlite3` (stdlib) for storage. |
| NF3 | **Deployment**: Run as a single long-running process. Deploy script for remote SSH machines. |
| NF4 | **Configuration**: YAML config file for WeCom credentials, working directory, CLI flags, timeouts, etc. Supports single dict or list for multi-agent. |
| NF5 | **Security**: WeCom callback verification; no task content logged to disk by default (configurable). |
| NF6 | **Reliability**: Graceful shutdown - finish or cancel running task on SIGTERM/SIGINT. Self-healing session management. Network error resilience in poll loop. |
| NF7 | **Logging**: Structured logging with configurable level. |
| NF8 | **Sleep prevention**: Automatic `caffeinate` on macOS to prevent system sleep (important for VPN connections). |

## 7. Configuration

```yaml
# config.yaml — single agent
wecom:
  name: "my-agent"
  corp_id: "your_corp_id"
  agent_id: 1000002
  secret: "your_agent_secret"
  token: "callback_verification_token"
  encoding_aes_key: "callback_encoding_aes_key"
  mode: "relay"  # "relay" (AWS Lambda relay) or "callback" (direct webhook via ngrok)
  relay_url: "https://<api-id>.execute-api.ap-southeast-1.amazonaws.com"
  relay_poll_interval_seconds: 5.0

# Multiple agents:
# wecom:
#   - name: "coding"
#     corp_id: "..."
#     agent_id: 1000002
#     ...
#   - name: "review"
#     corp_id: "..."
#     agent_id: 1000003
#     ...

agent:
  claude_command: "claude"
  default_working_dir: "/path/to/your/projects"
  allowed_tools: []  # empty = use claude defaults
  model: ""  # empty = use claude default
  permission_mode: ""  # e.g., "bypassPermissions", "acceptEdits"
  task_timeout_seconds: 600
  max_output_length: 4000

server:
  host: "0.0.0.0"
  port: 8080

storage:
  db_path: "./remote_control.db"

notifications:
  progress_interval_seconds: 30
  streaming_interval_seconds: 10.0
```

## 8. Tech Stack

| Component | Choice | Rationale |
|-----------|--------|-----------|
| Language | Python 3.11+ | Async support, rapid development |
| HTTP Framework | aiohttp | Async WeCom callback handling |
| HTTP Client | httpx | Async HTTP for WeCom API calls |
| Storage | SQLite | Zero-config, single-user, good enough |
| Process Mgmt | asyncio.subprocess | Non-blocking CLI execution |
| Config | PyYAML + Pydantic | Parsing + validation |
| WeCom Crypto | pycryptodome | AES decryption for callback messages |
| Cron | croniter | Cron expression parsing and next-run computation |

## 9. Milestones

### M1: Foundation ✅
- Project scaffolding (pyproject.toml, config loading, logging)
- SQLite task storage (create, update, query)
- Basic WeCom message receiving (callback verification + message parsing)
- Basic WeCom message sending (text reply)

### M2: Core Loop ✅
- Agent Runner: spawn Claude Code CLI, capture output
- Task queue: sequential execution
- Wire up end-to-end: WeCom message → task → Claude Code → reply

### M3: Polish ✅
- Slash commands (/status, /cancel, /list, /cd, /help, /output, /new, /clear)
- Progress notifications during long tasks
- Task timeout and cancellation
- Smart output handling (split/file upload)
- Graceful shutdown

### M4: Hardening ✅
- Error handling and retry logic for WeCom API
- WeCom callback signature verification
- Self-healing session management
- Network error resilience (VPN drops)
- Configuration validation
- Structured logging
- caffeinate integration (macOS)
- README and usage documentation

### M5: Multi-Agent & Deployment ✅
- Multi-agent support (multiple WeCom bots per server)
- File upload and image reply via WeCom API
- Deploy script for remote SSH machines

### M6: Streaming, Media & Scheduling ✅
- Streaming output: real-time progress via `StreamHandler` (line-by-line stdout, throttled sends)
- Media message support: receive images, voice, video, files from WeCom (download + save + pass to Claude)
- Cron scheduler: scheduled recurring tasks via `/cron` commands (croniter, 30s check interval)
- 198 tests across 12 test files

## 10. Open Questions (Resolved)

1. **Polling vs Callback**: Both supported. Relay (polling) is recommended. Callback requires ngrok.

2. **Multiple repos**: Per-session working directory via `/cd`. Default set in config.

3. **Authentication**: Any user in the WeCom app can interact.

4. **Session context**: Shared across chat session via Claude Code's `--resume`. Self-healing on mismatches.

5. **Git integration**: No Git — will do more than coding work.
