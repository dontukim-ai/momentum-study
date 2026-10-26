"""Collect one day of KRX order books and minute bars (intraday-cost-probe-v10 §3).

Read-only KIS real-account market data; no order route exists in the client.
Run on the Mac before 08:30 KST on a trading day (keys from the environment):

    uv run --extra agent python tools/kis_intraday_probe.py

From 08:30 to 15:30 KST every symbol's ten-level book is read every
--interval seconds; at each hour 10:00..15:00 the latest 30 one-minute bars;
after 15:31 the whole day's bars, 30 at a time back to 09:00. Each answer is
appended with its receipt time to data/kis/intraday/<day>/ (kept out of Git,
not redistributed). An existing day directory is refused unless --resume.
A response that contains a key is never written. Request errors are logged
and the day goes on; 30 failures in a row stop it.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time as clock
from collections.abc import Callable
from datetime import datetime, time, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

KST = ZoneInfo("Asia/Seoul")
SYMBOLS = ("069500", "102110", "229200", "278530", "005930")
BOOK_START, BOOK_END, CLOSE_PULL = time(8, 30), time(15, 30), time(15, 31)
HOURS = (time(10), time(11), time(12), time(13), time(14), time(15))
SESSION_OPEN = time(9)
MAX_FAILURES = 30
SECRET_NAMES = ("KIS_APP_KEY", "KIS_APP_SECRET", "KIS_ACCOUNT")


class StopProbe(Exception):
    pass


class Probe:
    def __init__(self, client: Any, out: Path, symbols: tuple[str, ...], *,
                 now: Callable[[], datetime], monotonic: Callable[[], float] = clock.monotonic,
                 environ: dict[str, str] | None = None):
        self.client, self.out, self.symbols = client, out, symbols
        self.now, self.monotonic = now, monotonic
        env = os.environ if environ is None else environ
        self.secrets = [v for v in (env.get(n, "") for n in SECRET_NAMES) if len(v) >= 4]
        self.failures = 0

    def _write(self, name: str, record: dict) -> None:
        text = json.dumps(record, ensure_ascii=False, sort_keys=True)
        if any(secret in text for secret in self.secrets):
            raise StopProbe("a response contains a credential; nothing written")
        with (self.out / name).open("a", encoding="utf-8") as handle:
            handle.write(text + "\n")

    def call(self, name: str, symbol: str, read: Callable[[], dict], **extra: str) -> dict | None:
        received = self.now()
        started = self.monotonic()
        try:
            result = read()
        except Exception as exc:  # noqa: BLE001 - every failure is logged, never retried here
            self.failures += 1
            self._write("errors.jsonl", {"received_at": received.isoformat(), "symbol": symbol, "read": name,
                                         "error": f"{type(exc).__name__}: {exc}"[:300], **extra})
            if self.failures >= MAX_FAILURES:
                raise StopProbe(f"{MAX_FAILURES} failures in a row") from None
            return None
        self.failures = 0
        response = {k: v for k, v in result.items() if k != "_response_headers"}
        self._write(f"{name}-{symbol}.jsonl", {
            "received_at": received.isoformat(), "elapsed_ms": round((self.monotonic() - started) * 1000),
            "response": response, **extra})
        return response

    def book_round(self) -> None:
        for symbol in self.symbols:
            self.call("asking", symbol, lambda s=symbol: dict(self.client.asking(s)))

    def latest_bars(self, at: time) -> None:
        hour = at.strftime("%H%M%S")
        for symbol in self.symbols:
            self.call("minute", symbol, lambda s=symbol: dict(self.client.minute_bars(s, hour)), pull="hourly",
                      hour=hour)

    def day_bars(self) -> None:
        """All bars from 15:30 back to the open, 30 per request."""
        for symbol in self.symbols:
            hour = "153000"
            for _ in range(20):
                response = self.call("minute", symbol, lambda s=symbol, h=hour: dict(self.client.minute_bars(s, h)),
                                     pull="close", hour=hour)
                rows = (response or {}).get("output2") or []
                times = sorted(r["stck_cntg_hour"] for r in rows if r.get("stck_cntg_hour"))
                if not times or times[0] <= SESSION_OPEN.strftime("%H%M%S"):
                    break
                minutes = int(times[0][:2]) * 60 + int(times[0][2:4]) - 1
                hour = f"{minutes // 60:02d}{minutes % 60:02d}00"


def run_day(probe: Probe, interval: float, sleep: Callable[[float], None]) -> dict[str, int]:
    """Poll books until 15:30, pull hourly bars on the hour, then the day's bars."""
    today = probe.now().date()
    pending = [h for h in HOURS if probe.now().time() < h]
    rounds = 0
    while True:
        now = probe.now()
        if now.date() != today or now.time() >= BOOK_END:
            break
        if now.time() >= BOOK_START:
            probe.book_round()
            rounds += 1
        while pending and probe.now().time() >= pending[0]:
            probe.latest_bars(pending.pop(0))
        target = now + timedelta(seconds=interval)
        if now.time() < BOOK_START:
            target = datetime.combine(today, BOOK_START, KST)
        wait = (target - probe.now()).total_seconds()
        if wait > 0:
            sleep(wait)
    while probe.now().time() < CLOSE_PULL:
        sleep(max(1.0, (datetime.combine(today, CLOSE_PULL, KST) - probe.now()).total_seconds()))
    probe.day_bars()
    return {"book_rounds": rounds}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", type=Path, default=Path("data/kis/intraday"))
    parser.add_argument("--symbols", default=",".join(SYMBOLS))
    parser.add_argument("--interval", type=float, default=10.0)
    parser.add_argument("--resume", action="store_true", help="append to today's directory after a crash")
    args = parser.parse_args(argv)
    from trading_agent.kis_real import KisRealReadOnly

    now = datetime.now(KST)
    if now.weekday() >= 5 or now.time() >= BOOK_END or args.interval < 5:
        print("kis-intraday-probe: STOP: run on a weekday before 15:30 KST with --interval >= 5")
        return 1
    out = args.out / now.date().isoformat()
    if out.exists() and not args.resume:
        print(f"kis-intraday-probe: STOP: {out} exists; use --resume to append")
        return 1
    out.mkdir(parents=True, exist_ok=True)
    symbols = tuple(s.strip() for s in args.symbols.split(",") if s.strip())
    probe = Probe(KisRealReadOnly(token_cache_dir=Path.home() / ".cache" / "kis-intraday"), out, symbols,
                  now=lambda: datetime.now(KST))
    try:
        summary = run_day(probe, args.interval, clock.sleep)
    except StopProbe as exc:
        print(f"kis-intraday-probe: STOP: {exc}")
        return 1
    print(f"kis-intraday-probe: {out} {summary}", file=sys.stdout)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
