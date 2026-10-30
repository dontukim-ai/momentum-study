"""Evidence for each unexplained jump in a research input (v23 step 1, docs/blueprint/ma-cross-v20.md).

Mac, read-only, writes nothing (FSC quotes and OpenDART lists already collected by the repository tools):

    uv run --extra agent python tools/jump_evidence.py --input data/krx/research-fwd-full \
        --fsc-root data/krx/raw/fsc --dart-root data/krx/raw

For every jump ``tools/input_audit.py`` finds, two independent pieces of evidence:
- the listed share count (FSC ``lstgStCnt``) on the last bar before the jump and on the jump bar;
- OpenDART filings of that stock (list types B and I) from 183 days before the last bar to the jump
  bar whose title names a share-changing event (merger, split, reduction, bonus issue, relisting ...).
Each jump gets one label:
- ``share_change_consistent``: the share count changed and price x shares moved within the daily limit,
  i.e. an unrecorded split, reverse split or reduction whose ratio the share counts give;
- ``share_change_other``: the share count changed but the value did not carry over (merger, spin-off);
- ``no_share_change``: same share count on both bars (a relisting, a price-base reset or a data error);
- ``no_share_data``: a count is missing on either bar (never filled in).
Nothing is repaired here: the report decides how the input is fixed, and no ratio is inferred for use.
Prints a summary, the list (largest first) and one JSON line.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import math
import re
import sys
from collections import Counter
from collections.abc import Callable, Sequence
from datetime import date, timedelta
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
input_audit = _load("input_audit")
LOOKBACK = timedelta(days=183)
KEYWORDS = ("합병", "분할", "감자", "병합", "무상증자", "상장폐지", "재상장", "변경상장", "추가상장",
            "매매거래정지", "주식교환", "주식이전")


def label(worst_ratio: float, shares0: int | None, shares1: int | None) -> str:
    if shares0 is None or shares1 is None:
        return "no_share_data"
    if shares0 == shares1:
        return "no_share_change"
    value = worst_ratio * shares1 / shares0
    return "share_change_consistent" if abs(value - 1) <= ma.JUMP_LIMIT + 0.005 else "share_change_other"


def evidence(data: Any, shares_at: Callable[[str, date], int | None], events: Sequence[Any]) -> dict[str, Any]:
    """Label every input_audit jump; ``shares_at(symbol, day)`` is the listed share count or None."""
    filings: dict[str, list[Any]] = {}
    for e in events:
        if e.stock_code and any(k in e.report_nm for k in KEYWORDS):
            filings.setdefault(e.stock_code, []).append(e)
    found = input_audit.audit(data)
    rows = []
    for j in found["jumps"]:
        last, day = date.fromisoformat(j["last_day"]), date.fromisoformat(j["day"])
        ratios = [r for r in (j["open_ratio"], j["close_ratio"]) if r is not None]
        worst = max(ratios, key=lambda r: abs(math.log(r)))
        s0, s1 = shares_at(j["symbol"], last), shares_at(j["symbol"], day)
        named = sorted({(e.rcept_date.isoformat(), e.report_nm) for e in filings.get(j["symbol"], [])
                        if last - LOOKBACK <= e.rcept_date <= day})
        rows.append({**j, "worst_ratio": worst, "shares_before": s0, "shares_after": s1,
                     "share_ratio": s1 / s0 if s0 and s1 else None, "label": label(worst, s0, s1),
                     "filings": [{"date": d, "title": t} for d, t in named]})
    tally = Counter((r["label"], bool(r["filings"])) for r in rows)
    return {"jumps": rows, "symbols": found["symbols"],
            "by_label": {f"{lab}{' +filing' if filed else ''}": n for (lab, filed), n in sorted(tally.items())},
            "common_stock_jumps": sum(r["common_stock"] for r in rows)}


def text(result: dict[str, Any], limit: int = 60) -> str:
    lines = [f"{len(result['jumps'])} unexplained jumps ({result['common_stock_jumps']} in common stocks)"]
    lines += [f"  {k}: {n}" for k, n in result["by_label"].items()]
    for r in result["jumps"][:limit]:
        share = f" shares x{r['share_ratio']:.4f}" if r["share_ratio"] else ""
        titles = "; ".join(f"{f['date']} {f['title']}" for f in r["filings"][:3])
        lines.append(f"  {r['symbol']}{'' if r['common_stock'] else ' (not common)'} {r['last_day']} -> "
                     f"{r['day']} x{r['worst_ratio']:.3f}{share} {r['label']}{' | ' + titles if titles else ''}")
    if len(result["jumps"]) > limit:
        lines.append(f"  ... {len(result['jumps']) - limit} more in the JSON line")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--fsc-root", type=Path, default=Path("data/krx/raw/fsc"))
    parser.add_argument("--dart-root", type=Path, default=Path("data/krx/raw"))
    parser.add_argument("--types", default="B,I",
                        help="OpenDART list types: B (major events) and I (exchange) carry the share-changing "
                             "decisions and are the ones the action pipelines already parse")
    args = parser.parse_args(argv)
    from momentum.agent.cli import read_input
    from momentum.krx.dart_collect import list_pages
    from momentum.krx.dart_events import parse_list_pages
    from momentum.krx.fsc_adjustments import _rows

    root = args.input
    data = read_input(root / "prices.csv", root / "securities.jsonl", root / "actions.json", root / "sessions.json",
                      securities_mode="session-confirmed")
    cache: dict[date, dict[str, int | None]] = {}

    def shares_at(symbol: str, day: date) -> int | None:
        if day not in cache:
            rows = _rows(args.fsc_root / "stock" / day.isoformat(), day) or []
            cache[day] = {}
            for row in rows:
                count = str(row.get("lstgStCnt") or "").strip()
                cache[day][str(row.get("srtnCd") or "").strip()] = (
                    int(count) if re.fullmatch(r"\d+", count) and int(count) > 0 else None)
        return cache[day].get(symbol)

    kinds = [k for k in args.types.split(",") if (args.dart_root / "dart_list" / k).is_dir()]
    events = parse_list_pages([page for kind in kinds for page in list_pages(args.dart_root, kind)])
    print(f"OpenDART list types read: {', '.join(kinds) or 'none'} ({len(events)} filings)")
    result = evidence(data, shares_at, events)
    print(text(result))
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
