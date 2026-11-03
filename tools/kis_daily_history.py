"""Collect long daily closes of KOSPI 200 and 069500 for the entry-timing study (docs/blueprint/entry-timing.md).

Read-only KIS real-account market data (FHKUP03500100 index, FHKST03010100 stock/ETF, unadjusted). Run on the
Mac:

    uv run --extra agent python tools/kis_daily_history.py --end 2026-10-30

Each series is read newest first, 100 days per read, walking back until a read brings nothing new or reaches
``--start``. A finished series is written whole once to <out>/<name>.json and never fetched again (delete the
file to fetch it anew). A response carrying a credential stops the run. Data stay in data/kis (no Git, no
redistribution). Prints the first and last date and the number of closes per series.
"""

from __future__ import annotations

import argparse
import json
import os
from collections.abc import Callable
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

KST = ZoneInfo("Asia/Seoul")
SERIES = {"kospi200": ("index", "2001", "bstp_nmix_prpr"), "069500": ("etf", "069500", "stck_clpr")}
SECRET_NAMES = ("KIS_APP_KEY", "KIS_APP_SECRET", "KIS_ACCOUNT")
MAX_READS = 200


class StopCollection(Exception):
    pass


def _day(text: str) -> date:
    return date(int(text[:4]), int(text[4:6]), int(text[6:8]))


def collect(client: Any, name: str, start: date, end: date) -> tuple[list[dict[str, Any]], int]:
    """(oldest-first [{"date", "close"}], reads) of one series between ``start`` and ``end``."""
    kind, code, field = SERIES[name]
    read = client.index_daily if kind == "index" else client.daily_bars
    rows: dict[str, str] = {}
    cursor, reads = end, 0
    while reads < MAX_READS and cursor >= start:
        reads += 1
        page = read(code, start, cursor)["output2"]
        new = {r["stck_bsop_date"]: r[field] for r in page if r["stck_bsop_date"] not in rows}
        if not new:
            break
        rows.update(new)
        cursor = _day(min(new)) - timedelta(days=1)
    else:
        if reads >= MAX_READS:
            raise StopCollection(f"{name}: {MAX_READS} reads without reaching {start}")
    if not rows:
        raise StopCollection(f"{name}: no closes between {start} and {end}")
    out = []
    for d in sorted(rows):
        close = float(rows[d])
        if close <= 0:
            raise StopCollection(f"{name}: non-positive close on {d}")
        out.append({"date": _day(d).isoformat(), "close": close})
    return out, reads


def save(path: Path, name: str, rows: list[dict[str, Any]], reads: int, secrets: list[str],
         now: Callable[[], datetime] = lambda: datetime.now(KST)) -> None:
    kind, code, _ = SERIES[name]
    text = json.dumps({"name": name, "kind": kind, "code": code, "fetched_at": now().isoformat(), "reads": reads,
                       "rows": rows}, ensure_ascii=False, sort_keys=True)
    if any(secret in text for secret in secrets):
        raise StopCollection("a response contains a credential; nothing written")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".partial")
    temporary.write_text(text + "\n", encoding="utf-8")
    os.replace(temporary, path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--start", type=date.fromisoformat, default=date(1990, 1, 1))
    parser.add_argument("--end", type=date.fromisoformat, required=True)
    parser.add_argument("--out", type=Path, default=Path("data/kis/daily-history"))
    args = parser.parse_args(argv)
    if not args.start <= args.end < datetime.now(KST).date():
        print("kis-daily-history: STOP: need start <= end < today")
        return 1
    from trading_agent.kis_real import KisRealReadOnly

    client = KisRealReadOnly(token_cache_dir=Path.home() / ".cache" / "kis-intraday")
    secrets = [v for v in (os.environ.get(n, "") for n in SECRET_NAMES) if len(v) >= 4]
    for name in SERIES:
        path = args.out / f"{name}.json"
        if path.exists():
            rows = json.loads(path.read_text(encoding="utf-8"))["rows"]
            print(f"kis-daily-history: {name} kept {rows[0]['date']}~{rows[-1]['date']} {len(rows)} closes")
            continue
        try:
            rows, reads = collect(client, name, args.start, args.end)
            save(path, name, rows, reads, secrets)
        except StopCollection as exc:
            print(f"kis-daily-history: STOP: {exc}")
            return 1
        print(f"kis-daily-history: {name} {rows[0]['date']}~{rows[-1]['date']} {len(rows)} closes, {reads} reads")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
