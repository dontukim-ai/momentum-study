"""Build price rows and security states for one market ETF benchmark, offline.

Example (after collecting ETF days with tools/fsc_collect.py --kind etf):
  uv run python tools/benchmark_etf.py --fsc-root data/krx/raw/fsc \
    --calendar data/krx/calendar-all --symbol 069500 --expect-index "코스피 200" \
    --out data/krx/benchmark-etf

Writes prices.csv (accepted rows of that ETF), securities.jsonl and report.json.
Append them to a copy of the research input; never to the original. The index
name must match every quote of the symbol exactly. Keep outputs out of Git.
"""

from __future__ import annotations

import argparse
import csv
import json
from datetime import date
from pathlib import Path

from trading_agent.data import CSV_FIELDS

from momentum.config import SpecUndefinedError
from momentum.krx.benchmark_etf import build_benchmark_etf


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fsc-root", type=Path, default=Path("data/krx/raw/fsc"))
    parser.add_argument("--calendar", type=Path, required=True)
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--expect-index", required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        sessions = tuple(
            date.fromisoformat(d)
            for d in json.loads((args.calendar / "calendar.json").read_text())
        )
        rows, records, report = build_benchmark_etf(
            args.fsc_root, sessions, args.symbol, args.expect_index
        )
    except (SpecUndefinedError, OSError, ValueError, KeyError) as exc:
        print(f"benchmark-etf: {type(exc).__name__}: {exc}")
        return 1
    args.out.mkdir(parents=True, exist_ok=True)
    with (args.out / "prices.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    with (args.out / "securities.jsonl").open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    (args.out / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(
        f"benchmark-etf: {args.symbol} rows={len(rows)} events={len(records)} "
        f"quarantined={report['quarantined']} index={args.expect_index!r}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
