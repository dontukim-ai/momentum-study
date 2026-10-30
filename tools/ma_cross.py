"""v20 A: buy on a 20/60 golden cross, sell on the dead cross; v21: buy the cross only in MA5>20>60>120
alignment, sell at a close 5% below or 20% above the entry fill (docs/blueprint/ma-cross-v20.md).

Mac, read-only, run once (a record file stops a second run):

    uv run --extra agent python tools/ma_cross.py --input data/krx/research-fwd-full \
        --record data/krx/ma-cross-v20/run.json            (--variant v21 --record data/krx/ma-cross-v21/run.json)

Moving averages use closes divided back through confirmed splits, bonus
issues and capital reductions (actions published by the decision). Each
session's close decides; orders are sized on that close and filled at the
next open through the one ledger (costs, 1%-of-ADV cap, corporate actions,
whole won). Up to 20 names at 5% of NAV each, no rebalancing; a dead cross,
a managed/suspended/delisted state sells everything at the next open. The
069500 benchmark is bought once (95%) through the same ledger. Prints the
table and one JSON line; writes only the record file.
"""

from __future__ import annotations

import argparse
import json
import math
import statistics
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, time
from decimal import ROUND_FLOOR, Decimal
from itertools import pairwise
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from momentum.config import ExecutionCfg, SpecUndefinedError
from momentum.ledger.ledger import CorporateAction, Ledger
from momentum.portfolio.targets import TargetOrder

KST = ZoneInfo("Asia/Seoul")
SHORT, LONG, ALIGN = 20, 60, (5, 20, 60, 120)
HISTORY = max(ALIGN) + 1                      # 121 sessions: MA120 today and yesterday
TOP, SLOTS, SLOT_WEIGHT = 300, 20, Decimal("0.05")
ADV_DAYS = 20
CAPITAL = Decimal(100_000_000)
COSTS = {"base": 36.5, "low_slippage": 21.5}   # bp one way: 1.5 + slippage 25 (or 10) + half of a 0.20% sell tax
FIRST_TRADE = date(2020, 4, 1)
HALVES = ((date(2020, 4, 1), date(2023, 6, 30)), (date(2023, 7, 1), date(2026, 10, 20)))
MARKET_ETF = "069500"
DECIDE_AT = time(18)                          # research bars are available at 18:00 KST


def sma(values: Sequence[float], n: int) -> float | None:
    """Mean of the last ``n`` values, or None without ``n`` of them."""
    return statistics.fmean(values[-n:]) if len(values) >= n else None


def cross(prev_short: float, prev_long: float, short: float, long: float) -> str | None:
    if prev_short <= prev_long and short > long:
        return "golden"
    if prev_short >= prev_long and short < long:
        return "dead"
    return None


def alignment(mas: Sequence[float]) -> str | None:
    """'bull' for MA5 > MA20 > MA60 > MA120, 'bear' for the reverse, else None."""
    if all(a > b for a, b in pairwise(mas)):
        return "bull"
    if all(a < b for a, b in pairwise(mas)):
        return "bear"
    return None


def adjust(closes: Sequence[float], factors: Sequence[float]) -> list[float]:
    """Divide every close before an action by its factor; ``factors[k]`` belongs to session k (1 = none)."""
    out, scale = [0.0] * len(closes), 1.0
    for k in range(len(closes) - 1, -1, -1):
        out[k] = closes[k] / scale
        scale *= factors[k]
    return out


def signals(adjusted: Sequence[float]) -> dict[str, Any]:
    """Cross, alignment and the moving averages on the last of ``adjusted`` (needs HISTORY closes)."""
    if len(adjusted) < HISTORY:
        raise SpecUndefinedError(f"need {HISTORY} closes, have {len(adjusted)}")
    today, yesterday = adjusted[-HISTORY:], adjusted[-HISTORY:-1]
    ma = {n: sma(today, n) for n in ALIGN}
    ma_prev = {n: sma(yesterday, n) for n in ALIGN}
    now = alignment([ma[n] for n in ALIGN])  # type: ignore[misc]
    before = alignment([ma_prev[n] for n in ALIGN])  # type: ignore[misc]
    return {"cross": cross(ma_prev[SHORT], ma_prev[LONG], ma[SHORT], ma[LONG]),  # type: ignore[arg-type]
            "alignment": now, "entered": now if now is not None and now != before else None,
            "ma": ma}


