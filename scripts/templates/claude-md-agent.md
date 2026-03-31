# {agent_name}

## Self-Configurable Files / 可自定义配置

You can edit these files to adjust your own behavior:

| File | Purpose | Description |
|------|---------|-------------|
| `.system-prompt.md` | System prompt injected before each task | Modify output style, response length, special rules |
| `.dashboard-workstations.json` | Dashboard workstation config | Add new workstations and keywords for new task types |
| `.dashboard-tabs.json` | Dashboard custom tabs | Create tabs to display data, charts, or custom HTML in the dashboard |
| `.schedules/*.yaml` | Scheduled task config | Modify prompts, enable/disable tasks |
| `MEMORY.md` | Long-term knowledge | User preferences, key decisions, project state |
| `.agent-profile.yaml` | Agent preferences | Output style, model selection, custom commands, notification prefs |
| This file `CLAUDE.md` | Operating manual | Maintain the "Custom Rules" section below |

## Self-Configuration Tools

You have MCP tools for managing your preferences:
- `get_agent_config` — view current settings
- `set_agent_config` — change a setting (with rationale for audit)
- `list_agent_config` — show all settings
- `reset_agent_config` — revert to defaults

## Output Rules / 输出规则

- Responses are read on WeCom mobile — keep them concise
- WeCom has limited markdown support: bold, links, lists work; tables and code highlighting do not
- For detailed content, save to a file and send via `send_wecom_file`

## Scheduled Task Format / 定时任务格式

Each `.yaml` file in `.schedules/` defines a scheduled task:

```yaml
name: Task name
schedule: "cron expression"
schedule_human: "Human-readable schedule description"
enabled: true
timeout: 1200
user_id: "YourUserID"          # used for {user_id} variable substitution
prompt: |
  Task prompt content...
  After completion, send results via send_wecom_message to user_id="{user_id}".
```

## Dashboard Custom Tabs / 自定义仪表盘标签

Create `.dashboard-tabs.json` to add custom data views to the dashboard:

```json
[
  {"id": "stocks", "label": "Portfolio", "type": "data", "source": "data/stocks.json"},
  {"id": "chart", "label": "Trend", "type": "chart", "source": "data/trend.json",
   "chart_options": {"chart_type": "line", "title": "Price Trend"}},
  {"id": "report", "label": "Report", "type": "html", "source": "reports/daily.html"}
]
```

**Tab types:**
- `data` — JSON array (renders as table) or JSON object (renders as key-value pairs). Override with `"template": "table"` or `"template": "key-value"`.
- `chart` — JSON file with `{labels: [...], datasets: [{label, data, color}]}`. Supports `line` and `bar` chart types via `chart_options`.
- `html` — HTML file rendered in a sandboxed iframe.

**Example: stock portfolio tab**
1. Write data: `echo '[{"ticker":"AAPL","price":150},{"ticker":"GOOG","price":2800}]' > data/stocks.json`
2. Register: add `{"id":"stocks","label":"Portfolio","type":"data","source":"data/stocks.json"}` to `.dashboard-tabs.json`

Source paths must be relative to working_dir or absolute paths within it. Files >1MB are rejected.

## Custom Rules / 自定义规则

(Rules learned during work — maintain this section yourself)

