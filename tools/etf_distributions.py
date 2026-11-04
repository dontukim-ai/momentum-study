"""Add 069500's distributions from a hand-made file to a research input (v31 benchmark, 2026-11-04).

Mac, no network:
  uv run python tools/etf_distributions.py --file data/krx/kind/069500-distributions.csv \
    --calendar <tools/fsc_calendar.py --out> --research data/krx/v2-2026-11-04e/research \
    --out data/krx/v2-2026-11-04e/research-dist

The file format and rules are in momentum.krx.etf_distributions. Writes a new research directory (the
three other files copied, actions with the distributions added) and never overwrites one. Prints one row
per distribution and the count per year, so a missing year shows. Keep outputs out of Git.
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from datetime import date
from pathlib import Path

from momentum.config import SpecUndefinedError
from momentum.krx.etf_distributions import build, read_rows, with_distributions


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--file", type=Path, required=True)
    parser.add_argument("--calendar", type=Path, required=True)
    parser.add_argument("--research", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--symbol", default="069500")
    args = parser.parse_args(argv)
    try:
        sessions = tuple(date.fromisoformat(d) for d in json.loads((args.calendar / "calendar.json").read_text()))
        report = json.loads((args.calendar / "calendar-report.json").read_text())
        actions = build(read_rows(args.file), args.symbol, sessions, date.fromisoformat(report["confirmed_through"]))
        added = with_distributions(args.research, actions, args.out)
    except (SpecUndefinedError, OSError, ValueError, KeyError) as exc:
        print(f"etf-distributions: {type(exc).__name__}: {exc}")
        return 1
    for a in actions:
        print(f"  {a['security_id']} record {a['evidence']['record_date']} ex {a['ex_date']} pay {a['pay_date']} "
              f"{a['cash_amount']} won, public {a['available_at']}")
    years = Counter(a["ex_date"][:4] for a in actions)
    print(f"etf-distributions: {added} added to {args.out}; per year {dict(sorted(years.items()))}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
