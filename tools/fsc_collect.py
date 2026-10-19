"""Collect FSC V2 daily quotes for a date range (read-only, resumable).

Example (key from the environment, never on the command line):
  uv run python tools/fsc_collect.py --kind stock --start 2024-01-02 --end 2024-06-28 \
    --root data/krx/raw/fsc

Each weekday becomes <root>/<kind>/<YYYY-MM-DD>/ with untouched pages and
fetch.json. Completed days are skipped on rerun. Supplier data may not be
redistributed; keep the root out of Git (data/krx/ is ignored).
"""

from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

from momentum.krx.fsc_collect import ENDPOINTS, CollectError, Collector


def main(argv: list[str] | None = None, **collector_options: object) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kind", choices=sorted(ENDPOINTS), required=True)
    parser.add_argument("--start", type=date.fromisoformat, required=True)
    parser.add_argument("--end", type=date.fromisoformat, required=True)
    parser.add_argument("--root", type=Path, default=Path("data/krx/raw/fsc"))
    parser.add_argument("--pause", type=float, default=1.0)
    parser.add_argument(
        "--keep-going", action="store_true", help="continue after a failed day"
    )
    args = parser.parse_args(argv)
    if args.end < args.start or args.pause < 0.1:
        parser.error("need start <= end and --pause >= 0.1")
    try:
        collector = Collector(
            args.root,
            args.kind,
            pause=args.pause,
            progress=lambda m: print(f"fsc-collect: {m}", file=sys.stderr, flush=True),
            **collector_options,  # type: ignore[arg-type]
        )
    except CollectError as exc:
        print(f"fsc-collect: {exc}")
        return 1
    results = collector.collect(args.start, args.end, stop_on_error=not args.keep_going)
    for result in results:
        if result.status == "error":
            print(f"fsc-collect: {result.day} error: {result.message}", file=sys.stderr)
    counts = {
        s: sum(r.status == s for r in results) for s in ("fetched", "skipped", "error")
    }
    print(
        f"fsc-collect: {counts['fetched']} fetched, {counts['skipped']} skipped, "
        f"{counts['error']} errors"
    )
    return 1 if counts["error"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
