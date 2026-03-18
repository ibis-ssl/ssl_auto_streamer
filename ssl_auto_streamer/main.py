# Copyright (c) 2026 ibis-ssl
#
# Use of this source code is governed by an MIT-style
# license that can be found in the LICENSE file or at
# https://opensource.org/licenses/MIT.

"""SSL Auto Streamer - Entry Point."""

import argparse
import asyncio
import logging
import os
import signal
from pathlib import Path
from typing import Any, Dict, Optional

import yaml


def load_config(args: argparse.Namespace) -> Dict[str, Any]:
    """Load config from YAML file and merge with CLI arguments."""
    config_path = Path(args.config)
    if not config_path.is_absolute():
        # Resolve relative to CWD or package root
        if not config_path.exists():
            config_path = Path(__file__).parent.parent / args.config

    config: Dict[str, Any] = {}
    if config_path.exists():
        with open(config_path, "r", encoding="utf-8") as f:
            config = yaml.safe_load(f) or {}
        logging.getLogger(__name__).info(f"Loaded config from {config_path}")
    else:
        logging.getLogger(__name__).warning(
            f"Config file not found: {config_path}, using defaults"
        )

    # Ensure nested dicts exist
    config.setdefault("gemini", {})
    config.setdefault("audio", {})
    config.setdefault("ssl", {})
    config.setdefault("commentary", {})
    config.setdefault("web", {})

    # Override with CLI arguments
    if args.gemini_api_key:
        config["gemini"]["api_key"] = args.gemini_api_key
    elif not config["gemini"].get("api_key"):
        config["gemini"]["api_key"] = os.environ.get("GEMINI_API_KEY", "")

    if args.our_team_color:
        config["ssl"]["our_team_color"] = args.our_team_color
    if args.our_team_name:
        config["ssl"]["our_team_name"] = args.our_team_name
    if args.tracker_addr:
        config["ssl"]["tracker_addr"] = args.tracker_addr
    if args.tracker_port:
        config["ssl"]["tracker_port"] = args.tracker_port
    if args.gc_addr:
        config["ssl"]["gc_addr"] = args.gc_addr
    if args.gc_port:
        config["ssl"]["gc_port"] = args.gc_port

    if args.web_port is not None:
        if args.web_port == 0:
            config["web"]["enabled"] = False
        else:
            config["web"]["port"] = args.web_port

    return config


def main() -> None:
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="SSL Auto Streamer - RoboCup SSL real-time AI commentary"
    )
    parser.add_argument(
        "--config",
        default="config/config.yaml",
        help="Path to config YAML file (default: config/config.yaml)",
    )
    parser.add_argument(
        "--gemini-api-key",
        default=None,
        help="Gemini API key (overrides config and GEMINI_API_KEY env var)",
    )
    parser.add_argument(
        "--our-team-color",
        choices=["blue", "yellow"],
        default=None,
        help="Our team color (default: blue)",
    )
    parser.add_argument(
        "--our-team-name",
        default=None,
        help="Our team name for initial context (default: ibis)",
    )
    parser.add_argument(
        "--tracker-addr",
        default=None,
        help="SSL Vision Tracker multicast address (default: 224.5.23.2)",
    )
    parser.add_argument(
        "--tracker-port",
        type=int,
        default=None,
        help="SSL Vision Tracker port (default: 10010)",
    )
    parser.add_argument(
        "--gc-addr",
        default=None,
        help="SSL Game Controller multicast address (default: 224.5.23.1)",
    )
    parser.add_argument(
        "--gc-port",
        type=int,
        default=None,
        help="SSL Game Controller port (default: 10003)",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Logging level (default: INFO)",
    )
    parser.add_argument(
        "--web-port",
        type=int,
        default=None,
        help="Web UI port (default: 8080, 0 to disable)",
    )

    args = parser.parse_args()

    # Configure logging
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    config = load_config(args)

    from ssl_auto_streamer.app import CommentaryApp

    app = CommentaryApp(config)

    # Handle Ctrl+C gracefully
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    def _shutdown(sig, frame):
        logging.getLogger(__name__).info(f"Received signal {sig}, shutting down...")
        for task in asyncio.all_tasks(loop):
            task.cancel()

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    try:
        loop.run_until_complete(app.run())
    except asyncio.CancelledError:
        pass
    except KeyboardInterrupt:
        pass
    finally:
        loop.run_until_complete(app.shutdown())
        loop.close()


if __name__ == "__main__":
    main()
