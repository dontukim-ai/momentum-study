"""Build the observed KRX session calendar from collected FSC stock days.

Example:
  uv run python tools/fsc_calendar.py --root data/krx/raw/fsc \
    --start 2024-01-02 --end 2024-06-28 --out data/krx/calendar

Writes calendar.json (ISO session dates through the last confirmed weekday,
the research input format) and calendar-report.json (closed weekdays, pending
and uncollected days, rows per session).
"""

from __future__ import annotations

import argparse
import json
from datetime import date
from pathlib import Path

from momentum.config import SpecUndefinedError
from momentum.krx.fsc_calendar import build_calendar


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path("data/krx/raw/fsc"))
    parser.add_argument("--start", type=date.fromisoformat, required=True)
    parser.add_argument("--end", type=date.fromisoformat, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        result = build_calendar(args.root, args.start, args.end)
    except (SpecUndefinedError, OSError, ValueError, KeyError, IndexError) as exc:
        print(f"fsc-calendar: {type(exc).__name__}: {exc}")
        return 1
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "calendar.json").write_text(
        json.dumps([d.isoformat() for d in result.sessions]) + "\n", encoding="utf-8"
    )
    (args.out / "calendar-report.json").write_text(
        json.dumps(result.report(), indent=2) + "\n", encoding="utf-8"
    )
    print(
        f"fsc-calendar: {len(result.sessions)} sessions through {result.confirmed_through}, "
        f"{len(result.closed)} closed weekdays, {len(result.pending)} pending, "
        f"{len(result.uncollected)} uncollected, "
        f"{len(result.before_coverage)} before coverage"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
