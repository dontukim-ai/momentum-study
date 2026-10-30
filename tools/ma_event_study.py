"""v23: does the moving-average entry signal itself beat 069500 after costs? (docs/blueprint/ma-cross-v20.md)

Mac, read-only, run once (a record file stops a second run):

    uv run --extra agent python tools/ma_event_study.py --input data/krx/research-fwd-full \
        --record data/krx/ma-v23/run.json

No exits, no slots, no sizing: every signal is one event. An event is a session close on which an eligible
common stock in the top 300 by 20-session trading value (v20/v21's universe) shows
- ``golden_bull``: the 20/60 golden cross with MA5 > MA20 > MA60 > MA120 (v21's entry), or
- ``golden``: the 20/60 golden cross (v20's entry),
on closes divided back through the share actions published by that decision (as in tools/ma_cross.py).
Its outcome over ``h`` sessions is bought at the next open and valued at the close ``h`` sessions after the
decision, share actions in between applied; 069500 over the same open and close is the benchmark. The net
excess is that difference less one round trip (2 x 36.5bp). Dividends are left out on both sides.

An event is left out, and counted, when
- the stock has no bar with an open on the entry session or no bar on the exit session (``missing``);
- any of its bars inside the window moves beyond the daily limit with no recorded share action
  (``unexplained``, rule 3: never booked as a return);
- the exit session lies after the input (``open_at_end``).
Events on the same decision day are averaged into one cohort; the t statistic is the cohort means' mean over a
Newey-West standard error with ``h`` lags. Development and confirmation halves are reported separately.
Prints the table and one JSON line; writes only the record file.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import math
import statistics
import sys
from collections import defaultdict
from collections.abc import Sequence
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
SIGNALS = ("golden_bull", "golden")
HORIZONS, PRIMARY = (5, 20, 60), 20
ROUND_TRIP = 2 * ma.COSTS["base"] / 10_000
PERIODS = {"development": ma.HALVES[0], "confirmation": ma.HALVES[1]}
THRESHOLD = 3.26                               # learning ledger: Bonferroni over 90 trials (88 + these 2)


def newey_west_t(xs: Sequence[float], lags: int) -> float:
    """Mean over its Newey-West (Bartlett) standard error; nan for fewer than two values or no variance."""
    n = len(xs)
    if n < 2:
        return math.nan
    m = statistics.fmean(xs)
    u = [x - m for x in xs]
    var = sum(v * v for v in u) / n
    for lag in range(1, min(lags, n - 1) + 1):
        var += 2 * (1 - lag / (lags + 1)) * sum(u[i] * u[i - lag] for i in range(lag, n)) / n
    return m / math.sqrt(var / n) if var > 0 else math.nan


def events(data: Any) -> list[dict[str, Any]]:
    """Every (decision day, symbol, signals) of the universe, decided only on what that close knew."""
    sessions = list(data.sessions)
    bars: dict[date, dict[str, Any]] = defaultdict(dict)
    for bar in data.snapshot.bars:
        bars[bar.session][bar.symbol] = bar
    factor_at: dict[str, list[tuple[date, datetime, float]]] = defaultdict(list)
    for announced in data.actions:
        f = ma.action_factor(announced.action)
        if f is not None:
            factor_at[announced.action.security_id].append((announced.action.ex_date, announced.available_at, f))
    closes: dict[str, list[tuple[date, float]]] = defaultdict(list)
    found = []
    for k, day in enumerate(sessions):
        today = bars.get(day, {})
        for s, b in today.items():
            closes[s].append((day, float(b.close)))
        if k + 1 >= len(sessions) or sessions[k + 1] < ma.FIRST_TRADE:
            continue
        states = data.securities.states(ma._decision(day))
        hits: dict[str, list[str]] = {}
        for s in today:
            series = closes[s]
            need = ma.LONG + 1
            if (not ma.common_stock(s) or not ma._eligible(states.get(s)) or len(series) < need
                    or series[-need][0] != sessions[k - need + 1]):
                continue
            full = len(series) >= ma.HISTORY and series[-ma.HISTORY][0] == sessions[k - ma.HISTORY + 1]
            window = series[-(ma.HISTORY if full else need):]
            known = [(ex, f) for ex, avail, f in factor_at.get(s, []) if avail <= ma._decision(day) and ex <= day]
            adjusted = ma.adjust([c for _, c in window], [math.prod(f for ex, f in known if ex == d) for d, _ in window])
            if ma.cross(ma.sma(adjusted[:-1], ma.SHORT), ma.sma(adjusted[:-1], ma.LONG),
                        ma.sma(adjusted, ma.SHORT), ma.sma(adjusted, ma.LONG)) != "golden":
                continue
            hits[s] = ["golden"] + (["golden_bull"] if full and ma.signals(adjusted)["alignment"] == "bull" else [])
        if hits:
            values = ma._universe(bars, sessions, k, states)
            top = set(sorted(values, key=values.__getitem__, reverse=True)[:ma.TOP])
            found += [{"k": k, "day": day, "symbol": s, "signals": sig} for s, sig in sorted(hits.items()) if s in top]
    return found


def outcomes(data: Any, found: list[dict[str, Any]], h: int) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Net excess of each event over ``h`` sessions, and the counts of events left out by reason."""
    sessions = list(data.sessions)
    series: dict[str, dict[int, Any]] = defaultdict(dict)
    index = {d: k for k, d in enumerate(sessions)}
    for bar in data.snapshot.bars:
        series[bar.symbol][index[bar.session]] = bar
    actions: dict[str, list[Any]] = defaultdict(list)
    for announced in data.actions:
        actions[announced.action.security_id].append(announced.action)
    out, left = [], {"missing": 0, "unexplained": 0, "open_at_end": 0, "no_benchmark": 0}
    for e in found:
        k, s = e["k"], e["symbol"]
        if k + h >= len(sessions):
            left["open_at_end"] += 1
            continue
        own, etf = series[s], series[ma.MARKET_ETF]
        if k + 1 not in own or own[k + 1].open <= 0 or k + h not in own:
            left["missing"] += 1
            continue
        if k + 1 not in etf or etf[k + 1].open <= 0 or k + h not in etf:
            left["no_benchmark"] += 1
            continue
        held = sorted(j for j in own if k <= j <= k + h)
        jumped = False
        for j0, j1 in pairwise(held):
            if own[j0].close <= 0:
                continue
            kinds = [a.action_type for a in actions.get(s, []) if sessions[j0] < a.ex_date <= sessions[j1]]
            for px in (own[j1].open, own[j1].close):
                if px > 0 and ma.unexplained(float(px / own[j0].close), kinds):
                    jumped = True
        if jumped:
            left["unexplained"] += 1
            continue
        factor = 1.0                           # an action after the entry open rescales the shares bought
        for a in actions.get(s, []):
            f = ma.action_factor(a)
            if f and sessions[k + 1] < a.ex_date <= sessions[k + h]:
                factor *= f
        r = float(own[k + h].close) * factor / float(own[k + 1].open) - 1
        rb = float(etf[k + h].close / etf[k + 1].open) - 1
        out.append({**e, "ret": r, "bench": rb, "net_excess": r - rb - ROUND_TRIP})
    return out, left


