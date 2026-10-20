"""Security-state history for research input from quotes and KRX lists (DATA-07b).

Policy 1 (user decision, 2026-10-20). KRX publishes 관리종목 and 매매거래정지
only as *current* lists (data.krx 「이슈 통계」, downloaded by hand, CP949,
dates ``YYYY/MM/DD``); designation history is not available in bulk. So:

- trading status (NORMAL / SUSPENDED / DELISTED) and market come from the
  quotes (``fsc_states``); the KRX halt and delisting lists only cross-check;
- ``managed`` is true from a listed 최초지정일 onward and false everywhere
  else, which is *unverified* before the snapshot: a stock designated and
  released before it reads as false. A listed row without 최초지정일 is held
  out, not guessed;
- ``caution`` (market warnings) has no history at all and is false;
- every record names that basis in ``source`` so results carry it.

Delisting rows keep only 증권구분 주권; identical duplicates across files
are dropped, conflicting ones refused. ``이전상장후_종목코드`` is not a
code change (it holds asset managers and issuers) and is not read.
"""

from __future__ import annotations

import csv
import io
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from momentum.agent.market import KRX
from momentum.config import SpecUndefinedError

from .fsc_states import StatusEvent

SOURCE = (
    "fsc-quotes status; krx manual snapshot {snapshot} for 관리종목; "
    "managed/caution history unverified before snapshot (policy 1)"
)


@dataclass(frozen=True)
class KrxLists:
    managed: dict[str, date]
    managed_held: list[str]
    halted: dict[str, date]
    delisted: dict[str, date]


def _day(text: str) -> date | None:
    text = text.strip()
    if not text:
        return None
    parts = text.split("/")
    if len(parts) != 3 or not all(p.isdigit() for p in parts):
        raise SpecUndefinedError(f"unreadable KRX date: {text!r}")
    return date(int(parts[0]), int(parts[1]), int(parts[2]))


def _rows(path: Path) -> list[dict[str, str]]:
    raw = path.read_bytes()
    try:
        text = raw.decode("cp949")
    except UnicodeDecodeError:
        text = raw.decode("utf-8-sig")
    return list(csv.DictReader(io.StringIO(text)))


def read_lists(paths: Iterable[Path]) -> KrxLists:
    """Classifies each downloaded file by its header, not its name."""
    managed: dict[str, date] = {}
    held: list[str] = []
    halted: dict[str, date] = {}
    delisted: dict[str, tuple[date, dict[str, str]]] = {}
    for path in sorted(paths):
        rows = _rows(path)
        header = set(rows[0]) if rows else set()
        for row in rows:
            code = row["종목코드"].strip()
            if "최초지정일" in header:
                start = _day(row["최초지정일"])
                if start is None:
                    held.append(code)
                else:
                    managed[code] = start
            elif "정지일" in header:
                start = _day(row["정지일"])
                if start is not None:
                    halted[code] = start
            elif "폐지일" in header:
                if row.get("증권구분", "").strip() != "주권":
                    continue
                day = _day(row["폐지일"])
                if day is None:
                    raise SpecUndefinedError(f"{path}: {code} without 폐지일")
                key = {k: v for k, v in row.items() if k != "번호"}
                previous = delisted.get(code)
                if previous is not None and previous[1] != key:
                    raise SpecUndefinedError(f"{code}: conflicting delisting rows")
                delisted[code] = (day, key)
            else:
                raise SpecUndefinedError(f"{path}: unrecognized KRX list header")
    return KrxLists(
        managed, sorted(held), halted, {c: d for c, (d, _) in delisted.items()}
    )


def build(
    events: Iterable[StatusEvent],
    lists: KrxLists,
    sessions: tuple[date, ...],
    snapshot: date,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Security-state JSONL records and a cross-check report."""
    by_symbol: dict[str, list[StatusEvent]] = {}
    for event in events:
        by_symbol.setdefault(event.symbol, []).append(event)
    source = SOURCE.format(snapshot=snapshot.isoformat())
    records: list[dict[str, Any]] = []
    for symbol, history in sorted(by_symbol.items()):
        history.sort(key=lambda e: e.session)
        start = lists.managed.get(symbol)
        # A designation becomes a change point on the first session from it.
        points = {e.session: e for e in history}
        if start is not None:
            first = next((d for d in sorted(sessions) if d >= start), None)
            if first is not None and first > history[0].session:
                current = [e for e in history if e.session <= first][-1]
                points.setdefault(
                    first, StatusEvent(symbol, first, current.status, current.market)
                )
        for day in sorted(points):
            event = points[day]
            records.append(
                {
                    "symbol": symbol,
                    "effective_at": KRX.open_at(day).isoformat(),
                    "available_at": KRX.historical_bar_at(day).isoformat(),
                    "status": event.status,
                    "asset_type": "STOCK",
                    "market": event.market,
                    "managed": start is not None and day >= start,
                    "caution": False,
                    "leveraged": False,
                    "inverse": False,
                    "bid": None,
                    "ask": None,
                    "source": source,
                }
            )
    last = max(sessions)
    inferred = {
        s: e.session for s, h in by_symbol.items() for e in h if e.status == "DELISTED"
    }
    listed = {c: d for c, d in lists.delisted.items() if d <= last}
    final = {s: h[-1].status for s, h in by_symbol.items()}  # sorted above
    report = {
        "snapshot": snapshot.isoformat(),
        "records": len(records),
        "symbols": len(by_symbol),
        "managed_held_without_start": lists.managed_held,
        "delisting": {
            "both": sum(1 for c in listed if c in inferred),
            # How 폐지일 relates to the last quoted session is not yet checked.
            "date_mismatch": sorted(
                {
                    c: (str(listed[c]), str(inferred[c]))
                    for c in listed
                    if c in inferred and inferred[c] != listed[c]
                }.items()
            ),
            "krx_only": sorted(
                c for c in listed if c not in inferred and c in by_symbol
            ),
            "krx_never_quoted": sorted(c for c in listed if c not in by_symbol),
            "quotes_only": sorted(c for c in inferred if c not in lists.delisted),
        },
        "halted_now_but_not_suspended_at_end": sorted(
            c
            for c, d in lists.halted.items()
            if d <= last and c in final and final[c] != "SUSPENDED"
        ),
    }
    return records, report
