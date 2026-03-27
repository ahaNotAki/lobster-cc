# lobster-cc 🦞

Control [Claude Code](https://docs.anthropic.com/en/docs/claude-code) from your phone via [WeCom (企业微信)](https://work.weixin.qq.com/). Send coding tasks, get streaming progress, receive results — all while away from your dev machine.

## Features

- **WeCom Integration** — Send tasks from your phone, receive streaming results in chat
- **Multi-Agent** — Run multiple bots with independent executors and isolated stores
- **Relay Mode** — AWS Lambda relay, no public URL needed locally
- **Dashboard** — Lobster aquarium WebUI with real-time streaming output and thinking
- **Session Context** — Continuous conversation via Claude Code `--session-id`
- **Persistent Memory** — Dual-layer: Claude's native MEMORY.md + SQLite keyword recall
- **Media Support** — Send images, voice, video, files for Claude to analyze
- **Scheduling** — Natural language scheduling via Claude Code scheduler plugin
- **WeCom MCP Tools** — Claude can send messages/images/files back to WeCom
- **Process Watchdog** — Safety net for runaway processes

## How It Works

```
You (WeCom app)  →  AWS Lambda relay  ←  Local server (polls)  →  Claude Code CLI
     ↑                                     |       |
     └──────────── WeCom API (replies) ────┘       └── Dashboard WebUI
```

1. You send a message in WeCom
2. WeCom pushes it to an AWS Lambda relay (stored in DynamoDB)
3. Your local server polls the relay, decrypts, and runs it through Claude Code CLI
4. Results stream back to you via WeCom API

## Quick Start

### Prerequisites

- **Python 3.11+**
- **Claude Code CLI** installed and authenticated (`claude --version`)
- **WeCom enterprise account** with a custom app (自建应用)
- **AWS account** for the relay (Lambda + DynamoDB + API Gateway)

### 1. Install

```bash
git clone https://github.com/anthropics/lobster-cc.git
cd lobster-cc
pip install -e .
```

### 2. Deploy the AWS Relay

```bash
cd relay
sam build && sam deploy --guided
```

This creates the Lambda + API Gateway + DynamoDB in one command. See [relay/README.md](relay/README.md) for details.

### 3. Configure

```bash
lobster init
```

Interactive wizard that prompts for WeCom credentials and validates them. Or manually:

```bash
cp config.example.yaml config.yaml
# Edit with your WeCom Corp ID, Agent ID, Secret, Token, AES Key, relay URL
```

### 4. Configure WeCom Callback

In WeCom admin console → Your App → 接收消息 → 设置API接收:
- **URL**: Your API Gateway endpoint from step 2
- **Token** / **EncodingAESKey**: Must match both Lambda env vars and config.yaml

### 5. Run

```bash
lobster -c config.yaml
```

Send a message to your WeCom bot — it should respond!

## Usage

Send any text to create a coding task. Use slash commands for control:

| Command | Description |
|---------|-------------|
| `/status` | Show latest task status |
| `/cancel` | Cancel running task |
| `/list` | List recent tasks |
| `/new` | Start fresh session (reset context) |
| `/cd <path>` | Change working directory |
| `/memory` | View memory stats |
| `/help` | Show all commands |

### Session Context

Messages share context within a session:

```
You:  read src/config.py and explain the structure
Bot:  [explains the config module]

You:  add a new field "max_retries" with default 3
Bot:  [adds the field, knowing exactly which file you mean]
```

### Multi-Agent Setup

Run multiple bots from a single server — each with its own executor, store, and working directory:

```yaml
wecom:
  - name: "coding"
    agent_id: 1000002
    working_dir: "/path/to/project-a"
    # ... credentials
  - name: "review"
    agent_id: 1000003
    working_dir: "/path/to/project-b"
    # ... credentials
```

### Deploy to Remote Machine

```bash
./deploy.sh user@host [/remote/path]

# With fixed outbound IP (WeCom IP whitelist):
./deploy.sh user@host /path --proxy-ip <elastic-ip> --proxy-key ~/.ssh/rc-proxy-key.pem
```

### Docker

```bash
docker-compose up  # mount config.yaml via volume
```

## Documentation

| Doc | Description |
|-----|-------------|
| [DESIGN.md](DESIGN.md) | Full technical design and architecture |
| [REQUIREMENTS.md](REQUIREMENTS.md) | Requirements and milestones |
| [relay/README.md](relay/README.md) | AWS relay setup (SAM + manual) |
| [docs/aws-proxy.md](docs/aws-proxy.md) | Fixed outbound IP proxy setup |
| [docs/wecom-mcp.md](docs/wecom-mcp.md) | WeCom MCP server for Claude |
| [CONTRIBUTING.md](CONTRIBUTING.md) | Development setup and PR process |
| [CHANGELOG.md](CHANGELOG.md) | Version history |

## Development

```bash
pip install -e ".[dev]"
python -m pytest              # run all tests
ruff check src/ tests/        # lint
```

## License

[MIT](LICENSE)
