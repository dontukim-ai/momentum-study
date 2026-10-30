"""Find unexplained jumps in a research input (v20 diagnosis follow-up, docs/blueprint/ma-cross-v20.md).

Mac, read-only, writes nothing:

    uv run --extra agent python tools/input_audit.py --input data/krx/research-fwd-full

For every symbol, each bar against the symbol's previous bar (across any
gap, e.g. a suspension): the open and the close as a multiple of the previous
close. A multiple beyond the +-30% daily limit with no split, bonus issue or
capital reduction recorded in between is listed: an unrecorded share change
(merger, split, reduction), a relisting or a data error. Bars are never
repaired here. Prints a summary, the list (largest first) and one JSON line.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import math
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


def audit(data: Any) -> dict[str, Any]:
    sessions = list(data.sessions)
    index = {d: k for k, d in enumerate(sessions)}
    bars: dict[str, list] = defaultdict(list)
    for bar in data.snapshot.bars:
        bars[bar.symbol].append(bar)
    actions: dict[str, list] = defaultdict(list)
    for announced in data.actions:
        actions[announced.action.security_id].append(announced.action)
    found = []
    for symbol, series in bars.items():
        series.sort(key=lambda b: b.session)
        for b0, b1 in pairwise(series):
            if b0.close <= 0:
                continue
            kinds = [a.action_type for a in actions.get(symbol, []) if b0.session < a.ex_date <= b1.session]
            ratios = {"open": float(b1.open / b0.close) if b1.open > 0 else None, "close": float(b1.close / b0.close)}
            worst = max((r for r in ratios.values() if r is not None), key=lambda r: abs(math.log(r)))
            if ma.unexplained(worst, kinds):
                found.append({"symbol": symbol, "last_day": b0.session.isoformat(), "day": b1.session.isoformat(),
                              "gap_sessions": index[b1.session] - index[b0.session] - 1,
                              "open_ratio": ratios["open"], "close_ratio": ratios["close"],
                              "common_stock": ma.common_stock(symbol), "other_actions": kinds})
    found.sort(key=lambda f: -abs(math.log(f["close_ratio"])))
    return {"symbols": len(bars), "bars": sum(map(len, bars.values())), "jumps": found,
            "symbols_with_jumps": len({f["symbol"] for f in found}),
            "after_a_gap": sum(f["gap_sessions"] > 0 for f in found)}


def text(result: dict[str, Any], limit: int = 60) -> str:
    lines = [(f"{result['symbols']} symbols, {result['bars']} bars: {len(result['jumps'])} unexplained jumps "
              f"(beyond +-{ma.JUMP_LIMIT:.0%}, no share action) in {result['symbols_with_jumps']} symbols, "
              f"{result['after_a_gap']} of them right after a gap in quotes")]
    for f in result["jumps"][:limit]:
        open_part = f"open x{f['open_ratio']:.3f} " if f["open_ratio"] is not None else ""
        lines.append(f"  {f['symbol']}{'' if f['common_stock'] else ' (not common)'} {f['last_day']} -> {f['day']} "
                     f"gap {f['gap_sessions']} {open_part}close x{f['close_ratio']:.3f}"
                     f"{' ' + '/'.join(f['other_actions']) if f['other_actions'] else ''}")
    if len(result["jumps"]) > limit:
        lines.append(f"  ... {len(result['jumps']) - limit} more in the JSON line")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--input", type=Path, required=True)
    args = parser.parse_args(argv)
    from momentum.agent.cli import read_input

    root = args.input
    data = read_input(root / "prices.csv", root / "securities.jsonl", root / "actions.json", root / "sessions.json",
                      securities_mode="session-confirmed")
    result = audit(data)
    print(text(result))
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
