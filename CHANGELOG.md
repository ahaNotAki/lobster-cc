# Changelog

## [0.2.0] — 2026-03-27

### Features
- Agent Profile System: per-agent self-configuration via `.agent-profile.yaml`
  - Output style tuning (language, format, max length, code block handling)
  - Notification preferences (streaming/progress intervals, completion/error toggles)
  - Model selection with task-type overrides (regex-matched model routing)
  - Memory preferences (keyword limits, context size)
  - Custom slash commands (agent-defined prompt expansion)
- Profile MCP Server: `get_agent_config`, `set_agent_config`, `list_agent_config`, `reset_agent_config` tools
- ProfileManager: hot-reload (mtime-based), audit trail (`.agent-profile-history/`), bootstrap from existing config files
- Atomic profile writes, schema-enforced safety, full reset capability

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