def summary(rows: list[dict[str, Any]], h: int, lo: date, hi: date) -> dict[str, Any]:
    rows = [r for r in rows if lo <= r["day"] <= hi]
    if not rows:
        return {"events": 0}
    cohorts: dict[date, list[float]] = defaultdict(list)
    for r in rows:
        cohorts[r["day"]].append(r["net_excess"])
    means = [statistics.fmean(cohorts[d]) for d in sorted(cohorts)]
    net = [r["net_excess"] for r in rows]
    return {"events": len(rows), "cohorts": len(means), "mean_ret": statistics.fmean(r["ret"] for r in rows),
            "mean_bench": statistics.fmean(r["bench"] for r in rows), "mean_net_excess": statistics.fmean(net),
            "median_net_excess": statistics.median(net), "beat_after_cost": sum(x > 0 for x in net) / len(net),
            "cohort_mean": statistics.fmean(means), "t": newey_west_t(means, h)}


def study(data: Any) -> dict[str, Any]:
    found = events(data)
    result: dict[str, Any] = {"round_trip": ROUND_TRIP, "threshold": THRESHOLD, "signals": {}}
    for sig in SIGNALS:
        mine = [e for e in found if sig in e["signals"]]
        entry = result["signals"][sig] = {}
        for h in HORIZONS:
            rows, left = outcomes(data, mine, h)
            entry[f"h{h}"] = {"left_out": left, **{name: summary(rows, h, lo, hi) for name, (lo, hi) in PERIODS.items()}}
    for sig in SIGNALS:
        p = result["signals"][sig][f"h{PRIMARY}"]
        dev, conf = p["development"], p["confirmation"]
        result["signals"][sig]["edge"] = bool(
            dev.get("events") and conf.get("events") and dev["cohort_mean"] > 0 and dev["t"] >= THRESHOLD
            and conf["cohort_mean"] > 0)
        result["signals"][sig]["dev_t"] = dev.get("t")
    return result


def text(result: dict[str, Any]) -> str:
    lines = [(f"round trip {result['round_trip']:.2%}, edge needs development t >= {result['threshold']} at "
              f"h{PRIMARY} and a positive confirmation mean")]
    for sig in SIGNALS:
        lines.append(f"\n{sig}: edge {'YES' if result['signals'][sig]['edge'] else 'no'}")
        for h in HORIZONS:
            for name in PERIODS:
                s = result["signals"][sig][f"h{h}"][name]
                if not s.get("events"):
                    lines.append(f"  h{h} {name}: no events")
                    continue
                lines.append(f"  h{h} {name}: {s['events']} events / {s['cohorts']} days, stock {s['mean_ret']:+.2%} "
                             f"069500 {s['mean_bench']:+.2%}, net excess mean {s['mean_net_excess']:+.2%} "
                             f"median {s['median_net_excess']:+.2%}, beat {s['beat_after_cost']:.1%}, "
                             f"cohort mean {s['cohort_mean']:+.2%} t {s['t']:.2f}")
        lines.append("  left out: " + ", ".join(f"h{h} {json.dumps(result['signals'][sig][f'h{h}']['left_out'])}"
                                                for h in HORIZONS))
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--record", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.record.exists():
        raise SystemExit(f"{args.record} exists: v23 runs once")
    from momentum.agent.cli import read_input

    root = args.input
    data = read_input(root / "prices.csv", root / "securities.jsonl", root / "actions.json", root / "sessions.json",
                      securities_mode="session-confirmed")
    result = study(data)
    args.record.parent.mkdir(parents=True, exist_ok=True)
    args.record.write_text(json.dumps(result, sort_keys=True, default=str) + "\n", encoding="utf-8")
    print(text(result))
    print(json.dumps(result, sort_keys=True, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
