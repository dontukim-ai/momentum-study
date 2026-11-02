"""v31: the diversity-weighted portfolio against 069500 (docs/blueprint/diversity-weighted-v31.md).

Mac, read-only, run once (a record file stops a second run):

    uv run --extra agent python tools/dwp_study.py --input data/krx/v2-2026-11-01d/research \
        --events data/krx/v2-2026-11-01/market-events.json --shares data/krx/dwp-v31/shares.csv \
        --record data/krx/dwp-v31/run.json

At the close of the last session of March, June, September and December, the 200 eligible common stocks
(not quarantined) with the largest close x listed shares published by then are weighted
pi_i = mu_i^p / sum mu_j^p (mu: market-cap share among the 200); 95% of NAV is set to those weights at the next
open and held as shares until the next rebalance. The ledger, fills and costs are the shared engine's
(momentum.ledger.Ledger, as tools/ma_cross.py and tools/etf_timing.py); this file only says what to hold.
Prints the table and one JSON line; writes only the record file.
"""

from __future__ import annotations

import argparse
import csv
import importlib.util
import json
import math
import sys
from collections import defaultdict
from datetime import date, datetime
from decimal import ROUND_FLOOR, Decimal
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
VARIANTS = {"dwp_p0": 0.0, "dwp_p05": 0.5, "dwp_p08": 0.8, "cap_p1": 1.0}
JUDGED = ("dwp_p0", "dwp_p05", "dwp_p08")         # cap_p1 is the same-universe cap-weighted contrast
COSTS = {"base": 36.5, "low": 21.5}               # bp one way; the verdict uses base
TOP = 200
INVESTED = Decimal("0.95")
MDD_SLACK = 1.5                                   # v9 §4.1: drawdown at most 1.5 x 069500's
REBALANCE_MONTHS = (3, 6, 9, 12)
DIVERSITY_P = (0.5, 0.8)


def weights(caps: dict[str, float], p: float) -> dict[str, float]:
    """pi_i = mu_i^p / sum mu_j^p, mu the cap share; p = 0 is equal weight, p = 1 the cap weight."""
    total = sum(caps.values())
    raw = {s: (c / total) ** p for s, c in caps.items()}
    z = sum(raw.values())
    return {s: v / z for s, v in raw.items()}


def decision_days(sessions: list[date]) -> list[int]:
    """Indexes of the last session of each rebalance month."""
    return [k for k, d in enumerate(sessions) if d.month in REBALANCE_MONTHS
            and (k + 1 == len(sessions) or sessions[k + 1].month != d.month)]


def read_shares(path: Path) -> Any:
    """Point-in-time listed shares (tools/fsc_shares.py CSV) as the kis-agent share history."""
    from trading_agent.contracts import timestamp
    from trading_agent.fundamentals import FundamentalHistory, ShareCount

    with path.open(newline="", encoding="utf-8") as handle:
        rows = [ShareCount(r["symbol"], date.fromisoformat(r["session"]), date.fromisoformat(r["through"]),
                           timestamp(r["available_at"]), int(r["listed_shares"]), r["source"])
                for r in csv.DictReader(handle)]
    return FundamentalHistory([], rows)


def universe(today: dict[str, Any], states: dict[str, Any], shares: Any, market: Any, day: date, at: datetime,
             top: int = TOP) -> tuple[dict[str, float], dict[str, Any]]:
    """The ``top`` eligible common stocks by close x published listed shares; a stock without either is
    left out and counted, never filled. Fewer than ``top`` is undefined."""
    from momentum.config import SpecUndefinedError

    caps, missing = {}, 0
    for s, bar in today.items():
        if not ma.common_stock(s) or not ma._eligible(states.get(s)) or market.quarantined(s, at):
            continue
        n = shares.listed_shares(s, day, at)
        if n is None or bar.close <= 0:
            missing += 1
            continue
        caps[s] = float(bar.close) * n
    if len(caps) < top:
        raise SpecUndefinedError(f"{day}: {len(caps)} stocks with a market cap, fewer than {top}")
    chosen = dict(sorted(caps.items(), key=lambda kv: (-kv[1], kv[0]))[:top])
    mu = weights(chosen, 1.0)
    ranked = sorted(mu.values(), reverse=True)
    diag = {"day": day.isoformat(), "missing_shares": missing, "mu_max": ranked[0], "top2": sum(ranked[:2]),
            **{f"D_{p}": sum(m ** p for m in mu.values()) ** (1 / p) for p in DIVERSITY_P}}
    return chosen, diag


