"""Build split corporate actions from detected adjustments and prior disclosures.

Example:
  uv run python tools/split_actions.py --fsc-root data/krx/raw/fsc --dart-root data/krx/raw \
    --start 2024-04-01 --end 2024-06-28 --out data/krx/actions

Writes actions.json (the research --actions format, splits only, each with its
evidence) and split-report.json (detections that did not reconcile; their
price jumps stay quarantined).
"""

from __future__ import annotations

import argparse
import json
from datetime import date
from pathlib import Path

from momentum.config import SpecUndefinedError
from momentum.krx.dart_collect import list_pages
from momentum.krx.dart_events import parse_list_pages
from momentum.krx.fsc_adjustments import detect
from momentum.krx.split_actions import reconcile


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fsc-root", type=Path, default=Path("data/krx/raw/fsc"))
    parser.add_argument("--dart-root", type=Path, default=Path("data/krx/raw"))
    parser.add_argument("--start", type=date.fromisoformat, required=True)
    parser.add_argument("--end", type=date.fromisoformat, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        pages = list_pages(args.dart_root, "I")
        if not pages:
            raise SpecUndefinedError("no collected I-type disclosure pages")
        result = reconcile(
            detect(args.fsc_root, args.start, args.end), parse_list_pages(pages)
        )
    except (SpecUndefinedError, OSError, ValueError, KeyError) as exc:
        print(f"split-actions: {type(exc).__name__}: {exc}")
        return 1
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "actions.json").write_text(
        json.dumps(result.actions, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    (args.out / "split-report.json").write_text(
        json.dumps({"unresolved": result.unresolved}, ensure_ascii=False, indent=2)
        + "\n",
        encoding="utf-8",
    )
    print(
        f"split-actions: {len(result.actions)} actions, {len(result.unresolved)} unresolved"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
