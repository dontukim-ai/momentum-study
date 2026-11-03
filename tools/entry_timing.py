"""Entry timing without a signal: lump sum against monthly instalments, and how much the start day matters
for each holding period (docs/blueprint/entry-timing.md). Descriptive, not a strategy test: nothing is judged
and nothing enters the learning ledger.

Mac, read-only, writes nothing:

    uv run python tools/entry_timing.py --series data/kis/daily-history/kospi200.json \
        --series data/kis/daily-history/069500.json

For every start session s with a full window (a year is 252 sessions, a month 21):
- lump sum: all money at the close of s, value at the close H years later;
- instalments over M months: equal parts at the closes s, s+21, ..., s+21(M-1), uninvested money earning
  nothing, value at the same end;
- recovery: sessions after s until a close at or above the close of s; the share recovered within N years counts
  only starts with N years of data after them, and starts not yet recovered are reported separately.
Prices are closes without distributions; there are no costs. Windows overlap, so the counts are not
independent observations.
"""

from __future__ import annotations

import argparse
import json
import statistics
from pathlib import Path
from typing import Any

YEAR, MONTH = 252, 21
HORIZONS = (1, 2, 3, 5, 10)
INSTALMENTS = (6, 12)
RECOVERY_YEARS = (1, 3, 5)


def lump(prices: list[float], s: int, end: int) -> float:
    return prices[end] / prices[s]


def instalments(prices: list[float], s: int, end: int, months: int) -> float:
    """Value at ``end`` of 1 spent in ``months`` equal parts, a month apart, from session ``s``."""
    return statistics.fmean(prices[end] / prices[s + MONTH * j] for j in range(months))


def _quantile(values: list[float], q: float) -> float:
    ordered = sorted(values)
    pos = q * (len(ordered) - 1)
    lo = int(pos)
    hi = min(lo + 1, len(ordered) - 1)
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (pos - lo)


def _annual(total: float, years: float) -> float:
    return total ** (1 / years) - 1


def horizon(prices: list[float], years: int) -> dict[str, Any] | None:
    span = YEAR * years
    starts = range(len(prices) - span)
    if not starts:
        return None
    lumps = [_annual(lump(prices, s, s + span), years) for s in starts]
    out: dict[str, Any] = {"starts": len(lumps), "lump": _summary(lumps)}
    for m in INSTALMENTS:
        if MONTH * (m - 1) >= span:
            continue
        dca = [_annual(instalments(prices, s, s + span, m), years) for s in starts]
        out[f"dca{m}"] = _summary(dca)
        out[f"lump_beats_dca{m}"] = sum(a > b for a, b in zip(lumps, dca, strict=True)) / len(lumps)
    return out


def _summary(values: list[float]) -> dict[str, float]:
    return {"mean": statistics.fmean(values), "p10": _quantile(values, .1), "median": _quantile(values, .5),
            "p90": _quantile(values, .9), "min": min(values), "max": max(values),
            "loss": sum(v < 0 for v in values) / len(values)}


def recovery(prices: list[float]) -> dict[str, Any]:
    """Sessions after each start until a close at or above the start close (next-greater-or-equal, one pass)."""
    n = len(prices)
    waits: list[int | None] = [None] * n
    stack: list[int] = []
    for s in range(n - 1, -1, -1):
        while stack and prices[stack[-1]] < prices[s]:
            stack.pop()
        waits[s] = stack[-1] - s if stack else None
        stack.append(s)
    known = [w for w in waits if w is not None]
    within = {}
    for y in RECOVERY_YEARS:
        eligible = [w for s, w in enumerate(waits) if n - 1 - s >= YEAR * y]   # starts with y years of data after
        within[f"within_{y}y"] = (sum(w is not None and w <= YEAR * y for w in eligible) / len(eligible)
                                  if eligible else None)
    return {"starts": n, "never_recovered": n - len(known), **within,
            "longest_recovered_sessions": max(known, default=0),
            "worst_open_sessions": max((n - 1 - s for s, w in enumerate(waits) if w is None), default=0)}


def study(rows: list[dict[str, Any]]) -> dict[str, Any]:
    prices = [float(r["close"]) for r in rows]
    return {"first": rows[0]["date"], "last": rows[-1]["date"], "closes": len(prices),
            "horizons": {str(y): h for y in HORIZONS if (h := horizon(prices, y)) is not None},
            "recovery": recovery(prices)}


def text(name: str, result: dict[str, Any]) -> str:
    lines = [f"{name}: {result['first']}~{result['last']}, {result['closes']} closes (price only, no costs)"]
    for y, h in result["horizons"].items():
        lines.append(f"  {y}y, {h['starts']} starts: annualised p10 / median / p90 (min..max), loss share")
        for key in ("lump", *(f"dca{m}" for m in INSTALMENTS)):
            if key in h:
                s = h[key]
                beat = f", lump beats it {h[f'lump_beats_{key}']:.0%}" if key != "lump" else ""
                lines.append(f"    {key:6} {s['p10']:+.1%} / {s['median']:+.1%} / {s['p90']:+.1%} "
                             f"({s['min']:+.1%}..{s['max']:+.1%}), loss {s['loss']:.0%}{beat}")
    r = result["recovery"]
    shares = ", ".join(f"{y}y {r[f'within_{y}y']:.0%}" for y in RECOVERY_YEARS if r[f"within_{y}y"] is not None)
    lines.append(f"  back to the start close within: {shares}; never yet {r['never_recovered']} starts; longest recovered "
                 f"{r['longest_recovered_sessions'] / YEAR:.1f}y; longest still open {r['worst_open_sessions'] / YEAR:.1f}y")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--series", type=Path, action="append", required=True,
                        help="tools/kis_daily_history.py output; repeat for several")
    args = parser.parse_args(argv)
    results = {}
    for path in args.series:
        saved = json.loads(path.read_text(encoding="utf-8"))
        results[saved["name"]] = study(saved["rows"])
        print(text(saved["name"], results[saved["name"]]))
    print(json.dumps(results, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
