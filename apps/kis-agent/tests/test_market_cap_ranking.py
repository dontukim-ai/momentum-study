"""Market-cap rankings (strategy-candidates-v8 C4): top caps, optionally only above their 200-session average."""

import unittest
from datetime import date, timedelta

from trading_agent.contracts import AgentError, Bar, Costs, D, session_time
from trading_agent.data import Snapshot
from trading_agent.fundamentals import FundamentalHistory, ShareCount
from trading_agent.logic import candidate_grid
from trading_agent.selection import (
    CAP_TREND_SESSIONS,
    RankedStrategy,
    SecurityHistory,
    SecurityState,
    SelectionPolicy,
    select_candidates,
)

POLICY = SelectionPolicy(max_market_cap_rank=3)
SPLIT = 100  # C splits 1:5 here: its raw close drops 80% while its market cap does not


def close_of(symbol, i):
    if symbol == "A":
        return D(10000) + 10 * i
    if symbol == "B":  # rises, then falls under its average in the last sessions
        return D(10000) + 10 * i if i < 225 else D(9000)
    if symbol == "C":
        return (D(20000) + 10 * i) / (5 if i >= SPLIT else 1)
    return D(5000)  # D: smallest cap, outside the universe


def market(count=240):
    bars, records, sessions, day = [], [], [], date(2023, 1, 2)
    while len(sessions) < count:
        if day.weekday() < 5:
            i = len(sessions)
            sessions.append(day)
            for symbol, kind in (("A", "STOCK"), ("B", "STOCK"), ("C", "STOCK"), ("D", "STOCK"), ("ETF", "ETF")):
                close = close_of(symbol, i) if kind == "STOCK" else D(30000)
                bars.append(Bar(day, symbol, close, close, close, close, 1000000, session_time(day, 15, 40)))
                records.append(SecurityState(symbol, session_time(day, 15, 30), session_time(day, 15, 40), "NORMAL",
                                             kind, "KOSPI", False, False, False, False, close, close, "TEST"))
        day += timedelta(days=1)
    first, last = sessions[0], sessions[-1]
    shares = [ShareCount(s, first, last, session_time(first, 15, 40), 1000, "TEST") for s in ("A", "B", "D")]
    shares += [ShareCount("C", first, sessions[SPLIT - 1], session_time(first, 15, 40), 1000, "TEST"),
               ShareCount("C", sessions[SPLIT], last, session_time(sessions[SPLIT], 15, 40), 5000, "TEST")]
    return Snapshot(bars), SecurityHistory(records), FundamentalHistory([], shares), sessions


def screen(ranking, day, data, policy=POLICY, fundamentals=True):
    snapshot, securities, history, _ = data
    at = session_time(day, 16)
    return select_candidates(RankedStrategy(20, 3, D(".6"), ranking=ranking), snapshot.view(at, day, allow_missing=True),
                             securities, policy, Costs(), at + timedelta(hours=1),
                             fundamentals=history if fundamentals else None)


def rows(report):
    return {r["symbol"]: r for r in report["candidates"]}


class MarketCapRankingTests(unittest.TestCase):
    def setUp(self):
        self.data = market()
        self.last = self.data[3][-1]

    def test_market_cap_holds_the_largest_caps_at_fixed_weights(self):
        report = screen("market_cap", self.last, self.data)
        self.assertEqual(report["selected"], ["C", "A", "B"])
        found = rows(report)
        self.assertEqual(found["C"]["features"]["score"], close_of("C", 239) * 5000)
        self.assertEqual(found["D"]["reasons"], ["OUTSIDE_MARKET_CAP_UNIVERSE"])
        self.assertIn("MARKET_CAP_UNAVAILABLE", found["ETF"]["reasons"])
        self.assertEqual(set(report["decision"].target_weights.values()), {D(".2")})

    def test_trend_drops_stocks_at_or_under_their_average_without_reweighting(self):
        report = screen("market_cap_trend", self.last, self.data)
        found = rows(report)
        self.assertEqual(report["selected"], ["C", "A"])
        self.assertEqual(found["B"]["reasons"], ["BELOW_TREND"])
        # The split leaves C's raw close far under its raw average, but not its market cap.
        closes = [close_of("C", i) for i in range(240 - CAP_TREND_SESSIONS, 240)]
        self.assertLess(closes[-1], sum(closes, D(0)) / len(closes))
        caps = [close_of("C", i) * (5000 if i >= SPLIT else 1000) for i in range(240 - CAP_TREND_SESSIONS, 240)]
        self.assertEqual(found["C"]["features"]["trend_average"], sum(caps, D(0)) / len(caps))
        self.assertNotIn("trend_ratio", found["D"]["features"])  # outside the universe: not computed
        self.assertEqual(report["decision"].target_weights, {"C": D(".2"), "A": D(".2")})
        self.assertEqual(report["cash_weight"], D(".6"))

    def test_trend_needs_full_share_history(self):
        early = rows(screen("market_cap_trend", self.data[3][CAP_TREND_SESSIONS - 2], self.data))
        self.assertEqual(early["A"]["reasons"], ["INSUFFICIENT_TREND_HISTORY"])
        exact = rows(screen("market_cap_trend", self.data[3][CAP_TREND_SESSIONS - 1], self.data))
        self.assertEqual(exact["A"]["status"], "SELECTED")
        snapshot, securities, history, sessions = self.data
        gap = FundamentalHistory([], [r for r in history.share_records if r.symbol != "C"]
                                 + [ShareCount("C", sessions[SPLIT], sessions[-1], session_time(sessions[SPLIT], 15, 40),
                                               5000, "TEST")])
        found = rows(screen("market_cap_trend", self.last, (snapshot, securities, gap, sessions)))
        self.assertEqual(found["C"]["reasons"], ["INSUFFICIENT_TREND_HISTORY"])

    def test_needs_fundamentals_and_is_proposed_once(self):
        with self.assertRaisesRegex(AgentError, "fundamentals"):
            screen("market_cap", self.last, self.data, SelectionPolicy(), fundamentals=False)
        grid = candidate_grid(RankedStrategy(20), (20, 40), ("momentum", "market_cap", "market_cap_trend"))
        self.assertEqual([(s.ranking, s.lookback) for s in grid],
                         [("momentum", 40), ("market_cap", 20), ("market_cap_trend", 20)])


if __name__ == "__main__":
    unittest.main()
