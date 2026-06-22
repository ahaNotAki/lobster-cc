<p align="center">
  <img src="https://em-content.zobj.net/source/apple/391/lobster_1f99e.png" width="120" alt="lobster">
</p>

<h1 align="center">lobster-cc</h1>

<p align="center">
  <strong>Your Claude Code, on a leash.</strong><br>
  Control Claude Code from your phone. Send tasks, watch it think, get results back — all through WeCom.<br>
  <em>Agents that remember, learn, and evolve with every task.</em>
</p>

<p align="center">
  <a href="#quick-start">Quick Start</a> &middot;
  <a href="#features">Features</a> &middot;
  <a href="#how-it-works">How It Works</a> &middot;
  <a href="DESIGN.md">Design</a> &middot;
  <a href="CONTRIBUTING.md">Contributing</a>
</p>

---

## Why?

You're in a meeting. On the train. At lunch. You think of something — a bug to fix, a file to check, a task to run. Your dev machine is at your desk, Claude Code is ready, but you're not there.

**lobster-cc** bridges that gap. Send a message from your phone, and Claude Code gets to work. You get real-time streaming progress, and results land right back in your chat.

No SSH. No VPN. No laptop required.

## Features

### Talk to Claude Code like texting a colleague

Send any message and it becomes a coding task. Claude Code runs it in your project directory with full context — session history, file access, and all your MCP tools.

```
You:  the login page has a bug — users can't reset their password
Bot:  [streams progress as Claude investigates, finds the issue, fixes it]
Bot:  Fixed. The reset handler was checking `email` instead of `username`. Updated and tests pass.
```

### Continuous conversation, not one-off commands

Every message builds on the last. Claude remembers what it just did, what files it read, what you discussed. It's a real working session, not isolated queries.

```
You:  read the README and summarize it
Bot:  [summarizes the project]

You:  now add an installation section
Bot:  [adds it — knows exactly which README you mean]
```

Use `/new` when you want a fresh start.

### Agents that evolve themselves

This is the part that feels like magic. Each agent has a set of config files it can **read and write on its own**:

| File | What it controls | How the agent uses it |
|------|------------------|-----------------------|
| `MEMORY.md` | Long-term knowledge | Saves user preferences, project decisions, accumulated know-how |
| `.system-prompt.md` | Its own personality and rules | Adjusts output style, adds domain-specific rules as it learns |
| `.agent-profile.yaml` | Preferences & behavior tuning | Adjusts output style, model selection, custom commands, notification prefs via MCP tools |
| `.dashboard-workstations.json` | Dashboard work categories | Adds new workstation icons when it discovers new task types |
| `.dashboard-tabs.json` | Custom dashboard tabs | Creates data views (tables, charts, HTML) for portfolio tracking, reports, analytics |
| `.schedules/*.yaml` | Scheduled tasks | Creates, modifies, or disables recurring tasks |
| `CLAUDE.md` | Its operating manual | Maintains a "custom rules" section with learned conventions |

The agent doesn't just execute tasks — it **adapts**. Ask it to do stock analysis a few times, and it starts remembering your preferred format, adding a "Stock" workstation to the dashboard, and tuning its system prompt for financial data. Ask it to monitor a service, and it sets up its own scheduled task.

You deploy a general-purpose agent. Over time, it becomes *your* agent.

```
Day 1:   "check AAPL stock price"        → generic response
Day 3:   "check AAPL"                    → remembers your format preference, adds 📈 workstation
Day 5:   agent tunes its own profile     → concise output style, Chinese language, faster streaming
Day 7:   agent has a morning briefing schedule, custom prompt for financial analysis,
         profile-driven model selection, portfolio tracking tab in dashboard,
         and MEMORY.md full of your portfolio context
```

### Memory that persists

Memory is persistent across sessions:

- **Long-term knowledge** — Claude manages its own auto-memory (`~/.claude/projects/.../memory/MEMORY.md`), accumulating decisions, preferences, and project context across all sessions
- **Task archive** — completed task outputs are archived to `.task-archive/` with Claude-generated summaries. Claude can query past results on demand via MCP tools (no blind context injection)

