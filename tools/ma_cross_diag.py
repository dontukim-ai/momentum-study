"""Diagnose v20 A's NAV swings (docs/blueprint/ma-cross-v20.md, diagnosis after the run; never a new verdict).

Mac, read-only, no record file (the v20 verdict stays as recorded):

    uv run --extra agent python tools/ma_cross_diag.py --input data/krx/research-fwd-full

Re-runs the same backtest at the base cost with a trace, then reports
- the 20 sessions with the largest NAV moves and, for each, the holdings that
  moved NAV most (shares x mark change), their close-to-close ratio and any
  corporate action of that symbol on that day;
- every held symbol-day whose close moved beyond the +-30% daily limit with no
  split, bonus issue or capital reduction on that day ("unexplained jumps");
- the annualised volatility with and without the sessions holding such jumps.
Prints a text report and one JSON line; writes nothing.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import math
import statistics
import sys
from collections import defaultdict
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
TOP_DAYS, TOP_NAMES = 20, 3
LIMIT, SHARE_ACTIONS = ma.JUMP_LIMIT, ma.SHARE_ACTIONS


def diagnose(trace: list, actions: list) -> dict[str, Any]:
    by_symbol: dict[str, list] = defaultdict(list)
    for announced in actions:
        by_symbol[announced.action.security_id].append(announced.action)
    last_close: dict[str, tuple] = {}              # symbol -> (day, close): bridges suspensions
    rets, moves, jumps = [], [], []
    for (d0, nav0, held0), (d1, nav1, held1) in pairwise(trace):
        for s, entry in held0.items():
            if entry[2] is not None:
                last_close[s] = (d0, entry[2])
        r = nav1 / nav0 - 1
        rets.append(r)
        contrib = []
        for s, (q0, mark0, _, _) in held0.items():
            if q0 <= 0:
                continue
            q1, mark1, close1, open1 = held1.get(s, (0, mark0, None, None))
            since, ref = last_close.get(s, (None, None))
            kinds = [a.action_type for a in by_symbol.get(s, []) if since is None or since < a.ex_date <= d1]
            reshaped = any(k in SHARE_ACTIONS for k in kinds)
            if q1:   # still held: a share action rescales shares and mark together, so compare values
                change = q1 * mark1 - q0 * mark0 if reshaped else q0 * (mark1 - mark0)
            else:    # sold at today's open
                change = q0 * (open1 - mark0) if open1 is not None else 0.0
            px = open1 if open1 is not None else close1
            ratio = px / ref if px is not None and ref else None
            contrib.append({"symbol": s, "nav_change": change, "ratio": ratio, "actions": kinds,
                            "sold": not q1, "since": since.isoformat() if since else None})
            if ratio is not None and ma.unexplained(ratio, kinds):
                jumps.append({"day": d1.isoformat(), "symbol": s, "since": since.isoformat(), "ratio": ratio,
                              "nav_change": change, "sold": not q1})
        contrib.sort(key=lambda c: -abs(c["nav_change"]))
        moves.append({"day": d1.isoformat(), "nav_return": r, "top": contrib[:TOP_NAMES]})
    moves.sort(key=lambda m: -abs(m["nav_return"]))
    jump_days = {j["day"] for j in jumps}
    clean = [r for r, (_, (d1, _, _)) in zip(rets, pairwise(trace), strict=False)
             if d1.isoformat() not in jump_days]

    def vol(xs: list[float]) -> float:
        return statistics.pstdev(xs) * math.sqrt(252) if len(xs) > 1 else math.nan

    return {"sessions": len(rets), "vol": vol(rets), "vol_without_jump_days": vol(clean),
            "jump_days": len(jump_days), "jumps": sorted(jumps, key=lambda j: -abs(j["nav_change"])),
            "largest_moves": moves[:TOP_DAYS]}


def text(result: dict[str, Any]) -> str:
    lines = [(f"sessions {result['sessions']}, annualised vol {result['vol']:.1%}, without the "
              f"{result['jump_days']} sessions holding unexplained jumps {result['vol_without_jump_days']:.1%}"),
             (f"\nunexplained jumps of held names (open, else close, vs the last close beyond {LIMIT:.0%}, "
              f"no share action): {len(result['jumps'])}")]
    for j in result["jumps"][:40]:
        lines.append(f"  {j['day']} {j['symbol']} x{j['ratio']:.3f} since {j['since']}"
                     f"{' (sold at that open)' if j['sold'] else ''} NAV {j['nav_change']:+,.0f}")
    lines.append(f"\nlargest {TOP_DAYS} NAV moves")
    for m in result["largest_moves"]:
        names = ", ".join(f"{c['symbol']} {c['nav_change']:+,.0f} (x{c['ratio']:.3f}"
                          f"{' sold' if c['sold'] else ''}{' ' + '/'.join(c['actions']) if c['actions'] else ''})"
                          if c["ratio"] is not None else f"{c['symbol']} {c['nav_change']:+,.0f} (no price)"
                          for c in m["top"])
        lines.append(f"  {m['day']} NAV {m['nav_return']:+.2%}: {names}")
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
    ma.backtest(data, ma.COSTS["base"], trace, strict=False)
    result = diagnose(trace, list(data.actions))
    print(text(result))
    print(json.dumps(result, sort_keys=True, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
