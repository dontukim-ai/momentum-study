"""v14: compare rebalancing rules for fixed target weights (docs/blueprint/allocation-rebalance-v14.md).

Mac, read-only, no network (FSC ETF pages already saved):

    uv run python tools/allocation_rebalance.py --root data/krx/raw/fsc

Each rule decides on the previous close (calendar or drift), sizes orders on
the previous close (``compute_weight_targets``) and fills at the next open
through the one engine (``Ledger.step_session``): costs, the 1%-of-ADV cap,
cancelled orders on a missing open, whole-won ledger. Trading starts once
every ETF has 20 sessions of volume history. An implausible close (-40% or
+60% in a session) stops the run rather than being repaired. Prints the
table, the §8 verdict per mix and one JSON line; writes nothing.
"""

from __future__ import annotations

import argparse
import json
import math
import statistics
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any, NamedTuple

from momentum.config import ExecutionCfg, SpecUndefinedError
from momentum.ledger.ledger import Ledger
from momentum.portfolio.targets import compute_weight_targets

ETFS = {"domestic_equity": "278530", "global_equity": "360750", "bonds": "273130", "gold": "132030",
        "cash": "357870"}
MIXES = {  # §2, fixed before any price was read
    "conservative": {"domestic_equity": 0.10, "global_equity": 0.20, "bonds": 0.50, "gold": 0.10, "cash": 0.10},
    "balanced": {"domestic_equity": 0.20, "global_equity": 0.40, "bonds": 0.30, "gold": 0.10},
    "aggressive": {"domestic_equity": 0.25, "global_equity": 0.55, "bonds": 0.15, "gold": 0.05},
}
RULES = ("H", "M", "Q", "A", "B5", "B10")
BANDS = {"B5": 0.05, "B10": 0.10}
COSTS = {"base": 4.5, "stress": 11.5}           # bp one way: 1.5 commission + 3 or 10 slippage (§11)
CAPITAL, HISTORY, YEAR = Decimal(100_000_000), 20, 252
LOW, HIGH = Decimal("0.6"), Decimal("1.6")      # close / previous close outside this is not a market move


class Bar(NamedTuple):
    open: Decimal | None
    close: Decimal | None
    volume: float | None


Prices = dict[date, dict[str, Bar]]


def _items(obj: Any) -> list[dict] | None:
    if isinstance(obj, dict):
        if "item" in obj:
            v = obj["item"]
            return v if isinstance(v, list) else [v]
        for v in obj.values():
            found = _items(v)
            if found is not None:
                return found
    return None


def _price(text: Any) -> Decimal | None:
    try:
        value = Decimal(str(text))
    except ArithmeticError:
        return None
    return value if value > 0 else None


def load_fsc(root: Path, symbols: tuple[str, ...]) -> Prices:
    """Open, close and volume of ``symbols`` from saved FSC ETF pages (<root>/etf/<date>/page-*.json)."""
    prices: Prices = {}
    for folder in sorted(p for p in (root / "etf").iterdir() if p.is_dir() and not p.name.startswith(".")):
        row: dict[str, Bar] = {}
        for page in sorted(folder.glob("page-*.json")):
            for it in _items(json.loads(page.read_text(encoding="utf-8"))) or []:
                if it.get("srtnCd") in symbols:
                    volume = it.get("trqu")
                    row[it["srtnCd"]] = Bar(_price(it.get("mkp")), _price(it.get("clpr")),
                                            float(volume) if volume not in (None, "") else None)
        if row:
            prices[date.fromisoformat(folder.name)] = row
    return prices


def decide(rule: str, prev: date, day: date, now: dict[str, float], target: dict[str, float]) -> bool:
    """Rebalance at ``day``'s open? Calendar rules look at the session dates, bands at previous-close weights."""
    if rule == "H":
        return False
    if rule == "M":
        return (day.year, day.month) != (prev.year, prev.month)
    if rule == "Q":
        return (day.year, day.month) != (prev.year, prev.month) and day.month in (1, 4, 7, 10)
    if rule == "A":
        return day.year != prev.year
    if rule in BANDS:
        return max(abs(now.get(s, 0.0) - w) for s, w in target.items()) > BANDS[rule]
    raise SpecUndefinedError(f"unknown rule {rule}")


def _check(prices: Prices, sessions: list[date], symbols: list[str]) -> None:
    last: dict[str, Decimal] = {}
    for d in sessions:
        for s in symbols:
            bar = prices[d].get(s)
            if bar is None or bar.close is None:
                continue
            if s in last and not LOW < bar.close / last[s] < HIGH:
                raise SpecUndefinedError(f"implausible close for {s} on {d}: {last[s]} -> {bar.close}")
            last[s] = bar.close


