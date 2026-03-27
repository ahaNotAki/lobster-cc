# Changelog

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
