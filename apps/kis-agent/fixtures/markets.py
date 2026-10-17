"""Engineered deterministic markets for software tests, never investment evidence."""

from datetime import date, timedelta

from trading_agent.contracts import Bar, D, session_time
from trading_agent.data import Snapshot


def release_market(count=403):
    """An obvious repeating cycle exercises the eligible-release code path.

    The 400-session evaluation, 10,000-unit capital and lookbacks 20/2 are
    fixed fixture parameters. Its positive result has no financial significance.
    """
    rows, day, closing = [], date(2024, 1, 2), D(100)
    while len(rows) < count:
        if day.weekday() < 5:
            opening = closing
            closing = (opening * (D("1.02") if len(rows) % 20 < 10 else D(".98"))).quantize(D(".01"))
            rows.append(Bar(day, "DEMO_CYCLE", opening, max(opening, closing), min(opening, closing),
                            closing, 100000, session_time(day, 15, 40), "ENGINEERED_TEST_CYCLE"))
        day += timedelta(days=1)
    return Snapshot(rows)
