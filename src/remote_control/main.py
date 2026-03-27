"""Entry point for the remote control server."""

import argparse
import logging
import sys

from aiohttp import web

from remote_control.config import load_config
from remote_control.server import create_app

logger = logging.getLogger(__name__)


def main() -> None:
    parser = argparse.ArgumentParser(description="Remote Control - Claude Code via WeCom")
    subparsers = parser.add_subparsers(dest="command")

    # 'init' subcommand — interactive config generator
    subparsers.add_parser("init", help="Interactively generate a config.yaml file")

    # Server arguments (used when no subcommand is given)
    parser.add_argument(
        "-c", "--config",
        default="config.yaml",
        help="Path to config file (default: config.yaml)",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Log level (default: INFO)",
    )
    args = parser.parse_args()

    # Handle 'init' subcommand
    if args.command == "init":
        from remote_control.cli_init import init_config

        init_config()
        return

    # Default: start the server
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    try:
        config = load_config(args.config)
    except (FileNotFoundError, ValueError) as e:
        logging.error("Failed to load config: %s", e)
        sys.exit(1)

    app = create_app(config)
    web.run_app(app, host=config.server.host, port=config.server.port)


if __name__ == "__main__":
    main()
