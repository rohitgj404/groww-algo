"""Entry point: starts the trading engine and the dashboard server together.

    python run.py                 # dashboard on http://127.0.0.1:8080
    python run.py --port 9000
    python run.py --mode live     # requires Groww credentials in .env
"""

from __future__ import annotations

import argparse
import sys
import webbrowser

# Windows consoles default to cp1252, which cannot print the rupee sign used in
# logs and dashboard messages.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

import uvicorn

from algo.config import Settings


def main() -> int:
    parser = argparse.ArgumentParser(description="GrowwAlgo - automated Groww trading engine")
    parser.add_argument("--host", default=None, help="bind address (default 127.0.0.1)")
    parser.add_argument("--port", type=int, default=None, help="dashboard port (default 8080)")
    parser.add_argument("--mode", choices=["paper", "live"], default=None, help="trading mode")
    parser.add_argument("--open", action="store_true", help="open the dashboard in a browser")
    args = parser.parse_args()

    import os

    settings = Settings()
    host = args.host or settings.host
    port = args.port or settings.port or 8080
    if args.mode:
        settings.mode = args.mode
        # The server builds its own engine from env, so mirror the CLI choice.
        os.environ["TRADING_MODE"] = args.mode

    url = f"http://{host}:{port}"
    print("=" * 62)
    print("  GROWW ALGO  -  automated trading engine")
    print(f"  Dashboard : {url}")
    print(f"  Mode      : {(args.mode or settings.mode).upper()}")
    print(f"  Data      : {'Groww API' if settings.has_credentials else 'simulated (no credentials)'}")
    print("=" * 62)

    # Keep the in-app log banner consistent with the port actually bound.
    os.environ["PORT"] = str(port)
    os.environ["HOST"] = host

    if args.open:
        webbrowser.open(url)

    uvicorn.run("algo.server:app", host=host, port=port, log_level="info", access_log=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
