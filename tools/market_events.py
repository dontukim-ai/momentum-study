"""Build the market events file: liquidation-trading and restructuring filings (momentum.krx.market_events).

Mac, read-only on the OpenDART lists already collected (types B and I), writes one file once:

    uv run python tools/market_events.py --dart-root data/krx/raw --out data/krx/market-events.json

Keep the output out of Git (it holds filing titles from OpenDART). Prints the counts per kind.
"""

from __future__ import annotations

import argparse
from collections import Counter
from pathlib import Path

from momentum.config import SpecUndefinedError
from momentum.krx.dart_collect import list_pages
from momentum.krx.dart_events import parse_list_pages
from momentum.krx.market_events import build, write

TYPES = ("B", "I")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dart-root", type=Path, default=Path("data/krx/raw"))
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    pages = [page for kind in TYPES for page in list_pages(args.dart_root, kind)]
    if not pages:
        raise SpecUndefinedError(f"no collected B or I list pages under {args.dart_root}")
    rows = build(parse_list_pages(pages))
    write(rows, args.out)
    counts = Counter(r["kind"] for r in rows)
    print(f"market events: {len(rows)} filings, {len({r['symbol'] for r in rows})} stocks, {dict(sorted(counts.items()))}"
          f" -> {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
