"""Collect OpenDART disclosure-list pages for a date range (read-only, resumable).

Example (key from the environment):
  uv run python tools/dart_collect.py --type I --start 2024-01-01 --end 2024-06-30 \
    --root data/krx/raw

Writes <root>/dart_list/<type>/<start>_<end>/ per period of at most 92 days.
Completed periods are skipped on rerun. Keep the root out of Git.
"""

from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

from momentum.krx.dart_collect import DartListCollector
from momentum.krx.fsc_collect import CollectError


def main(argv: list[str] | None = None, **collector_options: object) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--type", default="I", help="pblntf_ty, e.g. I (exchange) or B")
    parser.add_argument("--start", type=date.fromisoformat, required=True)
    parser.add_argument("--end", type=date.fromisoformat, required=True)
    parser.add_argument("--root", type=Path, default=Path("data/krx/raw"))
    parser.add_argument("--pause", type=float, default=1.0)
    args = parser.parse_args(argv)
    if args.end < args.start or args.pause < 0.1:
        parser.error("need start <= end and --pause >= 0.1")
    try:
        collector = DartListCollector(
            args.root,
            args.type,
            pause=args.pause,
            progress=lambda m: print(f"dart-collect: {m}", file=sys.stderr, flush=True),
            **collector_options,  # type: ignore[arg-type]
        )
    except CollectError as exc:
        print(f"dart-collect: {exc}")
        return 1
    results = collector.collect(args.start, args.end)
    for result in results:
        if result.status == "error":
            print(
                f"dart-collect: {result.start}~{result.end} error: {result.message}",
                file=sys.stderr,
            )
    counts = {
        s: sum(r.status == s for r in results) for s in ("fetched", "skipped", "error")
    }
    rows = sum(r.total_count or 0 for r in results)
    print(
        f"dart-collect: {counts['fetched']} periods fetched ({rows} disclosures), "
        f"{counts['skipped']} skipped, {counts['error']} errors"
    )
    return 1 if counts["error"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
