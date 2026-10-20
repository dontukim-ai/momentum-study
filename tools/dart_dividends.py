"""Fetch and parse OpenDART cash-dividend decision documents.

Example (key from the environment):
  uv run python tools/dart_dividends.py --dart-root data/krx/raw --out data/krx/dividends

Reads the collected I-type lists (tools/dart_collect.py), fetches the
document of every 현금ㆍ현물배당결정 filing into <root>/dart_docs/ (skipping
saved ones), parses them and writes dividends.jsonl plus dividend-report.json
(parse failures and subsidiary filings). It creates no corporate action:
ex-date and payment-date policy are decided separately. Keep outputs out of Git.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from momentum.config import SpecUndefinedError
from momentum.krx.dart_collect import DartDocumentFetcher, list_pages
from momentum.krx.dart_dividends import parse_dividend_document, read_document
from momentum.krx.dart_events import parse_list_pages
from momentum.krx.fsc_collect import CollectError

TOKEN = "현금ㆍ현물배당결정"


def main(argv: list[str] | None = None, **fetch_options: object) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dart-root", type=Path, default=Path("data/krx/raw"))
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--pause", type=float, default=1.0)
    parser.add_argument(
        "--no-fetch", action="store_true", help="parse saved documents only"
    )
    args = parser.parse_args(argv)
    try:
        events = [
            e
            for e in parse_list_pages(list_pages(args.dart_root, "I"))
            if TOKEN in e.report_nm.replace(" ", "")
        ]
        fetcher = (
            DartDocumentFetcher(
                args.dart_root,
                pause=args.pause,
                progress=lambda m: print(
                    f"dart-dividends: {m}", file=sys.stderr, flush=True
                ),
                **fetch_options,  # type: ignore[arg-type]
            )
            if not args.no_fetch
            else None
        )
    except (SpecUndefinedError, CollectError) as exc:
        print(f"dart-dividends: {exc}")
        return 1
    decisions, problems = [], []
    for event in events:
        path = args.dart_root / "dart_docs" / f"{event.rcept_no}.zip"
        try:
            if fetcher is not None:
                fetcher.fetch(event.rcept_no)
            if not path.exists():
                raise SpecUndefinedError("document not saved")
            decision = parse_dividend_document(
                event.rcept_no, read_document(path.read_bytes())
            )
        except CollectError as exc:
            problems.append(
                {"rcept_no": event.rcept_no, "stage": "fetch", "reason": str(exc)}
            )
            if "retries exhausted" in str(exc):
                break
            continue
        except SpecUndefinedError as exc:
            problems.append(
                {"rcept_no": event.rcept_no, "stage": "parse", "reason": str(exc)}
            )
            continue
        record = {
            "rcept_no": decision.rcept_no,
            "stock_code": event.stock_code,
            "corp_name": event.corp_name,
            "report_nm": event.report_nm.strip(),
            "available_at": event.available_at.isoformat(),
            "dividend_kind": decision.dividend_kind,
            "payment_kind": decision.payment_kind,
            "per_share_common": None
            if decision.per_share_common is None
            else str(decision.per_share_common),
            "per_share_class": None
            if decision.per_share_class is None
            else str(decision.per_share_class),
            "record_date": decision.record_date.isoformat(),
            "pay_date": None
            if decision.pay_date is None
            else decision.pay_date.isoformat(),
            "board_date": None
            if decision.board_date is None
            else decision.board_date.isoformat(),
            "agm_date": None
            if decision.agm_date is None
            else decision.agm_date.isoformat(),
            "subsidiary": decision.subsidiary,
            "correction": decision.correction,
        }
        decisions.append(record)
    args.out.mkdir(parents=True, exist_ok=True)
    with (args.out / "dividends.jsonl").open("w", encoding="utf-8") as handle:
        for record in decisions:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    (args.out / "dividend-report.json").write_text(
        json.dumps({"problems": problems}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    subsidiary = sum(1 for r in decisions if r["subsidiary"])
    no_pay = sum(1 for r in decisions if r["pay_date"] is None)
    print(
        f"dart-dividends: {len(events)} filings, {len(decisions)} parsed "
        f"({subsidiary} subsidiary, {no_pay} without pay date), {len(problems)} problems"
    )
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
