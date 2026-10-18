"""Build a disclosure ledger from local probe pages without network access."""

from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime
from pathlib import Path

from momentum.config import SpecUndefinedError
from momentum.krx.dart_events import KST, DisclosureLedger, parse_list_pages


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pages", nargs="+", type=Path, required=True)
    parser.add_argument("--ledger", type=Path, default=Path("data/krx/dart_events.jsonl"))
    args = parser.parse_args()
    try:
        ledger = DisclosureLedger(args.ledger)
        result = ledger.append(parse_list_pages(args.pages))
        events = ledger.as_of(datetime.max.replace(tzinfo=KST))
    except SpecUndefinedError as exc:
        parser.exit(1, f"dart-ledger: {exc}\n")
    for kind, count in sorted(Counter(event.report_kind for event in events).items()):
        print(f"{kind}: {count}")
    print(f"dart-ledger: {result.added} added, {result.unchanged} unchanged, {len(events)} total")


if __name__ == "__main__":
    main()