def backtest(data: Any, shares: Any, p: float, cost_bps: float, market: Any = None, top: int = TOP,
             first_trade: date = ma.FIRST_TRADE) -> dict[str, Any]:
    from momentum.config import ExecutionCfg, SpecUndefinedError
    from momentum.krx.market_events import MarketEvents
    from momentum.ledger.ledger import CorporateAction, Ledger
    from momentum.portfolio.targets import TargetOrder

    market = market or MarketEvents([])
    sessions = list(data.sessions)
    bars: dict[date, dict[str, Any]] = defaultdict(dict)
    for bar in data.snapshot.bars:
        bars[bar.session][bar.symbol] = bar
    actions_by_day: dict[date, list[CorporateAction]] = defaultdict(list)
    data_actions: dict[str, list[CorporateAction]] = defaultdict(list)
    for announced in data.actions:
        a = announced.action
        data_actions[a.security_id].append(a)
        actions_by_day[a.ex_date].append(a)
        if a.pay_date is not None and a.pay_date != a.ex_date and a.action_type in ("cash_dividend", "bonus_issue"):
            actions_by_day[a.pay_date].append(a)
    cfg = ExecutionCfg(base_cost_bps=cost_bps, participation_limit=0.01, participation_lookback_days=ma.ADV_DAYS,
                       max_weight=float(INVESTED), cash_buffer=0.005)
    book = Ledger(ma.CAPITAL, cfg, money_quantum=Decimal(1), fractional_split="drop")
    bench = Ledger(ma.CAPITAL, cfg, money_quantum=Decimal(1), fractional_split="drop")
    rebalance = set(decision_days(sessions))
    last: dict[str, tuple[date, float]] = {}
    counts = {"rebalances": 0, "quarantine_exits": 0, "written_off": 0, "orders_waiting_no_adv": 0,
              "orders_deferred_action": 0, "buys_cancelled": 0, "buys_short": 0, "sells_carried": 0}
    out: dict[str, Any] = {"days": [], "nav": [], "bench": [], "diagnostics": []}
    pending: list[Any] = []
    bench_pending: list[Any] = []
    for k, day in enumerate(sessions):
        today = bars.get(day, {})
        opens = {s: b.open for s, b in today.items() if b.open > 0}
        close_px = {s: b.close for s, b in today.items() if b.close > 0}
        carried: list[Any] = []
        if k > 0:
            prev = sessions[k - 1]
            for s in sorted(q for q, n in book.positions.items() if n > 0 and q in today and q in last):
                last_day, last_close = last[s]
                px = float(today[s].open) if today[s].open > 0 else float(today[s].close)
                kinds = [a.action_type for a in data_actions.get(s, []) if last_day < a.ex_date <= day]
                if ma.unexplained(px / last_close, kinds, market.liquidating(s, day)):
                    raise SpecUndefinedError(
                        f"held {s} moved x{px / last_close:.3f} from {last_day} to {day} with no recorded share "
                        "action: an unrecorded split, merger or data error (rule 3, not booked)")
            lookback = sessions[max(0, k - ma.ADV_DAYS):k]
            adv = ma._adv(bars, lookback, pending)
            counts["orders_waiting_no_adv"] += sum(o.security_id not in adv for o in pending)
            reshaped = {a.security_id for a in actions_by_day.get(day, [])
                        if a.ex_date == day and a.action_type in ma.SHARE_ACTIONS}
            counts["orders_deferred_action"] += sum(o.security_id in reshaped for o in pending)
            live = [o for o in pending if o.security_id in adv and o.security_id not in reshaped]
            before = len(book.fills)
            book.step_session(day, prev, actions_by_day.get(day, []), live, opens, adv, close_px)
            done: dict[tuple[str, str], int] = defaultdict(int)
            for f in book.fills[before:]:
                done[(f.security_id, f.side)] += f.shares
            for o in pending:
                left = o.shares - done[(o.security_id, o.side)]
                if o.side == "sell" and left > 0 and book.positions.get(o.security_id, 0) > 0:
                    # a sell stays on the book until it fills (sized on the holding if a share action came)
                    carried.append(TargetOrder(o.security_id, "sell", min(left, book.positions[o.security_id]), -1))
                    counts["sells_carried"] += 1
                elif o.side == "buy" and left > 0:
                    counts["buys_cancelled" if left == o.shares else "buys_short"] += 1
            # 069500's own distributions are booked like the stocks' dividends (tools/ma_cross.py passes none)
            etf_actions = [a for a in actions_by_day.get(day, []) if a.security_id == ma.MARKET_ETF]
            bench.step_session(day, prev, etf_actions, bench_pending, opens, ma._adv(bars, lookback, bench_pending),
                               close_px)
        for s, b in today.items():
            if b.close > 0:
                last[s] = (day, float(b.close))
        pending, bench_pending = carried, []
        nxt = sessions[k + 1] if k + 1 < len(sessions) else None
        trading = nxt is not None and nxt >= first_trade
        if trading and not bench.positions.get(ma.MARKET_ETF) and ma.MARKET_ETF in today:
            qty = int((ma.CAPITAL * INVESTED / today[ma.MARKET_ETF].close).to_integral_value(ROUND_FLOOR))
            bench_pending = [TargetOrder(ma.MARKET_ETF, "buy", qty, 0)]
        at = ma._decision(day)
        states = data.securities.states(at)
        for s in [s for s, q in book.positions.items() if q > 0]:
            st = states.get(s)
            if st is not None and st.status == "DELISTED" and s not in today and book.write_off(s, day):
                counts["written_off"] += 1
        held = {s: q for s, q in book.positions.items() if q > 0}
        if trading and k in rebalance:
            chosen, diag = universe(today, states, shares, market, day, at, top)
            out["diagnostics"].append(diag)
            counts["rebalances"] += 1
            pi = weights(chosen, p)
            investable = book.nav * INVESTED
            target = {s: int((investable * Decimal(str(w)) / today[s].close).to_integral_value(ROUND_FLOOR))
                      for s, w in pi.items()}
            pending = [TargetOrder(s, "sell", held[s] - target.get(s, 0), -1)
                       for s in sorted(held) if held[s] > target.get(s, 0)]
            buys = sorted((s for s in target if target[s] > held.get(s, 0)), key=lambda s: (-pi[s], s))
            pending += [TargetOrder(s, "buy", target[s] - held.get(s, 0), r) for r, s in enumerate(buys)]
        elif trading:
            exits = {s for s in held if market.quarantined(s, at)} - {o.security_id for o in pending}
            counts["quarantine_exits"] += len(exits)
            pending += [TargetOrder(s, "sell", held[s], -1) for s in sorted(exits)]
        if day >= first_trade:
            out["days"].append(day)
            out["nav"].append(float(book.nav))
            out["bench"].append(float(bench.nav))
    out["fills"] = book.fills
    out["counts"] = counts
    return out


