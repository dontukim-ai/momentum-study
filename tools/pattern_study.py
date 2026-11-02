"""v28: a 15-session fall then a 5-session rebound against 069500, as an entry signal
(docs/blueprint/ma-cross-v20.md, v28).

Mac, read-only, run once (a record file stops a second run):

    uv run --extra agent python tools/pattern_study.py --input data/krx/v2-2026-11-01d/research \
        --events data/krx/v2-2026-11-01/market-events.json --record data/krx/pattern-v28/run.json

An event is a decision close k on which an eligible common stock in the top 300 by 20-session trading value,
not quarantined by a restructuring filing, shows, on closes divided back through the share actions published
by that close (069500 likewise):
- ``fall``: its return from close k-20 to close k-5 less 069500's is at most ``FALL`` (-6%p), and
- ``rebound``: its return from close k-5 to close k less 069500's is at least ``REBOUND`` (+10%p),
and did not show both on k-1 (one event per episode). The path, the left-out reasons, the h20 net excess and
the verdict are v27's (tools/ma_path_study.py), so the two read alike.
Prints the table and one JSON line; writes only the record file.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import math
import sys
from collections import defaultdict
from datetime import date, datetime
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


ma = _load("ma_cross")
es = _load("ma_event_study")
ps = _load("ma_path_study")
SIGNAL = "drop_rebound"
FALL, REBOUND = -0.06, 0.10
LOOK = 20                                        # closes k-20 .. k
SPLIT = 5                                        # the rebound is the last 5 sessions
NEED = LOOK + 2                                  # k-21 .. k, so that k-1 can be judged too
THRESHOLD = 3.30                                 # learning ledger: Bonferroni over 103 trials (102 + this one)


def shape(stock: list[float], etf: list[float]) -> tuple[float, float]:
    """(fall, rebound) excess over 069500 on the last LOOK + 1 closes of each."""
    a, b, c = -1 - LOOK, -1 - SPLIT, -1
    fall = (stock[b] / stock[a] - 1) - (etf[b] / etf[a] - 1)
    rebound = (stock[c] / stock[b] - 1) - (etf[c] / etf[b] - 1)
    return fall, rebound


def hit(stock: list[float], etf: list[float]) -> bool:
    fall, rebound = shape(stock, etf)
    return fall <= FALL and rebound >= REBOUND


def events(data: Any, market: Any = None) -> list[dict[str, Any]]:
    from momentum.krx.market_events import MarketEvents

    market = market or MarketEvents([])
    sessions = list(data.sessions)
    bars: dict[date, dict[str, Any]] = defaultdict(dict)
    for bar in data.snapshot.bars:
        bars[bar.session][bar.symbol] = bar
    factor_at: dict[str, list[tuple[date, datetime, float]]] = defaultdict(list)
    for announced in data.actions:
        f = ma.action_factor(announced.action)
        if f is not None:
            factor_at[announced.action.security_id].append((announced.action.ex_date, announced.available_at, f))
    history: dict[str, list[tuple[int, float]]] = defaultdict(list)
    found = []

    def adjusted(s: str, k: int, at: datetime) -> list[float] | None:
        series = history[s]
        if len(series) < NEED or series[-NEED][0] != k - NEED + 1 or any(c <= 0 for _, c in series[-NEED:]):
            return None
        window = series[-NEED:]
        known = [(ex, f) for ex, avail, f in factor_at.get(s, []) if avail <= at and ex <= sessions[k]]
        per_day = [math.prod(f for ex, f in known if ex == sessions[j]) for j, _ in window]
        return ma.adjust([c for _, c in window], per_day)

    for k, day in enumerate(sessions):
        today = bars.get(day, {})
        for s, b in today.items():
            history[s].append((k, float(b.close)))
        if k + 1 >= len(sessions) or sessions[k + 1] < ma.FIRST_TRADE:
            continue
        at = ma._decision(day)
        etf = adjusted(ma.MARKET_ETF, k, at)
        if etf is None:
            continue
        states = data.securities.states(at)
        hits = []
        for s in today:
            if not ma.common_stock(s) or not ma._eligible(states.get(s)):
                continue
            closes = adjusted(s, k, at)
            if closes is not None and hit(closes, etf) and not hit(closes[:-1], etf[:-1]):
                hits.append(s)
        if hits:
            values = ma._universe(bars, sessions, k, states)
            top = set(sorted((s for s in values if not market.quarantined(s, at)), key=values.__getitem__,
                             reverse=True)[:ma.TOP])
            found += [{"k": k, "day": day, "symbol": s, "signals": [SIGNAL]} for s in sorted(hits) if s in top]
    return found


def study(data: Any, market: Any = None) -> dict[str, Any]:
    rows, left = ps.paths(data, events(data, market), market)
    entry: dict[str, Any] = {"left_out": left, **{name: ps.summary(rows, lo, hi) for name, (lo, hi) in es.PERIODS.items()}}
    dev, conf = entry["development"], entry["confirmation"]
    entry["edge"] = bool(dev.get("events") and conf.get("events") and dev["h20_cohort_mean"] > 0
                         and dev["h20_t"] >= THRESHOLD and conf["h20_cohort_mean"] > 0)
    entry["dev_t"] = dev.get("h20_t")
    return {"threshold": THRESHOLD, "round_trip": es.ROUND_TRIP, "fall": FALL, "rebound": REBOUND,
            "signals": {SIGNAL: entry}}


def text(result: dict[str, Any]) -> str:
    entry = result["signals"][SIGNAL]
    lines = [(f"v28 {SIGNAL}: excess over 069500 k-20..k-5 <= {result['fall']:+.0%}, k-5..k >= "
              f"{result['rebound']:+.0%}; edge needs development h20 cohort mean > 0 with t >= {result['threshold']} "
              f"and a positive confirmation mean (round trip {result['round_trip']:.2%})"),
             f"edge {'YES' if entry['edge'] else 'no'}; left out {json.dumps(entry['left_out'])}"]
    for name in es.PERIODS:
        s = entry[name]
        if not s.get("events"):
            lines.append(f"  {name}: no events")
            continue
        lines.append(f"  {name}: {s['events']} events / {s['cohorts']} days, h20 net excess cohort mean "
                     f"{s['h20_cohort_mean']:+.2%} t {s['h20_t']:.2f}, mean {s['h20_mean']:+.2%} median "
                     f"{s['h20_median']:+.2%}, beat {s['h20_beat']:.1%}")
        lines.append("    " + "  ".join(f"{k} {v['mean']:+.2%} ({v['t']:+.2f})" for k, v in s["spans"].items()))
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--events", type=Path, required=True, help="market events (tools/market_events.py)")
    parser.add_argument("--record", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.record.exists():
        raise SystemExit(f"{args.record} exists: v28 runs once")
    from momentum.agent.cli import read_input
    from momentum.krx.market_events import MarketEvents

    root = args.input
    data = read_input(root / "prices.csv", root / "securities.jsonl", root / "actions.json", root / "sessions.json",
                      securities_mode="session-confirmed")
    result = study(data, MarketEvents.load(args.events))
    args.record.parent.mkdir(parents=True, exist_ok=True)
    args.record.write_text(json.dumps(result, sort_keys=True, default=str) + "\n", encoding="utf-8")
    print(text(result))
    print(json.dumps(result, sort_keys=True, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
