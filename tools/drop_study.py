"""v29: does a large drop against 069500 with no filing reverse? (docs/blueprint/ma-cross-v20.md, v29).

Mac, read-only, run once (a record file stops a second run):

    uv run --extra agent python tools/drop_study.py --input data/krx/v2-2026-11-01d/research \
        --events data/krx/v2-2026-11-01/market-events.json --dart-root data/krx/raw \
        --record data/krx/drop-v29/run.json

An event is a decision close k on which an eligible common stock in the top 300 by 20-session trading value,
not quarantined by a restructuring filing, on closes divided back through the share actions published by that
close (069500 likewise), shows a drop:
- its return from close k-5 to close k less 069500's is at most ``-Z`` times its own volatility over five
  sessions: the standard deviation of its daily return less 069500's over the 60 sessions ending at k-5,
  times the square root of 5;
- and it did not on k-1 (one event per episode).
It is ``drop_nonews`` when the company filed nothing (OpenDART list types A periodic, B major, I exchange)
dated from session k-5 to session k, else ``drop_news``. Path, left-out reasons and h20 are v27's
(tools/ma_path_study.py); ``h5`` is the first span (next open to close k+5) less one round trip.
Prints the table and one JSON line; writes only the record file.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import math
import statistics
import sys
from bisect import bisect_left
from collections import defaultdict
from collections.abc import Iterable
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
GROUPS = ("drop_nonews", "drop_news")
TRIAL = "drop_nonews"                            # drop_news is the contrast: reported, never judged
DROP_DAYS, VOL_DAYS, Z = 5, 60, 3.0
NEED = VOL_DAYS + DROP_DAYS + 2                  # closes k-66 .. k, so that k-1 can be judged too
FILING_TYPES = ("A", "B", "I")
THRESHOLD = 3.31                                 # learning ledger: Bonferroni over 107 trials (v29 + v30 included)


def drop(stock: list[float], etf: list[float]) -> tuple[float, float]:
    """(five-session excess return, its volatility) on the last DROP_DAYS + VOL_DAYS + 1 closes of each."""
    n = DROP_DAYS + VOL_DAYS + 1
    s, e = stock[-n:], etf[-n:]
    daily = [(s[j] / s[j - 1] - 1) - (e[j] / e[j - 1] - 1) for j in range(1, VOL_DAYS + 1)]
    move = (s[-1] / s[-1 - DROP_DAYS] - 1) - (e[-1] / e[-1 - DROP_DAYS] - 1)
    return move, statistics.stdev(daily) * math.sqrt(DROP_DAYS)


def hit(stock: list[float], etf: list[float]) -> bool:
    move, vol = drop(stock, etf)
    return vol > 0 and move <= -Z * vol


def filing_days(pages: Iterable[Path]) -> dict[str, list[date]]:
    """Each stock's filing dates (``rcept_dt``, as tools/dart_financials.py reads periodic lists)."""
    days: dict[str, set[date]] = defaultdict(set)
    for path in pages:
        for row in json.loads(path.read_text(encoding="utf-8")).get("list") or []:
            stock, day = (row.get("stock_code") or "").strip(), str(row.get("rcept_dt") or "")
            if stock and len(day) == 8 and day.isdigit():
                days[stock].add(date(int(day[:4]), int(day[4:6]), int(day[6:])))
    return {s: sorted(d) for s, d in days.items()}


def filed(days: list[date], lo: date, hi: date) -> bool:
    i = bisect_left(days, lo)
    return i < len(days) and days[i] <= hi


def events(data: Any, market: Any = None, filings: dict[str, list[date]] | None = None) -> list[dict[str, Any]]:
    from momentum.krx.market_events import MarketEvents

    market = market or MarketEvents([])
    filings = filings or {}
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

    def adjusted(s: str, k: int, at: datetime) -> list[float] | None:
        series = history[s]
        if len(series) < NEED or series[-NEED][0] != k - NEED + 1 or any(c <= 0 for _, c in series[-NEED:]):
            return None
        window = series[-NEED:]
        known = [(ex, f) for ex, avail, f in factor_at.get(s, []) if avail <= at and ex <= sessions[k]]
        per_day = [math.prod(f for ex, f in known if ex == sessions[j]) for j, _ in window]
        return ma.adjust([c for _, c in window], per_day)

    found = []
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
        hits = {}
        for s in today:
            if not ma.common_stock(s) or not ma._eligible(states.get(s)):
                continue
            closes = adjusted(s, k, at)
            if closes is not None and hit(closes, etf) and not hit(closes[:-1], etf[:-1]):
                news = filed(filings.get(s, []), sessions[k - DROP_DAYS], day)
                hits[s] = "drop_news" if news else "drop_nonews"
        if hits:
            values = ma._universe(bars, sessions, k, states)
            top = set(sorted((s for s in values if not market.quarantined(s, at)), key=values.__getitem__,
                             reverse=True)[:ma.TOP])
            found += [{"k": k, "day": day, "symbol": s, "signals": [g]} for s, g in sorted(hits.items()) if s in top]
    return found


