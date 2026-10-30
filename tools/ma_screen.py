"""v20 B: daily moving-average screen on FSC stock quotes (docs/blueprint/ma-cross-v20.md).

    uv run python tools/ma_screen.py --root data/krx/raw/fsc [--day YYYY-MM-DD]

For the latest saved session (or --day) with quotes: the 300 common stocks
with the largest 20-session mean trading value (trPrc) among those quoted on
each of the last 121 sessions; for each, closes divided back through the
exchange's own base-price adjustments (clpr - vs differing from the previous
close by more than 0.5 won), then the 20/60 cross and the 5/20/60/120
alignment today against yesterday. Prints a text report and one JSON line;
writes nothing. A screening aid, not a trading signal.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import statistics
import sys
from pathlib import Path
from typing import Any, NamedTuple

from momentum.config import SpecUndefinedError

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


ma, premium = _load("ma_cross"), _load("etf_premium_stats")
TOP, VALUE_DAYS, BASE_TOL = 300, 20, 0.5


class Quote(NamedTuple):
    name: str
    market: str
    close: float
    base: float      # the exchange's base price for the day: clpr - vs
    value: float     # trPrc, KRW traded


def common_share(code: str, name: str, market: str) -> bool:
    return market in ("KOSPI", "KOSDAQ") and ma.common_stock(code) and "스팩" not in name


def read_day(folder: Path) -> dict[str, Quote]:
    rows = {}
    for page in sorted(folder.glob("page-*.json")):
        for it in premium._items(json.loads(page.read_text(encoding="utf-8"))) or []:
            code, name, market = it.get("srtnCd", ""), it.get("itmsNm", ""), it.get("mrktCtg", "")
            close = premium._num(it.get("clpr"))
            if common_share(code, name, market) and close > 0:
                rows[code] = Quote(name, market, close, close - premium._num(it.get("vs")), premium._num(it.get("trPrc")))
    return rows


def load(root: Path, day: str | None = None, sessions: int = ma.HISTORY) -> dict[str, dict[str, Quote]]:
    """The last ``sessions`` days with quotes up to ``day`` (empty folders are holidays and skipped)."""
    folders = sorted((p for p in (root / "stock").iterdir() if p.is_dir() and not p.name.startswith(".")),
                     reverse=True)
    out: dict[str, dict[str, Quote]] = {}
    for folder in folders:
        if day is not None and folder.name > day:
            continue
        rows = read_day(folder)
        if rows:
            out[folder.name] = rows
        if len(out) == sessions:
            break
    return dict(sorted(out.items()))


def screen(days: dict[str, dict[str, Quote]], top: int = TOP) -> dict[str, Any]:
    sessions = list(days)
    if len(sessions) < ma.HISTORY:
        raise SpecUndefinedError(f"need {ma.HISTORY} sessions with quotes, have {len(sessions)}")
    sessions = sessions[-ma.HISTORY:]
    today = sessions[-1]
    complete = [c for c in days[today] if all(c in days[d] for d in sessions)]
    value = {c: statistics.fmean(days[d][c].value for d in sessions[-VALUE_DAYS:]) for c in complete}
    universe = sorted(complete, key=lambda c: (-value[c], c))[:top]
    out: dict[str, Any] = {"day": today, "universe": len(universe), "quoted_today": len(days[today]),
                           "short_history": len(days[today]) - len(complete), "adjusted": 0,
                           "golden": [], "dead": [], "bull_new": [], "bear_new": [], "bull": 0, "bear": 0}
    for c in universe:
        closes = [days[d][c].close for d in sessions]
        factors = [1.0] + [closes[k - 1] / days[d][c].base
                           if days[d][c].base > 0 and abs(days[d][c].base - closes[k - 1]) > BASE_TOL else 1.0
                           for k, d in enumerate(sessions) if k > 0]
        out["adjusted"] += any(f != 1.0 for f in factors)
        sig = ma.signals(ma.adjust(closes, factors))
        row = {"code": c, "name": days[today][c].name, "close": closes[-1], "ma20": sig["ma"][20],
               "ma60": sig["ma"][60], "value_20d": value[c]}
        if sig["cross"] == "golden":
            out["golden"].append(row)
        elif sig["cross"] == "dead":
            out["dead"].append(row)
        if sig["alignment"]:
            out[sig["alignment"]] += 1
        if sig["entered"]:
            out[f"{sig['entered']}_new"].append(row)
    for key in ("golden", "dead", "bull_new", "bear_new"):
        out[key].sort(key=lambda r: -r["value_20d"])
    return out


def text(result: dict[str, Any]) -> str:
    lines = [f"이평선 조회 {result['day']} (금융위 종가, 거래대금 상위 {result['universe']}종목)",
             (f"정배열 {result['bull']}종목, 역배열 {result['bear']}종목 | 이력 부족 제외 {result['short_history']}, "
              f"기준가 조정 반영 {result['adjusted']}")]
    titles = {"golden": "골든크로스 (20일선이 60일선 위로)", "dead": "데드크로스 (20일선이 60일선 아래로)",
              "bull_new": "정배열 새 진입 (5>20>60>120)", "bear_new": "역배열 새 진입 (5<20<60<120)"}
    for key, title in titles.items():
        rows = result[key]
        lines.append(f"\n[{title}] {len(rows)}종목")
        for r in rows:
            lines.append(f"  {r['name']}({r['code']}) 종가 {r['close']:,.0f} MA20 {r['ma20']:,.0f} "
                         f"MA60 {r['ma60']:,.0f} 거래대금 {r['value_20d'] / 1e8:,.0f}억")
    lines.append("\n조회 도구이며 매매 신호가 아니다(v20 B).")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--root", type=Path, default=Path("data/krx/raw/fsc"))
    parser.add_argument("--day", help="screen this session (default: the latest saved one with quotes)")
    args = parser.parse_args(argv)
    result = screen(load(args.root, args.day))
    if args.day is not None and result["day"] != args.day:
        raise SystemExit(f"no quotes saved for {args.day}; the latest before it is {result['day']}")
    print(text(result))
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