def report(result: dict[str, Any]) -> dict[str, Any]:
    days, nav, bench = result["days"], result["nav"], result["bench"]
    periods = {"full": (days[0], days[-1]), "first_half": ma.HALVES[0], "second_half": ma.HALVES[1]}
    table = {name: ma._stats(nav, bench, days, lo, hi) for name, (lo, hi) in periods.items()}
    passed = all(table[h].get("strategy_cagr", -1) >= table[h].get("069500_cagr", 0)
                 and table[h].get("strategy_mdd", 1) <= table[h].get("069500_mdd", 0) * MDD_SLACK
                 for h in ("first_half", "second_half"))
    fills = [f for f in result["fills"] if f.shares > 0]
    years = len(days) / 252
    traded = sum(float(f.price * f.shares) for f in fills)
    mean_nav = sum(nav) / len(nav)
    return {"periods": table, "e1_pass": passed, "dev_excess_t": table["first_half"].get("excess_t"),
            "turnover_per_year": traded / 2 / mean_nav / years if years else math.nan,
            "costs": float(sum(f.cost for f in fills)), "counts": result["counts"]}


def study(data: Any, shares: Any, market: Any = None, top: int = TOP) -> dict[str, Any]:
    runs: dict[str, Any] = {}
    diagnostics: list[dict[str, Any]] = []
    for name, p in VARIANTS.items():
        for cost, bps in COSTS.items():
            result = backtest(data, shares, p, bps, market, top)
            runs.setdefault(name, {})[cost] = report(result)
            diagnostics = result["diagnostics"]
    judged = {name: {"e1_pass": runs[name]["base"]["e1_pass"], "dev_t": runs[name]["base"]["dev_excess_t"]}
              for name in JUDGED}
    return {"top": top, "invested": float(INVESTED), "mdd_slack": MDD_SLACK, "costs_bp": COSTS,
            "variants": VARIANTS, "runs": runs, "judged": judged, "diagnostics": diagnostics}


