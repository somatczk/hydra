"""``python -m hydra.trader``: run the trader process, or ``check`` (the Docker healthcheck)."""

from __future__ import annotations

import asyncio
import sys

from hydra.trader.main import check, main

if __name__ == "__main__":
    command = sys.argv[1] if len(sys.argv) > 1 else "run"
    if command == "check":
        sys.exit(check())
    if command != "run":
        print("usage: python -m hydra.trader [run|check]", file=sys.stderr)
        sys.exit(2)
    asyncio.run(main())
