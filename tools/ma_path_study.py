"""v27: the path around moving-average crosses, and whether a 5/20 cross (with volume) is an entry signal
(docs/blueprint/ma-cross-v20.md, v27).

Mac, read-only, run once (a record file stops a second run):

    uv run --extra agent python tools/ma_path_study.py --input data/krx/v2-2026-11-01d/research \
        --events data/krx/v2-2026-11-01/market-events.json --record data/krx/ma-v27/run.json

Signals, on closes divided back through the share actions published by the decision close (as in v23), for an
eligible common stock in the top 300 by 20-session trading value that is not quarantined by a restructuring
filing:
- ``gc20_60``: the 20/60 golden cross (v23's ``golden``; closed trial, reported for its path only);
- ``gc5_20``: the 5/20 golden cross;
- ``gc5_20_vol``: the 5/20 golden cross on a session whose volume (scaled through the same actions) is at least
  ``VOLUME_MULTIPLE`` times the mean of the 20 sessions before it.

Path of each event, as the stock's return less 069500's over the same span (share actions in between applied):
- before: closes k-20 -> k-15 -> k-10 -> k-5 -> k (k = the decision close; known at the decision);
- after: the next open k+1 -> close k+5, then closes k+5 -> k+10 -> k+15 -> k+20 (gross, no cost);
- ``h20``: open k+1 -> close k+20, less one round trip, as v23's net excess (the verdict).
An event is left out, and counted, when a bar of the stock or of 069500 is missing at an anchor (``missing``),
a move beyond the daily limit with no recorded share action lies in k-20..k+20 (``unexplained``; a filed
liquidation's jump is a real price), or k+20 lies after the input (``open_at_end``).
Events on one decision day are averaged into one cohort; t is the cohort means' mean over a Newey-West error
(5 lags for a 5-session span, 20 for h20). For each 20/60 cross, the sessions since that stock's last 5/20
cross are counted as well. Prints the table and one JSON line; writes only the record file.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import math
import statistics
import sys
from collections import defaultdict
from datetime import date, datetime
from itertools import pairwise
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
SIGNALS = ("gc20_60", "gc5_20", "gc5_20_vol")
TRIALS = ("gc5_20", "gc5_20_vol")                # gc20_60 is v23's closed trial: path only, no verdict
FAST, SLOW = 5, 20
VOLUME_DAYS, VOLUME_MULTIPLE = 20, 2.0
BEFORE = (-20, -15, -10, -5, 0)                  # close anchors before and at the decision
AFTER = (5, 10, 15, 20)                          # close anchors after the entry open (k + 1)
SPANS = ("-20..-15", "-15..-10", "-10..-5", "-5..0", "open1..5", "5..10", "10..15", "15..20")
H = AFTER[-1]
LEAD_BINS = ((1, 5), (6, 10), (11, 20), (21, 60))
THRESHOLD = 3.30                                 # learning ledger: Bonferroni over 102 trials (100 + these 2)


def crosses(data: Any, events: Any = None) -> tuple[list[dict[str, Any]], dict[str, list[int]]]:
    """Every (decision day, symbol, signals) of the universe, and each stock's 5/20 golden-cross sessions
    (any eligible common stock, in the universe or not), decided only on what that close knew."""
    from momentum.krx.market_events import MarketEvents

    events = events or MarketEvents([])
    sessions = list(data.sessions)
    bars: dict[date, dict[str, Any]] = defaultdict(dict)
    for bar in data.snapshot.bars:
        bars[bar.session][bar.symbol] = bar
    factor_at: dict[str, list[tuple[date, datetime, float]]] = defaultdict(list)
    for announced in data.actions:
        f = ma.action_factor(announced.action)
        if f is not None:
            factor_at[announced.action.security_id].append((announced.action.ex_date, announced.available_at, f))
    history: dict[str, list[tuple[int, float, float]]] = defaultdict(list)     # (k, close, volume)
    fast_days: dict[str, list[int]] = defaultdict(list)
    v23 = {(e["k"], e["symbol"]) for e in es.events(data)}
    found = []
    need = SLOW + 1
    for k, day in enumerate(sessions):
        today = bars.get(day, {})
        for s, b in today.items():
            history[s].append((k, float(b.close), float(b.volume)))
        if k + 1 >= len(sessions) or sessions[k + 1] < ma.FIRST_TRADE:
            continue
        at = ma._decision(day)
        states = data.securities.states(at)
        hits: dict[str, list[str]] = {}
        for s in today:
            series = history[s]
            if (not ma.common_stock(s) or not ma._eligible(states.get(s)) or len(series) < need
                    or series[-need][0] != k - need + 1):
                continue
            window = series[-need:]
            if any(c <= 0 for _, c, _ in window):
                continue
            known = [(ex, f) for ex, avail, f in factor_at.get(s, []) if avail <= at and ex <= day]
            per_day = [math.prod(f for ex, f in known if ex == sessions[j]) for j, _, _ in window]
            scale = ma.adjust([1.0] * need, per_day)                         # 1 / cumulative factor after each day
            closes = [c * a for (_, c, _), a in zip(window, scale, strict=True)]
            volumes = [v / a for (_, _, v), a in zip(window, scale, strict=True)]
            if ma.cross(ma.sma(closes[:-1], FAST), ma.sma(closes[:-1], SLOW),
                        ma.sma(closes, FAST), ma.sma(closes, SLOW)) != "golden":
                if (k, s) in v23:
                    hits[s] = ["gc20_60"]
                continue
            fast_days[s].append(k)
            prior = statistics.fmean(volumes[-1 - VOLUME_DAYS:-1])
            sig = ["gc5_20"] + (["gc5_20_vol"] if prior > 0 and volumes[-1] >= VOLUME_MULTIPLE * prior else [])
            hits[s] = (["gc20_60"] if (k, s) in v23 else []) + sig
        if hits:
            values = ma._universe(bars, sessions, k, states)
            ranked = sorted((s for s in values if not events.quarantined(s, at)), key=values.__getitem__,
                            reverse=True)[:ma.TOP]
            top = set(ranked)
            found += [{"k": k, "day": day, "symbol": s, "signals": sig} for s, sig in sorted(hits.items()) if s in top]
    return found, dict(fast_days)


def paths(data: Any, found: list[dict[str, Any]], events: Any = None) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Each event's excess return over 069500 per span and its h20 net excess, and the events left out."""
    sessions = list(data.sessions)
    index = {d: k for k, d in enumerate(sessions)}
    series: dict[str, dict[int, Any]] = defaultdict(dict)
    for bar in data.snapshot.bars:
        series[bar.symbol][index[bar.session]] = bar
    actions: dict[str, list[Any]] = defaultdict(list)
    for announced in data.actions:
        actions[announced.action.security_id].append(announced.action)

    def factor(s: str, a: int, b: int) -> float:
        """Shares held from session a to b grow by the actions with ex in (a, b]."""
        out = 1.0
        for act in actions.get(s, []):
            f = ma.action_factor(act)
            if f and sessions[a] < act.ex_date <= sessions[b]:
                out *= f
        return out

    def ret(s: str, a: int, a_px: Any, b: int) -> float:
        return float(series[s][b].close) * factor(s, a, b) / float(a_px) - 1

    out, left = [], {"missing": 0, "unexplained": 0, "open_at_end": 0}
    for e in found:
        k, s = e["k"], e["symbol"]
        if k + H >= len(sessions) or k + BEFORE[0] < 0:
            left["open_at_end"] += 1
            continue
        anchors = [k + d for d in BEFORE] + [k + 1] + [k + d for d in AFTER]
        own, etf = series[s], series[ma.MARKET_ETF]
        if any(j not in own or j not in etf or own[j].close <= 0 or etf[j].close <= 0 for j in anchors) \
                or own[k + 1].open <= 0 or etf[k + 1].open <= 0:
            left["missing"] += 1
            continue
        held = sorted(j for j in own if k + BEFORE[0] <= j <= k + H)
        jumped = False
        for j0, j1 in pairwise(held):
            if own[j0].close <= 0:
                continue
            kinds = [a.action_type for a in actions.get(s, []) if sessions[j0] < a.ex_date <= sessions[j1]]
            liquidating = events is not None and events.liquidating(s, sessions[j1])
            for px in (own[j1].open, own[j1].close):
                if px > 0 and ma.unexplained(float(px / own[j0].close), kinds, liquidating):
                    jumped = True
        if jumped:
            left["unexplained"] += 1
            continue
        spans = []
        for a, b in pairwise([k + d for d in BEFORE]):
            spans.append(ret(s, a, own[a].close, b) - ret(ma.MARKET_ETF, a, etf[a].close, b))
        prev = k + 1
        for d in AFTER:
            b = k + d
            if prev == k + 1:
                spans.append(ret(s, prev, own[prev].open, b) - ret(ma.MARKET_ETF, prev, etf[prev].open, b))
            else:
                spans.append(ret(s, prev, own[prev].close, b) - ret(ma.MARKET_ETF, prev, etf[prev].close, b))
            prev = b
        net = (ret(s, k + 1, own[k + 1].open, k + H) - ret(ma.MARKET_ETF, k + 1, etf[k + 1].open, k + H)
               - es.ROUND_TRIP)
        out.append({**e, "spans": spans, "h20": net})
    return out, left