def text(result: dict[str, Any]) -> str:
    lines = [(f"v31 diversity-weighted, top {result['top']} by cap, {result['invested']:.0%} invested, quarterly; "
              f"E1 needs CAGR >= 069500 and MDD <= {result['mdd_slack']} x 069500's in both halves (base cost)")]
    for name, by_cost in result["runs"].items():
        verdict = (f"E1 {'PASS' if result['judged'][name]['e1_pass'] else 'no'}" if name in result["judged"]
                   else "contrast, not judged")
        for cost, r in by_cost.items():
            lines.append(f"\n{name} ({cost} {result['costs_bp'][cost]}bp): {verdict}; turnover "
                         f"{r['turnover_per_year']:.2f}/yr, costs {r['costs']:,.0f}, counts {json.dumps(r['counts'])}")
            for half in ("first_half", "second_half", "full"):
                s = r["periods"][half]
                if not s:
                    lines.append(f"  {half}: no sessions")
                    continue
                lines.append(f"  {half}: CAGR {s['strategy_cagr']:+.2%} vs {s['069500_cagr']:+.2%}, MDD "
                             f"{s['strategy_mdd']:.1%} vs {s['069500_mdd']:.1%}, vol {s['strategy_vol']:.1%} vs "
                             f"{s['069500_vol']:.1%}, daily excess t {s['excess_t']:.2f}")
    lines.append("\ndiagnostics (not judged): day, mu_max, top2, D_0.5, D_0.8, missing shares")
    lines += [f"  {d['day']} {d['mu_max']:.3f} {d['top2']:.3f} {d['D_0.5']:.3f} {d['D_0.8']:.3f} "
              f"{d['missing_shares']}" for d in result["diagnostics"]]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--events", type=Path, required=True, help="market events (tools/market_events.py)")
    parser.add_argument("--shares", type=Path, required=True, help="listed-share runs (tools/fsc_shares.py)")
    parser.add_argument("--record", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.record.exists():
        raise SystemExit(f"{args.record} exists: v31 runs once")
    from momentum.agent.cli import read_input
    from momentum.krx.market_events import MarketEvents

    root = args.input
    data = read_input(root / "prices.csv", root / "securities.jsonl", root / "actions.json", root / "sessions.json",
                      securities_mode="session-confirmed")
    result = study(data, read_shares(args.shares), MarketEvents.load(args.events))
    args.record.parent.mkdir(parents=True, exist_ok=True)
    args.record.write_text(json.dumps(result, sort_keys=True, default=str) + "\n", encoding="utf-8")
    print(text(result))
    print(json.dumps(result, sort_keys=True, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
