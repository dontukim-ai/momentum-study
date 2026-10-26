"""Cost metrics from collected order books and minute bars (intraday-cost-probe-v10 §5, §6).

    uv run python tools/kis_intraday_report.py data/kis/intraday/2026-10-28 data/kis/intraday/2026-10-29 ...

Per symbol: spread (bp of the mid) by 30-minute bucket, the round-trip cost
estimate (mid-session spread median + 2 x commission + sell tax), the KRW size
of the best levels and how often 1M/10M/100M won fits inside them, minute-bar
completeness and delay, errors and slow reads. The §6 call uses the ETFs'
09:30~15:00 round-trip medians. Prints a table and one JSON line; writes nothing.
"""

from __future__ import annotations

import argparse
import json
import statistics
from collections import defaultdict
from datetime import datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

from momentum.config import SpecUndefinedError

KST = ZoneInfo("Asia/Seoul")
ETFS = ("069500", "102110", "229200", "278530")
SELL_TAX_BPS = {"005930": 20.0}  # listed stock 0.20%; domestic equity ETFs pay none
SIZES = (1_000_000, 10_000_000, 100_000_000)
CONTINUOUS = (time(9), time(15, 20))
MID_SESSION = (time(9, 30), time(15))
BARS_PER_DAY = 381  # 09:00..15:30 nominal, intraday-cost-probe-v10 §5
SLOW_MS = 1000


def quantile(values: list[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(q * len(ordered)))]


def book_row(record: dict) -> dict | None:
    """Spread and best-level sizes of one snapshot; None when a side is empty."""
    book = record["response"].get("output1") or {}
    try:
        ask, bid = float(book["askp1"]), float(book["bidp1"])
        ask_size, bid_size = float(book["askp_rsqn1"]), float(book["bidp_rsqn1"])
    except (KeyError, TypeError, ValueError) as exc:
        raise SpecUndefinedError(f"order book without best levels: {exc}") from None
    if ask <= 0 or bid <= 0 or ask < bid:
        return None
    at = datetime.fromisoformat(record["received_at"]).astimezone(KST)
    return {"at": at, "spread_bp": (ask - bid) / ((ask + bid) / 2) * 1e4,
            "ask_krw": ask * ask_size, "bid_krw": bid * bid_size}


def hhmmss(text: str) -> time:
    return time(int(text[:2]), int(text[2:4]), int(text[4:6]))