def run(prices: Prices, weights: dict[str, float], rule: str, cost_bps: float) -> dict[str, Any]:
    symbols = sorted(weights)
    sessions = sorted(prices)
    _check(prices, sessions, symbols)
    seen = {s: [i for i, d in enumerate(sessions) if s in prices[d]] for s in symbols}
    if any(len(v) <= HISTORY for v in seen.values()):
        raise SpecUndefinedError("not enough history for every ETF")
    start = max(v[0] for v in seen.values()) + HISTORY
    cfg = ExecutionCfg(base_cost_bps=cost_bps, participation_limit=0.01, participation_lookback_days=HISTORY,
                       max_weight=1.0, cash_buffer=0.005)
    ledger = Ledger(CAPITAL, cfg, money_quantum=Decimal(1))
    out: dict[str, Any] = {"sessions": [], "nav": [], "drift": [], "rebalances": [], "traded": [], "costs": []}
    for i in range(start, len(sessions)):
        day, prev = sessions[i], sessions[i - 1]
        nav = ledger.nav
        now = {s: float(q * ledger.marks[s] / nav) for s, q in ledger.positions.items() if q and nav > 0}
        orders = []
        if i == start or decide(rule, prev, day, now, weights):
            prev_close = {s: b.close for s, b in prices[prev].items() if s in weights and b.close is not None}
            orders = compute_weight_targets(weights, nav, prev_close, dict(ledger.positions), cfg)
            out["rebalances"].append(day)
        adv = {}
        for s in {o.security_id for o in orders}:
            window = [prices[d][s].volume for d in sessions[i - HISTORY:i] if s in prices[d]]
            vols = [v for v in window if v is not None]
            if vols:
                adv[s] = statistics.fmean(vols)
        opens = {s: b.open for s, b in prices[day].items()}
        closes = {s: b.close for s, b in prices[day].items() if b.close is not None}
        before = len(ledger.fills)
        ledger.step_session(day, prev, [], orders, opens, adv, closes)
        today = ledger.fills[before:]
        out["traded"].append(sum((f.shares * f.price for f in today), Decimal(0)))
        out["costs"].append(sum((f.cost for f in today), Decimal(0)))
        nav = ledger.nav
        held = {s: q * ledger.marks[s] / nav for s, q in ledger.positions.items() if q}
        out["drift"].append(float(sum(abs(held.get(s, Decimal(0)) - Decimal(str(w))) for s, w in weights.items())))
        out["sessions"].append(day)
        out["nav"].append(nav)
    out["ledger"] = ledger
    return out


def _segment(result: dict, lo: int, hi: int, opening: bool) -> dict[str, float]:
    nav = [float(x) for x in result["nav"][lo:hi]]
    base = float(result["nav"][lo - 1]) if lo > 0 else float(CAPITAL)
    returns = [b / a - 1 for a, b in zip([base, *nav], nav, strict=False)]
    years = len(nav) / YEAR
    peak, mdd = base, 0.0
    for x in nav:
        peak = max(peak, x)
        mdd = max(mdd, 1 - x / peak)
    skip = 1 if opening else 0                     # the opening buy is the same for every rule
    avg = statistics.fmean(nav)
    traded = float(sum(result["traded"][lo + skip:hi]))
    cost = float(sum(result["costs"][lo + skip:hi]))
    days = set(result["sessions"][lo + skip:hi])
    return {"sessions": len(nav), "rebalances": sum(d in days for d in result["rebalances"]),
            "turnover_per_year": traded / 2 / avg / years, "cost_bp_per_year": cost / avg / years * 1e4,
            "drift_mean": statistics.fmean(result["drift"][lo:hi]), "drift_max": max(result["drift"][lo:hi]),
            "annual_return": (nav[-1] / base) ** (1 / years) - 1,
            "volatility": statistics.pstdev(returns) * math.sqrt(YEAR), "max_drawdown": mdd}


def metrics(result: dict) -> dict[str, dict[str, float]]:
    n = len(result["nav"])
    half = n // 2
    return {"full": _segment(result, 0, n, True), "first_half": _segment(result, 0, half, True),
            "second_half": _segment(result, half, n, False)}


def judge(table: dict[str, dict[str, dict[str, float]]]) -> dict[str, Any]:
    """§8: replace A only if drift is lower, drawdown no deeper and cost at most 5bp/yr higher, in every period."""
    base = table["A"]

    def better(rule: str) -> bool:
        return all(table[rule][p]["drift_mean"] < base[p]["drift_mean"]
                   and table[rule][p]["max_drawdown"] <= base[p]["max_drawdown"]
                   and table[rule][p]["cost_bp_per_year"] - base[p]["cost_bp_per_year"] <= 5.0
                   for p in ("full", "first_half", "second_half"))

    passing = [r for r in RULES if r != "A" and r in table and better(r)]
    choice = min(passing, key=lambda r: table[r]["full"]["cost_bp_per_year"]) if passing else "A"
    return {"passing": passing, "choice": choice}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--root", type=Path, default=Path("data/krx/raw/fsc"))
    args = parser.parse_args(argv)
    try:
        prices = load_fsc(args.root, tuple(ETFS.values()))
        report: dict[str, Any] = {}
        for mix, sleeves in MIXES.items():
            weights = {ETFS[k]: w for k, w in sleeves.items()}
            report[mix] = {}
            for cost_name, bps in COSTS.items():
                table = {}
                for rule in RULES:
                    result = run(prices, weights, rule, bps)
                    table[rule] = metrics(result)
                    span = [result["sessions"][0].isoformat(), result["sessions"][-1].isoformat()]
                report[mix][cost_name] = {"table": table, "verdict": judge(table), "span": span}
    except SpecUndefinedError as exc:
        print(f"allocation-rebalance: STOP: {exc}")
        return 1
    for mix, by_cost in report.items():
        print(f"\n{mix} {by_cost['base']['span'][0]}~{by_cost['base']['span'][1]}")
        print(f"{'cost':<6} {'rule':<4} {'rebal':>5} {'turn/y':>7} {'cost bp/y':>9} {'drift':>6} {'d max':>6} "
              f"{'ret/y':>7} {'vol':>6} {'MDD':>6}")
        for cost_name, part in by_cost.items():
            for rule, m in part["table"].items():
                f = m["full"]
                print(f"{cost_name:<6} {rule:<4} {f['rebalances']:>5} {f['turnover_per_year']:>7.3f} "
                      f"{f['cost_bp_per_year']:>9.2f} {f['drift_mean']:>6.3f} {f['drift_max']:>6.3f} "
                      f"{f['annual_return']:>7.2%} {f['volatility']:>6.2%} {f['max_drawdown']:>6.2%}")
            print(f"{cost_name:<6} verdict (§8): {part['verdict']}")
    print(json.dumps(report, sort_keys=True, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
