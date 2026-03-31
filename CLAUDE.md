# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Rules
- Update related docs after every change.
- Run all tests to ensure pass after every change.
- For every code change, ask yourself "is this change make the architecture worse or better?", if worse, find a way to become better.

## Project Overview

Remote Control: a self-hosted system that lets you control a local Claude Code CLI instance via 企业微信 (WeCom) messages. Send task instructions from your phone, get results back in chat. Context is shared across messages using Claude Code's native `--session-id` mechanism.

See `README.md` for user-facing setup guide, `REQUIREMENTS.md` for full requirements, and `DESIGN.md` for technical design.

## Build & Run

```bash
# Install dependencies
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

# Generate config interactively (validates WeCom credentials)
lobster init

# Run the server (relay mode — polls AWS Lambda relay, no tunnel needed)
lobster -c config.yaml
# Or: python -m remote_control.main -c config.yaml

# Test (all)
python -m pytest tests/ -v

# Test (single file)
python -m pytest tests/test_store.py -v

# Lint
ruff check src/ tests/

# Deploy to remote machine
./deploy.sh user@host [/remote/path]

# Deploy with fixed outbound IP proxy
./scripts/setup.sh --proxy                  # one-time EC2 + Elastic IP setup
./deploy.sh user@host /path --proxy-ip <elastic-ip> --proxy-key ~/.ssh/rc-proxy-key.pem
```

## Architecture

```
WeCom → [AWS relay] → aiohttp server → Command Router → Executor → Claude Code CLI
                                           ↕                          ↕
                                       Notifier ←──── text output ────┘
                                           ↕
                                     WeCom API (reply / file upload)
```

**Key modules** (`src/remote_control/`):

- `wecom/message_source.py` — `MessageSource` abstraction with `CallbackSource` (webhook) and `RelayPollingSource` (poll relay service) implementations
- `wecom/gateway.py` — HTTP callback handler: signature verification, message decryption, dispatch. Supports text, image, voice, video, and file message types.
- `wecom/crypto.py` — WeCom AES-CBC encryption/decryption protocol
- `wecom/api.py` — WeCom API client: access token management, message/file/image sending, media upload/download. Auto-splits long messages via `_send_chunks()` with byte-level splitting, 0.5s inter-chunk delay, and retry-on-error. Supports optional SOCKS5 proxy (`wecom.proxy`).
- `core/router.py` — Slash command parsing (`/status`, `/cancel`, `/new`, `/clear`, `/memory`, etc.) vs task creation. Supports custom slash commands from agent profile. Scheduling is handled by Claude Code's scheduler MCP plugin (natural language).
- `core/executor.py` — Task queue orchestration: picks queued tasks, runs them sequentially via AgentRunner, with streaming output. Injects memory context, WeCom MCP tool hints, profile-aware output style hints, and output format guidelines before each task. Supports per-task model override from profile. Captures thinking for dashboard. Sends queue confirmation when tasks are waiting.
- `core/runner.py` — Spawns `claude -p --output-format stream-json --verbose --include-partial-messages --resume <session_id>`. Supports per-task `model_override` parameter. Parses thinking blocks, text deltas, model info from stream events.
- `core/memory.py` — Memory utilities (`extract_keywords`, `build_context_block`, `clean_message`) for task history recall via keyword matching. Supports CJK characters.
- `core/store.py` — SQLite persistence with `Store` (shared) and `ScopedStore` (per-agent). Tasks, sessions, memories isolated by `agent_id`.
- `core/notifier.py` — Sends notifications to WeCom (only on failure). Streaming output via `StreamHandler` buffers and sends at throttled intervals (respects WeCom 30 msgs/min rate limit). `_send_text_smart` delegates to `send_text` (byte-level split with retry). Supports sending images and files.
- `core/watchdog.py` — `ProcessWatchdog` safety net: tracks spawned claude processes by PID, kills any exceeding `watchdog_timeout_seconds` (default 20 min), updates task status and notifies user.
- `core/profile.py` — `AgentProfile` Pydantic model and `ProfileManager` with hot-reload, audit trail, and per-agent preferences (output style, model selection, memory prefs, custom commands)
- `core/models.py` — `Task`, `Session`, `CronJob`, and `Memory` dataclasses
- `mcp/wecom_server.py` — Standalone MCP server exposing WeCom message/image/file sending as tools for Claude Code
- `mcp/profile_server.py` — Standalone MCP server exposing agent self-configuration tools (`get_agent_config`, `set_agent_config`, `list_agent_config`, `reset_agent_config`)
- `config.py` — Pydantic-validated YAML config loading (supports single or multiple WeCom agents)
- `dashboard/routes.py` — Read-only dashboard web UI with password auth, IP lockout, task detail API (`/api/task/{task_id}`), tab data API (`/api/tab/{agent_id}/{tab_id}`)
- `dashboard/status.py` — Agent status assembly, workstation config loading, schedule config loading (prompt + working_dir from `.schedules/*.yaml`), cron parsing, tab config loading
- `dashboard/tabs.py` — Custom tab config loading (`.dashboard-tabs.json`) and tab data loading with path security. Supports data (table/key-value), chart (line/bar), and HTML tab types.
- `dashboard/static/dashboard.html` — Lobster aquarium WebUI with multi-agent support, expandable task details, custom tabs with table/chart/HTML renderers
- `server.py` — aiohttp app wiring (routes, per-agent component setup, lifecycle hooks)
- `main.py` — CLI entry point (subcommands: `init` for interactive config generation, default starts server)
- `cli_init.py` — Interactive `config.yaml` generator with WeCom credential validation

