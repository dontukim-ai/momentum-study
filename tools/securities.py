"""Build securities.jsonl (research security states) from quotes and KRX lists.

Example:
  uv run python tools/securities.py --fsc-root data/krx/raw/fsc \
    --calendar data/krx/calendar-all --kind-dir data/krx/kind \
    --snapshot 2026-10-20 --out data/krx/securities

--kind-dir holds the KRX 「이슈 통계」 files downloaded by hand (관리종목 현황,
매매거래정지 현황, 상장폐지); --snapshot is the day they were downloaded.
Writes securities.jsonl and securities-report.json (delisting and halt
cross-checks, managed rows held out). Policy 1 limits are documented in
momentum.krx.securities. Keep outputs out of Git.
"""

from __future__ import annotations

import argparse
import json
from datetime import date
from pathlib import Path

from momentum.config import SpecUndefinedError
from momentum.krx.fsc_states import status_events
from momentum.krx.securities import build, read_lists


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fsc-root", type=Path, default=Path("data/krx/raw/fsc"))
    parser.add_argument("--calendar", type=Path, required=True)
    parser.add_argument("--kind-dir", type=Path, required=True)
    parser.add_argument("--snapshot", type=date.fromisoformat, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        sessions = tuple(
            date.fromisoformat(d)
            for d in json.loads((args.calendar / "calendar.json").read_text())
        )
        if not sessions:
            raise SpecUndefinedError("calendar has no sessions")
        files = sorted(p for p in args.kind_dir.iterdir() if p.suffix.lower() == ".csv")
        if not files:
            raise SpecUndefinedError(f"no KRX list files in {args.kind_dir}")
        records, report = build(
            status_events(args.fsc_root, sessions),
            read_lists(files),
            sessions,
            args.snapshot,
        )
    except (SpecUndefinedError, OSError, ValueError, KeyError) as exc:
        print(f"securities: {type(exc).__name__}: {exc}")
        return 1
    args.out.mkdir(parents=True, exist_ok=True)
    with (args.out / "securities.jsonl").open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    (args.out / "securities-report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    delisting = report["delisting"]
    print(
        f"securities: {report['symbols']} symbols, {report['records']} records, "
        f"delisting both {delisting['both']} / date mismatch "
        f"{len(delisting['date_mismatch'])} / krx only {len(delisting['krx_only'])} / "
        f"quotes only {len(delisting['quotes_only'])}, "
        f"managed held {len(report['managed_held_without_start'])}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
