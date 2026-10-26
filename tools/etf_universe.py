"""Fix the minute-bar ETF universe from saved FSC ETF quotes (intraday-candidates-v10 §5).

    uv run python tools/etf_universe.py --calendar data/krx/fwd9/2026-10-23/calendar \
        --fsc-root data/krx/raw/fsc --out data/kis/etf-universe.json

Rule, fixed before any minute bar is read: over the last --sessions sessions
of the calendar, keep ETFs quoted on every one of them whose name and base
index carry no excluded keyword (foreign markets, bonds, rates, money market,
commodities, currencies, REITs, multi-asset, leverage, covered calls; 1x
inverse stays in), then take the --top by average daily trading value (trPrc).
Writes the list with names, index and average value; nothing else.
"""

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from pathlib import Path

from momentum.config import SpecUndefinedError

EXCLUDE = (
    "미국", "나스닥", "S&P", "다우", "필라델피아", "차이나", "중국", "항셍", "홍콩", "대만", "일본", "니케이", "TOPIX",
    "인도", "베트남", "유로", "유럽", "독일", "글로벌", "선진", "신흥", "MSCI", "아시아", "China", "US", "Global",
    "채권", "국채", "국고채", "회사채", "금리", "CD", "KOFR", "머니마켓", "단기", "통안", "크레딧",
    "골드", "금현물", "금선물", "은선물", "구리", "원유", "WTI", "원자재", "농산물", "달러", "엔선물", "통화",
    "리츠", "부동산", "인프라", "TDF", "TRF", "혼합", "자산배분",
    "레버리지", "2X", "3X", "커버드콜", "위클리", "프리미엄", "버퍼",
)


def excluded(*texts: str) -> str | None:
    joined = " ".join(texts)
    return next((k for k in EXCLUDE if k in joined), None)


def select(pages_by_day: dict[str, list[Path]], top: int) -> dict:
    seen: dict[str, dict] = {}
    values: dict[str, list[float]] = defaultdict(list)
    for day, pages in sorted(pages_by_day.items()):
        for page in pages:
            body = json.loads(page.read_text(encoding="utf-8"))["response"]["body"]
            for item in (body.get("items") or {}).get("item", []):
                try:
                    symbol, value = str(item["srtnCd"]), float(item["trPrc"])
                except (KeyError, TypeError, ValueError) as exc:
                    raise SpecUndefinedError(f"{page}: ETF row without srtnCd/trPrc: {exc}") from None
                seen[symbol] = {"name": str(item.get("itmsNm") or ""), "index": str(item.get("bssIdxIdxNm") or "")}
                values[symbol].append(value)
    days = len(pages_by_day)
    kept, dropped = [], defaultdict(int)
    for symbol, meta in seen.items():
        reason = excluded(meta["name"], meta["index"])
        if reason:
            dropped[reason] += 1
        elif len(values[symbol]) < days:
            dropped["not quoted every session"] += 1
        else:
            kept.append({"symbol": symbol, **meta, "average_trading_value": sum(values[symbol]) / days})
    kept.sort(key=lambda r: (-r["average_trading_value"], r["symbol"]))
    return {"sessions": sorted(pages_by_day), "top": top, "symbols": kept[:top],
            "eligible": len(kept), "dropped": dict(sorted(dropped.items()))}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--calendar", type=Path, required=True)
    parser.add_argument("--fsc-root", type=Path, default=Path("data/krx/raw/fsc"))
    parser.add_argument("--sessions", type=int, default=60)
    parser.add_argument("--top", type=int, default=30)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.out.exists():
        print(f"etf-universe: STOP: {args.out} exists; the universe is fixed once")
        return 1
    sessions = json.loads((args.calendar / "calendar.json").read_text())[-args.sessions:]
    pages = {d: sorted((args.fsc_root / "etf" / d).glob("page-*.json")) for d in sessions}
    if any(not p for p in pages.values()):
        print(f"etf-universe: STOP: sessions without ETF pages: {[d for d, p in pages.items() if not p]}")
        return 1
    result = select(pages, args.top)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(f"etf-universe: {result['sessions'][0]}~{result['sessions'][-1]}, eligible {result['eligible']}, "
          f"kept {len(result['symbols'])}, dropped {result['dropped']}")
    for row in result["symbols"]:
        print(f"{row['symbol']} {row['average_trading_value'] / 1e8:>10,.0f}억 {row['name']} | {row['index']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
