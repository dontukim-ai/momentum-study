"""Market trend filter (strategy-candidates-v8 C1): hold one ETF only above its 10-month average."""

import unittest
from datetime import date, timedelta

from trading_agent.contracts import AgentError, Bar, Costs, D, session_time
from trading_agent.data import Snapshot
from trading_agent.logic import LogicSettings, candidate_grid
from trading_agent.selection import (
    TREND_MONTHS,
    RankedStrategy,
    SecurityHistory,
    SecurityState,
    SelectionPolicy,
    select_candidates,
)

POLICY = SelectionPolicy(market_symbol="IDX")


def market(close_of):
    """Weekday bars from 2023-01-02; close_of(i) gives IDX's close on session i."""
    bars, records, sessions, day = [], [], [], date(2023, 1, 2)
    while len(sessions) < 260:
        if day.weekday() < 5:
            i = len(sessions)
            sessions.append(day)
            for symbol, kind, close in (("IDX", "ETF", close_of(i)), ("STOCK", "STOCK", D(10000) + i)):
                bars.append(Bar(day, symbol, close, close, close, close, 1000000, session_time(day, 15, 40)))
                records.append(SecurityState(symbol, session_time(day, 15, 30), session_time(day, 15, 40), "NORMAL",
                                             kind, "KOSPI", False, False, False, False, close, close, "TEST"))
        day += timedelta(days=1)
    return Snapshot(bars), SecurityHistory(records), sessions


def screen(snapshot, securities, day, policy=POLICY):
    at = session_time(day, 16)
    return select_candidates(RankedStrategy(20, 1, D(".4"), ranking="market_trend"),
                             snapshot.view(at, day, allow_missing=True), securities, policy, Costs(), at + timedelta(hours=1))


class TrendRankingTests(unittest.TestCase):
    def test_holds_the_etf_above_its_ten_month_average_of_month_end_closes(self):
        snapshot, securities, sessions = market(lambda i: D(10000) + 10 * i)
        day = sessions[-1]
        report = screen(snapshot, securities, day)
        self.assertEqual(report["selected"], ["IDX"])
        rows = {r["symbol"]: r for r in report["candidates"]}
        month_ends = {}
        for d_index, d in enumerate(sessions):
            month_ends[(d.year, d.month)] = D(10000) + 10 * d_index
        expected = sum(list(month_ends.values())[-TREND_MONTHS:], D(0)) / TREND_MONTHS
        self.assertEqual(rows["IDX"]["features"]["trend_average"], expected)
        self.assertEqual(rows["STOCK"]["reasons"], ["NOT_TREND_SYMBOL"])
        self.assertNotIn("MOMENTUM_BELOW_THRESHOLD", rows["IDX"]["reasons"])

    def test_cash_below_the_average_and_without_ten_months(self):
        # Rises, then falls back under its average in the last weeks.
        snapshot, securities, sessions = market(lambda i: D(10000) + 10 * i if i < 230 else D(10000))
        report = screen(snapshot, securities, sessions[-1])
        self.assertEqual(report["selected"], [])
        self.assertEqual({r["symbol"]: r for r in report["candidates"]}["IDX"]["reasons"], ["BELOW_TREND"])
        early = screen(snapshot, securities, sessions[150])  # about seven months of history
        self.assertEqual({r["symbol"]: r for r in early["candidates"]}["IDX"]["reasons"], ["INSUFFICIENT_TREND_HISTORY"])

    def test_market_symbol_is_required_and_proposed_once(self):
        snapshot, securities, sessions = market(lambda i: D(10000))
        with self.assertRaisesRegex(AgentError, "market_symbol"):
            screen(snapshot, securities, sessions[-1], SelectionPolicy())
        with self.assertRaisesRegex(AgentError, "market symbol"):
            SelectionPolicy(market_symbol="bad/name")
        grid = candidate_grid(RankedStrategy(20), (20, 40), ("momentum", "market_trend"))
        self.assertEqual([(s.ranking, s.lookback) for s in grid], [("momentum", 40), ("market_trend", 20)])
        LogicSettings(champion=RankedStrategy(20), lookbacks=(20, 40), rankings=("momentum", "market_trend"),
                      candidate_budget=2)


if __name__ == "__main__":
    unittest.main()