def _cohorts(rows: list[dict[str, Any]], value: Any) -> list[float]:
    by_day: dict[date, list[float]] = defaultdict(list)
    for r in rows:
        by_day[r["day"]].append(value(r))
    return [statistics.fmean(by_day[d]) for d in sorted(by_day)]


def summary(rows: list[dict[str, Any]], lo: date, hi: date) -> dict[str, Any]:
    rows = [r for r in rows if lo <= r["day"] <= hi]
    if not rows:
        return {"events": 0}
    spans = {}
    for i, name in enumerate(SPANS):
        means = _cohorts(rows, lambda r, i=i: r["spans"][i])
        spans[name] = {"mean": statistics.fmean(means), "t": es.newey_west_t(means, 5)}
    h20 = _cohorts(rows, lambda r: r["h20"])
    net = [r["h20"] for r in rows]
    return {"events": len(rows), "cohorts": len(h20), "spans": spans, "h20_cohort_mean": statistics.fmean(h20),
            "h20_t": es.newey_west_t(h20, H), "h20_mean": statistics.fmean(net),
            "h20_median": statistics.median(net), "h20_beat": sum(x > 0 for x in net) / len(net)}


def lead_times(found: list[dict[str, Any]], fast_days: dict[str, list[int]]) -> dict[str, int]:
    """For each 20/60 cross: sessions since the stock's last 5/20 cross, binned."""
    out = {f"{lo}-{hi}": 0 for lo, hi in LEAD_BINS} | {"same_day": 0, "none_within_60": 0}
    for e in found:
        if "gc20_60" not in e["signals"]:
            continue
        before = [e["k"] - j for j in fast_days.get(e["symbol"], []) if 0 <= e["k"] - j <= 60]
        if not before:
            out["none_within_60"] += 1
            continue
        lag = min(before)
        if lag == 0:
            out["same_day"] += 1
            continue
        for lo, hi in LEAD_BINS:
            if lo <= lag <= hi:
                out[f"{lo}-{hi}"] += 1
    return out


