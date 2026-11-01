"""Diagnose v24b ma120's second-half drawdown (docs/blueprint/ma-cross-v20.md; never a new verdict).

Mac, read-only, writes nothing:

    uv run --extra agent python tools/etf_timing_diag.py --input data/krx/v2-2026-11-01d/research

Re-runs the recorded variant and cost (ma120, 5bp) with a trace, then reports for the second half
- the strategy's and the benchmark's deepest drawdown: peak and trough day, NAV, and the state in between;
- every round trip (buy fill to sell fill): days, fill prices and return, so whipsaw losses show;
- sessions held from close to close with no fill whose NAV return differs from the benchmark's by more than
  0.5%p (a bookkeeping difference rather than market exposure, if any);
- the 069500 corporate actions in the input.
Prints a text report and one JSON line.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
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


et = _load("etf_timing")
GAP = 0.005


def drawdown(days: list, navs: list[float]) -> dict[str, Any]:
    peak_i, best = 0, (0.0, 0, 0)
    for i, v in enumerate(navs):
        if v > navs[peak_i]:
            peak_i = i
        dd = 1 - v / navs[peak_i]
        if dd > best[0]:
            best = (dd, peak_i, i)
    dd, p, t = best
    return {"mdd": dd, "peak_day": days[p].isoformat(), "peak_nav": navs[p], "trough_day": days[t].isoformat(),
            "trough_nav": navs[t], "peak_index": p, "trough_index": t}


def diagnose(trace: list, fills: list, lo: Any, hi: Any) -> dict[str, Any]:
    rows = [r for r in trace if lo <= r[0] <= hi]
    days = [r[0] for r in rows]
    strat = drawdown(days, [r[1] for r in rows])
    bench = drawdown(days, [r[2] for r in rows])
    window = rows[strat["peak_index"]:strat["trough_index"] + 1]
    strat["held_share_of_window"] = sum(bool(r[3]) for r in window) / len(window) if window else 0.0
    trips, buy = [], None
    for f in (f for f in fills if f.shares > 0):
        if f.side == "buy":
            buy = f
        elif buy is not None:
            trips.append({"buy_day": buy.session_date.isoformat(), "buy": float(buy.price),
                          "sell_day": f.session_date.isoformat(), "sell": float(f.price),
                          "ret": float(f.price / buy.price) - 1})
            buy = None
    trips = [t for t in trips if lo.isoformat() <= t["sell_day"] <= hi.isoformat()]
    fill_days = {f.session_date for f in fills if f.shares > 0}
    odd = []
    for (d0, n0, b0, q0, _), (d1, n1, b1, q1, _) in pairwise(rows):
        if q0 and q1 and q0 == q1 and d1 not in fill_days:
            rs, rb = n1 / n0 - 1, b1 / b0 - 1
            if abs(rs - rb) > GAP:
                odd.append({"day": d1.isoformat(), "strategy": rs, "benchmark": rb})
    return {"strategy_dd": strat, "benchmark_dd": bench, "round_trips": trips,
            "losing_trips": sum(t["ret"] < 0 for t in trips),
            "trip_return_product": _product(t["ret"] for t in trips), "odd_sessions": odd}


def _product(rets: Any) -> float:
    out = 1.0
    for r in rets:
        out *= 1 + r
    return out - 1


def text(result: dict[str, Any], actions: list) -> str:
    s, b = result["strategy_dd"], result["benchmark_dd"]
    lines = [(f"strategy MDD {s['mdd']:.2%}: peak {s['peak_day']} {s['peak_nav']:,.0f} -> trough {s['trough_day']} "
              f"{s['trough_nav']:,.0f}, held {s['held_share_of_window']:.0%} of those sessions"),
             f"069500   MDD {b['mdd']:.2%}: peak {b['peak_day']} -> trough {b['trough_day']}",
             (f"round trips closed in the half: {len(result['round_trips'])}, losing {result['losing_trips']}, "
              f"compounded {result['trip_return_product']:+.2%}")]
    lines += [f"  {t['buy_day']} {t['buy']:,.0f} -> {t['sell_day']} {t['sell']:,.0f} {t['ret']:+.2%}"
              for t in result["round_trips"]]
    lines.append(f"held sessions whose NAV moved >{GAP:.1%}p off the benchmark: {len(result['odd_sessions'])}")
    lines += [f"  {o['day']} strategy {o['strategy']:+.2%} 069500 {o['benchmark']:+.2%}"
              for o in result["odd_sessions"][:20]]
    lines.append(f"069500 corporate actions in the input: {len(actions)}")
    lines += [f"  {a.ex_date} {a.action_type} {a.split_ratio or a.cash_amount}" for a in actions[:20]]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--input", type=Path, required=True)
    args = parser.parse_args(argv)
    from momentum.agent.cli import read_input

    root = args.input
    data = read_input(root / "prices.csv", root / "securities.jsonl", root / "actions.json", root / "sessions.json",
                      securities_mode="session-confirmed")
    trace: list = []
    run = et.backtest(data, "ma120", et.COSTS["base"], trace)
    lo, hi = et.HALVES[1]
    result = diagnose(trace, run["fills"], lo, hi)
    actions = [a.action for a in data.actions if a.action.security_id == et.ETF]
    print(text(result, actions))
    print(json.dumps(result, sort_keys=True, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
