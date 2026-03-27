# {agent_name}

## 可自定义配置

以下文件你可以直接用 Edit/Write 工具编辑来调整自己的行为：

| 文件 | 用途 | 说明 |
|------|------|------|
| `.system-prompt.md` | 每次任务前注入的提示 | 修改输出风格、回复长度、特殊规则 |
| `.dashboard-workstations.json` | Dashboard 工位配置 | 新任务类型时添加工位和关键词 |
| `.schedules/*.yaml` | 定时任务配置 | 修改 prompt、启用/禁用任务 |
| `MEMORY.md` | 长期知识 | 用户偏好、重要决策、项目状态 |
| 本文件 `CLAUDE.md` | 操作手册 | 修改下方"自定义规则"区域 |

## 输出规则

- 回复通过企微手机端阅读，保持简洁
- 企微 markdown 支持有限：支持加粗、链接、列表；不支持表格、代码高亮
- 详细内容保存文件后用 send_wecom_file 发送

## 定时任务格式

`.schedules/` 目录下每个 `.yaml` 文件定义一个定时任务：

```yaml
name: 任务名称
schedule: "cron 表达式"
schedule_human: "人类可读的时间描述"
enabled: true
timeout: 1200
user_id: "YourUserID"          # 用于 {user_id} 变量替换
prompt: |
  任务 prompt 内容...
  完成后用 send_wecom_message 发送给 user_id="{user_id}"。
```

## 自定义规则

(在工作中学到的规则，请自行维护这个区域)

