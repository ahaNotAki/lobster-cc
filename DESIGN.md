# lobster-cc — Technical Design

## 0. Message Receiving: Two Modes

WeCom does **not** provide a polling/pull API for 应用消息. We support two pluggable modes via the `MessageSource` abstraction:

### Mode 1: Relay (recommended)

WeCom pushes callbacks to an AWS Lambda relay. The relay stores raw encrypted messages in DynamoDB. The local server polls the relay and decrypts locally. No public URL needed on the local machine.

```
┌──────────┐  callback  ┌──────────────────────┐  poll  ┌──────────────────┐
│  WeCom   │───────────►│  AWS Lambda + APIGW  │◄──────│  Remote Control  │
│  Server  │            │  + DynamoDB          │──────►│  Server (local)  │
└──────────┘            └──────────────────────┘       └──────────────────┘
```

Configure via `config.yaml`: `wecom.mode: "relay"`, `wecom.relay_url`, and `wecom.relay_poll_interval_seconds`.

Replies are sent **directly** from the local server to WeCom API (`qyapi.weixin.qq.com`) — no relay needed for outbound messages. Optionally routed through a SOCKS5 proxy for fixed outbound IP (see below).

### Mode 2: Callback (alternative)

Push-based via WeCom webhook directly to local server. Requires a public URL (e.g., ngrok).

```
┌──────────┐    HTTPS     ┌─────────────────┐   localhost   ┌──────────────────┐
│  WeCom   │─────────────►│  ngrok / tunnel │─────────────► │  Remote Control  │
│  Server  │◄─────────────│                 │◄───────────── │  Server (local)  │
└──────────┘              └─────────────────┘               └──────────────────┘
```

Configure via `config.yaml`: `wecom.mode: "callback"`. Requires `ngrok http 8080` or similar tunnel running.

### Outbound Proxy (fixed IP for WeCom API)

WeCom may require a fixed IP whitelist for API calls. Since the deploy host's IP can change, we route outbound WeCom traffic through an EC2 instance with an Elastic IP via a SOCKS5 tunnel:

```
┌──────────────────┐  SSH SOCKS5 tunnel  ┌──────────────────────┐  HTTPS  ┌──────────────┐
│  Deploy host     │─────────────────────►│  EC2 t3.micro        │────────►│  WeCom API   │
│  (remote_control)│                      │  Elastic IP: x.x.x.x│         │  qyapi...    │
└──────────────────┘                      └──────────────────────┘         └──────────────┘
```

- `autossh` maintains a persistent SOCKS5 tunnel from the deploy host to EC2
- `httpx.AsyncClient(proxy="socks5://...")` routes all `WeComAPI` requests through the tunnel
- Configure via `config.yaml`: `wecom.proxy: "socks5://127.0.0.1:1080"`
- The `deploy.sh` script can automatically set up and manage the tunnel with `--proxy-ip`
- See `docs/aws-proxy.md` for full setup guide

---

## 1. System Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                    Remote Control Server                         │
│                                                                 │
│  ┌─────────────┐   ┌──────────────┐   ┌─────────────────────┐  │
│  │  Message     │   │   Command    │   │   Agent Runner      │  │
│  │  Source      │──►│   Router     │──►│                     │  │
│  │             │   │              │   │  claude -p --resume  │  │
│  │ - callback  │   │ - /cmd parse │   │  <session_id>       │  │
│  │ - relay     │   │ - task text  │   │  "user prompt"      │  │
│  │             │   │              │   │                     │  │
│  └─────────────┘   └──────────────┘   └──────┬──────────────┘  │
│        ▲                                      │                 │
│        │           ┌──────────────┐           │                 │
│        └───────────│  Notifier    │◄──────────┘                 │
│                    │              │                              │
│                    │ - streaming  │     ┌──────────────┐        │
│                    │ - errors     │     │  Task Store  │        │
│                    │ - files      │     │  (SQLite)    │        │
│                    └──────┬───────┘     └──────────────┘        │
│                           │                                     │
│                    ┌──────▼───────┐                              │
│                    │  Dashboard   │                              │
│                    │  WebUI       │                              │
│                    │ /dashboard   │                              │
│                    └──────────────┘                              │
└─────────────────────────────────────────────────────────────────┘
```

### Multi-Agent Support

The server supports multiple WeCom agents simultaneously. Each agent gets its own:
- `MessageSource` (callback or relay poller)
- `WeComAPI` client (separate access tokens)
- `Executor` (independent task queue)
- `Notifier`
- `ScopedStore` (agent-scoped wrapper around the shared Store)

All agents share a single `Store` (SQLite database), but per-agent operations (tasks, sessions, memories) are isolated via `ScopedStore`, which auto-filters by `agent_id` on all queries. Global operations (kv, cron, task status updates) delegate to the shared store. Routes are namespaced by `agent_id`:
- Callback: `/wecom/callback/{agent_id}`
- Relay status: `/relay/status/{agent_id}`

Each agent can have its own `working_dir` override (via `wecom.working_dir` in config), falling back to `agent.default_working_dir`.

Config can be either a single dict (backwards compatible) or a list of agents.

### Components

| Component | Responsibility |
|-----------|---------------|
| **Message Source** | Pluggable adapter for receiving messages. `CallbackSource` (WeCom webhook via ngrok) or `RelayPollingSource` (polls AWS Lambda relay). See Section 14. |
| **WeCom Gateway** | HTTP callback handler for verification, decryption, and dispatch. Used internally by `CallbackSource`. |
| **Command Router** | Parses incoming messages: slash commands (`/status`, `/cancel`, `/clear`, `/memory`, etc.) are handled immediately; everything else becomes a task. Scheduling is handled by Claude Code's `scheduler` MCP plugin via natural language. |
| **Agent Runner** | Manages Claude Code CLI subprocess. Uses `--output-format stream-json` for structured streaming. Parses `system/init`, `assistant`, and `result` events to extract thinking blocks, token usage, model info, and cost. Self-heals session mismatches by retrying with opposite `--session-id`/`--resume` flag. |
| **Notifier** | Sends notifications to WeCom on task failure. Prepends task labels (first line of user message) to all notifications. Auto-splits text and markdown that exceed WeCom's 2048-byte limit. `StreamHandler` sends buffered output at throttled intervals with dashboard reference for real-time streaming. |
| **Task Store** | SQLite-backed persistence for tasks, sessions, memories, and key-value pairs (e.g., relay cursor). `ScopedStore` wrapper provides per-agent isolation via `agent_id` column. |
| **Dashboard** | Password-protected read-only web UI at `/dashboard`. Shows real-time agent state (idle/working/done), streaming output, thinking, model info, token usage, recent tasks, cron jobs, and configurable workstations. Multi-agent aware. |
| **Memory (Task History)** | SQLite-backed task history with keyword matching for contextual recall. Long-term knowledge is managed by Claude via native `MEMORY.md` files. |
| **WeCom MCP Server** | Standalone stdio-based MCP server exposing `send_wecom_message`, `send_wecom_image`, `send_wecom_file` tools. Auto-configured via `.mcp.json` so all Claude processes (including scheduler-spawned) can send WeCom messages. See Section 15. |
| **Agent Profile MCP Server** | Standalone stdio-based MCP server exposing `get_agent_config`, `set_agent_config`, `list_agent_config`, `reset_agent_config` tools. Enables agents to self-configure output style, model selection, notifications, and custom commands. See Section 17. |

---

## 2. Session Context Design

**Goal**: Messages share context across a conversation, leveraging Claude Code's native session mechanism.

### How It Works

Claude Code CLI supports `--session-id <uuid>` (create new) and `--resume <session_id>` (continue existing) to maintain conversation continuity. We use this to keep a persistent session per user.

```
Message 1: "read the README and summarize"
  → claude -p --output-format text --session-id abc-123 "read the README and summarize"