def action_factor(action: CorporateAction) -> float | None:
    """How much the price before ``ex_date`` must be divided by (the ledger's own ratios)."""
    if action.split_ratio is None:
        return None
    if action.action_type in ("split", "capital_reduction"):
        return float(action.split_ratio)
    if action.action_type == "bonus_issue":
        return 1 + float(action.split_ratio)
    return None


@dataclass(frozen=True)
class Rule:
    """Entry and exit of one preregistered variant; the engine, universe, sizing and costs are shared."""
    name: str
    entry: str = "golden"          # "golden": 20/60 golden cross; "golden_bull": the cross on a day MA5>20>60>120
    exit: str = "dead"             # "dead": the 20/60 dead cross; "stop_take": close vs entry price
    stop: float = 0.05             # stop_take: sell once the close is more than 5% below the entry price
    take: float = 0.20             # stop_take: sell once the close is 20% or more above it


RULES = {"v20": Rule("v20"), "v21": Rule("v21", entry="golden_bull", exit="stop_take")}
JUMP_LIMIT = 0.30                            # KRX daily price limit since 2015-06
SHARE_ACTIONS = ("split", "bonus_issue", "capital_reduction")


def unexplained(ratio: float, kinds: Sequence[str]) -> bool:
    """A move beyond the daily limit with no share action in between: an unrecorded split, merger or error.

    A limit-up or limit-down day can print a hair past 30% of the previous close (the limit is taken on the
    exchange's base price and rounded to the tick), so the line sits half a point beyond the limit."""
    return abs(ratio - 1) > JUMP_LIMIT + 0.005 and not any(k in SHARE_ACTIONS for k in kinds)


def common_stock(symbol: str) -> bool:
    return len(symbol) == 6 and symbol.isdigit() and symbol.endswith("0")


def _decision(d: date) -> datetime:
    return datetime.combine(d, DECIDE_AT, KST)


def _stats(nav: list[float], bench: list[float], days: list[date], lo: date, hi: date) -> dict[str, float]:
    idx = [k for k, d in enumerate(days) if lo <= d <= hi]
    if len(idx) < 2:
        return {}
    a, b = idx[0], idx[-1]
    years = (b - a) / 252
    out: dict[str, float] = {}
    for name, series in (("strategy", nav), ("069500", bench)):
        seg = series[a:b + 1]
        rets = [y / x - 1 for x, y in pairwise(seg)]
        peak, mdd = seg[0], 0.0
        for v in seg:
            peak = max(peak, v)
            mdd = max(mdd, 1 - v / peak)
        out[f"{name}_cagr"] = (seg[-1] / seg[0]) ** (1 / years) - 1 if years > 0 else math.nan
        out[f"{name}_mdd"] = mdd
        out[f"{name}_vol"] = statistics.pstdev(rets) * math.sqrt(252)
    excess = [(nav[k + 1] / nav[k]) - (bench[k + 1] / bench[k]) for k in range(a, b)]
    sd = statistics.stdev(excess) if len(excess) > 1 else 0.0
    out["excess_t"] = statistics.fmean(excess) / (sd / math.sqrt(len(excess))) if sd > 0 else math.nan
    return out


def _trades(fills: list, sessions: list[date]) -> dict[str, Any]:
    """Round trips per symbol: buys until the first sell, sells until the next buy (partial fills included)."""
    pos = {d: k for k, d in enumerate(sessions)}
    live: dict[str, dict[str, Any]] = {}
    done = []
    for f in fills:
        if f.shares <= 0:
            continue
        t = live.get(f.security_id)
        if f.side == "buy":
            if t is not None and t["selling"]:
                done.append(live.pop(f.security_id))
                t = None
            if t is None:
                t = live[f.security_id] = {"start": f.session_date, "end": None, "cost": Decimal(0),
                                           "proceeds": Decimal(0), "selling": False}
            t["cost"] += f.price * f.shares + f.cost
        elif t is not None:
            t["proceeds"] += f.price * f.shares - f.cost
            t["selling"], t["end"] = True, f.session_date
    done += [t for t in live.values() if t["selling"]]
    if not done:
        return {"trades": 0}
    rets = [float(t["proceeds"] / t["cost"] - 1) for t in done]
    days = [pos[t["end"]] - pos[t["start"]] for t in done if t["start"] in pos and t["end"] in pos]
    return {"trades": len(done), "win_rate": sum(r > 0 for r in rets) / len(rets),
            "mean_ret": statistics.fmean(rets), "median_ret": statistics.median(rets),
            "mean_days": statistics.fmean(days) if days else math.nan,
            "open_at_end": sum(not t["selling"] for t in live.values())}


