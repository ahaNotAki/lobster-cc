# Changelog

## [0.3.0] — 2026-06-15

### Security
- **Self-hosted relay replaces AWS API Gateway + Lambda + DynamoDB** (AppSec finding `APIGAuthenticationCheck`: the old `/messages/fetch` and `/callback` endpoints were unauthenticated). The legacy AWS stack was torn down.
  - New relay (`src/remote_control/relay/`): aiohttp + SQLite buffer on the existing EC2 box
  - `/callback` verifies the WeCom signature **and** a 5-minute timestamp freshness window
  - `/messages/fetch` requires an `Authorization: Bearer <relay_token>` shared secret
  - Inbound port restricted to WeCom callback IPs only (never `0.0.0.0/0`); `audit-sg.sh` gate enforces this
  - Raw callback XML parsed with `defusedxml` (entity-expansion / XXE hardening)
  - Secrets live in a `0600` systemd `EnvironmentFile`, not the unit file
- **`wecom.relay_token`** is now required in relay mode (the poller authenticates to the relay).

### Infrastructure
- **`deploy-self-relay.sh`**: one-command relay cutover that reads all credentials from the remote `config.yaml` (no secrets in the repo or on the command line), derives WeCom IPs via `getcallbackip`, provisions the relay (SG + venv + systemd), and health-checks it.
- The poller routes its fetch through the configured SOCKS proxy, so the relay needs no public exposure to the poller's IP.
- **Removed**: `relay/` (Lambda code + SAM template), `scripts/setup-relay.sh`, and `scripts/setup.sh`. EC2 proxy provisioning is now solely `scripts/setup-proxy.sh`.

## [0.2.0] — 2026-03-27

### Features
- **Agent Profile System**: per-agent self-configuration via `.agent-profile.yaml`
  - Output style tuning (language, format, max length, code block handling)
  - Notification preferences (streaming/progress intervals, completion/error toggles)
  - Model selection with task-type overrides (regex-matched model routing)
  - Memory preferences (keyword limits, context size)
  - Custom slash commands (agent-defined prompt expansion)
- **Profile MCP Server**: `get_agent_config`, `set_agent_config`, `list_agent_config`, `reset_agent_config` tools
- **ProfileManager**: hot-reload (mtime-based), audit trail (`.agent-profile-history/`), bootstrap from existing config files
- Atomic profile writes, schema-enforced safety, full reset capability

### Dashboard Improvements
- **Custom Dashboard Tabs**: agents can create data views via `.dashboard-tabs.json`
  - Table renderer for JSON arrays
  - Key-value renderer for JSON objects
  - Chart renderer (line/bar charts via Canvas 2D API, no external libs)
  - HTML renderer (sandboxed iframe)
  - `/api/tab/{agent_id}/{tab_id}` endpoint for on-demand tab data loading
  - Security: path validation (realpath), 1MB file size limit
- **Expandable Recent Tasks**: click to expand task details inline
  - Status emoji tags (✓ completed, ✗ failed, ⏹ cancelled, ⏳ running)
  - `/api/task/{task_id}` endpoint for full task details (output, error, timestamps, duration)
  - Animated expansion with CSS slideDown
  - Per-task detail caching to avoid redundant fetches
- **Font Size Improvements**: increased readability across all dashboard text (task items, labels, workstation text, streaming output)
- **Tab Bar Grouping**: tabs grouped by agent name with visual separators

### Infrastructure
- **Unified Setup Script**: `setup.sh` combines relay and optional proxy setup in one command
  - `--token`, `--aes-key` flags for WeCom credentials
  - `--proxy` flag to optionally deploy EC2 proxy with Elastic IP
  - Fully idempotent, skips existing resources
- **Standalone Relay Setup**: `setup-relay.sh` for relay-only deployments (no proxy)
- Both scripts use pure AWS CLI (no SAM/CloudFormation dependencies)

## [0.1.0] — 2026-03-27

Initial open source release.

### Features
- WeCom integration: send tasks from phone, receive results in chat
- Multi-agent support: run multiple bots with isolated stores
- Relay mode: AWS Lambda relay, no public URL needed locally
- Dashboard: lobster aquarium WebUI with real-time streaming
- Session context: continuous conversation via Claude Code --session-id
- Persistent memory: dual-layer (MEMORY.md + SQLite keyword recall)
- Streaming output: throttled real-time progress updates
- Media support: images, voice, video, files
- Natural language scheduling via Claude Code scheduler plugin
- WeCom MCP tools: Claude can send messages/files back to WeCom
- Process watchdog: kills runaway processes
- Deploy script with optional SOCKS5 proxy for fixed outbound IP
