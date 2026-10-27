"""Bid-ask bounce check for the direction lab (direction-lab-v12 §6, §8; reference only).

    uv run python tools/bounce_check.py data/kis/intraday/2026-10-28 data/kis/intraday/2026-10-29 ...

Reads the cost-probe days (order books every 10 seconds and the day's minute
bars) and builds two per-minute series for 09:00..15:19 per symbol-day: the
last trade (minute bar close, as in the direction lab) and the mid of the last
book snapshot inside that minute. A minute without a trade or a snapshot keeps
the last value; nothing is invented before the first one.

For h in {1, 5}: of consecutive h-minute moves that are both non-zero, the
share that reverse. A random walk gives 0.5. A trade price bouncing between
bid and ask reverses more often than the mid does; ``bounce_share`` is the
part of the trade series' excess reversal over 0.5 that is gone on the mid.
Prints a table and one JSON line; writes nothing. Three probe days are too
few for a verdict: this is reference only.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np

TOOLS = Path(__file__).resolve().parent


def _load(name: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, TOOLS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    assert spec is not None and spec.loader is not None
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


lab, report = _load("direction_lab"), _load("kis_intraday_report")
KST, GRID, OPEN = report.KST, lab.GRID, lab.OPEN
HORIZONS = (1, 5)


def mid_grid(books: list[dict], day: str) -> np.ndarray:
    """Mid of the last snapshot at or before the end of each minute 09:00..15:19."""
    start = datetime.fromisoformat(f"{day}T00:00:00").replace(tzinfo=KST)
    mid = np.full(GRID, np.nan)
    for row in sorted(books, key=lambda r: r["at"]):
        i = int((row["at"] - start).total_seconds() // 60) - OPEN
        if 0 <= i < GRID:
            mid[i] = row["mid"]
    last = np.nan
    for i in range(GRID):
        if np.isnan(mid[i]):
            mid[i] = last
        last = mid[i]
    return mid


def reversals(series: np.ndarray, h: int) -> tuple[int, int]:
    """(reversing pairs, pairs) over consecutive non-zero h-minute moves."""
    step = series[h:] - series[:-h]
    first, second = step[:-h], step[h:]
    both = ~np.isnan(first) & ~np.isnan(second) & (first != 0) & (second != 0)
    return int((np.sign(first[both]) != np.sign(second[both])).sum()), int(both.sum())


def _cell(counts: dict[str, list[int]]) -> dict:
    out: dict[str, Any] = {}
    for kind in ("trade", "mid"):
        k, n = counts[kind]
        out[kind] = {"pairs": n, "reversal": k / n if n else None}
    trade, mid = out["trade"]["reversal"], out["mid"]["reversal"]
    out["bounce_share"] = (None if trade is None or mid is None or trade <= 0.5
                           else (trade - mid) / (trade - 0.5))
    return out


def check(days: list[Path]) -> dict:
    counts: dict[str, dict[str, dict[str, list[int]]]] = defaultdict(
        lambda: {f"h{h}": {"trade": [0, 0], "mid": [0, 0]} for h in HORIZONS})
    for day in days:
        for path in sorted(day.glob("minute-*.jsonl")):
            symbol = path.stem.split("-", 1)[1]
            asking = day / f"asking-{symbol}.jsonl"
            if not asking.exists():
                continue
            rows: dict[str, dict] = {}
            for line in path.read_text(encoding="utf-8").splitlines():
                record = json.loads(line)
                if record.get("pull") != "close":
                    continue
                for r in record["response"].get("output2") or []:
                    if r.get("stck_bsop_date") == day.name.replace("-", ""):
                        rows[r["stck_cntg_hour"]] = {**r, "cntg_vol": r.get("cntg_vol", "0")}
            books = [b for b in (report.book_row(json.loads(line))
                                 for line in asking.read_text(encoding="utf-8").splitlines()) if b is not None]
            series = {"trade": lab.grid(list(rows.values()))[0], "mid": mid_grid(books, day.name)}
            for h in HORIZONS:
                for kind, values in series.items():
                    k, n = reversals(values, h)
                    counts[symbol][f"h{h}"][kind][0] += k
                    counts[symbol][f"h{h}"][kind][1] += n
    pooled = {f"h{h}": {kind: [sum(c[f"h{h}"][kind][j] for c in counts.values()) for j in (0, 1)]
                        for kind in ("trade", "mid")} for h in HORIZONS}
    return {"days": [d.name for d in days],
            "symbols": {s: {h: _cell(c) for h, c in cells.items()} for s, cells in sorted(counts.items())},
            "pooled": {h: _cell(c) for h, c in pooled.items()}}


def _fmt(value: float | None) -> str:
    return "  n/a" if value is None else f"{value:5.1%}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("days", nargs="+", type=Path)
    result = check(parser.parse_args(argv).days)
    print(f"bounce check {result['days']} (reference only, direction-lab-v12 §8)")
    print(f"{'symbol':<8} {'h':>3} {'trade rev':>9} {'n':>7} {'mid rev':>8} {'n':>7} {'bounce':>7}")
    for symbol, cells in [("pooled", result["pooled"]), *result["symbols"].items()]:
        for h, c in cells.items():
            print(f"{symbol:<8} {h:>3} {_fmt(c['trade']['reversal']):>9} {c['trade']['pairs']:>7} "
                  f"{_fmt(c['mid']['reversal']):>8} {c['mid']['pairs']:>7} {_fmt(c['bounce_share']):>7}")
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