def summary(rows: list[dict[str, Any]], lo: date, hi: date) -> dict[str, Any]:
    out = ps.summary(rows, lo, hi)
    if out.get("events"):
        h5 = ps._cohorts([r for r in rows if lo <= r["day"] <= hi], lambda r: r["spans"][4] - es.ROUND_TRIP)
        out["h5_cohort_mean"], out["h5_t"] = statistics.fmean(h5), es.newey_west_t(h5, DROP_DAYS)
    return out


def _edge(dev: dict[str, Any], conf: dict[str, Any], h: str) -> bool:
    return bool(dev.get("events") and conf.get("events") and dev[f"{h}_cohort_mean"] > 0
                and dev[f"{h}_t"] >= THRESHOLD and conf[f"{h}_cohort_mean"] > 0)


def judge(found: list[dict[str, Any]], data: Any, market: Any, groups: tuple[str, ...], trial: str) -> dict[str, Any]:
    signals = {}
    for g in groups:
        rows, left = ps.paths(data, [e for e in found if g in e["signals"]], market)
        entry: dict[str, Any] = {"left_out": left,
                                 **{name: summary(rows, lo, hi) for name, (lo, hi) in es.PERIODS.items()}}
        if g == trial:
            dev, conf = entry["development"], entry["confirmation"]
            entry["edge_h5"], entry["edge_h20"] = _edge(dev, conf, "h5"), _edge(dev, conf, "h20")
            entry["dev_t_h5"], entry["dev_t_h20"] = dev.get("h5_t"), dev.get("h20_t")
        signals[g] = entry
    return signals


def study(data: Any, market: Any = None, filings: dict[str, list[date]] | None = None) -> dict[str, Any]:
    return {"threshold": THRESHOLD, "round_trip": es.ROUND_TRIP, "z": Z, "drop_days": DROP_DAYS,
            "vol_days": VOL_DAYS, "signals": judge(events(data, market, filings), data, market, GROUPS, TRIAL)}


def text(result: dict[str, Any], label: str = "v29") -> str:
    lines = [(f"{label}: five-session excess drop <= -{result['z']} x its {result['vol_days']}-session volatility; "
              f"edge needs development cohort mean > 0 with t >= {result['threshold']} and a positive confirmation "
              f"mean (round trip {result['round_trip']:.2%}); spans are gross excess over 069500")]
    for g, entry in result["signals"].items():
        verdict = (f"edge h5 {'YES' if entry['edge_h5'] else 'no'}, h20 {'YES' if entry['edge_h20'] else 'no'}"
                   if "edge_h5" in entry else "contrast, not judged")
        lines.append(f"\n{g}: {verdict}; left out {json.dumps(entry['left_out'])}")
        for name in es.PERIODS:
            s = entry[name]
            if not s.get("events"):
                lines.append(f"  {name}: no events")
                continue
            lines.append(f"  {name}: {s['events']} events / {s['cohorts']} days, h5 net {s['h5_cohort_mean']:+.2%} "
                         f"t {s['h5_t']:.2f}, h20 net {s['h20_cohort_mean']:+.2%} t {s['h20_t']:.2f} "
                         f"(mean {s['h20_mean']:+.2%} median {s['h20_median']:+.2%}, beat {s['h20_beat']:.1%})")
            lines.append("    " + "  ".join(f"{k} {v['mean']:+.2%} ({v['t']:+.2f})" for k, v in s["spans"].items()))
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--events", type=Path, required=True, help="market events (tools/market_events.py)")
    parser.add_argument("--dart-root", type=Path, required=True, help="saved OpenDART list pages (A, B, I)")
    parser.add_argument("--record", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.record.exists():
        raise SystemExit(f"{args.record} exists: v29 runs once")
    from momentum.agent.cli import read_input
    from momentum.config import SpecUndefinedError
    from momentum.krx.dart_collect import list_pages
    from momentum.krx.market_events import MarketEvents

    kinds = {kind: list_pages(args.dart_root, kind) for kind in FILING_TYPES}
    if not all(kinds.values()):
        raise SpecUndefinedError(f"missing OpenDART list pages for {[k for k, v in kinds.items() if not v]}")
    filings = filing_days(p for pages in kinds.values() for p in pages)
    root = args.input
    data = read_input(root / "prices.csv", root / "securities.jsonl", root / "actions.json", root / "sessions.json",
                      securities_mode="session-confirmed")
    result = study(data, MarketEvents.load(args.events), filings)
    result["filing_pages"] = {k: len(v) for k, v in kinds.items()}
    args.record.parent.mkdir(parents=True, exist_ok=True)
    args.record.write_text(json.dumps(result, sort_keys=True, default=str) + "\n", encoding="utf-8")
    print(f"filing pages {result['filing_pages']}, stocks with filings {len(filings)}")
    print(text(result))
    print(json.dumps(result, sort_keys=True, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
