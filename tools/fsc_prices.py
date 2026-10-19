"""Convert saved FSC daily quote pages into a research price CSV, offline.

Example:
  uv run python tools/fsc_prices.py --kind stock \
    --pages artifacts/data-probe-stock/fsc_stock/2024-06-03/page-*.json \
    --out data/krx/fsc/2024-06-03-stock

Outputs prices.csv (raw supplier values, never repaired), no_trade.jsonl
(sessions without trades; the supplier's carried-forward close is kept as
evidence, never as a bar), identities.jsonl (session, symbol, ISIN, name) and
manifest.json (page hashes, row screen counts). Supplier data may not be redistributed; keep outputs out of Git.
"""

from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path

from trading_agent.data import CSV_FIELDS

from momentum.agent.quality import screen_price_rows
from momentum.config import SpecUndefinedError
from momentum.krx.fsc_prices import SOURCES, convert_pages, identity_changes


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kind", choices=sorted(SOURCES), required=True)
    parser.add_argument("--pages", nargs="+", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        result = convert_pages(args.pages, args.kind)
    except SpecUndefinedError as exc:
        print(f"fsc-prices: {exc}")
        return 1
    screen = screen_price_rows(result.rows)
    args.out.mkdir(parents=True, exist_ok=True)
    with (args.out / "prices.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS)
        writer.writeheader()
        writer.writerows(result.rows)
    with (args.out / "no_trade.jsonl").open("w", encoding="utf-8") as handle:
        for record in result.no_trade:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    with (args.out / "identities.jsonl").open("w", encoding="utf-8") as handle:
        for identity in result.identities:
            handle.write(json.dumps(identity.to_dict(), ensure_ascii=False) + "\n")
    manifest = {
        "kind": args.kind,
        "pages": result.pages,
        "rows": len(result.rows),
        "no_trade": len(result.no_trade),
        "identities": len(result.identities),
        "identity_changes": identity_changes(result.identities),
        "screen": {"accepted": len(screen.accepted), "quarantined": screen.by_code()},
        "basis": "available_at = session day 18:00 KST (preregistered E1 assumption)",
    }
    (args.out / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(
        f"fsc-prices: {len(result.rows)} rows, {len(screen.accepted)} accepted, "
        f"{len(result.rows) - len(screen.accepted)} quarantined, "
        f"{len(result.no_trade)} no-trade, "
        f"{len(result.identities)} identities"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
