"""Run the self-hosted WeCom relay: python -m remote_control.relay"""

import logging
import os

from aiohttp import web

from remote_control.relay.app import RelayConfig, create_relay_app


def main() -> None:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    config = RelayConfig.from_env()
    app = create_relay_app(config, run_purge=True)
    host = os.environ.get("RELAY_HOST", "0.0.0.0")
    port = int(os.environ.get("RELAY_PORT", "8443"))
    web.run_app(app, host=host, port=port)


if __name__ == "__main__":
    main()