def bucket(at: datetime) -> str:
    minute = (at.minute // 30) * 30
    return f"{at.hour:02d}:{minute:02d}"


def report(days: list[Path], commission_bps: float) -> dict:
    books: dict[str, list[dict]] = defaultdict(list)
    empty: dict[str, int] = defaultdict(int)
    slow: dict[str, int] = defaultdict(int)
    bars: dict[str, dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))
    delays: dict[str, list[float]] = defaultdict(list)
    errors = 0
    for day in days:
        for path in sorted(day.glob("asking-*.jsonl")):
            symbol = path.stem.split("-", 1)[1]
            for line in path.read_text(encoding="utf-8").splitlines():
                record = json.loads(line)
                slow[symbol] += record["elapsed_ms"] > SLOW_MS
                row = book_row(record)
                if row is None:
                    empty[symbol] += 1
                else:
                    books[symbol].append(row)
        for path in sorted(day.glob("minute-*.jsonl")):
            symbol = path.stem.split("-", 1)[1]
            for line in path.read_text(encoding="utf-8").splitlines():
                record = json.loads(line)
                slow[symbol] += record["elapsed_ms"] > SLOW_MS
                rows = record["response"].get("output2") or []
                times = [r["stck_cntg_hour"] for r in rows if r.get("stck_bsop_date") == day.name.replace("-", "")]
                if record.get("pull") == "close":
                    bars[symbol][day.name].update(t for t in times if "090000" <= t <= "153000")
                elif times:
                    received = datetime.fromisoformat(record["received_at"]).astimezone(KST)
                    latest = datetime.combine(received.date(), hhmmss(max(times)), KST)
                    delays[symbol].append((received - latest).total_seconds())
        if (day / "errors.jsonl").exists():
            errors += len((day / "errors.jsonl").read_text(encoding="utf-8").splitlines())

    symbols = {}
    for symbol in sorted(set(books) | set(bars)):
        rows = books[symbol]
        continuous = [r for r in rows if CONTINUOUS[0] <= r["at"].time() < CONTINUOUS[1]]
        mid = [r["spread_bp"] for r in rows if MID_SESSION[0] <= r["at"].time() < MID_SESSION[1]]
        by_bucket = defaultdict(list)
        for r in continuous:
            by_bucket[bucket(r["at"])].append(r["spread_bp"])
        median_mid = statistics.median(mid) if mid else None
        counts = [len(v) for v in bars[symbol].values()]
        symbols[symbol] = {
            "snapshots": len(rows), "empty_snapshots": empty[symbol],
            "auction_snapshots": len(rows) - len(continuous),
            "spread_bp_by_bucket": {k: {"median": statistics.median(v), "p90": quantile(v, .9), "n": len(v)}
                                    for k, v in sorted(by_bucket.items())},
            "mid_session_spread_bp_median": median_mid,
            "round_trip_bp": None if median_mid is None
            else median_mid + 2 * commission_bps + SELL_TAX_BPS.get(symbol, 0.0),
            "best_level_krw_median": {
                "ask": statistics.median(r["ask_krw"] for r in continuous) if continuous else None,
                "bid": statistics.median(r["bid_krw"] for r in continuous) if continuous else None},
            "fits_best_level": {str(size): (sum(min(r["ask_krw"], r["bid_krw"]) >= size for r in continuous)
                                            / len(continuous) if continuous else None) for size in SIZES},
            "minute_bars_per_day": counts, "minute_bars_nominal": BARS_PER_DAY,
            "minute_delay_seconds_median": statistics.median(delays[symbol]) if delays[symbol] else None,
            "slow_reads": slow[symbol],
        }
    return {"days": [d.name for d in days], "commission_bps": commission_bps, "errors": errors,
            "symbols": symbols, "decision": stage_call({s: symbols[s]["round_trip_bp"] if s in symbols else None for s in ETFS})}


def stage_call(etf_round_trips: dict[str, float | None]) -> str:
    """intraday-cost-probe-v10 §6, fixed before any result."""
    costs = [etf_round_trips.get(s) for s in ETFS]
    if any(c is None for c in costs):
        return "INCOMPLETE: not every ETF has mid-session books"
    cheapest = min(c for c in costs if c is not None)
    if cheapest <= 5:
        return "NEXT: at least one ETF at or below 5bp; write the stage-2 design"
    if cheapest <= 15:
        return "USER: ETFs between 5 and 15bp; the user decides"
    return "STOP: every ETF above 15bp"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("days", type=Path, nargs="+")
    parser.add_argument("--commission-bps", type=float, default=1.5,
                        help="one-way commission; set to the account's actual KIS rate")
    args = parser.parse_args(argv)
    result = report(sorted(args.days), args.commission_bps)
    print(f"days {len(result['days'])} ({result['days'][0]}~{result['days'][-1]}), "
          f"commission {result['commission_bps']}bp one way, errors {result['errors']}")
    print(f"{'symbol':<7} {'mid spread':>10} {'round trip':>10} {'best ask KRW':>13} "
          f"{'1M':>5} {'10M':>5} {'100M':>5} {'bars/day':>10} {'delay s':>7}")
    for symbol, row in result["symbols"].items():
        fits = row["fits_best_level"]
        spread, cost = row["mid_session_spread_bp_median"], row["round_trip_bp"]
        depth, delay = row["best_level_krw_median"]["ask"], row["minute_delay_seconds_median"]
        print(f"{symbol:<7} {'-' if spread is None else f'{spread:.2f}':>10} {'-' if cost is None else f'{cost:.2f}':>10} "
              f"{'-' if depth is None else f'{depth:,.0f}':>13} "
              + " ".join(f"{'-' if fits[str(s)] is None else f'{fits[str(s)]:.0%}':>5}" for s in SIZES)
              + f" {min(row['minute_bars_per_day'], default=0)}-{max(row['minute_bars_per_day'], default=0):<6}"
              + f" {'-' if delay is None else f'{delay:.0f}':>7}")
    print(f"decision (§6): {result['decision']}")
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