**Multi-agent support**: Config `wecom` can be a single dict or a list. Each agent gets its own `WeComAPI`, `MessageSource`, `Executor`, `CommandRouter`, and `ScopedStore`. They share a single `Store` (SQLite DB) with `agent_id` isolation. Per-agent `working_dir` override supported. Routes are namespaced by agent_id (e.g., `/wecom/callback/{agent_id}`, `/relay/status/{agent_id}`). Dashboard shows all agents with separate lobsters and status panels.

**Message source modes** (`wecom.mode` in config):
- `relay` (recommended) — WeCom pushes raw callbacks to an AWS Lambda relay (API Gateway + DynamoDB). Local server polls the relay and decrypts messages using `crypto.py`. No public URL needed locally. See `relay/README.md` for infrastructure details.
- `callback` — WeCom pushes messages directly to `/wecom/callback/{agent_id}` endpoint. Requires public URL (e.g., ngrok).

**Outbound proxy**: Optional SOCKS5 proxy for fixed outbound IP (WeCom IP whitelist). Configure `wecom.proxy: "socks5://127.0.0.1:1080"` and use `deploy.sh --proxy-ip` to auto-manage the tunnel. See `docs/aws-proxy.md`.

**Session context**: Each user gets a persistent Claude Code session ID stored in SQLite. Messages use `--session-id <id>` (with automatic `--resume` retry on session mismatch) so Claude Code maintains conversation history. `/new` resets to a fresh session.

**Persistent memory**: Dual-layer design. Long-term knowledge lives in Claude Code's native `MEMORY.md` (auto-read by all Claude processes including cron tasks). Task history is stored in SQLite — raw summaries are auto-saved after each successful task, and keyword-matched history is prepended to each task message for contextual recall. Claude is prompted to self-manage MEMORY.md when it learns something of lasting value. `/memory` commands let users view stats, show knowledge, or clear memory.

**WeCom MCP Server**: A standalone MCP server (`mcp/wecom_server.py`) exposes `send_wecom_message`, `send_wecom_image`, and `send_wecom_file` tools. The server auto-generates `.mcp.json` in the working directory on startup so all Claude processes (including scheduler-spawned ones) can send messages back to WeCom. Every task message includes a hint with the user's ID and instructions to use these tools for scheduled tasks and on-demand file sending. See `docs/wecom-mcp.md` for installation guide.

**Agent Profile System**: Per-agent preferences stored in `.agent-profile.yaml` in the working directory, managed by `ProfileManager` with hot-reload (mtime check) and audit trail (timestamped snapshots in `.agent-profile-history/`). Profiles control output style (language, format, max length), memory prefs (keyword/recent limits), model selection (per-task-type regex overrides), notification prefs, and custom slash commands. Claude can self-configure via MCP tools (`get_agent_config`, `set_agent_config`, `list_agent_config`, `reset_agent_config`) exposed by `mcp/profile_server.py`. The profile is automatically bootstrapped on first access, extracting initial prefs from existing `.system-prompt.md` if present. All profile fields are backward compatible — if profile_manager is None, components fall back to config.yaml defaults.
