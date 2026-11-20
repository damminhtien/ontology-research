#!/usr/bin/env python3
"""Start the loopback console with a persistent default admin token."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if REPO_ROOT.as_posix() not in sys.path:
    sys.path.insert(0, REPO_ROOT.as_posix())

from foundry.console.local_auth import ensure_local_console_token  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8787)
    args = parser.parse_args()

    ensure_local_console_token()
    import uvicorn

    uvicorn.run("foundry.console.app:app", host="127.0.0.1", port=args.port)


if __name__ == "__main__":
    main()
