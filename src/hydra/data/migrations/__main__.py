"""``python -m hydra.data.migrations upgrade|current|check|heads`` (DSN from DATABASE_URL)."""

from __future__ import annotations

import argparse
import asyncio
import os
import sys

from hydra.data.migrations.runner import (
    DSN_ENV,
    MigrationError,
    current_revision,
    head_revision,
    heads,
    upgrade_to_head,
)


def _dsn() -> str:
    dsn = os.environ.get(DSN_ENV)
    if not dsn:
        raise MigrationError(f"{DSN_ENV} is not set")
    return dsn


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m hydra.data.migrations")
    parser.add_argument("command", choices=["upgrade", "current", "check", "heads"])
    args = parser.parse_args(argv)
    try:
        if args.command == "heads":
            found = heads()
            print(" ".join(found))
            head_revision()
            return 0
        dsn = _dsn()
        if args.command == "upgrade":
            upgrade_to_head(dsn)
        current = asyncio.run(current_revision(dsn))
        head = head_revision()
        print(f"current={current or 'none'} head={head}")
        return 1 if args.command == "check" and current != head else 0
    except MigrationError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
