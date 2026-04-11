# Next Steps Plan

> Generated 2026-04-08 from 3-agent debate (PM + Senior Dev + Staff Engineer, 6 rounds total).

## P0 — Must Do

- [x] **Memory feedback loop** — Replaced entirely: old SQLite keyword-injection system removed, replaced with task archive + recall MCP tools. Claude generates 📋 summaries; full outputs archived to `.task-archive/`.
- [ ] **Runner + Executor integration test** — E2E test with real `claude -p --output-format stream-json` (mock CLI binary), covering timeout cascade (executor → first-response → watchdog). Zero coverage today.
- [ ] **Docker Compose one-click deploy** — Required before open source. User provides WeCom credentials as env vars, everything else auto-configured. README restructure: first page = 3-step quickstart.

## P1 — Should Do

- [ ] **Profile multi-process write safety** — Scheduler-spawned claude processes can concurrent-read/write `.agent-profile.yaml`. Add `fcntl.flock` on `_save()`. (Staff Eng confirmed blind spot)
- [ ] **WeCom retry: distinguish transient vs permanent errors** — `_send_chunks()` retries all errors equally. Token expiry (42001) should raise, not silently drop. Upstream callers don't check return values.
- [ ] **ProfileManager: notify user on validation fail** — Currently `except Exception: logger.debug(...)` silently falls back to default. Change to notify via WeCom when profile YAML is broken.
- [ ] **WeCom messages: auto-push Dashboard link** — Task completion/failure messages include `dashboard_url` with one-time token. Closes the WeCom↔Dashboard experience gap.
- [ ] **Runner timeout retry: flip is_resume** — `_run_once()` L111-113: first-response timeout retries with original `is_resume` value. Should force `--session-id` rebuild on timeout (session may be stale/dead).

## P2 — Nice to Have

- [ ] **Memory keyword LIKE wildcard escape** — `extract_keywords()` doesn't escape `%` and `_` before SQL LIKE matching. One-line `re.escape` fix.
- [ ] **Dashboard: SSE instead of 1s polling** — Replace `/api/status` polling with Server-Sent Events. ~50 lines, reduces latency and server load.
- [ ] **Dashboard: Retry button** — Allow retrying failed tasks from the web UI instead of requiring WeCom `/retry` command.
- [ ] **Memory keyword extraction: CJK test coverage** — `re.findall(r'[\w.]+', ...)` works for CJK by luck (Python `\w` includes Unicode). Add explicit CJK + emoji test cases.
- [ ] **Profile template library** — Pre-built `.agent-profile.yaml` templates (finance, social, dev). Just YAML files + `lobster profile install <name>`. ~3 hours investment.

## Frozen (Shelved Disputes)

- **ScopedStore** — Don't expand, don't delete. Revisit when a third use case appears.
- **MessageSource abstraction** — Keep. 38-line ABC with clear boundary. Revisit when second IM platform arrives.
- **Watchdog vs Executor dual timeout** — Keep defense-in-depth. But add test proving watchdog can intervene when executor timeout fails.
- **Runner refactor** — Test first (lock behavior), refactor later (separate PR).
- **contextvars for agent_id** — Not doing. Explicit passing is verbose but safe and grep-friendly.

## Not Doing

- Agent Marketplace / Multi-user multi-tenancy / Task DAG workflow
- Delete Workstation classification (marginal cost = 0, silent fallback is fine)
- Delete MessageSource ABC / Watchdog / dual timeout mechanism
