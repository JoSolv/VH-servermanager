#!/usr/bin/env python3
"""Entry point for the Valheim Server Manager.

    python run.py                      # http://127.0.0.1:8080
    python run.py --host 0.0.0.0       # reachable on the LAN (see the warning)
    VHSM_FAKE_SERVER=1 python run.py   # development mode, no Steam download
"""

from __future__ import annotations

import argparse
import logging
import sys

import uvicorn

from vhsm.config import settings


def main() -> int:
    parser = argparse.ArgumentParser(description="Valheim Server Manager")
    parser.add_argument("--host", default=settings.host, help="bind address")
    parser.add_argument("--port", type=int, default=settings.port, help="bind port")
    parser.add_argument("--data-root", default=None, help="override the data directory")
    parser.add_argument("--fake-server", action="store_true",
                        help="launch a simulated server instead of the real binary")
    parser.add_argument("--reload", action="store_true", help="auto-reload on code changes")
    parser.add_argument("--log-level", default="info")
    args = parser.parse_args()

    logging.basicConfig(
        level=args.log_level.upper(),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )

    if args.data_root:
        from pathlib import Path

        settings.data_root = Path(args.data_root).expanduser().resolve()
    if args.fake_server:
        settings.fake_server = True
    settings.host, settings.port = args.host, args.port
    settings.ensure_dirs()

    if args.host not in ("127.0.0.1", "localhost", "::1"):
        logging.warning(
            "Binding to %s with NO AUTHENTICATION. Anyone who can reach this port can "
            "start processes on this machine. Put it behind an authenticating reverse "
            "proxy, or bind to 127.0.0.1 and use an SSH tunnel.",
            args.host,
        )

    logging.info("data root: %s", settings.data_root)
    logging.info("listening on http://%s:%s", args.host, args.port)

    uvicorn.run(
        "vhsm.web.app:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
        log_level=args.log_level,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