Message 2: "now add a section about installation"
  → claude -p --output-format text --resume abc-123 "now add a section about installation"
  (Claude remembers the README context from message 1)
```

### Session Lifecycle

| Event | Action |
|-------|--------|
| First message from user | Generate a UUID, store as `session_id` for that `(user_id, agent_id)` pair. Run with `--session-id <uuid>`. Mark `initialized=false`. |
| Successful first run | Mark `initialized=true`. |
| Subsequent messages | Run with `--resume <session_id>`. Claude Code loads prior conversation context. |
| Session mismatch error | Automatically retry with opposite flag (`--session-id` ↔ `--resume`). |
| User sends `/new` | Generate a fresh UUID, replace `session_id`, reset `initialized=false`. Starts a clean session. |
| Server restart | Load `session_id` and `initialized` from SQLite. Claude Code sessions are persisted on disk by Claude Code itself. |

### Self-Healing Session Retry

The `AgentRunner.run()` method detects session errors ("no conversation found", "already in use") and automatically retries with the opposite flag. This makes the system resilient to:
- DB state drift (e.g., session deleted from Claude Code's storage)
- Fresh DB with existing CLI sessions
- Any mismatch between our `initialized` flag and CLI reality

### Storage Schema

Sessions are scoped by `(user_id, agent_id)` so each user gets a separate session per WeCom agent:

```sql
CREATE TABLE sessions (
    user_id       TEXT NOT NULL,
    agent_id      TEXT NOT NULL DEFAULT '',
    session_id    TEXT NOT NULL,
    working_dir   TEXT NOT NULL,
    created_at    TEXT NOT NULL,
    last_used_at  TEXT NOT NULL,
    initialized   INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (user_id, agent_id)
);
```

### Context + Claude Code Native Memory

Claude Code has built-in memory (`~/.claude/` auto-memory) and reads `CLAUDE.md` from the working directory. Our system benefits from both:

- **Session context**: Maintained via `--resume` (conversation history)
- **Persistent knowledge**: Claude Code's auto-memory writes learnings to `~/.claude/projects/...`
- **Project rules**: `CLAUDE.md` in the working directory provides project-specific guidance

### Persistent Memory System

A dual-layer memory design that separates long-term knowledge from task history recall:

**Layer 1 — Long-term knowledge (Claude-managed)**: Claude Code's native `MEMORY.md` files (`~/.claude/projects/.../memory/MEMORY.md`) store persistent knowledge. All Claude processes — executor tasks, cron jobs, manual CLI — share the same MEMORY.md. The executor's prompt hint instructs Claude to update MEMORY.md when it learns something of lasting value during a task. This follows Anthropic's recommended "agentic memory" pattern where the model itself decides what to remember.

**Layer 2 — Task history (SQLite-backed)**: After each successful task, a raw memory entry is saved in the `memories` table with the task message, truncated output, and extracted keyword tags. Before each task, keyword-matched history entries are prepended to the user's message as a `<context>` block. This provides contextual recall that Claude's native memory cannot do (e.g., "what was the result of the stock analysis yesterday?").

**Retrieval**: Recency-based (last N entries) + keyword matching via SQL `LIKE` with relevance scoring (sum of tag matches). No embedding model required.

**Slash commands**: `/memory` (stats), `/memory show` (view knowledge), `/memory clear` (reset).

---

## 3. Agent Runner Design

### CLI Invocation

```python
cmd = [
    config.agent.claude_command,  # "claude"
    "-p",                         # print mode (non-interactive)
    "--output-format", "stream-json",  # structured streaming output
    "--verbose",
    "--include-partial-messages",
    "--dangerously-skip-permissions",
]

# Session handling (with auto-retry on mismatch)
if session.initialized:
    cmd += ["--resume", session_id]
else:
    cmd += ["--session-id", session_id]

# Optional config-driven flags
if config.agent.model:
    cmd += ["--model", config.agent.model]
if config.agent.allowed_tools:
    cmd += ["--allowedTools"] + config.agent.allowed_tools

# "--" separates flags from the positional prompt argument.
# Without it, variadic flags like --allowedTools swallow the prompt.
cmd += ["--", user_message]
```

### Output Handling (stream-json)

Using `--output-format stream-json`, Claude Code streams JSON events line by line. The runner parses each event by type:

| Event Type | Subtype | Data Extracted |
|-----------|---------|----------------|
| `system` | `init` | `model`, `session_id`, `claude_code_version`, `mcp_servers` |
| `assistant` | — | Cumulative `content` blocks: `text` (output) and `thinking` (reasoning). Deltas are computed by tracking cumulative lengths. Token usage (`input_tokens`, `output_tokens`, `cache_read_input_tokens`). |
| `result` | — | Final `result` text, total cost, turn count, duration, full usage stats, model context window, max output tokens. |

The runner maintains a `model_info` dict (shared with the dashboard) that accumulates metadata across events. Two callbacks are supported:
- `on_output(text)` — invoked with text output deltas for `StreamHandler`
- `on_thinking(text)` — invoked with thinking deltas for dashboard display

Stderr is read concurrently via `asyncio.gather()` to avoid buffer deadlocks.

### Process Management

```python
class AgentRunner:
    _process: asyncio.subprocess.Process | None

    async def run(self, message, session_id, is_resume, working_dir,
                  on_output=None, on_thinking=None, task_id="") -> RunResult:
        """Run with automatic retry on session mismatch."""
        result = await self._run_once(message, session_id, is_resume, working_dir)
        if result.exit_code != 0 and self._is_session_error(result.error):
            result = await self._run_once(message, session_id, not is_resume, working_dir)
        return result

    async def cancel(self):
        """Send SIGTERM to gracefully stop Claude Code."""
        if self._process:
            self._process.terminate()
            await asyncio.wait_for(self._process.wait(), timeout=10)
