"""Dev-server entrypoint. Wraps `uvicorn.run` with timeouts tuned so Ctrl+C
exits in <1s instead of hanging on "shutting down" while idle keep-alive
connections drain.

Usage:
    python -m backend.run                     # reload, port 8000
    python -m backend.run --port 8001         # custom port
    python -m backend.run --no-reload         # production-like

Why this exists:
    The default `uvicorn` shutdown waits up to 5s for in-flight requests AND
    a separate 5s for keep-alive connections to close — on Windows the
    --reload supervisor adds another delay propagating SIGINT to the worker.
    For a single-user dev workflow that's all dead weight; we squash the
    timeouts to 1s/2s so Ctrl+C feels instant.
"""
from __future__ import annotations
import argparse
import sys

import uvicorn


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run the TileClass FastAPI app with dev-friendly shutdown timeouts.",
    )
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument(
        "--no-reload", dest="reload", action="store_false",
        help="Disable auto-reload (closer to production behavior).",
    )
    parser.set_defaults(reload=True)
    parser.add_argument(
        "--log-level", default="info",
        choices=["critical", "error", "warning", "info", "debug", "trace"],
    )
    args = parser.parse_args(argv)

    uvicorn.run(
        "backend.main:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
        log_level=args.log_level,
        # 1s budget for in-flight requests to finish on SIGINT. Dev workflow:
        # nothing important is mid-write that we'd want to wait for.
        timeout_graceful_shutdown=1,
        # 2s idle keep-alive; without this, a browser tab idling on the page
        # holds the TCP connection open and Ctrl+C waits for it to time out.
        timeout_keep_alive=2,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
