"""Supported local entry point; deliberately no configurable public bind address."""
import argparse
import os
from pathlib import Path

import uvicorn

from .contracts import ROOT
from .http import create_app


def main():
    parser = argparse.ArgumentParser(description="Single-user local companion (loopback only)")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--data-dir", type=Path, default=ROOT / "be" / ".data")
    args = parser.parse_args()
    data_dir = args.data_dir.resolve()
    if not data_dir.is_relative_to(ROOT):
        parser.error("--data-dir must stay under the workspace root")
    if not 1024 <= args.port <= 65535:
        parser.error("--port must be between 1024 and 65535")
    os.umask(0o077)
    # No reload, multiple workers, proxy headers, access payload logs, or public preview.
    uvicorn.run(create_app(data_dir), host="127.0.0.1", port=args.port,
                workers=1, proxy_headers=False, access_log=False, log_level="warning")


if __name__ == "__main__":
    main()
