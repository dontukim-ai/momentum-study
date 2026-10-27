"""Forward record of the v11 holdout winners from 2026-10-26 (intraday-dev-plan-v11 §10).

Mac, once a month with the v9 forward update, after the month's minute bars
are collected (``kis_minute_history.py --start 2026-10-26 --end <last day>``):

    uv run python tools/intraday_forward.py --manifest config/intraday-v11/manifest.json \
        --holdout-record data/kis/intraday-holdout-run.json --bars data/kis/minute-history --end 2026-11-27

Only candidates that passed the holdout run (its record, tied to this manifest
by hash) are recorded, through the same frozen code (the manifest gate). The
last WARMUP days before the start go in first for history; nothing trades
before FORWARD_FROM. Every run re-records from FORWARD_FROM to ``--end``.
Reports the candidate, its report-only comparisons and 069500 at 95% over the
same days. Reads only; writes nothing.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from momentum.config import SpecUndefinedError

TOOLS = Path(__file__).resolve().parent
sys.path.insert(0, str(TOOLS))
import intraday_holdout as holdout  # tools are loaded as plain modules

FORWARD_FROM = "2026-10-26"  # first session after the holdout (2026-07-01..10-23)


class FromForward(holdout.Gated):
    def _live(self, view: Any) -> bool:
        return view.day >= FORWARD_FROM


def passed(manifest_path: Path, record: Path) -> list[str]:
    if not record.exists():
        raise SpecUndefinedError(f"the holdout has not run ({record})")
    ran = json.loads(record.read_text(encoding="utf-8"))
    if ran["manifest_sha256"] != holdout._sha(manifest_path):
        raise SpecUndefinedError("the holdout record belongs to another manifest")
    names = [row["name"] for row in ran["rows"] if row["passed"]]
    if not names:
        raise SpecUndefinedError("no candidate passed the holdout; nothing to record")
    return names


def run(manifest_path: Path, record: Path, bars_root: Path, end: str) -> dict:
    manifest = holdout.verified(manifest_path)
    names = passed(manifest_path, record)
    universe = tuple(manifest["universe"])
    before = sorted({p.stem for s in universe for p in (bars_root / s).glob("*.json")
                     if holdout.HOLDOUT_START.isoformat() <= p.stem < FORWARD_FROM})
    first = before[-holdout.WARMUP] if len(before) >= holdout.WARMUP else FORWARD_FROM
    days = holdout.engine.days_from_kis({s: holdout._bars(bars_root, s, first, end) for s in universe})
    live = {d: bars for d, bars in days.items() if d >= FORWARD_FROM}
    if not live:
        raise SpecUndefinedError(f"no day from {FORWARD_FROM} to {end} in the input")
    configs = {c.name: c for c in holdout.candidates.configs(universe)}
    rows, trades = [], {}
    for name in names:
        strategy = configs[name].make()
        measured, trades[name] = _forward_metrics(days, strategy)
        row = {"name": name, **measured}
        compare = {label: _forward_metrics(days, other)[0] for label, other in holdout.comparisons(strategy).items()}
        if compare:
            row["report_only"] = compare
        rows.append(row)
    bench = {**holdout.dev.buy_and_hold(live, holdout.candidates.KOSPI), "days": len(live)}
    return {"manifest_sha256": holdout._sha(manifest_path), "days": [min(live), max(live)], "rows": rows,
            "benchmark_069500_95": bench, "trades": trades}


def _forward_metrics(days: Any, strategy: Any) -> tuple[dict, list]:
    result = holdout.engine.run(days, FromForward(strategy))
    result.daily = [d for d in result.daily if d["day"] >= FORWARD_FROM]
    return holdout.dev.metrics(result), result.trades


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--manifest", type=Path, default=Path("config/intraday-v11/manifest.json"))
    parser.add_argument("--holdout-record", type=Path, default=Path("data/kis/intraday-holdout-run.json"))
    parser.add_argument("--bars", type=Path, default=Path("data/kis/minute-history"))
    parser.add_argument("--end", required=True, help="last session to record (YYYY-MM-DD)")
    args = parser.parse_args(argv)
    try:
        result = run(args.manifest, args.holdout_record, args.bars, args.end)
    except SpecUndefinedError as exc:
        print(f"intraday-forward: STOP: {exc}")
        return 1
    bench = result["benchmark_069500_95"]
    print(f"forward {result['days'][0]}~{result['days'][1]}; 069500 95%: net {bench['net_return']:+.2%} "
          f"MDD {bench['max_drawdown']:.2%}")
    for row in result["rows"]:
        print(f"{row['name']:<28} net {row['net_return']:+.2%} t {row['t']:.2f} MDD {row['max_drawdown']:.2%} "
              f"days {row['days']}")
        for label, c in row.get("report_only", {}).items():
            print(f"  report only {label:<14} net {c['net_return']:+.2%} t {c['t']:.2f} MDD {c['max_drawdown']:.2%}")
    print(json.dumps({k: v for k, v in result.items() if k != "trades"}, default=str, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
