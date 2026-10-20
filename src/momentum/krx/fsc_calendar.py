"""Observed KRX session calendar from collected FSC stock days (DATA-03a).

A weekday is a session when the supplier returned rows for it. An empty answer
is ambiguous: a holiday, or a day the supplier has not published yet (it
publishes one business day later, after 13:00). It counts as closed only once a
*later* day has returned rows, which proves the supplier had published past it.
Empty days before the first day with rows are outside the supplier's coverage,
not holidays: FSC V2 answers 0 rows without error for any date before it
started (2020-01-02 returned rows, 2019-01-02 and earlier none; checked
2026-10-20). They are reported as ``before_coverage`` and the calendar starts at
the first observed session.
The calendar stops before the first weekday that is uncollected or still
ambiguous, so it never claims a session structure it has not observed.
Weekends are not requested; KRX has no weekend sessions.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

from momentum.config import SpecUndefinedError

from .fsc_collect import weekdays


@dataclass(frozen=True)
class CalendarResult:
    sessions: tuple[date, ...]
    confirmed_through: date | None
    closed: tuple[date, ...]
    pending: tuple[date, ...] = ()
    uncollected: tuple[date, ...] = ()
    before_coverage: tuple[date, ...] = ()
    counts: dict[str, int] = field(default_factory=dict)

    def report(self) -> dict[str, Any]:
        return {
            "confirmed_through": self.confirmed_through.isoformat()
            if self.confirmed_through
            else None,
            "sessions": len(self.sessions),
            "closed_weekdays": [d.isoformat() for d in self.closed],
            "pending": [d.isoformat() for d in self.pending],
            "uncollected": [d.isoformat() for d in self.uncollected],
            "before_coverage": [d.isoformat() for d in self.before_coverage],
            "rows_per_session": self.counts,
        }


def _observed(day_dir: Path, day: date) -> int | None:
    record_path = day_dir / "fetch.json"
    if not record_path.exists():
        return None
    record = json.loads(record_path.read_text(encoding="utf-8"))
    if record.get("requested_date") != day.isoformat():
        raise SpecUndefinedError(f"{record_path}: requested_date mismatch")
    total = int(record["total_count"])
    if total > 0:
        first = json.loads(
            (day_dir / record["pages"][0]["file"]).read_text(encoding="utf-8")
        )
        item = first["response"]["body"]["items"]["item"]
        sample = item if isinstance(item, dict) else item[0]
        if str(sample.get("basDt")) != day.strftime("%Y%m%d"):
            raise SpecUndefinedError(f"{record_path}: rows are for another date")
    return total


def build_calendar(
    root: Path, start: date, end: date, kind: str = "stock"
) -> CalendarResult:
    totals = {
        day: _observed(Path(root) / kind / day.isoformat(), day)
        for day in weekdays(start, end)
    }
    with_rows = [d for d, t in totals.items() if t]
    first_with_rows = min(with_rows, default=None)
    last_with_rows = max(with_rows, default=None)
    sessions, closed, pending, uncollected, before = [], [], [], [], []
    confirmed: date | None = None
    blocked = False
    for day, total in totals.items():
        if total is None:
            uncollected.append(day)
            blocked = True
            continue
        if total > 0:
            state = "session"
        elif first_with_rows is not None and day < first_with_rows and not blocked:
            before.append(day)
            continue
        elif last_with_rows is not None and day < last_with_rows:
            state = "closed"
        else:
            pending.append(day)
            blocked = True
            continue
        if blocked:
            continue
        confirmed = day
        if state == "session":
            sessions.append(day)
        else:
            closed.append(day)
    counts = {d.isoformat(): totals[d] or 0 for d in sessions}
    return CalendarResult(
        tuple(sessions),
        confirmed,
        tuple(closed),
        tuple(pending),
        tuple(uncollected),
        tuple(before),
        counts,
    )
