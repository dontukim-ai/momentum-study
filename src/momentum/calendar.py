"""Trading calendar: sessions, next-session lookup, and rebalance dates.

Named ``momentum.calendar`` deliberately, colliding with the stdlib
``calendar`` module. The package uses absolute imports throughout and a
``src/`` layout, so ``from momentum.calendar import TradingCalendar`` is
unambiguous; do not write a bare ``import calendar`` anywhere expecting the
stdlib module while this module is on the path under that name.

Scope is intentionally narrow: the session list is supplied by the caller
(no holiday rules, no exchange awareness, no timezone handling). Task 6
bridges the ``calendar`` table produced by ``momentum.synth`` /
``momentum.data.schema`` into this class.
"""

from __future__ import annotations

from bisect import bisect_right
from collections.abc import Callable
from datetime import date
from typing import Literal, Self


class TradingCalendar:
    """A trading calendar backed by an explicit list of session dates.

    All "which day" questions here are answered relative to the supplied
    session list, never relative to the Gregorian calendar: a month-end is
    the last *session* of that month, not the last day of the month, and a
    week-end is the last session of an ISO week, not the last day of a
    Mon-Sun week that happens to overlap a year boundary.
    """

    def __init__(self, sessions: list[date]) -> None:
        self._s = sorted(sessions)

    @classmethod
    def from_dates(cls, dates: list[date]) -> Self:
        return cls(dates)

    def sessions(self, start: date, end: date) -> list[date]:
        """Sessions in ``[start, end]`` inclusive, ascending order."""
        return [d for d in self._s if start <= d <= end]

    def next_session(self, d: date) -> date | None:
        """The first session strictly after ``d``.

        ``d`` need not itself be a session (e.g. a weekend or holiday) — the
        result is simply the first session strictly later than ``d``.
        Returns ``None`` when ``d`` is on or after the last known session,
        i.e. at the end of the data, rather than raising. Tasks 9 and 11
        rely on this ``None`` to detect an unclosable position at the end
        of the sample.
        """
        i = bisect_right(self._s, d)
        return self._s[i] if i < len(self._s) else None

    def _is_last_of(self, d: date, key: Callable[[date], object]) -> bool:
        if d not in self._s:
            return False
        nxt = self.next_session(d)
        # A session with no later session at all (end of the supplied data)
        # is deliberately NOT treated as a month/week end: the calendar has
        # no evidence that the month or week has actually finished, only
        # that the caller's session list ran out. Without this, the very
        # last session in any dataset would always be flagged as both a
        # month-end and a week-end regardless of the date, which would be
        # wrong whenever the data is simply truncated mid-month/mid-week.
        return nxt is not None and key(nxt) != key(d)

    def is_month_end(self, d: date) -> bool:
        """True iff ``d`` is a session and the last session of its month.

        Determined by comparing ``d`` to its next *session* (not by ``d``'s
        position within the calendar month), so a month whose last calendar
        day falls on a holiday or weekend still gets a month-end on the last
        trading session before it. A ``d`` that is not itself a session is
        never a month-end. A ``d`` with no later session at all (the last
        session in the supplied data) is also never a month-end: the
        calendar cannot tell a truncated dataset from a genuine month
        boundary, so it declines to guess.
        """
        return self._is_last_of(d, lambda x: (x.year, x.month))

    def is_week_end(self, d: date) -> bool:
        """True iff ``d`` is a session and the last session of its ISO week.

        Weeks are ISO weeks (``date.isocalendar()[:2]``), so a week that
        spans a year boundary is not split into two weeks. As with
        ``is_month_end``, a ``d`` with no later session at all is never a
        week-end — the calendar declines to guess whether the data simply
        ran out mid-week.
        """
        return self._is_last_of(d, lambda x: x.isocalendar()[:2])

    def rebalance_dates(
        self, freq: Literal["daily", "weekly", "monthly"], start: date, end: date
    ) -> list[date]:
        """Signal observation dates for ``freq`` within ``[start, end]``.

        These are observation dates, NEVER execution dates: the signal is
        read at the close of each returned date, and the corresponding
        trade happens at the open of ``next_session(returned_date)``. Tasks
        10 and 11 build execution schedules by calling ``next_session`` on
        each date this returns — never treat a rebalance date itself as
        when a trade fills.
        """
        pred = {
            "daily": lambda d: True,
            "weekly": self.is_week_end,
            "monthly": self.is_month_end,
        }[freq]
        return [d for d in self.sessions(start, end) if pred(d)]