Ask Claude about something it did last week and it remembers.

### Watch it think in real time

The **Lobster Dashboard** gives you a live window into Claude's work:

- Streaming output as Claude types
- Thinking/reasoning blocks
- Token usage, context window, cost
- Task history with expandable details
- Per-agent lobster with workstation animations
- Custom tabs for agent-created data views (tables, charts, HTML reports)

The dashboard polls every second and shows exactly what Claude is doing right now — including its internal reasoning.

### Run multiple specialized agents

One server, many bots. Each gets its own WeCom identity, working directory, task queue, and isolated storage:

```yaml
wecom:
  - name: "backend"
    working_dir: "/projects/api-server"
    # ...
  - name: "frontend"
    working_dir: "/projects/web-app"
    # ...
```

Send backend tasks to one bot, frontend tasks to another. They work independently with zero cross-talk.

### No public URL needed

Most chat-to-CLI tools need ngrok or a public endpoint. lobster-cc uses an **autossh reverse tunnel** from your local box to a small always-on EC2 box — WeCom pushes messages to EC2:80, the tunnel forwards them to the lobster-cc server on your local box, no inbound port on your machine needed.

```
Phone → WeCom → EC2:80 → reverse SSH tunnel → lobster-cc on your box → Claude Code
                                                                          ↓
                                                                  Results back to your phone
```

### Self-configuration via MCP tools

Agents can read and modify their own configuration profile at runtime using built-in MCP tools (`get_agent_config`, `set_agent_config`, `list_agent_config`, `reset_agent_config`). Changes to output style, notification intervals, model selection, and custom commands are persisted in `.agent-profile.yaml` with a full audit trail.

### Claude can message you back

Via built-in MCP tools, Claude can proactively send you messages, images, and files:

```
You:  generate a chart of this week's metrics and send it to me
Bot:  [creates chart.png, then sends it as a WeCom image message]
```

This works for scheduled tasks too — set up a daily report and Claude sends results to your chat automatically.

### Natural language scheduling

No crontabs to write. Just describe what you want:

```
You:  every weekday at 9am, run the test suite and report failures
Bot:  [sets up the schedule — results delivered via WeCom]
```

### Send anything

Not just text — send images, voice messages, videos, and files. Claude sees them all:

```
You:  [sends screenshot of a UI bug]
Bot:  I see the issue — the modal is overflowing on mobile. Let me fix the CSS...
```

## How It Works

```
┌──────────┐  HTTP POST  ┌──────────────┐  reverse SSH   ┌─────────────────┐
│  You on  │────────────►│  EC2:80      │───tunnel─────► │  lobster-cc     │
│  WeCom   │             │  (always on) │                │  server (yours) │
│  📱      │◄────────────│              │                │                 │
└──────────┘   reply     └──────────────┘                │  Claude Code ←──┤
                                                         │  Dashboard   ←──┤
                                                         └─────────────────┘
```

1. You send a message in WeCom
2. WeCom POSTs the encrypted callback to your EC2 box on port 80
3. An autossh reverse SSH tunnel forwards EC2:80 → your local lobster-cc server
4. The server verifies the WeCom signature + 5-min timestamp freshness, decrypts, and dispatches to Claude Code
5. Results stream back to you via WeCom API — short replies inline, long ones as files

All crypto happens on your machine. EC2 is just an inbound port + tunnel forwarder. See
[ADR 0002](docs/architecture-decisions/0002-inline-callback-processing.md) and [docs/security.md](docs/security.md).

## Quick Start

### Prerequisites

