"""Interactive config.yaml generator for the `lobster init` command."""

import asyncio
import os
import sys
from pathlib import Path

import httpx

CONFIG_TEMPLATE = """\
wecom:
  name: "{name}"
  corp_id: "{corp_id}"
  agent_id: {agent_id}
  secret: "{secret}"
  token: "{token}"
  encoding_aes_key: "{encoding_aes_key}"
  mode: "{mode}"
  relay_url: "{relay_url}"

agent:
  claude_command: "claude"
  default_working_dir: "{working_dir}"
  task_timeout_seconds: 1800
  max_output_length: 4000

server:
  host: "0.0.0.0"
  port: 8080

storage:
  db_path: "./remote_control.db"

notifications:
  progress_interval_seconds: 30

memory:
  enabled: true

dashboard:
  enabled: false
  password: ""
  secret: "change-me-to-random-string"
"""


def _prompt(label: str, *, default: str = "", secret: bool = False) -> str:
    """Prompt the user for input with an optional default."""
    suffix = f" [{default}]" if default else ""
    try:
        value = input(f"  {label}{suffix}: ").strip()
    except (EOFError, KeyboardInterrupt):
        print("\nAborted.")
        sys.exit(1)
    return value or default


async def _validate_credentials(corp_id: str, secret: str) -> tuple[bool, str]:
    """Validate WeCom credentials by fetching an access token.

    Returns (success, message).
    """
    url = "https://qyapi.weixin.qq.com/cgi-bin/gettoken"
    params = {"corpid": corp_id, "corpsecret": secret}
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(url, params=params)
            data = resp.json()
    except httpx.HTTPError as e:
        return False, f"HTTP error: {e}"

    errcode = data.get("errcode", -1)
    if errcode == 0:
        return True, "Access token obtained successfully."
    return False, f"WeCom API error {errcode}: {data.get('errmsg', 'unknown')}"


def init_config() -> None:
    """Interactively generate a config.yaml file."""
    output_path = Path("config.yaml")

    print("\n  Lobster Init — generate config.yaml\n")

    # Warn if config.yaml already exists
    if output_path.exists():
        overwrite = _prompt("config.yaml already exists. Overwrite? (y/N)", default="N")
        if overwrite.lower() not in ("y", "yes"):
            print("  Aborted. Existing config.yaml left unchanged.")
            return

    # Collect WeCom credentials
    print("  WeCom Credentials:")
    corp_id = _prompt("  Corp ID")
    agent_id = _prompt("  Agent ID (numeric)", default="1000002")
    secret = _prompt("  Agent Secret")
    token = _prompt("  Callback Token")
    encoding_aes_key = _prompt("  Encoding AES Key (43 chars)")
    name = _prompt("  Agent name (label)", default="my-agent")

    # Message source mode
    mode = _prompt("  Mode (relay / callback)", default="relay")
    relay_url = ""
    if mode == "relay":
        relay_url = _prompt("  Relay URL")

    # Working directory
    default_wd = os.getcwd()
    working_dir = _prompt("  Working directory", default=default_wd)

    # Validate agent_id is numeric
    try:
        agent_id_int = int(agent_id)
    except ValueError:
        print(f"\n  Error: Agent ID must be numeric, got '{agent_id}'")
        sys.exit(1)

    # Validate credentials
    print("\n  Validating WeCom credentials...")
    success, message = asyncio.run(_validate_credentials(corp_id, secret))
    if success:
        print(f"  OK: {message}")
    else:
        print(f"  Warning: {message}")
        proceed = _prompt("  Continue anyway? (y/N)", default="N")
        if proceed.lower() not in ("y", "yes"):
            print("  Aborted.")
            return

    # Write config
    content = CONFIG_TEMPLATE.format(
        name=name,
        corp_id=corp_id,
        agent_id=agent_id_int,
        secret=secret,
        token=token,
        encoding_aes_key=encoding_aes_key,
        mode=mode,
        relay_url=relay_url,
        working_dir=working_dir,
    )

    output_path.write_text(content)
    print(f"\n  Config written to {output_path.resolve()}")
    print("  Run 'lobster -c config.yaml' to start the server.\n")
