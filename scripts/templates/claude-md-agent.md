# {agent_name}

## Self-Configurable Files / 可自定义配置

You can edit these files to adjust your own behavior:

| File | Purpose | Description |
|------|---------|-------------|
| `.system-prompt.md` | System prompt injected before each task | Modify output style, response length, special rules |
| `.dashboard-workstations.json` | Dashboard workstation config | Add new workstations and keywords for new task types |
| `.schedules/*.yaml` | Scheduled task config | Modify prompts, enable/disable tasks |
| `MEMORY.md` | Long-term knowledge | User preferences, key decisions, project state |
| This file `CLAUDE.md` | Operating manual | Maintain the "Custom Rules" section below |

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

## Custom Rules / 自定义规则

(Rules learned during work — maintain this section yourself)