def _adv(bars: dict, lookback: list[date], orders: list[TargetOrder]) -> dict[str, float]:
    out = {}
    for o in orders:
        vols = [bars[d][o.security_id].volume for d in lookback if o.security_id in bars.get(d, {})]
        if vols:
            out[o.security_id] = statistics.fmean(vols)
    return out


def _eligible(state: Any) -> bool:
    return (state is not None and state.status == "NORMAL" and state.asset_type == "STOCK"
            and state.market in ("KOSPI", "KOSDAQ") and not state.managed)


def _universe(bars: dict, sessions: list[date], k: int, states: dict) -> dict[str, float]:
    """20-session mean trading value of eligible common stocks present on every one of those sessions."""
    window = sessions[max(0, k - ADV_DAYS + 1):k + 1]
    common = set.intersection(*(set(bars.get(d, {})) for d in window)) if window else set()
    return {s: statistics.fmean(float(bars[d][s].close) * bars[d][s].volume for d in window)
            for s in common if common_stock(s) and _eligible(states.get(s))}


def backtest(data: Any, cost_bps: float, trace: list | None = None, strict: bool = True,
             rule: Rule = RULES["v20"]) -> dict[str, Any]:
    """Run A on a ResearchInput: daily NAVs of the strategy and the 069500 benchmark, fills and counts.

    With ``trace``, each session appends (day, NAV, {symbol: (shares, mark, close, open)}) for every name
    held into or out of that session (close/open None without a bar) for the diagnosis; the run itself is
    the same with or without it.

    ``strict``: a held name whose open (or close) moves beyond the daily limit from its last close with no
    split, bonus issue or capital reduction in between stops the run (rule 3: an unrecorded share change
    must not be booked as profit or loss). The diagnosis passes False and gets the cases in ``jumps``."""
    sessions = list(data.sessions)
    bars: dict[date, dict[str, Any]] = defaultdict(dict)
    for bar in data.snapshot.bars:
        bars[bar.session][bar.symbol] = bar
    actions_by_day: dict[date, list[CorporateAction]] = defaultdict(list)
    data_actions: dict[str, list[CorporateAction]] = defaultdict(list)
    jumps: list[dict[str, Any]] = []
    factor_at: dict[str, list[tuple[date, datetime, float]]] = defaultdict(list)
    for announced in data.actions:
        a = announced.action
        data_actions[a.security_id].append(a)
        actions_by_day[a.ex_date].append(a)
        if a.pay_date is not None and a.pay_date != a.ex_date and a.action_type in ("cash_dividend", "bonus_issue"):
            actions_by_day[a.pay_date].append(a)
        f = action_factor(a)
        if f is not None:
            factor_at[a.security_id].append((a.ex_date, announced.available_at, f))
    cfg = ExecutionCfg(base_cost_bps=cost_bps, participation_limit=0.01, participation_lookback_days=ADV_DAYS,
                       max_weight=float(SLOT_WEIGHT), cash_buffer=0.005)
    book = Ledger(CAPITAL, cfg, money_quantum=Decimal(1), fractional_split="drop")
    bench = Ledger(CAPITAL, cfg, money_quantum=Decimal(1), fractional_split="drop")
    closes: dict[str, list[tuple[date, float]]] = defaultdict(list)
    counts = {"golden_in_top": 0, "dead_exits": 0, "entries_skipped_full": 0, "forced_exits": 0,
              "written_off": 0, "orders_waiting_no_adv": 0,
              "orders_deferred_action": 0, "stop_exits": 0, "take_exits": 0}
    out: dict[str, Any] = {"days": [], "nav": [], "bench": []}
    pending: list[TargetOrder] = []
    bench_pending: list[TargetOrder] = []
    exiting: set[str] = set()
    waited: set[str] = set()                     # names whose orders waited for volume (long suspensions)
    need = HISTORY if rule.entry == "golden_bull" else LONG + 1   # 121 for MA120 alignment, 61 for the cross
    entry_px: dict[str, float] = {}                # stop_take: fill price, rescaled by share actions
    for k, day in enumerate(sessions):
        today = bars.get(day, {})
        opens = {s: b.open for s, b in today.items() if b.open > 0}
        close_px = {s: b.close for s, b in today.items() if b.close > 0}
        held_open = {s for s, q in book.positions.items() if q > 0}   # for the trace: names held into today
        if k > 0:
            prev = sessions[k - 1]

            lookback = sessions[max(0, k - ADV_DAYS):k]
            adv = _adv(bars, lookback, pending)
            # no volume in the last 20 sessions (a long suspension): the engine cannot size the cap, so the
            # order waits; the next decision issues it again
            counts["orders_waiting_no_adv"] += sum(o.security_id not in adv for o in pending)
            waited |= {o.security_id for o in pending if o.security_id not in adv}
            pending = [o for o in pending if o.security_id in adv]
            # a split, bonus issue or capital reduction applied this morning changes the shares an order was
            # sized for (the ledger applies actions before fills): such orders wait a session; sells come back
            # through ``exiting`` sized on the new holding, a deferred buy is not repeated
            reshaped = {a.security_id for a in actions_by_day.get(day, [])
                        if a.ex_date == day and a.action_type in ("split", "bonus_issue", "capital_reduction")}
            counts["orders_deferred_action"] += sum(o.security_id in reshaped for o in pending)
            pending = [o for o in pending if o.security_id not in reshaped]
            for s in sorted(q for q, n in book.positions.items() if n > 0 and q in today):
                last_day, last_close = closes[s][-1]
                px = float(today[s].open) if today[s].open > 0 else float(today[s].close)
                kinds = [a.action_type for a in data_actions.get(s, []) if last_day < a.ex_date <= day]
                if unexplained(px / last_close, kinds):
                    jumps.append({"symbol": s, "last_day": last_day.isoformat(), "day": day.isoformat(),
                                  "ratio": px / last_close})
                    if strict:
                        raise SpecUndefinedError(
                            f"held {s} moved x{px / last_close:.3f} from {last_day} to {day} with no recorded "
                            "share action: an unrecorded split, merger or data error (rule 3, not booked)")
            before = len(book.fills)
            book.step_session(day, prev, actions_by_day.get(day, []), pending, opens, adv, close_px)
            for a in actions_by_day.get(day, []):
                f = action_factor(a)
                if a.ex_date == day and f and a.security_id in entry_px:
                    entry_px[a.security_id] /= f
            for fill in book.fills[before:]:
                if fill.side == "buy" and fill.shares > 0:
                    entry_px[fill.security_id] = float(fill.price)
            for s_ in [s_ for s_ in entry_px if not book.positions.get(s_)]:
                entry_px.pop(s_)
            bench.step_session(day, prev, [], bench_pending, opens, _adv(bars, lookback, bench_pending), close_px)
        for s, b in today.items():
            closes[s].append((day, float(b.close)))
        pending, bench_pending = [], []
        nxt = sessions[k + 1] if k + 1 < len(sessions) else None
        trading = nxt is not None and nxt >= FIRST_TRADE
        if trading and not bench.positions.get(MARKET_ETF) and MARKET_ETF in today:
            qty = int((CAPITAL * Decimal("0.95") / today[MARKET_ETF].close).to_integral_value(ROUND_FLOOR))
            bench_pending = [TargetOrder(MARKET_ETF, "buy", qty, 0)]
        states = data.securities.states(_decision(day))
        held = {s: q for s, q in book.positions.items() if q > 0}
        for s in list(held):
            st = states.get(s)
            if st is not None and st.status == "DELISTED" and s not in today:
                if book.write_off(s, day):
                    counts["written_off"] += 1
                held.pop(s)
        exiting &= set(held)                       # sold names leave; the rest keep their sell order
        candidates = []
        for s in set(held) | {s for s in today if common_stock(s)}:
            if s in held and s not in exiting and not _eligible(states.get(s)):
                exiting.add(s)
                counts["forced_exits"] += 1
            series = closes.get(s, [])
            if rule.exit == "stop_take" and s in held:
                if s not in exiting and series and series[-1][0] == day and s in entry_px:
                    move = series[-1][1] / entry_px[s] - 1
                    if move < -rule.stop or move >= rule.take:
                        exiting.add(s)
                        counts["stop_exits" if move < 0 else "take_exits"] += 1
                continue
            if not series or series[-1][0] != day or len(series) < need or series[-need][0] != sessions[k - need + 1]:
                continue
            window = series[-need:]
            known = [(ex, f) for ex, avail, f in factor_at.get(s, []) if avail <= _decision(day) and ex <= day]
            factors = [math.prod(f for ex, f in known if ex == d) for d, _ in window]
            adjusted = adjust([c for _, c in window], factors)
            signal = cross(sma(adjusted[:-1], SHORT), sma(adjusted[:-1], LONG),  # type: ignore[arg-type]
                           sma(adjusted, SHORT), sma(adjusted, LONG))  # type: ignore[arg-type]
            if rule.entry == "golden_bull" and signal == "golden" and signals(adjusted)["alignment"] != "bull":
                signal = None
            if s in held:
                if signal == "dead" and s not in exiting:
                    exiting.add(s)
                    counts["dead_exits"] += 1
            elif signal == "golden" and _eligible(states.get(s)):
                candidates.append(s)
        # a sell stays on the book until it fills: a missing open or a long suspension only delays it
        pending += [TargetOrder(s, "sell", held[s], -1) for s in sorted(exiting)]
        if trading and candidates:
            values = _universe(bars, sessions, k, states)
            top = set(sorted(values, key=values.__getitem__, reverse=True)[:TOP])
            ranked = sorted((s for s in candidates if s in top), key=values.__getitem__, reverse=True)
            counts["golden_in_top"] += len(ranked)
            free = SLOTS - len(held)                 # a sell frees its slot once it has filled
            investable = book.nav * (Decimal(1) - Decimal(str(cfg.cash_buffer)))
            for rank, s in enumerate(ranked):
                if free <= 0:
                    counts["entries_skipped_full"] += 1
                    continue
                qty = int((investable * SLOT_WEIGHT / today[s].close).to_integral_value(ROUND_FLOOR))
                if qty > 0:
                    pending.append(TargetOrder(s, "buy", qty, rank))
                    free -= 1
        if trace is not None and day >= FIRST_TRADE:
            names = held_open | {s for s, q in book.positions.items() if q > 0}
            trace.append((day, float(book.nav),
                          {s: (book.positions.get(s, 0), float(book.marks.get(s, 0)),
                               float(today[s].close) if s in today else None,
                               float(today[s].open) if s in today and today[s].open > 0 else None)
                           for s in names}))
        if day >= FIRST_TRADE:
            out["days"].append(day)
            out["nav"].append(float(book.nav))
            out["bench"].append(float(bench.nav))
    out["fills"] = book.fills
    out["counts"] = counts
    out["fractions_dropped"] = len(book.fractions_dropped)
    out["held_at_end"] = sum(1 for q in book.positions.values() if q > 0)
    out["waited_symbols"] = sorted(waited)
    out["jumps"] = jumps
    return out


