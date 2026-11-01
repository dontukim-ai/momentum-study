"""v24b: hold 069500 or cash by its own moving average (docs/blueprint/ma-cross-v20.md, v24 and v24b).

Mac, read-only, run once (a record file stops a second run):

    uv run --extra agent python tools/etf_timing.py --input data/krx/v2-2026-11-01d/research \
        --record data/krx/etf-v24b/run.json

Each variant only says, from 069500's own closes up to a session's close, whether to hold it:
- ``ma60`` / ``ma120``: hold while the close is above its 60 / 120-session simple average;
- ``ma20_60``: hold while the 20-session average is above the 60-session average.
A change of state is traded at the next open through the one ledger (costs, 1%-of-ADV cap, corporate
actions, whole won): in, buy 95% of NAV sized on the deciding close; out, sell everything. Cash earns
nothing. The benchmark buys 069500 once (95%) through the same ledger with the same corporate actions.
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
from datetime import date
from decimal import ROUND_FLOOR, Decimal
from pathlib import Path
from typing import Any

from momentum.config import ExecutionCfg, SpecUndefinedError
from momentum.ledger.ledger import CorporateAction, Ledger
from momentum.portfolio.targets import TargetOrder

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
ETF = ma.MARKET_ETF
VARIANTS = ("ma60", "ma120", "ma20_60")
NEED = {"ma60": 60, "ma120": 120, "ma20_60": 60}
COSTS = {"base": 5.0, "stress": 10.0}     # bp one way: 1.5 commission + half the measured 1.1bp spread, rounded up; x2
INVESTED = Decimal("0.95")
ADV_DAYS = ma.ADV_DAYS
# v24b (user decision 2026-11-01, before any result): the research input's 069500 starts in 2020-01, so MA120
# exists only from late June 2020; every variant and the benchmark start trading on the same day instead
FIRST_TRADE = date(2020, 7, 1)
HALVES = ((date(2020, 7, 1), date(2023, 6, 30)), (date(2023, 7, 1), date(2026, 10, 20)))


def hold(closes: Sequence[float], variant: str) -> bool | None:
    """Whether the variant holds after the last of ``closes``; None without enough of them."""
    if len(closes) < NEED[variant]:
        return None
    if variant == "ma20_60":
        return statistics.fmean(closes[-20:]) > statistics.fmean(closes[-60:])
    n = NEED[variant]
    return closes[-1] > statistics.fmean(closes[-n:])


def backtest(data: Any, variant: str, cost_bps: float, trace: list | None = None) -> dict[str, Any]:
    """With ``trace``, each traded session appends (day, NAV, benchmark NAV, shares held, 069500 close) for the
    diagnosis; the run is the same with or without it."""
    if variant not in VARIANTS:
        raise SpecUndefinedError(f"unknown v24 variant {variant!r}")
    sessions = list(data.sessions)
    bars = {b.session: b for b in data.snapshot.bars if b.symbol == ETF}
    actions: dict[Any, list[CorporateAction]] = defaultdict(list)
    for announced in data.actions:
        a = announced.action
        if a.security_id == ETF:
            actions[a.ex_date].append(a)
            if a.pay_date is not None and a.pay_date != a.ex_date and a.action_type == "cash_dividend":
                actions[a.pay_date].append(a)
    factors = [(a.ex_date, announced.available_at, f) for announced in data.actions
               if (a := announced.action).security_id == ETF and (f := ma.action_factor(a)) is not None]
    cfg = ExecutionCfg(base_cost_bps=cost_bps, participation_limit=0.01, participation_lookback_days=ADV_DAYS,
                       max_weight=float(INVESTED), cash_buffer=0.005)
    book = Ledger(ma.CAPITAL, cfg, money_quantum=Decimal(1), fractional_split="drop")
    bench = Ledger(ma.CAPITAL, cfg, money_quantum=Decimal(1), fractional_split="drop")
    closes: list[tuple[Any, float]] = []
    pending: list[TargetOrder] = []
    bench_pending: list[TargetOrder] = []
    out: dict[str, Any] = {"days": [], "nav": [], "bench": [], "switches": 0, "held_sessions": 0}
    for k, day in enumerate(sessions):
        bar = bars.get(day)
        opens = {ETF: bar.open} if bar is not None and bar.open > 0 else {}
        close_px = {ETF: bar.close} if bar is not None and bar.close > 0 else {}
        if k > 0:
            lookback = sessions[max(0, k - ADV_DAYS):k]
            vols = [bars[d].volume for d in lookback if d in bars]
            adv = {ETF: statistics.fmean(vols)} if vols else {}
            today_actions = actions.get(day, [])
            book.step_session(day, sessions[k - 1], today_actions, pending if adv else [], opens, adv, close_px)
            bench.step_session(day, sessions[k - 1], today_actions, bench_pending if adv else [], opens, adv,
                               close_px)
        if bar is not None:
            closes.append((day, float(bar.close)))
        pending, bench_pending = [], []
        nxt = sessions[k + 1] if k + 1 < len(sessions) else None
        if nxt is not None and nxt >= FIRST_TRADE and bar is not None:
            if not bench.positions.get(ETF):
                qty = int((ma.CAPITAL * INVESTED / bar.close).to_integral_value(ROUND_FLOOR))
                bench_pending = [TargetOrder(ETF, "buy", qty, 0)]
            known = [(ex, f) for ex, avail, f in factors if avail <= ma._decision(day) and ex <= day]
            adjusted = ma.adjust([c for _, c in closes],
                                 [math.prod(f for ex, f in known if ex == d) for d, _ in closes])
            want = hold(adjusted, variant)
            if want is None:
                # no rule on the first trading decision would leave the variant in cash while the benchmark is
                # invested; the input must start early enough instead
                raise SpecUndefinedError(f"{variant} needs {NEED[variant]} closes before {FIRST_TRADE}, "
                                         f"has {len(adjusted)} on {day}")
            held = book.positions.get(ETF, 0)
            if want and not held:
                qty = int((book.nav * INVESTED / bar.close).to_integral_value(ROUND_FLOOR))
                pending = [TargetOrder(ETF, "buy", qty, 0)]
                out["switches"] += 1
            elif want is False and held:
                pending = [TargetOrder(ETF, "sell", held, -1)]
                out["switches"] += 1
        if day >= FIRST_TRADE:
            out["days"].append(day)
            out["nav"].append(float(book.nav))
            out["bench"].append(float(bench.nav))
            out["held_sessions"] += bool(book.positions.get(ETF))
            if trace is not None:
                trace.append((day, float(book.nav), float(bench.nav), book.positions.get(ETF, 0),
                              float(bar.close) if bar is not None else None))
    out["fills"] = book.fills
    return out


def report(result: dict[str, Any]) -> dict[str, Any]:
    days, nav, bench = result["days"], result["nav"], result["bench"]
    periods = {"full": (days[0], days[-1]), "first_half": HALVES[0], "second_half": HALVES[1]}
    table = {name: ma._stats(nav, bench, days, lo, hi) for name, (lo, hi) in periods.items()}
    passed = all(table[h].get("strategy_cagr", -1) >= table[h].get("069500_cagr", 0)
                 and table[h].get("strategy_mdd", 1) <= table[h].get("069500_mdd", 0)
                 for h in ("first_half", "second_half"))
    return {"periods": table, "e1_pass": passed, "switches": result["switches"],
            "time_in_market": result["held_sessions"] / len(days) if days else 0.0,
            "dev_excess_t": table["first_half"].get("excess_t")}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--record", type=Path, required=True, help="written once; an existing record stops the run")
    args = parser.parse_args(argv)
    if args.record.exists():
        raise SpecUndefinedError(f"{args.record} exists: v24b is recorded once")
    from momentum.agent.cli import read_input

    root = args.input
    data = read_input(root / "prices.csv", root / "securities.jsonl", root / "actions.json", root / "sessions.json",
                      securities_mode="session-confirmed")
    out = {f"{v}/{c}": report(backtest(data, v, bps)) for v in VARIANTS for c, bps in COSTS.items()}
    for name, rep in out.items():
        print(f"== {name}  E1 pass: {rep['e1_pass']}  dev excess t {rep['dev_excess_t']:.2f}  "
              f"switches {rep['switches']}  in market {rep['time_in_market']:.0%}")
        for period, row in rep["periods"].items():
            print(f"{period:<12} strategy CAGR {row['strategy_cagr']:+.2%} MDD {row['strategy_mdd']:.2%} "
                  f"vol {row['strategy_vol']:.2%} | 069500 CAGR {row['069500_cagr']:+.2%} "
                  f"MDD {row['069500_mdd']:.2%} | excess t {row['excess_t']:.2f}")
    args.record.parent.mkdir(parents=True, exist_ok=True)
    args.record.write_text(json.dumps(out, sort_keys=True, default=str) + "\n", encoding="utf-8")
    print(json.dumps(out, sort_keys=True, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
