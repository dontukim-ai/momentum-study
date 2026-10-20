"""Build cash-dividend corporate actions from parsed decisions and the calendar.

Example:
  uv run python tools/dividend_actions.py --dividends data/krx/dividends/dividends.jsonl \
    --calendar data/krx/calendar --out data/krx/actions-dividends

Writes actions.json (research --actions format, cash dividends, each with
its evidence and payment-date basis) and dividend-actions-report.json
(decisions that produced no action and why). Policy A for missing payment
dates is documented in momentum.krx.dividend_actions.
"""

from __future__ import annotations

import argparse
import json
from datetime import date
from pathlib import Path

from momentum.krx.dividend_actions import build


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dividends", type=Path, required=True)
    parser.add_argument(
        "--calendar", type=Path, required=True, help="tools/fsc_calendar.py --out"
    )
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        records = [
            json.loads(line)
            for line in args.dividends.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        sessions = tuple(
            date.fromisoformat(d)
            for d in json.loads((args.calendar / "calendar.json").read_text())
        )
        report = json.loads((args.calendar / "calendar-report.json").read_text())
        if not report.get("confirmed_through"):
            raise ValueError("calendar has no confirmed range")
        confirmed = date.fromisoformat(report["confirmed_through"])
    except (OSError, ValueError, KeyError) as exc:
        print(f"dividend-actions: {type(exc).__name__}: {exc}")
        return 1
    result = build(records, sessions, confirmed)
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "actions.json").write_text(
        json.dumps(result.actions, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    (args.out / "dividend-actions-report.json").write_text(
        json.dumps({"unresolved": result.unresolved}, ensure_ascii=False, indent=2)
        + "\n",
        encoding="utf-8",
    )
    print(
        f"dividend-actions: {len(result.actions)} actions, {len(result.unresolved)} unresolved"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
