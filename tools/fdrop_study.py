"""v30: a large drop against 069500 in a stock whose earnings barely move (docs/blueprint/ma-cross-v20.md, v30).

Mac, read-only, run once (a record file stops a second run):

    uv run --extra agent python tools/fdrop_study.py --input data/krx/v2-2026-11-01d/research \
        --events data/krx/v2-2026-11-01/market-events.json --dart-root data/krx/raw \
        --record data/krx/fdrop-v30/run.json

Events are v29's drops (tools/drop_study.py), filings ignored. On each event day the top-300 universe stocks
(not quarantined) with a fundamental volatility f (src/momentum/krx/dart_quarters.py: standard deviation of
quarterly operating income over assets, last eight quarters public at the decision, at least six) are split
by f into terciles (``statistics.quantiles(n=3, method="inclusive")``): f at or below the lower cut is
``drop_lowf``, at or above the upper cut ``drop_highf``, else ``drop_midf``. An event stock without f is
counted as ``no_fundamentals``. Only ``drop_lowf`` is judged, at h5 and h20, by v29's rule.
Prints the quarterly build report, the table and one JSON line; writes only the record file.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import statistics
import sys
from collections import defaultdict
from datetime import date
from pathlib import Path
from typing import Any

TOOLS = Path(__file__).resolve().parent


def _load(name: str) -> Any:
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, TOOLS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    assert spec is not None and spec.loader is not None
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


ds = _load("drop_study")
ma, es = ds.ma, ds.es
GROUPS = ("drop_lowf", "drop_midf", "drop_highf")
TRIAL = "drop_lowf"
THRESHOLD = 3.31                                 # learning ledger: Bonferroni over 107 trials (v29 + v30 included)


def group(f: float, values: list[float]) -> str:
    low, high = statistics.quantiles(values, n=3, method="inclusive")
    return "drop_lowf" if f <= low else "drop_highf" if f >= high else "drop_midf"


def classify(found: list[dict[str, Any]], data: Any, market: Any,
             quarters: dict[str, list[dict[str, Any]]]) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """v29 events relabelled by their f tercile on the day; events that cannot be placed are counted."""
    from momentum.krx.dart_quarters import volatility
    from momentum.krx.market_events import MarketEvents

    market = market or MarketEvents([])
    sessions = list(data.sessions)
    bars: dict[date, dict[str, Any]] = defaultdict(dict)
    for bar in data.snapshot.bars:
        bars[bar.session][bar.symbol] = bar
    out, left = [], {"no_fundamentals": 0, "no_tercile": 0}
    by_day: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for e in found:
        by_day[e["k"]].append(e)
    for k, day_events in sorted(by_day.items()):
        at = ma._decision(sessions[k])
        states = data.securities.states(at)
        values = ma._universe(bars, sessions, k, states)
        top = sorted((s for s in values if not market.quarantined(s, at)), key=values.__getitem__,
                     reverse=True)[:ma.TOP]
        f = {s: v for s in top if (v := volatility(quarters.get(s, []), at)) is not None}
        for e in day_events:
            if e["symbol"] not in f:
                left["no_fundamentals"] += 1
            elif len(f) < 3:
                left["no_tercile"] += 1
            else:
                out.append({**e, "signals": [group(f[e["symbol"]], list(f.values()))], "f": f[e["symbol"]]})
    return out, left


def study(data: Any, market: Any = None, quarters: dict[str, list[dict[str, Any]]] | None = None) -> dict[str, Any]:
    found, left = classify(ds.events(data, market), data, market, quarters or {})
    return {"threshold": THRESHOLD, "round_trip": es.ROUND_TRIP, "z": ds.Z, "drop_days": ds.DROP_DAYS,
            "vol_days": ds.VOL_DAYS, "unplaced": left, "signals": ds.judge(found, data, market, GROUPS, TRIAL)}


def text(result: dict[str, Any]) -> str:
    return ds.text(result, "v30") + f"\nunplaced events {json.dumps(result['unplaced'])}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--events", type=Path, required=True, help="market events (tools/market_events.py)")
    parser.add_argument("--dart-root", type=Path, required=True, help="saved OpenDART A lists, dart_fin, dart_finq")
    parser.add_argument("--record", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.record.exists():
        raise SystemExit(f"{args.record} exists: v30 runs once")
    from momentum.agent.cli import read_input
    from momentum.config import SpecUndefinedError
    from momentum.krx.dart_collect import list_pages
    from momentum.krx.dart_financials import annual_filings
    from momentum.krx.dart_quarters import build_quarters, interim_filings, read_rows
    from momentum.krx.market_events import MarketEvents

    pages = list_pages(args.dart_root, "A")
    if not pages or not (args.dart_root / "dart_finq").is_dir():
        raise SpecUndefinedError("v30 needs the saved A lists and dart_finq answers")
    built, report = build_quarters(read_rows(args.dart_root), annual_filings(pages) + interim_filings(pages))
    quarters: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for q in built:
        quarters[q["symbol"]].append(q)
    print(f"quarterly build {json.dumps(report, sort_keys=True)}, symbols {len(quarters)}")
    root = args.input
    data = read_input(root / "prices.csv", root / "securities.jsonl", root / "actions.json", root / "sessions.json",
                      securities_mode="session-confirmed")
    result = study(data, MarketEvents.load(args.events), quarters)
    result["quarterly_build"] = report
    args.record.parent.mkdir(parents=True, exist_ok=True)
    args.record.write_text(json.dumps(result, sort_keys=True, default=str) + "\n", encoding="utf-8")
    print(text(result))
    print(json.dumps(result, sort_keys=True, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