- Python 3.11+
- [Claude Code CLI](https://docs.anthropic.com/en/docs/claude-code) installed and authenticated
- [WeCom](https://work.weixin.qq.com/) enterprise account with a custom app (自建应用)
- An always-on host with a public IP (e.g. a small EC2 box) — used as the inbound endpoint for WeCom callbacks via a reverse SSH tunnel

### 1. Install

```bash
git clone https://github.com/ahaNotAki/lobster-cc.git
cd lobster-cc
pip install -e .
```

### 2. Provision the always-on EC2 box (skip if you already have one)

Creates the instance, an Elastic IP, an SSH key, and a security group:

```bash
./scripts/setup-proxy.sh
```

`deploy.sh --proxy-ip <elastic-ip>` (later) sets up the autossh tunnels — both
the outbound SOCKS tunnel (for WeCom API calls) and the reverse `EC2:80 →
desktop:8080` tunnel that lets WeCom reach your lobster-cc server.

See [docs/aws-proxy.md](docs/aws-proxy.md) for the EC2 box,
[docs/security.md](docs/security.md) for the auth model, and
[ADR 0002](docs/architecture-decisions/0002-inline-callback-processing.md) for
why callbacks are processed inline (no separate relay).

### 3. Configure

```bash
lobster init
```

Interactive wizard — prompts for WeCom credentials, validates them, writes `config.yaml`.

### 4. Set up WeCom callback

In WeCom admin → Your App → 接收消息 → 设置API接收:
- **URL**: `http://<elastic-ip>/wecom/callback/<agent_id>` (port 80, served by the reverse tunnel)
- **Token** / **EncodingAESKey**: Must match `wecom.token` / `wecom.encoding_aes_key` in your `config.yaml`

### 5. Run

```bash
lobster -c config.yaml
```

Send a message to your bot. It should respond.

## Commands

| Command | What it does |
|---------|-------------|
| *any text* | Creates a task — Claude Code runs it |
| `/status` | Latest task status |
| `/cancel` | Cancel running task |
| `/list` | Recent tasks |
| `/new` | Fresh session (reset context) |
| `/cd <path>` | Switch working directory |
| `/output <id>` | Full output of a completed task |
| `/restart` | Restart Claude (reload MCP servers) |
| `/help` | Show all commands |

## Deployment

### Remote machine

```bash
./deploy.sh user@host [/remote/path]
```

Syncs code, installs deps, starts the server. Your `config.yaml` and database stay untouched.

### With fixed outbound IP

WeCom may require IP whitelisting. `setup-proxy.sh` provisions the EC2 proxy box:

```bash
# Provision the EC2 proxy (Elastic IP) if you don't have one yet:
./scripts/setup-proxy.sh

# Deploy with proxy tunnel:
./deploy.sh user@host /path \
    --proxy-ip <elastic-ip> \
    --proxy-key ~/.ssh/rc-proxy-key.pem
```

See [docs/aws-proxy.md](docs/aws-proxy.md) for details.

### Docker

```bash
# Create config.yaml first (lobster init or copy config.example.yaml)
cp config.example.yaml config.yaml
# Edit config.yaml with your credentials...

# Run — config.yaml is volume-mounted into the container
docker-compose up
```

> **Note**: Claude Code CLI must be available inside the container. The default Dockerfile does not include it — you'll need to mount it or install it in a custom image. For most users, running directly with `pip install` is simpler.

## Documentation

| | |
|---|---|
| [DESIGN.md](DESIGN.md) | Technical architecture and design decisions |
| [docs/architecture-decisions/](docs/architecture-decisions/) | ADRs (decision records) |
| [docs/security.md](docs/security.md) | Relay auth model, TLS decision, token rotation |
| [docs/aws-proxy.md](docs/aws-proxy.md) | Fixed outbound IP proxy guide |
| [docs/wecom-mcp.md](docs/wecom-mcp.md) | WeCom MCP tools for Claude |
| [REQUIREMENTS.md](REQUIREMENTS.md) | Requirements and milestones |
| [CONTRIBUTING.md](CONTRIBUTING.md) | Development setup and PR process |
| [CHANGELOG.md](CHANGELOG.md) | Version history |

## Development

```bash
pip install -e ".[dev]"
python -m pytest              # 338 tests
ruff check src/ tests/        # lint
```

## License

[MIT](LICENSE)
