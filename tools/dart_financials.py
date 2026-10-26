"""Collect and build point-in-time annual financials (explore v7 §2).

Collect the periodic-report list first (key from the environment):
  uv run python tools/dart_collect.py --type A --start 2019-01-01 --end 2026-10-20
Then fetch fnlttMultiAcnt for every listed company with an annual filing:
  uv run python tools/dart_financials.py collect --years 2018 2025 --root data/krx/raw
and build the JSONL for ``momentum-agent --financials``:
  uv run python tools/dart_financials.py build --root data/krx/raw --out data/krx/financials-v7

Saved batches are never fetched again. Keep outputs out of Git.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from momentum.krx.dart_collect import list_pages
from momentum.krx.dart_financials import (
    DartFinancialsFetcher,
    annual_filings,
    build_records,
    listed_corps,
    read_batches,
    write_records,
)
from momentum.krx.fsc_collect import CollectError


def main(argv: list[str] | None = None, **fetch_options: object) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("collect", "build"))
    parser.add_argument("--root", type=Path, default=Path("data/krx/raw"))
    parser.add_argument("--years", type=int, nargs=2, metavar=("FIRST", "LAST"))
    parser.add_argument("--out", type=Path)
    parser.add_argument("--pause", type=float, default=1.0)
    args = parser.parse_args(argv)
    pages = list_pages(args.root, "A")
    if not pages:
        print("dart-financials: no saved A-type list pages; run tools/dart_collect.py --type A first")
        return 1
    filings = annual_filings(pages)
    if args.action == "collect":
        if not args.years or args.years[0] > args.years[1] or args.pause < 0.1:
            parser.error("collect needs --years FIRST LAST and --pause >= 0.1")
        try:
            fetcher = DartFinancialsFetcher(
                args.root, pause=args.pause,
                progress=lambda m: print(f"dart-financials: {m}", file=sys.stderr, flush=True),
                **fetch_options,  # type: ignore[arg-type]
            )
            for year in range(args.years[0], args.years[1] + 1):
                corps = listed_corps(filings, year)
                counts = fetcher.fetch_year(year, corps)
                print(f"dart-financials: {year} {len(corps)} companies, "
                      f"{counts['fetched']} batches fetched, {counts['skipped']} skipped")
        except CollectError as exc:
            print(f"dart-financials: {exc}")
            return 1
        return 0
    if args.out is None:
        parser.error("build needs --out")
    try:
        records, report = build_records(read_batches(args.root), filings)
    except (ValueError, KeyError) as exc:
        print(f"dart-financials: {type(exc).__name__}: {exc}")
        return 1
    write_records(records, args.out, report)
    print(f"dart-financials: {len(records)} records, {report['companies']} company-years, "
          f"statements {report['statement']}, missing {report['missing']}, "
          f"fallback to API reception {report['fallback_api_reception']}, no symbol {report['no_symbol']}, "
          f"no period end {report['no_period_end']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
