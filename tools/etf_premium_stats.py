"""Descriptive ETF premium statistics: close vs end-of-day NAV from saved FSC ETF pages.

Mac, read-only, no network:

    uv run python tools/etf_premium_stats.py --root data/krx/raw/fsc

Premium = close / NAV - 1 in bp, for every ETF-day with a positive close and
NAV and some volume. Statistics only, no strategy: the size distribution by
rough group (name keywords), by liquidity (top ETFs by mean trading value),
and how much of a premium of 30bp or more is still there the next session.
The FSC NAV is an end-of-day figure, not the iNAV at the 15:30 close, and
overseas ETFs' NAV lags their markets, so part of what shows up is timing,
not mispricing. Prints a table and one JSON line; writes nothing.
"""

from __future__ import annotations

import argparse
import json
import statistics
from collections import defaultdict
from itertools import pairwise
from pathlib import Path
from typing import Any, NamedTuple

GROUPS = (  # first match wins; rough, name-based
    ("leveraged_inverse", ("레버리지", "인버스", "2X", "2x", "선물인버스")),
    ("bond", ("채권", "국고채", "국채", "회사채", "금리", "KOFR", "CD", "단기", "머니마켓", "MMF")),
    ("overseas", ("미국", "S&P", "나스닥", "중국", "차이나", "일본", "인도", "베트남", "유로", "글로벌", "선진", "신흥",
                  "독일", "필라델피아", "다우", "항셍", "MSCI World", "원유", "골드", "금현물", "달러", "엔화")),
)
LINES = (10, 30, 50, 100)
PERSIST = 30.0


class Row(NamedTuple):
    day: str
    code: str
    name: str
    premium_bp: float
    value: float


def group(name: str) -> str:
    for label, words in GROUPS:
        if any(w in name for w in words):
            return label
    return "domestic_equity"


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


def _num(text: Any) -> float:
    try:
        return float(text)
    except (TypeError, ValueError):
        return 0.0


def load(root: Path) -> list[Row]:
    rows = []
    for folder in sorted(p for p in (root / "etf").iterdir() if p.is_dir() and not p.name.startswith(".")):
        for page in sorted(folder.glob("page-*.json")):
            for it in _items(json.loads(page.read_text(encoding="utf-8"))) or []:
                close, nav, volume = _num(it.get("clpr")), _num(it.get("nav")), _num(it.get("trqu"))
                if close > 0 and nav > 0 and volume > 0:
                    rows.append(Row(folder.name, it["srtnCd"], it.get("itmsNm", ""), (close / nav - 1) * 1e4,
                                    _num(it.get("trPrc"))))
    return rows


def _quantile(values: list[float], q: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(q * len(ordered)))]


def _describe(rows: list[Row]) -> dict[str, Any]:
    absolute = [abs(r.premium_bp) for r in rows]
    return {"etf_days": len(rows), "etfs": len({r.code for r in rows}),
            "mean_bp": statistics.fmean(r.premium_bp for r in rows),
            "abs_median_bp": statistics.median(absolute), "abs_p90_bp": _quantile(absolute, 0.9),
            "abs_p99_bp": _quantile(absolute, 0.99),
            "share_abs_over": {str(x): sum(a > x for a in absolute) / len(absolute) for x in LINES}}


def summarize(rows: list[Row], top_n: int = 100) -> dict[str, Any]:
    by_group: dict[str, list[Row]] = defaultdict(list)
    for r in rows:
        by_group[group(r.name)].append(r)
    mean_value: dict[str, list[float]] = defaultdict(list)
    for r in rows:
        mean_value[r.code].append(r.value)
    top = set(sorted(mean_value, key=lambda c: -statistics.fmean(mean_value[c]))[:top_n])
    days = sorted({r.day for r in rows})
    following = dict(pairwise(days))
    at = {(r.day, r.code): r.premium_bp for r in rows}
    kept, flips = [], 0
    for r in rows:
        nxt = at.get((following.get(r.day, ""), r.code))
        if abs(r.premium_bp) >= PERSIST and nxt is not None:
            kept.append(nxt / r.premium_bp)
            flips += nxt * r.premium_bp < 0
    return {"days": [days[0], days[-1]] if days else None, "all": _describe(rows),
            "groups": {g: _describe(v) for g, v in sorted(by_group.items())},
            "liquid": {"top": _describe([r for r in rows if r.code in top]),
                       "rest": _describe([r for r in rows if r.code not in top]) if len(top) < len(mean_value)
                       else {"etf_days": 0}},
            "top_n": top_n,
            "persistence": {"line_bp": PERSIST, "pairs": len(kept),
                            "kept_ratio_median": statistics.median(kept) if kept else None,
                            "sign_flip_share": flips / len(kept) if kept else None}}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--root", type=Path, default=Path("data/krx/raw/fsc"))
    parser.add_argument("--top", type=int, default=100)
    args = parser.parse_args(argv)
    out = summarize(load(args.root), args.top)
    print(f"ETF close vs NAV premium {out['days']} (descriptive only)")
    print(f"{'set':<22} {'etf-days':>9} {'etfs':>5} {'mean':>7} {'|med|':>6} {'|p90|':>6} {'|p99|':>7} "
          + " ".join(f">{x}bp" for x in LINES))
    sets = [("all", out["all"]), *out["groups"].items(), (f"top{args.top} by value", out["liquid"]["top"]),
            ("rest", out["liquid"]["rest"])]
    for name, d in sets:
        if not d.get("etf_days"):
            continue
        print(f"{name:<22} {d['etf_days']:>9} {d['etfs']:>5} {d['mean_bp']:>7.1f} {d['abs_median_bp']:>6.1f} "
              f"{d['abs_p90_bp']:>6.1f} {d['abs_p99_bp']:>7.1f} "
              + " ".join(f"{d['share_abs_over'][str(x)]:>6.1%}" for x in LINES))
    p = out["persistence"]
    print(f"next session after |premium| >= {p['line_bp']:.0f}bp: pairs {p['pairs']}, median kept ratio "
          f"{p['kept_ratio_median']}, sign flip share {p['sign_flip_share']}")
    print(json.dumps(out, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