def study(data: Any, events: Any = None) -> dict[str, Any]:
    found, fast_days = crosses(data, events)
    result: dict[str, Any] = {"threshold": THRESHOLD, "round_trip": es.ROUND_TRIP, "volume_multiple": VOLUME_MULTIPLE,
                              "lead_times": lead_times(found, fast_days), "signals": {}}
    for sig in SIGNALS:
        rows, left = paths(data, [e for e in found if sig in e["signals"]], events)
        entry: dict[str, Any] = {"left_out": left, **{name: summary(rows, lo, hi) for name, (lo, hi) in es.PERIODS.items()}}
        dev, conf = entry["development"], entry["confirmation"]
        if sig in TRIALS:
            entry["edge"] = bool(dev.get("events") and conf.get("events") and dev["h20_cohort_mean"] > 0
                                 and dev["h20_t"] >= THRESHOLD and conf["h20_cohort_mean"] > 0)
        entry["dev_t"] = dev.get("h20_t")
        result["signals"][sig] = entry
    return result


def text(result: dict[str, Any]) -> str:
    lines = [(f"v27: edge needs development h20 cohort mean > 0 with t >= {result['threshold']} and a positive "
              f"confirmation mean (round trip {result['round_trip']:.2%}); spans are gross excess over 069500"),
             f"20/60 crosses by sessions since the last 5/20 cross: {json.dumps(result['lead_times'])}"]
    for sig in SIGNALS:
        entry = result["signals"][sig]
        verdict = f"edge {'YES' if entry['edge'] else 'no'}" if "edge" in entry else "path only (v23 trial, closed)"
        lines.append(f"\n{sig}: {verdict}; left out {json.dumps(entry['left_out'])}")
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
        raise SystemExit(f"{args.record} exists: v27 runs once")
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
