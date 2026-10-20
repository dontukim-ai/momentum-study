"""Fetch OpenDART 기업개황 for every company with its own dividend filing.

Example (key from the environment):
  uv run python tools/dart_companies.py --dividends data/krx/dividends/dividends.jsonl \
    --dart-root data/krx/raw

Saves <root>/dart_company/<corp_code>.json (skipping saved ones). Their legal
names let tools/dividend_actions.py --dart-root match subsidiary filings,
which name the payer by legal name, to the payer's own filing. Keep outputs
out of Git.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from momentum.krx.dart_collect import DartCompanyFetcher
from momentum.krx.fsc_collect import CollectError


def main(argv: list[str] | None = None, **fetch_options: object) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dividends", type=Path, required=True)
    parser.add_argument("--dart-root", type=Path, default=Path("data/krx/raw"))
    parser.add_argument("--pause", type=float, default=1.0)
    args = parser.parse_args(argv)
    try:
        records = [
            json.loads(line)
            for line in args.dividends.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        codes = sorted(
            {
                r["corp_code"]
                for r in records
                if not r.get("subsidiary") and r.get("stock_code")
            }
        )
        fetcher = DartCompanyFetcher(
            args.dart_root,
            pause=args.pause,
            progress=lambda m: print(
                f"dart-companies: {m}", file=sys.stderr, flush=True
            ),
            **fetch_options,  # type: ignore[arg-type]
        )
    except KeyError:
        print(
            "dart-companies: dividends.jsonl has no corp_code; rerun dart_dividends.py"
        )
        return 1
    except (OSError, ValueError, CollectError) as exc:
        print(f"dart-companies: {exc}")
        return 1
    counts = {"fetched": 0, "skipped": 0}
    errors = []
    for code in codes:
        try:
            counts[fetcher.fetch(code)] += 1
        except CollectError as exc:
            errors.append(str(exc))
            if "retries exhausted" in str(exc):
                break
    for message in errors[:10]:
        print(f"dart-companies: {message}", file=sys.stderr)
    print(
        f"dart-companies: {len(codes)} companies, {counts['fetched']} fetched, "
        f"{counts['skipped']} skipped, {len(errors)} errors"
    )
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