```

### Timeout

An `asyncio.wait_for` wraps the entire run with `config.agent.task_timeout_seconds`. On timeout, the process is terminated and the task marked `failed` with a timeout reason.

### Process Watchdog

A `ProcessWatchdog` acts as a safety net alongside the primary `asyncio.wait_for` timeout. It tracks all spawned `claude -p` processes by PID and start time, running periodic checks (configurable via `agent.watchdog_interval_seconds`, default 60s). Any process exceeding `agent.watchdog_timeout_seconds` (default 1200s / 20 min) is killed via SIGTERM → SIGKILL, the task is marked `failed`, and the user is notified via WeCom. This catches processes that survive event loop stalls or missed cancellations.

---

## 4. WeCom Gateway Design

### Callback Verification (GET)

When WeCom verifies the callback URL, it sends a GET request:

```
GET /wecom/callback/{agent_id}?msg_signature=xxx&timestamp=xxx&nonce=xxx&echostr=xxx
```

We must:
1. Verify the signature using `SHA1(sort(token, timestamp, nonce, echostr))`.
2. Decrypt `echostr` using AES (CBC mode, PKCS#7 padding) with `EncodingAESKey`.
3. Return the decrypted string as plain text.

### Message Receiving (POST)

```
POST /wecom/callback/{agent_id}?msg_signature=xxx&timestamp=xxx&nonce=xxx
Body: <xml><Encrypt>...</Encrypt></xml>
```

Flow:
1. Verify signature.
2. Decrypt the `<Encrypt>` field → XML with `FromUserName`, `Content`, `MsgType`, `AgentID`, etc.
3. **AgentID filtering** (multi-agent relay): The outer XML includes an `AgentID` field. In relay mode, where a single relay receives callbacks for all agents, `RelayPollingSource._dispatch_message()` checks the `AgentID` and silently skips messages intended for other agents. This prevents duplicate processing in multi-agent setups.
4. Parse message type — supported types: `text`, `image`, `voice`, `video`, `file`.
5. For media messages: download via WeCom media API, save to `_media/` dir, prepend file path to prompt.
6. Pass to Command Router.
7. Return empty `"success"` response immediately (WeCom requires response within 5 seconds).

### Media Message Handling

WeCom sends non-text messages (image, voice, video, file) with a `MediaId`. The server downloads the media content via `GET /cgi-bin/media/get?media_id=...`, saves it to `{working_dir}/_media/{filename}`, and prepends the file path to the user's message so Claude Code can reference it:

```
[User sent an image: /project/_media/image_abc123.jpg]
```

Unsupported message types (e.g., `location`, `link`) are silently ignored.

### Message Sending

```
POST https://qyapi.weixin.qq.com/cgi-bin/message/send?access_token=TOKEN

{
    "touser": "user_id",
    "msgtype": "markdown",  // or "text", "file", "image"
    "agentid": 1000002,
    "markdown": {
        "content": "**Task completed** ✓\n> Files modified: 3\n\nSummary: ..."
    }
}
```

### File Upload & Sending

The WeCom API client supports uploading temporary media and sending files/images:

```python
# Upload and send a file
media_id = await api.upload_media("file", "/path/to/output.md")
await api.send_file(user_id, media_id)

# Or in one step:
await api.upload_and_send_file(user_id, "/path/to/output.md", "result.md")
await api.upload_and_send_image(user_id, "/path/to/chart.png")
```

### Access Token Management

```python
class TokenManager:
    """Cache access_token, refresh before expiry."""
    token: str | None
    expires_at: float  # time.monotonic()

    async def get_token(self) -> str:
        if self.token and time.monotonic() < self.expires_at - 300:  # 5min buffer
            return self.token
        self.token, expires_in = await self._fetch_token()
        self.expires_at = time.monotonic() + expires_in
        return self.token
```

---

## 5. Command Router Design

```python
COMMANDS = {
    "/status":  handle_status,   # Show latest or specific task status
    "/cancel":  handle_cancel,   # Cancel running task
    "/list":    handle_list,     # List recent tasks
    "/new":     handle_new,      # Start a new session (reset context)
    "/cd":      handle_cd,       # Change working directory
    "/output":  handle_output,   # Get full output of a task
    "/clear":   handle_clear,    # Clear all task history
    "/memory":  handle_memory,   # Memory stats/show/clear
    "/restart": handle_restart,  # Kill claude process, reset session, reload MCP servers
    "/help":    handle_help,     # Show available commands
}

async def route(user_id: str, message: str):
    parts = message.strip().split(maxsplit=1)
    cmd = parts[0].lower()

    if cmd in COMMANDS:
        arg = parts[1] if len(parts) > 1 else None
        return await COMMANDS[cmd](user_id, arg)

    # Not a command → create a task
    return await create_and_enqueue_task(user_id, message)
```

Commands are handled **synchronously** (instant reply). Tasks are **enqueued** and processed by the Agent Runner.

---

## 6. Task Store Design

### Schema

```sql
CREATE TABLE tasks (
    id          TEXT PRIMARY KEY,       -- UUID
    user_id     TEXT NOT NULL,
    agent_id    TEXT NOT NULL DEFAULT '',  -- scoped per WeCom agent
    session_id  TEXT NOT NULL,
    message     TEXT NOT NULL,          -- Original user message
    status      TEXT NOT NULL DEFAULT 'queued',  -- queued|running|completed|failed|cancelled
    output      TEXT,                   -- Full agent output
    summary     TEXT,                   -- Truncated summary for WeCom
    error       TEXT,                   -- Error message if failed
    created_at  TEXT NOT NULL,
    started_at  TEXT,
    finished_at TEXT
);
CREATE INDEX idx_tasks_agent ON tasks(agent_id, status, created_at);

CREATE TABLE sessions (
    user_id       TEXT NOT NULL,
    agent_id      TEXT NOT NULL DEFAULT '',
    session_id    TEXT NOT NULL,
    working_dir   TEXT NOT NULL,
    created_at    TEXT NOT NULL,
    last_used_at  TEXT NOT NULL,
    initialized   INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (user_id, agent_id)
);

