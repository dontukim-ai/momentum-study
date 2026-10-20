"""Report exchange base-price adjustments in collected FSC stock quotes.

Example:
  uv run python tools/fsc_adjustments.py --root data/krx/raw/fsc \
    --start 2024-04-01 --end 2024-05-10 --out data/krx/adjustments.jsonl

Detections are evidence for reconciliation with disclosures, not corporate
actions: they are observed on the event day, after the announcement. Listed
share changes on no-trade rows (a reduction relisting during a halt) are
added as HALTED_SHARE_CHANGE.
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from datetime import date
from pathlib import Path

from momentum.config import SpecUndefinedError
from momentum.krx.fsc_adjustments import detect, detect_halted_share_changes


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path("data/krx/raw/fsc"))
    parser.add_argument("--start", type=date.fromisoformat, required=True)
    parser.add_argument("--end", type=date.fromisoformat, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        found = detect(args.root, args.start, args.end)
        found += detect_halted_share_changes(args.root, args.start, args.end)
    except (SpecUndefinedError, OSError, ValueError, KeyError) as exc:
        print(f"fsc-adjustments: {type(exc).__name__}: {exc}")
        return 1
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("w", encoding="utf-8") as handle:
        for adjustment in found:
            handle.write(json.dumps(adjustment.to_dict(), ensure_ascii=False) + "\n")
    kinds = Counter(a.kind for a in found)
    summary = ", ".join(f"{k} {kinds[k]}" for k in sorted(kinds)) or "none"
    print(f"fsc-adjustments: {len(found)} adjustments ({summary})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
