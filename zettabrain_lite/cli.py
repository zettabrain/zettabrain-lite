"""ZettaBrain Lite — CLI entry points."""

import argparse
import sys
from pathlib import Path

from . import __version__
from .config import BASE_DIR, DATA_DIR

PKG_DIR = Path(__file__).parent


def _banner():
    print(f"\n{'=' * 54}")
    print(f"  ZettaBrain Lite  v{__version__}")
    print("  Local AI — RAG + Skills — your data stays private")
    print(f"{'=' * 54}\n")


def server_cmd():
    """Launch the ZettaBrain Lite web server."""
    import importlib.util

    _banner()

    if importlib.util.find_spec("uvicorn") is None:
        print("ERROR: uvicorn not installed. pip install uvicorn")
        sys.exit(1)

    parser = argparse.ArgumentParser(prog="zettabrain-lite", description="Launch ZettaBrain Lite")
    parser.add_argument("--host", default="0.0.0.0", help="Bind host (default: 0.0.0.0)")
    parser.add_argument("--port", default=7860, type=int, help="Port (default: 7860)")
    parser.add_argument("--reload", action="store_true", help="Dev mode: auto-reload")
    args, _ = parser.parse_known_args()

    BASE_DIR.mkdir(parents=True, exist_ok=True)
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    print("  Starting ZettaBrain Lite...")
    print(f"  Open in browser: http://localhost:{args.port}")
    print("  Press Ctrl+C to stop.\n")

    import uvicorn

    uvicorn.run(
        "zettabrain_lite.server:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
        log_level="warning",
    )


def main():
    """Main entry point — launches server by default."""
    server_cmd()
