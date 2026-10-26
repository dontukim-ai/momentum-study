"""Collect a year of past one-minute bars for the fixed ETF universe (intraday-candidates-v10 §5).

Read-only KIS real-account market data (FHKST03010230). Run on the Mac:

    uv run --extra agent python tools/kis_minute_history.py --universe data/kis/etf-universe.json \
        --start 2025-10-27 --end 2026-10-23

Each symbol-day becomes <out>/<symbol>/<YYYY-MM-DD>.json, written whole once
every bar from 15:30 back to 09:00 has arrived (120 per read). A finished day
is never fetched again, so a rerun resumes. Days without bars (holidays, or
outside what KIS keeps) are saved as empty. A response with a credential stops
the run; 30 failures in a row stop it. Data stay in data/kis (no Git, no
redistribution).

The split is fixed here before any bar is read: DEVELOPMENT_END and earlier
for designing candidates, HOLDOUT_START and later only after the candidates
are frozen in a manifest. ``development_bars`` refuses any later day.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time as clock
from collections.abc import Callable
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from momentum.config import SpecUndefinedError

KST = ZoneInfo("Asia/Seoul")
DEVELOPMENT_END = date(2026, 6, 30)
HOLDOUT_START = date(2026, 7, 1)
OPEN, CLOSE = "090000", "153000"
MAX_FAILURES = 30
SECRET_NAMES = ("KIS_APP_KEY", "KIS_APP_SECRET", "KIS_ACCOUNT")


class StopCollection(Exception):
    pass


def weekdays(start: date, end: date) -> list[date]:
    return [start + timedelta(days=k) for k in range((end - start).days + 1)
            if (start + timedelta(days=k)).weekday() < 5]


def previous_minute(hhmmss: str) -> str:
    minutes = int(hhmmss[:2]) * 60 + int(hhmmss[2:4]) - 1
    return f"{minutes // 60:02d}{minutes % 60:02d}00"


def fetch_day(client: Any, symbol: str, day: date) -> tuple[list[dict], int]:
    """Every bar of ``day``, oldest first, and the number of reads it took."""
    wanted, bars, hour, reads = day.strftime("%Y%m%d"), {}, CLOSE, 0
    for _ in range(10):
        reads += 1
        rows = [r for r in client.minute_bars_on(symbol, day, hour).get("output2") or []
                if r.get("stck_bsop_date") == wanted and OPEN <= str(r.get("stck_cntg_hour")) <= CLOSE]
        new = {r["stck_cntg_hour"]: r for r in rows if r["stck_cntg_hour"] not in bars}
        if not new:
            break
        bars.update(new)
        earliest = min(new)
        if earliest <= OPEN:
            break
        hour = previous_minute(earliest)
    return [bars[t] for t in sorted(bars)], reads


class Collector:
    def __init__(self, client: Any, out: Path, *, environ: dict[str, str] | None = None,
                 now: Callable[[], datetime] = lambda: datetime.now(KST)):
        env = os.environ if environ is None else environ
        self.client, self.out, self.now = client, out, now
        self.secrets = [v for v in (env.get(n, "") for n in SECRET_NAMES) if len(v) >= 4]
        self.failures = 0

    def day(self, symbol: str, day: date) -> str:
        path = self.out / symbol / f"{day.isoformat()}.json"
        if path.exists():
            return "skipped"
        try:
            bars, reads = fetch_day(self.client, symbol, day)
        except Exception as exc:  # noqa: BLE001 - logged, the day stays unfinished for a rerun
            self.failures += 1
            path.parent.mkdir(parents=True, exist_ok=True)
            with (self.out / "errors.jsonl").open("a", encoding="utf-8") as handle:
                handle.write(json.dumps({"at": self.now().isoformat(), "symbol": symbol, "day": day.isoformat(),
                                         "error": f"{type(exc).__name__}: {exc}"[:300]}) + "\n")
            if self.failures >= MAX_FAILURES:
                raise StopCollection(f"{MAX_FAILURES} failures in a row") from None
            return "error"
        self.failures = 0
        text = json.dumps({"symbol": symbol, "day": day.isoformat(), "fetched_at": self.now().isoformat(),
                           "reads": reads, "bars": bars}, ensure_ascii=False, sort_keys=True)
        if any(secret in text for secret in self.secrets):
            raise StopCollection("a response contains a credential; nothing written")
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".partial")
        temporary.write_text(text + "\n", encoding="utf-8")
        os.replace(temporary, path)
        return "empty" if not bars else "fetched"


def development_bars(root: Path, symbol: str) -> dict[str, list[dict]]:
    """Saved bars by day, development days only (holdout days stay unread)."""
    days = {}
    for path in sorted((root / symbol).glob("*.json")):
        day = date.fromisoformat(path.stem)
        if day > DEVELOPMENT_END:
            continue
        days[path.stem] = json.loads(path.read_text(encoding="utf-8"))["bars"]
    return days


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--universe", type=Path, required=True)
    parser.add_argument("--start", type=date.fromisoformat, required=True)
    parser.add_argument("--end", type=date.fromisoformat, required=True)
    parser.add_argument("--out", type=Path, default=Path("data/kis/minute-history"))
    args = parser.parse_args(argv)
    if args.end < args.start or args.end >= datetime.now(KST).date():
        print("kis-minute-history: STOP: need start <= end < today")
        return 1
    symbols = [row["symbol"] for row in json.loads(args.universe.read_text(encoding="utf-8"))["symbols"]]
    from trading_agent.kis_real import KisRealReadOnly

    collector = Collector(KisRealReadOnly(token_cache_dir=Path.home() / ".cache" / "kis-intraday"), args.out)
    counts: dict[str, int] = {}
    started = clock.monotonic()
    try:
        for symbol in symbols:
            for day in weekdays(args.start, args.end):
                status = collector.day(symbol, day)
                counts[status] = counts.get(status, 0) + 1
            print(f"kis-minute-history: {symbol} done {counts} {clock.monotonic() - started:.0f}s", flush=True)
    except (StopCollection, SpecUndefinedError) as exc:
        print(f"kis-minute-history: STOP: {exc} {counts}")
        return 1
    print(f"kis-minute-history: {len(symbols)} symbols {args.start}~{args.end} {counts}", file=sys.stdout)
    return 0 if not counts.get("error") else 1


if __name__ == "__main__":
    raise SystemExit(main())
