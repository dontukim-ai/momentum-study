"""Did an earlier study hold a name through an unexplained jump? (v20 diagnosis follow-up, ma-cross-v20.md)

Mac, read-only, writes nothing:

    uv run --extra agent python tools/holdings_audit.py --input data/krx/research-fwd-full \
        --db artifacts/**/*.sqlite3 --json artifacts/**/*.json

The jumps come from ``input_audit.audit`` on the given research input (a bar
beyond the daily limit from the symbol's previous bar with no share action in
between). Every evaluation result found in the study databases
(agent_cycles.evidence_json and result_json) or JSON reports is walked for an
``equity_curve`` whose rows carry ``positions``: a name held on the last
curve session before a jump day (shares > 0) was exposed to that jump. Each
exposure is listed with its file, study, cycle, the path to the result in the
evidence, the symbol, the day and the price multiple.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import sqlite3
import sys
from collections.abc import Iterator
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


input_audit = _load("input_audit")


def curves(obj: Any, path: str = "") -> Iterator[tuple[str, list]]:
    """(path, equity_curve) for every evaluation result nested anywhere in ``obj``."""
    if isinstance(obj, dict):
        curve = obj.get("equity_curve")
        if isinstance(curve, list) and curve and isinstance(curve[0], dict) and "positions" in curve[0]:
            yield path or "$", curve
        for key, value in obj.items():
            if key != "equity_curve":
                yield from curves(value, f"{path}.{key}" if path else key)
    elif isinstance(obj, list):
        for k, value in enumerate(obj):
            yield from curves(value, f"{path}[{k}]")


def exposures(curve: list, jumps: dict[str, list[dict]]) -> list[dict]:
    """Jumps of names held on the last curve session before the jump day."""
    out = []
    sessions = [row["session"] for row in curve]
    for symbol, cases in jumps.items():
        for case in cases:
            before = [k for k, s in enumerate(sessions) if s < case["day"]]
            if not before or case["day"] > sessions[-1]:
                continue
            held = curve[before[-1]]["positions"].get(symbol, 0)
            if held and float(held) > 0:
                out.append({"symbol": symbol, "day": case["day"], "last_day": case["last_day"],
                            "close_ratio": case["close_ratio"], "shares": held})
    return out


def scan(sources: list[tuple[str, str, Any]], jumps: dict[str, list[dict]]) -> list[dict]:
    """sources: (file, label, parsed JSON). Duplicate curves (same file, path, first and last session) count once."""
    found, seen = [], set()
    for file, label, obj in sources:
        for path, curve in curves(obj):
            key = (file, label, path, curve[0]["session"], curve[-1]["session"])
            if key in seen:
                continue
            seen.add(key)
            for e in exposures(curve, jumps):
                found.append({"file": file, "source": label, "path": path,
                              "window": [curve[0]["session"], curve[-1]["session"]], **e})
    return found


def from_db(path: Path) -> Iterator[tuple[str, str, Any]]:
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "agent_cycles" not in tables:
            return
        for study, ordinal, evidence, result in con.execute(
                "SELECT study, ordinal, evidence_json, result_json FROM agent_cycles ORDER BY study, ordinal"):
            for column, text in (("evidence", evidence), ("result", result)):
                if text:
                    yield str(path), f"{study}#{ordinal}.{column}", json.loads(text)
    finally:
        con.close()


def from_json(path: Path) -> Iterator[tuple[str, str, Any]]:
    try:
        yield str(path), path.name, json.loads(path.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--input", type=Path, required=True, help="research input whose jumps are checked")
    parser.add_argument("--db", type=Path, nargs="*", default=[])
    parser.add_argument("--json", type=Path, nargs="*", default=[])
    args = parser.parse_args(argv)
    from momentum.agent.cli import read_input

    root = args.input
    data = read_input(root / "prices.csv", root / "securities.jsonl", root / "actions.json", root / "sessions.json",
                      securities_mode="session-confirmed")
    jumps: dict[str, list[dict]] = {}
    for case in input_audit.audit(data)["jumps"]:
        jumps.setdefault(case["symbol"], []).append(case)
    sources: list[tuple[str, str, Any]] = []
    for db in args.db:
        sources += list(from_db(db))
    for js in args.json:
        sources += list(from_json(js))
    n_curves = sum(1 for _, _, obj in sources for _ in curves(obj))
    found = scan(sources, jumps)
    print(f"{sum(map(len, jumps.values()))} jumps in {len(jumps)} symbols; {len(args.db)} databases, "
          f"{len(args.json)} JSON files, {n_curves} evaluation curves; {len(found)} exposures")
    for f in sorted(found, key=lambda f: (f["file"], f["source"], f["day"])):
        print(f"  {f['file']} {f['source']} {f['path']} window {f['window'][0]}~{f['window'][1]}: "
              f"held {f['symbol']} into {f['day']} (x{f['close_ratio']:.3f} since {f['last_day']})")
    print(json.dumps({"exposures": found, "curves": n_curves}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
