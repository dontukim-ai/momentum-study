"""v22: choose v21's stop and take on the development half, check the choice once, then record it forward.

docs/blueprint/ma-cross-v20.md (v22). Mac, read-only apart from the two files named below:

    uv run --extra agent python tools/ma_grid.py select --input data/krx/research-fwd-full \
        --record data/krx/ma-v22/run.json --frozen data/krx/ma-v22/frozen.json
    uv run --extra agent python tools/ma_grid.py forward --input data/krx/fwd9/<end>/research \
        --frozen data/krx/ma-v22/frozen.json

``select`` runs v21's entry (golden cross in MA5>20>60>120 alignment) with
each stop x take of the grid on sessions up to the end of the development
half only (later sessions are never read), keeps the cell with the highest
development t of daily excess return over 069500 (cells the safety stop
halts are left out), then runs that one cell once over the whole history:
its second half is the validation (v20's E1 test). The record and the frozen
choice are written once. ``forward`` runs the frozen cell from 2026-10-21 on a
later research input and prints the forward record against 069500; it writes
nothing. No orders anywhere.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from collections.abc import Callable
from dataclasses import asdict, replace
from datetime import date
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
STOPS = (0.03, 0.05, 0.07, 0.10, 0.15)
TAKES = (0.10, 0.20, 0.30, 0.50)
DEV = (date(2020, 4, 1), date(2023, 6, 30))
FORWARD_FROM = date(2026, 10, 21)       # the first session after research-fwd-full (v9's forward start)


def cells() -> list[Any]:
    return [replace(ma.RULES["v21"], name=f"v22 s{int(s * 100)} t{int(t * 100)}", stop=s, take=t)
            for s in STOPS for t in TAKES]


def select(data: Any, run: Callable[..., dict] = ma.backtest) -> dict[str, Any]:
    """Development-only grid; the chosen cell has the highest development excess t (ties: grid order)."""
    rows = []
    for rule in cells():
        try:
            result = run(data, ma.COSTS["base"], rule=rule, last_day=DEV[1])
        except ma.SpecUndefinedError as exc:          # the safety stop: an unrecorded share change was held
            rows.append({"cell": rule.name, "stop": rule.stop, "take": rule.take, "stopped": str(exc)})
            continue
        stats = ma._stats(result["nav"], result["bench"], result["days"], *DEV)
        rows.append({"cell": rule.name, "stop": rule.stop, "take": rule.take, **stats,
                     "trades": ma._trades(result["fills"], result["days"]).get("trades", 0),
                     "stop_exits": result["counts"]["stop_exits"], "take_exits": result["counts"]["take_exits"]})
    usable = [r for r in rows if "stopped" not in r]
    if not usable:
        raise ma.SpecUndefinedError("every grid cell was halted by the safety stop")
    best = max(usable, key=lambda r: r["excess_t"])
    return {"grid": rows, "selected": best["cell"], "stop": best["stop"], "take": best["take"]}


def validate(data: Any, stop: float, take: float, run: Callable[..., dict] = ma.backtest) -> dict[str, Any]:
    rule = replace(ma.RULES["v21"], name=f"v22 s{int(stop * 100)} t{int(take * 100)}", stop=stop, take=take)
    return {name: ma.report(run(data, bps, rule=rule)) for name, bps in ma.COSTS.items()}


def forward(data: Any, frozen: dict, start: date = FORWARD_FROM,
            run: Callable[..., dict] = ma.backtest) -> dict[str, Any]:
    rule = replace(ma.RULES["v21"], name=frozen["cell"], stop=frozen["stop"], take=frozen["take"])
    out = {}
    for name, bps in ma.COSTS.items():
        result = run(data, bps, rule=rule, first_trade=start)
        if len(result["days"]) < 2:
            raise SystemExit(f"no forward session after {start}")
        out[name] = {"days": [result["days"][0].isoformat(), result["days"][-1].isoformat()],
                     **ma._stats(result["nav"], result["bench"], result["days"], result["days"][0], result["days"][-1]),
                     "nav_end": result["nav"][-1], "bench_end": result["bench"][-1],
                     "trades": ma._trades(result["fills"], result["days"]), "counts": result["counts"]}
    return out


def _read(root: Path) -> Any:
    from momentum.agent.cli import read_input

    return read_input(root / "prices.csv", root / "securities.jsonl", root / "actions.json", root / "sessions.json",
                      securities_mode="session-confirmed")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("action", choices=("select", "forward"))
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--record", type=Path, help="select: written once")
    parser.add_argument("--frozen", type=Path, required=True, help="select: written once; forward: read")
    args = parser.parse_args(argv)
    if args.action == "forward":
        frozen = json.loads(args.frozen.read_text(encoding="utf-8"))
        out = forward(_read(args.input), frozen)
        for name, row in out.items():
            print(f"== {name}: forward {row['days'][0]}~{row['days'][1]}  {frozen['cell']}  "
                  f"strategy NAV {row['nav_end']:,.0f} MDD {row['strategy_mdd']:.2%} | "
                  f"069500 NAV {row['bench_end']:,.0f} MDD {row['069500_mdd']:.2%} | excess t {row['excess_t']:.2f}")
            print(f"   trades {row['trades']} counts {row['counts']}")
        print(json.dumps({"frozen": frozen, **out}, sort_keys=True, default=str))
        return 0
    if args.record is None or args.record.exists() or args.frozen.exists():
        raise ma.SpecUndefinedError("select needs a new --record and a new --frozen (v22 is chosen once)")
    data = _read(args.input)
    chosen = select(data)
    print(f"development {DEV[0]}~{DEV[1]} (later sessions not read), base cost {ma.COSTS['base']} bp")
    print(f"{'cell':<14} {'CAGR':>8} {'MDD':>7} {'069500':>8} {'t':>6} {'trades':>6} {'stop':>5} {'take':>5}")
    for r in chosen["grid"]:
        if "stopped" in r:
            print(f"{r['cell']:<14} halted: {r['stopped']}")
            continue
        print(f"{r['cell']:<14} {r['strategy_cagr']:>+8.2%} {r['strategy_mdd']:>7.2%} {r['069500_cagr']:>+8.2%} "
              f"{r['excess_t']:>6.2f} {r['trades']:>6} {r['stop_exits']:>5} {r['take_exits']:>5}")
    print(f"selected {chosen['selected']} (highest development excess t)")
    frozen = {"cell": chosen["selected"], "stop": chosen["stop"], "take": chosen["take"],
              "rule": asdict(ma.RULES["v21"]) | {"stop": chosen["stop"], "take": chosen["take"]},
              "development": [DEV[0].isoformat(), DEV[1].isoformat()], "forward_from": FORWARD_FROM.isoformat()}
    args.frozen.parent.mkdir(parents=True, exist_ok=True)
    args.frozen.write_text(json.dumps(frozen, sort_keys=True) + "\n", encoding="utf-8")
    validation = validate(data, chosen["stop"], chosen["take"])
    for name, rep in validation.items():
        print(f"== validation {name} ({ma.COSTS[name]} bp)  E1 pass: {rep['e1_pass']}")
        for period, row in rep["periods"].items():
            print(f"{period:<12} strategy CAGR {row['strategy_cagr']:+.2%} MDD {row['strategy_mdd']:.2%} | "
                  f"069500 CAGR {row['069500_cagr']:+.2%} MDD {row['069500_mdd']:.2%} | excess t {row['excess_t']:.2f}")
        print(f"trades {rep['trades']}  counts {rep['counts']}")
    args.record.parent.mkdir(parents=True, exist_ok=True)
    args.record.write_text(json.dumps({"select": chosen, "frozen": frozen, "validation": validation},
                                      sort_keys=True, default=str) + "\n", encoding="utf-8")
    print(json.dumps({"select": chosen, "frozen": frozen, "validation": validation}, sort_keys=True, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
