"""Run the 27 intraday candidates on development days only (intraday-dev-plan-v11 §4).

    uv run python tools/intraday_dev.py --universe data/kis/etf-universe-v2.json \
        --bars data/kis/minute-history

Reads bars through DEVELOPMENT_END only (``development_bars``; holdout days stay
unread). Each combination runs at the planned costs and at twice the costs;
prints a table and one JSON line with the metrics and the §4 selection.
Writes nothing.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import math
import statistics
import sys
from decimal import Decimal as D
from pathlib import Path
from typing import Any

TOOLS = Path(__file__).resolve().parent


def _load(name: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, TOOLS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    assert spec is not None and spec.loader is not None
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


engine, candidates, history = _load("intraday_engine"), _load("intraday_candidates"), _load("kis_minute_history")
MIN_T, MIN_TRADE_DAYS, MAX_PICKS = 2.0, 40, 5


def metrics(result: Any, capital: D = engine.CAPITAL) -> dict[str, Any]:
    returns = [float(d["pnl"] / d["start_equity"]) for d in result.daily]
    n = len(returns)
    sd = statistics.stdev(returns) if n > 1 else 0.0
    mean = statistics.fmean(returns) if n else 0.0
    peak, mdd = float(capital), 0.0
    for d in result.daily:
        equity = float(d["end_equity"])
        peak = max(peak, equity)
        mdd = max(mdd, 1 - equity / peak)
    return {
        "days": n, "mean_daily": mean, "sd_daily": sd, "t": mean / (sd / math.sqrt(n)) if sd > 0 else 0.0,
        "net_return": float(result.final_equity / capital - 1), "max_drawdown": mdd,
        "trade_days": len({t["day"] for t in result.trades}), "trades": len(result.trades),
        "commission": float(result.commission_total), "tick_cost": float(result.tick_cost_total),
    }


def buy_and_hold(days: Any, symbol: str, weight: float = 0.95) -> dict[str, Any]:
    """069500 at 95% from the first open, marked at each close, no costs (comparison only)."""
    closes = [(day, bars[symbol][-1].close) for day, bars in days.items() if bars.get(symbol)]
    first = next(iter(days.values()))[symbol][0].open
    equity = [1 + weight * float(c / first - 1) for _, c in closes]
    peak, mdd = 1.0, 0.0
    for e in equity:
        peak = max(peak, e)
        mdd = max(mdd, 1 - e / peak)
    return {"net_return": equity[-1] - 1, "max_drawdown": mdd}


def select(rows: list[dict[str, Any]]) -> list[str]:
    """§4: t >= 2, >= 40 trade days, positive at twice the costs; best per candidate, at most five."""
    passing = [r for r in rows if r["t"] >= MIN_T and r["trade_days"] >= MIN_TRADE_DAYS and r["net_return_2x"] > 0]
    best: dict[str, dict[str, Any]] = {}
    for r in sorted(passing, key=lambda r: (-r["t"], r["name"])):
        best.setdefault(r["candidate"], r)
    return [r["name"] for r in sorted(best.values(), key=lambda r: (-r["t"], r["name"]))][:MAX_PICKS]


def evaluate(days: Any, universe: tuple[str, ...]) -> dict[str, Any]:
    rows = []
    for config in candidates.configs(universe):
        row = {"candidate": config.candidate, "name": config.name,
               **metrics(engine.run(days, config.make()))}
        row["net_return_2x"] = metrics(engine.run(days, config.make(), cost_scale=2))["net_return"]
        rows.append(row)
    return {"configs": len(rows), "rows": rows, "benchmark_069500_95": buy_and_hold(days, candidates.KOSPI),
            "selected": select(rows)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--universe", type=Path, required=True)
    parser.add_argument("--bars", type=Path, default=Path("data/kis/minute-history"))
    args = parser.parse_args(argv)
    universe = tuple(r["symbol"] for r in json.loads(args.universe.read_text(encoding="utf-8"))["symbols"])
    raw = {s: {d: b for d, b in history.development_bars(args.bars, s).items() if b} for s in universe}
    days = engine.days_from_kis(raw)
    if max(days) > history.DEVELOPMENT_END.isoformat():
        raise SystemExit("holdout day in development input")
    result = evaluate(days, universe)
    print(f"development {min(days)}~{max(days)} ({len(days)} days), {result['configs']} combinations")
    print(f"{'name':<28} {'net':>8} {'net2x':>8} {'MDD':>7} {'t':>6} {'tdays':>5} {'trades':>6}")
    for r in result["rows"]:
        print(f"{r['name']:<28} {r['net_return']:>+8.2%} {r['net_return_2x']:>+8.2%} {r['max_drawdown']:>7.2%} "
              f"{r['t']:>6.2f} {r['trade_days']:>5} {r['trades']:>6}")
    bench = result["benchmark_069500_95"]
    print(f"069500 95% hold: net {bench['net_return']:+.2%} MDD {bench['max_drawdown']:.2%}")
    print(f"selected for the holdout (§4): {result['selected'] or 'none'}")
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
