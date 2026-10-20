"""Build bonus-issue and capital-reduction actions (policy B).

Example (key from the environment; B-type lists from tools/dart_collect.py --type B):
  uv run python tools/capital_actions.py --dart-root data/krx/raw \
    --adjustments data/krx/adjustments.jsonl --calendar data/krx/calendar \
    --split-actions data/krx/actions/actions.json --out data/krx/actions-capital

Fetches the 무상증자/감자 detail answers into <root>/dart_detail/ (skipping
saved ones; --no-fetch reads saved answers only), then writes actions.json
(the research --actions format) and capital-report.json (unresolved and
skipped filings with reasons). Keep outputs out of Git.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

from momentum.config import SpecUndefinedError
from momentum.krx.capital_actions import KINDS, build, load_details, relevant
from momentum.krx.dart_collect import DartDetailFetcher, list_pages
from momentum.krx.dart_events import parse_list_pages
from momentum.krx.fsc_collect import CollectError


def main(argv: list[str] | None = None, **fetch_options: object) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dart-root", type=Path, default=Path("data/krx/raw"))
    parser.add_argument("--adjustments", type=Path, required=True)
    parser.add_argument("--calendar", type=Path, required=True)
    parser.add_argument("--split-actions", type=Path)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--pause", type=float, default=1.0)
    parser.add_argument("--no-fetch", action="store_true")
    args = parser.parse_args(argv)
    problems: list[dict[str, str]] = []
    try:
        pages = list_pages(args.dart_root, "B")
        if not pages:
            raise SpecUndefinedError("no collected B-type disclosure pages")
        events = relevant(parse_list_pages(pages))
        if not args.no_fetch:
            fetchers = {
                api: DartDetailFetcher(
                    args.dart_root,
                    api,
                    pause=args.pause,
                    progress=lambda m: print(
                        f"capital-actions: {m}", file=sys.stderr, flush=True
                    ),
                    **fetch_options,  # type: ignore[arg-type]
                )
                for api in KINDS.values()
            }
            # A correction is answered under its original's first reception
            # date, through the original's request; its own date finds nothing.
            for event in (e for e in events if not e.is_correction):
                try:
                    fetchers[KINDS[event.report_kind]].fetch(
                        event.rcept_no, event.corp_code, event.rcept_date
                    )
                except CollectError as exc:
                    problems.append({"rcept_no": event.rcept_no, "reason": str(exc)})
                    if "retries exhausted" in str(exc):
                        break
        details = load_details(args.dart_root)
        adjustments = [
            json.loads(line)
            for line in args.adjustments.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        sessions = tuple(
            date.fromisoformat(d)
            for d in json.loads((args.calendar / "calendar.json").read_text())
        )
        report = json.loads((args.calendar / "calendar-report.json").read_text())
        if not report.get("confirmed_through"):
            raise ValueError("calendar has no confirmed range")
        existing = (
            json.loads(args.split_actions.read_text(encoding="utf-8"))
            if args.split_actions
            else []
        )
        result = build(
            events,
            details,
            adjustments,
            sessions,
            date.fromisoformat(report["confirmed_through"]),
            existing,
        )
    except (SpecUndefinedError, CollectError, OSError, ValueError, KeyError) as exc:
        print(f"capital-actions: {type(exc).__name__}: {exc}")
        return 1
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "actions.json").write_text(
        json.dumps(result.actions, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    (args.out / "capital-report.json").write_text(
        json.dumps(
            {
                "unresolved": result.unresolved,
                "skipped": result.skipped,
                "problems": problems,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    kinds = {
        k: sum(a["action_type"] == k for a in result.actions)
        for k in ("bonus_issue", "capital_reduction")
    }
    print(
        f"capital-actions: {len(events)} filings, {kinds['bonus_issue']} bonus issues, "
        f"{kinds['capital_reduction']} reductions, {len(result.unresolved)} unresolved, "
        f"{len(result.skipped)} skipped, {len(problems)} problems"
    )
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