def report(result: dict[str, Any]) -> dict[str, Any]:
    days, nav, bench = result["days"], result["nav"], result["bench"]
    periods = {"full": (days[0], days[-1]), "first_half": HALVES[0], "second_half": HALVES[1]}
    table = {name: _stats(nav, bench, days, lo, hi) for name, (lo, hi) in periods.items()}
    passed = all(table[h].get("strategy_cagr", -1) >= table[h].get("069500_cagr", 0)
                 and table[h].get("strategy_mdd", 1) <= table[h].get("069500_mdd", 0)
                 for h in ("first_half", "second_half"))
    return {"periods": table, "trades": _trades(result["fills"], days), "counts": result["counts"],
            "fractions_dropped": result["fractions_dropped"], "e1_pass": passed,
            "waited_symbols": result.get("waited_symbols", []),
            "dev_excess_t": table["first_half"].get("excess_t")}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--record", type=Path, required=True, help="written once; an existing record stops the run")
    parser.add_argument("--variant", choices=sorted(RULES), default="v20")
    args = parser.parse_args(argv)
    if args.record.exists():
        raise SpecUndefinedError(f"{args.record} exists: the {args.variant} backtest is recorded once")
    from momentum.agent.cli import read_input

    root = args.input
    data = read_input(root / "prices.csv", root / "securities.jsonl", root / "actions.json", root / "sessions.json",
                      securities_mode="session-confirmed")
    rule = RULES[args.variant]
    out = {name: report(backtest(data, bps, rule=rule)) for name, bps in COSTS.items()}
    print(f"variant {rule}")
    for name, rep in out.items():
        print(f"== {name} ({COSTS[name]} bp one way)  E1 pass: {rep['e1_pass']}  dev excess t {rep['dev_excess_t']:.2f}")
        for period, row in rep["periods"].items():
            print(f"{period:<12} strategy CAGR {row['strategy_cagr']:+.2%} MDD {row['strategy_mdd']:.2%} "
                  f"vol {row['strategy_vol']:.2%} | 069500 CAGR {row['069500_cagr']:+.2%} "
                  f"MDD {row['069500_mdd']:.2%} | excess t {row['excess_t']:.2f}")
        print(f"trades {rep['trades']}  counts {rep['counts']}")
    args.record.parent.mkdir(parents=True, exist_ok=True)
    args.record.write_text(json.dumps({"variant": args.variant, **out}, sort_keys=True, default=str) + "\n",
                           encoding="utf-8")
    print(json.dumps(out, sort_keys=True, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