CREATE TABLE kv (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE cron_jobs (
    id          TEXT PRIMARY KEY,
    user_id     TEXT NOT NULL,
    schedule    TEXT NOT NULL,       -- cron expression, e.g. "0 9 * * *"
    message     TEXT NOT NULL,       -- task message to enqueue when triggered
    enabled     INTEGER NOT NULL DEFAULT 1,
    created_at  TEXT NOT NULL,
    last_run_at TEXT,
    next_run_at TEXT NOT NULL
);
```

The `kv` table stores persistent key-value pairs, currently used for relay cursor persistence (`relay_cursor_{agent_id}`).

The `memories` table stores per-user, per-agent memory entries:

```sql
CREATE TABLE IF NOT EXISTS memories (
    id               TEXT PRIMARY KEY,
    user_id          TEXT NOT NULL,
    agent_id         TEXT NOT NULL DEFAULT '',  -- scoped per WeCom agent
    type             TEXT NOT NULL,      -- 'raw' or 'consolidated'
    source_task      TEXT DEFAULT '',
    content          TEXT NOT NULL,
    tags             TEXT DEFAULT '',    -- comma-separated keywords
    category         TEXT DEFAULT '',    -- facts/decisions/preferences/project_state
    created_at       TEXT NOT NULL,
    consolidated_at  TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_memories_user_type
    ON memories(user_id, type, consolidated_at);
```

Raw entries are auto-created after successful tasks. Keyword matching uses `LIKE` with relevance scoring via summed `CASE` expressions.

### ScopedStore Pattern

The `ScopedStore` class wraps the shared `Store` and automatically filters all task, session, and memory operations by `agent_id`. This avoids requiring every caller to pass `agent_id` explicitly:

```python
class ScopedStore:
    def __init__(self, store: Store, agent_id: str):
        self._store = store
        self._agent_id = agent_id

    # Scoped: create_task, get_latest_task, get_running_task, list_tasks, ...
    # Scoped: get_or_create_session, reset_session, mark_session_initialized, ...
    # Scoped: create_memory, get_recent_memories, get_keyword_matched_memories, ...
    # Delegated: get_kv, set_kv, list_all_cron_jobs, update_task_status, ...
```

Each agent's `Executor`, `CommandRouter`, and `Notifier` receive a `ScopedStore` instead of the raw `Store`.

### Schema Migration

Existing databases are auto-migrated on startup. The `_migrate()` method:
1. Adds `initialized` column to `sessions` if missing.
2. Adds `agent_id` column to `tasks` and `memories` if missing (with index).
3. Recreates `sessions` table with composite `PRIMARY KEY (user_id, agent_id)` (SQLite cannot alter PKs, so the table is recreated with data migration).

### Task Lifecycle

```
[User sends message]
        │
        ▼
    ┌────────┐     Agent Runner picks up
    │ queued  │────────────────────────────► ┌─────────┐
    └────────┘                               │ running │
                                             └────┬────┘
                              ┌───────────────────┼───────────────────┐
                              ▼                   ▼                   ▼
                        ┌───────────┐      ┌──────────┐      ┌───────────┐
                        │ completed │      │  failed  │      │ cancelled │
                        └───────────┘      └──────────┘      └───────────┘
```

---

## 7. Notifier Design

### Smart Long Message Handling

Responses are handled based on length:

| Length | Strategy |
|--------|----------|
| ≤1800 chars | Sent inline as a single markdown message with header |
| 1800–5400 chars | Header sent as markdown, content split into multiple text messages at line boundaries |
| >5400 chars | Header with char count sent as markdown, full content uploaded as `.md` file |

Splitting tries to break at newlines for readability. Falls back to hard cuts if no good newline is found.

### Task Labels

All task notifications (completed, failed, cancelled, streaming) include a task label derived from the first line of the user's original message (cleaned of system-injected prefixes). This helps users identify which task a notification belongs to when multiple tasks are in flight:

```
📌 `check stock prices for AAPL`
**Task completed**
```

### Message Formatting

| Scenario | Format |
|----------|--------|
| Streaming output | Periodic buffered chunks via `StreamHandler`, with task label on first message |
| Task failed | `📌 task_label` header + error (via smart long message handling) |
| Task completed | `📌 task_label` header + output (via smart long message handling) |
| Task cancelled | `📌 task_label` + `**Task cancelled**` |
| Output files | Sent on demand via WeCom MCP tools (`send_wecom_image`, `send_wecom_file`) |

### Progress Update Strategy

Progress notifications are throttled by `progress_interval_seconds` (default 30s) to avoid spamming the user.

### Auto-Split for WeCom Limits

Both `send_text` and `send_markdown` in `WeComAPI` auto-split messages that exceed WeCom's 2048-byte limit (`WECOM_MAX_TEXT_BYTES`). Splitting uses `_split_by_bytes()` which tries to break at newlines for readability, falling back to hard byte-boundary cuts. The `Notifier._send_text_smart()` method also splits at the char level for reply messages.

### Streaming Output (StreamHandler)

While a task is running, the executor creates a `StreamHandler` that receives line-by-line output from the runner. The handler:

1. **Buffers** output lines in memory
2. **Sends** buffered content at `streaming_interval_seconds` intervals (default: 10s)
3. **Rate limits** to `_MAX_SENDS_PER_MINUTE = 25` (WeCom allows 30/min per user)
4. **Truncates** long chunks to `_MAX_CONTENT_CHARS` (1800 chars), showing the tail
5. **Prepends task label** on the first streaming message
6. **Updates dashboard** — a shared `dashboard_ref` dict is passed from the executor, and the handler writes the last 3000 chars of accumulated output to `dashboard_ref["buffer"]` on every output event. The executor also feeds thinking deltas to `dashboard_ref["thinking"]`. This enables real-time streaming in the Dashboard WebUI.
7. **Flushes** remaining buffer when the task completes

### Task Receipt Confirmation

When a task is enqueued while another task is already running, the executor sends a receipt confirmation to the user: `📥 收到，排队中（当前有任务运行）` with a preview of the queued message.

---

## 7.5 Task Scheduling

Recurring task scheduling is handled by Claude Code's `claude-code-scheduler` MCP plugin. Users describe schedules in natural language (e.g., "every day at 9am run the tests"), and Claude sets up the schedule via the MCP tool. No custom cron parsing or scheduler process is needed.

Scheduled tasks run as standalone Claude processes outside the executor pipeline. To deliver results back to the user, each task message includes a prompt hint with the user's WeCom ID and instructions to use the `send_wecom_message` MCP tool (see Section 15). The `.mcp.json` in the working directory ensures the WeCom tools are available to all Claude processes.

---

## 8. Project Structure

```
remote_control/
├── pyproject.toml
├── config.yaml              # User's config (gitignored)
├── config.example.yaml      # Template (single + multi-agent examples)
├── deploy.sh                # Remote SSH deployment script
├── CLAUDE.md
├── REQUIREMENTS.md
├── DESIGN.md
├── src/
│   └── remote_control/
│       ├── __init__.py
│       ├── main.py              # CLI entry point: `lobster` / `lobster init`
│       ├── cli_init.py          # Interactive config.yaml generator with credential validation
│       ├── config.py            # Config loading & validation (Pydantic), list normalization
│       ├── server.py            # aiohttp app setup, per-agent wiring, lifecycle
│       ├── wecom/
│       │   ├── __init__.py
│       │   ├── gateway.py       # Callback handler (verify, decrypt, dispatch)
│       │   ├── crypto.py        # WeCom message encryption/decryption
│       │   ├── api.py           # WeCom API client (text, markdown, file, image upload)
│       │   └── message_source.py # MessageSource ABC + CallbackSource, RelayPollingSource
│       ├── dashboard/
│       │   ├── __init__.py
│       │   ├── routes.py         # Auth (cookie HMAC), login page, /api/status endpoint
│       │   ├── status.py         # Agent status assembly, workstation classification, cron parsing
│       │   └── static/
│       │       └── dashboard.html  # Single-page dashboard UI
│       ├── mcp/
│       │   ├── wecom_server.py  # Standalone MCP server (WeCom sending tools)
│       │   └── profile_server.py  # Standalone MCP server (agent profile tools)
│       ├── core/
│       │   ├── __init__.py
│       │   ├── router.py        # Command router (slash commands + /memory)
│       │   ├── executor.py      # Task queue orchestration + memory/MCP hint injection
│       │   ├── runner.py        # Agent Runner (streaming output, session retry)
│       │   ├── notifier.py      # Failure notifications, file/image sending + StreamHandler
│       │   ├── memory.py        # Memory utilities (keyword extraction, context building)
│       │   ├── store.py         # SQLite store (Store + ScopedStore per-agent wrapper)
│       │   ├── models.py        # Task, Session, Memory data models
│       │   └── profile.py       # Agent profile system (ProfileManager, hot-reload, audit trail)
│       └── utils/
│           └── __init__.py
├── relay/
│   ├── lambda_function.py       # AWS Lambda handler (callback + fetch)
│   └── README.md                # Deployed AWS resource inventory
└── tests/
    ├── __init__.py
    ├── conftest.py
    ├── test_config.py
    ├── test_crypto.py
    ├── test_api.py
    ├── test_gateway.py
    ├── test_message_source.py
    ├── test_router.py
    ├── test_runner.py
    ├── test_executor.py
    ├── test_store.py
    ├── test_notifier.py
    ├── test_mcp_wecom.py
    └── test_server.py
```

---

## 9. Tech Stack (Final)

| Component | Choice | Rationale |
|-----------|--------|-----------|
| Python | 3.11+ | `asyncio.TaskGroup`, `tomllib` stdlib |
| HTTP Server | **aiohttp** | Lightweight async server, no need for FastAPI's extras |
| HTTP Client | **httpx[socks]** | Async HTTP client for WeCom API (SOCKS5 proxy support) |
| Config | **Pydantic** + YAML | Validation + type safety for config |
| Storage | **sqlite3** (stdlib) | Zero-dependency, single-user |
| WeCom Crypto | **pycryptodome** | AES-CBC decryption for message callbacks |
| Scheduling | **claude-code-scheduler** MCP plugin | Natural language task scheduling via Claude Code |
| MCP Server | **mcp** (Python SDK) | Expose WeCom sending as MCP tools for Claude Code |
| Dashboard | **aiohttp** (same server) + vanilla HTML/JS | Read-only web UI, HMAC cookie auth, SSE-style polling |
| Testing | **pytest** + **pytest-asyncio** | Async test support |
| Packaging | **uv** | Fast dependency management |

### Dependencies (pyproject.toml)

```toml
[project]
dependencies = [
    "aiohttp>=3.9",
    "httpx[socks]>=0.27",
    "pydantic>=2.0",
    "pyyaml>=6.0",
    "pycryptodome>=3.20",
    "mcp>=1.0",
]

[project.optional-dependencies]
dev = [
    "pytest>=8.0",
    "pytest-asyncio>=0.23",
    "ruff>=0.4",
]
```

---

## 10. Startup & Shutdown Flow

### Startup

```
main.py
  ├─ Load config.yaml → validate with Pydantic
  ├─ Normalize wecom config (single dict → list)
  ├─ Initialize shared Store (SQLite: create tables, run migrations)
  ├─ Initialize shared ProcessWatchdog
  ├─ For each wecom agent:
  │   ├─ Resolve per-agent working_dir (wecom.working_dir or agent.default_working_dir)
  │   ├─ Initialize WeComAPI (lazy token fetch)
  │   ├─ Create ScopedStore(store, agent_id) for agent isolation
  │   ├─ Initialize AgentRunner (with watchdog ref)
  │   ├─ Initialize Executor (with ScopedStore) + Notifier + CommandRouter
  │   ├─ Create MessageSource (callback or relay)
  │   ├─ Write .mcp.json (WeCom MCP tools for Claude processes)
  │   └─ Register routes (namespaced by agent_id)
  ├─ Register Dashboard routes (if dashboard.enabled and dashboard.password set)
  ├─ Register /health endpoint
  ├─ Start aiohttp server on configured host:port
  └─ Log: "Server started on http://0.0.0.0:8080"
```

### Shutdown (SIGTERM/SIGINT)

```
Signal received
  ├─ Stop accepting new HTTP requests
  ├─ For each agent:
  │   ├─ Stop MessageSource (cancel poll tasks)
  │   ├─ If task running:
  │   │   ├─ Send SIGTERM to Claude Code process
  │   │   ├─ Wait up to 10s for graceful exit
  │   │   ├─ SIGKILL if still alive
  │   │   └─ Mark task as "cancelled" in DB
  │   └─ Close WeComAPI HTTP client
  ├─ Close SQLite connection
  └─ Exit
```

---

## 11. Error Handling

| Error | Handling |
|-------|----------|
| WeCom API returns non-0 errcode | Log error. Notify user on persistent failure. |
| access_token expired (42001) | Refresh token immediately, retry the request once. |
| Claude Code process crashes | Mark task as `failed`, capture stderr, notify user with error. |
| Claude Code timeout | SIGTERM → wait 10s → SIGKILL. Mark task `failed`, notify user. |
| Session ID mismatch | Auto-retry with opposite `--session-id`/`--resume` flag. |
| Invalid config | Fail fast on startup with clear error message. |
| SQLite write failure | Log error, attempt retry. Critical path — surface to user if persistent. |
| Relay Lambda error | WeCom retries callback delivery up to 3 times. Lambda errors logged to CloudWatch. Local poll retries on next interval. |
| Relay unreachable | Local `_poll_loop` catches `httpx.ConnectError`, `httpx.TimeoutException`, and `OSError` silently, retries on next poll interval. No message loss — DynamoDB retains messages for 7 days. |
| File upload failure | Falls back to truncated inline text message with char count indicator. |

---

## 12. Security Considerations

- **WeCom signature verification**: Every callback request is verified before processing. Reject invalid signatures.
- **Config secrets**: `config.yaml` is gitignored. Secrets (corp_id, secret, AES key) never logged.
- **Claude Code permissions**: Use `--permission-mode` to control what Claude Code can do. Options include `"bypassPermissions"` (full access), `"acceptEdits"` (auto-approve edits), or default (asks for permission).
- **No arbitrary command injection**: User messages are passed as a single CLI argument, never interpolated into shell commands.

---

## 13. AWS Relay Setup

The relay is deployed on AWS (Lambda + API Gateway + DynamoDB) in `ap-southeast-1` (Singapore) for low latency from China mainland. See `relay/README.md` for the full resource inventory.

### Architecture

```
WeCom  ──POST──►  API Gateway  ──►  Lambda (store raw XML)  ──►  DynamoDB
                                                                      │
Local server  ──POST /messages/fetch──►  API Gateway  ──►  Lambda  ──┘
     │
     ▼
  Decrypt locally (crypto.py) → Command Router → Claude Code CLI
     │
     ▼
  WeCom API (qyapi.weixin.qq.com) ← replies sent directly (no relay)
```

### Lambda Function (`relay/lambda_function.py`)

Single function, 3 routes:

| Route | Action |
|-------|--------|
| `GET /callback` | WeCom URL verification — decrypts echostr, returns plaintext |
| `POST /callback` | Stores raw encrypted XML + query params in DynamoDB (pass-through, no decryption) |
| `POST /messages/fetch` | Returns messages with `seq > cursor`, cursor-based pagination |

The Lambda only does crypto for the one-time GET verification. All message decryption happens locally.

**Multi-agent relay sharing**: A single relay endpoint (`POST /callback`) can receive callbacks from multiple WeCom agents (since WeCom sends all agent callbacks to the same URL). The outer XML includes an `AgentID` field. Each local `RelayPollingSource` filters messages by its configured `agent_id` during `_dispatch_message()`, silently skipping messages for other agents. This means all agents can share one relay + DynamoDB table without interference.

### DynamoDB Table (`wecom_relay_messages`)

- **PK**: `msg_id` (UUID)
- **GSI** `seq-index`: `gsi_pk` (always `"msg"`) + `seq` (number) — enables efficient cursor-based range queries
- **TTL**: Auto-expire items after 7 days
- **Counter**: Special item `msg_id = "__counter__"` with atomic `seq` increment

### Updating Lambda Code

```bash
cp relay/lambda_function.py /tmp/lambda_package/
cd /tmp/lambda_package && zip -r /tmp/wecom_relay.zip .
aws lambda update-function-code --region ap-southeast-1 \
  --function-name wecom-relay --zip-file fileb:///tmp/wecom_relay.zip
```

---

## 14. Message Source Abstraction

The `MessageSource` ABC decouples message receiving from the rest of the system, allowing pluggable adapters.

### Interface

```python
class MessageSource(abc.ABC):
    async def start(self) -> None: ...     # Begin receiving (e.g., start poll loop)
    async def stop(self) -> None: ...      # Stop receiving, clean up
    def register_routes(self, app) -> None: ...  # Add HTTP routes to aiohttp app
```

### Implementations

| Source | Config | Routes | How it works |
|--------|--------|--------|--------------|
| `CallbackSource` | `mode: "callback"` | `GET/POST /wecom/callback/{agent_id}` | Wraps `WeComGateway`. WeCom pushes messages via webhook. |
| `RelayPollingSource` | `mode: "relay"` | `GET /relay/status/{agent_id}` | Background `asyncio.Task` polls a relay service on interval. Cursor persisted in `kv` table. |

### RelayPollingSource Details

The relay (AWS Lambda) is a **pass-through** — it stores raw encrypted WeCom callback data in DynamoDB. The local `RelayPollingSource` polls the relay, then **decrypts and parses locally** using the same `crypto.py` functions as `CallbackSource`.

```
_poll_loop()  ──►  _poll_once()  ──►  _fetch_messages(url, payload)
                       │                        │
                       │                        ▼
                       │               HTTP POST <relay_url>/messages/fetch
                       │                        │
                       ▼                        ▼
                 for msg in messages:     returns {messages (raw encrypted), next_cursor}
                   _dispatch_message(msg)
                       │
                       ▼
                 verify_signature() → decrypt_message() → parse_message_xml()
                       │
                       ▼
                 IncomingMessage → on_message callback
```

**Cursor persistence**: The relay cursor is stored in the SQLite `kv` table (`relay_cursor_{agent_id}`), so messages are not replayed after server restart.

**Network resilience**: The poll loop catches `httpx.ConnectError`, `httpx.TimeoutException`, and `OSError` silently (common during VPN disconnects or laptop sleep) and retries on the next interval. No messages are lost.

**Message flow (inbound):**
```
WeCom → API Gateway → Lambda (store raw XML + query params) → DynamoDB
Local server → poll Lambda /messages/fetch → decrypt locally → dispatch
```

**Message flow (outbound — replies):**
```
Local server → WeCom API (qyapi.weixin.qq.com) directly (no relay needed)
```

**Relay API contract:**

```
POST <relay_url>/messages/fetch
Request:  {"cursor": "<last_cursor>", "limit": 100}
Response: {
    "messages": [
        {
            "msg_id": "uuid",
            "seq": 42,
            "query_params": {"msg_signature": "...", "timestamp": "...", "nonce": "..."},
            "body": "<xml><Encrypt>...</Encrypt></xml>"
        },
        ...
    ],
    "next_cursor": "<seq_of_last_message>"
}
```

- **Raw pass-through**: Lambda stores encrypted XML as-is. All WeCom-specific crypto (AES decryption, signature verification) happens locally in `_dispatch_message()` via `crypto.py`.
- **Cursor management**: Cursor is the `seq` number of the last fetched message. DynamoDB GSI enables efficient `seq > cursor` range queries. Cursor is persisted in SQLite `kv` table across restarts.
- **Error isolation**: `_fetch_messages()` is extracted as a separate method for testability. Decryption or handler errors in `_dispatch_message()` are caught per-message to prevent crashing the poll loop.
- **Observability**: `GET /relay/status/{agent_id}` returns current cursor, relay URL, poll interval, and source type.
- **TTL**: DynamoDB items auto-expire after 7 days.

### Source Selection (server.py)

```python
def _create_message_source(wecom_config, on_message, store) -> MessageSource:
    if wecom_config.mode == "relay":
        return RelayPollingSource(wecom_config, wecom_config.relay_url, on_message, store=store, ...)
    return CallbackSource(wecom_config, on_message)
```

### Adding a New Source

To add a new message source (e.g., Telegram, Slack):
1. Create a class implementing `MessageSource`.
2. Register it in `_create_message_source()` factory in `server.py`.
3. Add any needed config fields to `WeComConfig` (or create a new config section).

---

## 15. WeCom MCP Server

A standalone MCP (Model Context Protocol) server that exposes WeCom message sending as tools for Claude Code. This enables any Claude process — including scheduler-spawned ones running outside the executor pipeline — to send messages back to users.

### Problem

The `claude-code-scheduler` plugin creates OS-level scheduled tasks that spawn standalone `claude` processes. These bypass the executor → notifier pipeline, so results never reach WeCom. Additionally, users had no way to ask Claude to send specific files on demand.

### Architecture

```
┌─────────────────────┐                    ┌──────────────────┐
│  Claude Code CLI    │  MCP tool call     │  WeCom MCP       │
│  (any process)      │───────────────────►│  Server (stdio)  │
│                     │                    │                  │
│  - executor task    │  send_wecom_message│  httpx → WeCom   │
│  - scheduled task   │  send_wecom_image  │  API (qyapi...)  │
│  - manual CLI       │  send_wecom_file   │                  │
└─────────────────────┘                    └──────────────────┘
```

### MCP Tools

| Tool | Parameters | Description |
|------|-----------|-------------|
| `send_wecom_message` | `user_id`, `content` | Send text message to WeCom user |
| `send_wecom_image` | `user_id`, `file_path` | Upload and send image |
| `send_wecom_file` | `user_id`, `file_path` | Upload and send file |

### `.mcp.json` Auto-Generation

On server startup, `server.py` writes `.mcp.json` in the `default_working_dir` with WeCom credentials from `config.yaml`. Claude Code automatically reads `.mcp.json` from the working directory, so all processes get the tools.

```json
{
  "mcpServers": {
    "wecom": {
      "command": "/path/to/.venv/bin/python",
      "args": ["-m", "remote_control.mcp.wecom_server"],
      "env": {
        "WECOM_CORP_ID": "...",
        "WECOM_AGENT_ID": "...",
        "WECOM_SECRET": "...",
        "WECOM_PROXY": ""
      }
    }
  }
}
```

Existing `.mcp.json` entries (e.g., other MCP servers) are preserved — only the `wecom` key is updated.

### Prompt Augmentation

The executor prepends a system hint to every task message containing:
- The current user's WeCom `user_id`
- Instructions to use `send_wecom_message` for scheduled task result delivery
- Instructions to use `send_wecom_file`/`send_wecom_image` when users request file sending
- Output format guidance (concise for mobile, under 1500 chars preferred)
- Memory hint: instructions to update `MEMORY.md` when learning something of lasting value
- Dashboard hint: instructions to edit `.dashboard-workstations.json` for new work categories

This ensures Claude knows who to reply to and how, both for immediate tasks and when creating scheduled ones.

### Standalone Design

The MCP server is a separate process (stdio-based) with its own `httpx.Client` and WeCom token management. It does not depend on the main server being running — once `.mcp.json` is written, any Claude process can use it independently.

See `docs/wecom-mcp.md` for the full installation and usage guide.

---

## 16. Dashboard WebUI

A password-protected, read-only web dashboard for monitoring the Remote Control system in real time.

### Configuration

```yaml
dashboard:
  enabled: true
  password: "your-password"
  secret: "hmac-signing-secret"  # optional, defaults to a built-in key
```

Config model: `DashboardConfig` in `config.py` with fields `enabled` (bool), `password` (str), and `secret` (str).

### Authentication

- Cookie-based HMAC token auth (`rc_dash_token`), valid for 24 hours.
- Login page at `/dashboard/login` with password form.
- IP-based rate limiting: max 5 failed attempts per IP, 15-minute lockout.
- Token is `{expires_timestamp}:{hmac_sha256_sig}`, verified on every request.

### Routes

| Route | Method | Description |
|-------|--------|-------------|
| `/dashboard` | GET | Main dashboard page (static HTML) |
| `/dashboard/login` | GET | Login form |
| `/dashboard/login` | POST | Login submission |
| `/api/status` | GET | JSON API — full system status |
| `/api/task/{task_id}` | GET | JSON API — full task detail (output, error, timestamps, duration) |
| `/api/tab/{agent_id}/{tab_id}` | GET | JSON API — custom tab data (table, key-value, chart, or HTML) |

### API Response (`/api/status`)

The status endpoint returns a comprehensive JSON payload assembled by `dashboard/status.py`:

```json
{
  "agent": {
    "state": "coding|stock|idle|done|error",
    "state_label": "human-readable label",
    "task_id": "...",
    "task_message": "cleaned message",
    "elapsed_seconds": 42,
    "streaming_output": "last 2000 chars of output",
    "thinking": "last 3000 chars of thinking"
  },
  "agents": [...],           // all agents' state (multi-agent)
  "all_processes": [...],    // per-agent process info
  "all_models": [...],       // per-agent model info
  "agent_names": {"1000002": "lobster"},  // agent_id → display name
  "process": {"pid": 12345, "rss_mb": 256},
  "model": {
    "model": "claude-opus-4-6",
    "claude_code_version": "2.1.79",
    "context_window": 200000,
    "input_tokens": 15000,
    "output_tokens": 2000,
    "total_cost_usd": 0.15
  },
  "recent_tasks": [...],
  "cron_jobs": [...],
  "system_crons": [...],
  "tabs": [{"id": "stocks", "label": "Stocks", "type": "data", "source": "stocks.json"}],
  "all_tabs": [[...], [...]],  // per-agent tab configs
  "lobster": {"name": "🦞", "emoji": "🦞"},
  "workstations": [{"id": "coding", "label": "CODE", "icon": "💻"}, ...],
  "timestamp": "2026-03-22T..."
}
```

### Workstation Classification

Tasks are classified into "workstations" based on keyword matching against the task message. Workstations are configurable via `.dashboard-workstations.json` (searched in project dir first, then working dir). The file supports both a plain list format and an object format with `lobster` customization:

```json
{
  "lobster": {"name": "🦞", "emoji": "🦞"},
  "workstations": [
    {"id": "coding", "label": "CODE", "icon": "💻", "keywords": ["code", "fix", "bug"]},
    {"id": "stock", "label": "STOCK", "icon": "📈", "keywords": ["股票", "stock"]},
    {"id": "general", "label": "WORK", "icon": "⚙️", "keywords": []}
  ]
}
```

The last workstation with empty `keywords` acts as the fallback ("general"). Config is cached by file mtime for efficient reloads.

### Custom Dashboard Tabs

Agents can create custom data views in the dashboard by placing a `.dashboard-tabs.json` file in their working directory. The tab system is implemented in `dashboard/tabs.py`.

**Config schema** (`.dashboard-tabs.json`):
```json
[
  {"id": "stocks", "label": "Portfolio", "type": "data", "source": "data/stocks.json"},
  {"id": "trend", "label": "Trend", "type": "chart", "source": "data/trend.json",
   "chart_options": {"chart_type": "line", "title": "Price Trend"}},
  {"id": "report", "label": "Report", "type": "html", "source": "reports/daily.html"}
]
```

**Tab types:**
- `data` — JSON file. Arrays render as tables, objects render as key-value pairs. Override with `"template": "table"` or `"key-value"`.
- `chart` — JSON file with `{labels: [...], datasets: [{label, data, color}]}`. Supports `line` and `bar` chart types. Rendered via native Canvas 2D API (no external libs).
- `html` — HTML file rendered in a sandboxed iframe.

**Security:** Source file paths are resolved via `os.path.realpath()` and must reside within the agent's `working_dir`. Path traversal (`../../`) and absolute paths outside the working dir are rejected. Files >1MB are rejected.

**UI:** The dashboard shows a horizontal tab bar below the aquarium. Home tab (always present) shows the default content. Custom tabs are loaded on click via `/api/tab/{agent_id}/{tab_id}`. Tab definitions refresh on each status poll; tab data only loads on click with a manual refresh button.

### System Crontab Integration

The dashboard reads the system crontab (`crontab -l`) and displays entries with human-readable schedule descriptions. Common cron expressions are mapped to Chinese labels (e.g., `"0 1 * * *"` → `"每天 09:00"`). Task names are extracted from trailing comments (`# task-name`) or script filenames.

### Real-Time Streaming

The dashboard polls `/api/status` at regular intervals. The executor shares a `dashboard_streaming` dict with each `StreamHandler`, which writes:
- `buffer`: Last 3000 chars of text output (updated on every output event)
- `thinking`: Last 5000 chars of thinking blocks (updated via `on_thinking` callback)

This enables the dashboard to show live output and reasoning as a task runs.

---

## 17. Agent Profile System

A per-agent self-configuration system that allows agents to tune their own behavior at runtime via MCP tools. Changes are persisted in `.agent-profile.yaml` in the agent's working directory with a full audit trail.

### Profile Schema

The profile is a Pydantic model (`AgentProfile`) with these sections:

```yaml
version: "1.0"
agent_id: "1000002"
updated_at: "2026-03-27T..."

output_style:
  language: "auto"          # "auto" | "zh-CN" | "en-US"
  format: "balanced"        # "concise" | "balanced" | "detailed"
  max_message_length: 1500
  code_block_handling: "inline"  # "inline" | "file" | "truncate"

notification:
  streaming_interval_seconds: 10.0
  progress_interval_seconds: 30.0
  notify_on_completion: false
  notify_on_error: true

model_selection:
  default_model: ""         # empty = use config.yaml default
  task_type_overrides:      # regex-matched model routing
    - pattern: "股票|stock"
      model: "claude-sonnet-4-5"
      rationale: "Faster for simple lookups"

memory:
  keyword_match_limit: 5
  recent_context_limit: 5
  max_context_chars: 2000

custom_commands:            # agent-defined slash commands
  morning:
    prompt: "Run morning briefing: check stocks, news, calendar"
    description: "Morning briefing"
```

### ProfileManager Architecture

`ProfileManager` (`core/profile.py`) provides:

- **Hot-reload**: Profile is re-read from disk when the file's mtime changes. No server restart needed.
- **Audit trail**: Every `update()` call saves a timestamped snapshot to `.agent-profile-history/` before applying changes. Each snapshot includes the old profile, timestamp, and rationale.
- **Bootstrap**: On first access, if no `.agent-profile.yaml` exists, the manager creates one by extracting hints from existing config files (`.system-prompt.md`, `.dashboard-workstations.json`).
- **Defaults**: A `.agent-profile.default.yaml` can provide per-agent defaults. `reset()` reverts to these defaults (or built-in ones if the default file is absent).
- **Atomic writes**: Profile saves use temp file + `os.replace()` to prevent corruption.

### MCP Tools (Profile Server)

The profile MCP server (`mcp/profile_server.py`) exposes four tools:

| Tool | Parameters | Description |
|------|-----------|-------------|
| `get_agent_config` | `name` (dotted key or "all") | Read a config value or the entire profile |
| `set_agent_config` | `name`, `value` (JSON), `rationale` | Set a config value with audit trail |
| `list_agent_config` | — | List full profile as formatted YAML with section descriptions |
| `reset_agent_config` | `name` (optional) | Reset one key or entire profile to defaults |

The server is registered in `.mcp.json` alongside the WeCom MCP server, so all Claude processes can self-configure:

```json
{
  "mcpServers": {
    "wecom": { ... },
    "agent-profile": {
      "command": "/path/to/.venv/bin/python",
      "args": ["-m", "remote_control.mcp.profile_server"],
      "env": {
        "AGENT_WORKING_DIR": "/path/to/working/dir",
        "AGENT_ID": "1000002"
      }
    }
  }
}
```

### Integration Points

- **Executor**: Reads the profile before each task to apply output style hints, model overrides, and notification preferences.
- **Runner**: Uses `model_selection.default_model` and `task_type_overrides` to select the model per task.
- **Router**: Expands `custom_commands` — if a message starts with a custom command name, it is expanded to the configured prompt.
- **Notifier**: Reads `notification` preferences for streaming and progress intervals.

### Safety Boundaries

- **Schema-enforced**: Only known keys can be set. Unknown fields are ignored (`extra="ignore"`).
- **Audit trail**: Every change is recorded with timestamp and rationale.
- **Reset capability**: Any key or the entire profile can be reset to defaults.
- **No secrets**: The profile never stores credentials or security-sensitive values.

### File Layout (per agent working directory)

```
working_dir/
├── .agent-profile.yaml           # Current profile (agent-managed)
├── .agent-profile.default.yaml   # Optional operator defaults (manual)
└── .agent-profile-history/       # Audit trail snapshots
    ├── 2026-03-27_093045_123456.yaml
    └── 2026-03-27_103012_654321.yaml
```
