"""Residual momentum (strategy-candidates-v8 C3): rank by market-adjusted return per unit of residual risk."""

import statistics
import unittest
from datetime import date, timedelta

from trading_agent.contracts import AgentError, Bar, Costs, D, session_time
from trading_agent.data import Snapshot
from trading_agent.selection import (
    RankedStrategy,
    SecurityHistory,
    SecurityState,
    SelectionPolicy,
    select_candidates,
)

POLICY = SelectionPolicy(market_symbol="IDX")


def market_return(i):
    return D("0.01") * ((i * 3) % 7 - 3) / 3  # deterministic swings, mean zero over 7 sessions


def noise(i, k):
    return D("0.001") * ((i * k) % 5 - 2)


RETURNS = {
    "IDX": market_return,
    "ALPHA": lambda i: market_return(i) + D("0.002") + noise(i, 2),   # beats the market each day
    "LOSER": lambda i: market_return(i) - D("0.002") + noise(i, 3),
    "BETA2": lambda i: 2 * market_return(i) + noise(i, 4),          # high beta, no alpha
}


def market(skip=None):
    bars, records, sessions, day = [], [], [], date(2024, 1, 2)
    closes = dict.fromkeys(RETURNS, D(10000))
    while len(sessions) < 90:
        if day.weekday() < 5:
            i = len(sessions)
            sessions.append(day)
            for symbol, rate in RETURNS.items():
                if i:
                    closes[symbol] *= 1 + rate(i)
                if (symbol, i) == skip:
                    continue
                close = closes[symbol]
                bars.append(Bar(day, symbol, close, close, close, close, 1000000, session_time(day, 15, 40)))
                records.append(SecurityState(symbol, session_time(day, 15, 30), session_time(day, 15, 40), "NORMAL",
                                             "ETF" if symbol == "IDX" else "STOCK", "KOSPI", False, False, False, False,
                                             close, close, "TEST"))
        day += timedelta(days=1)
    return Snapshot(bars, require_complete=False), SecurityHistory(records), sessions


def screen(snapshot, securities, day, policy=POLICY, lookback=60):
    at = session_time(day, 16)
    return select_candidates(RankedStrategy(lookback, 3, D(".3"), ranking="residual_momentum"),
                             snapshot.view(at, day, allow_missing=True), securities, policy, Costs(), at + timedelta(hours=1))


class ResidualRankingTests(unittest.TestCase):
    def test_ranks_market_adjusted_return_per_unit_of_residual_risk(self):
        snapshot, securities, sessions = market()
        report = screen(snapshot, securities, sessions[-1])
        rows = {r["symbol"]: r for r in report["candidates"]}
        self.assertEqual(report["selected"], ["ALPHA", "BETA2", "LOSER"])
        self.assertGreater(rows["ALPHA"]["features"]["score"], 0)
        self.assertLess(rows["LOSER"]["features"]["score"], 0)
        self.assertAlmostEqual(float(rows["BETA2"]["features"]["residual_beta"]), 2.0, places=1)
        # Independent recomputation for ALPHA over the 60-session lookback.
        closes = {s: [] for s in RETURNS}
        for bar in snapshot.bars:
            closes[bar.symbol].append(float(bar.close))
        r = [b / a - 1 for a, b in zip(closes["ALPHA"][-61:], closes["ALPHA"][-60:])]
        m = [b / a - 1 for a, b in zip(closes["IDX"][-61:], closes["IDX"][-60:])]
        beta = statistics.covariance(r, m) / statistics.variance(m)
        e = [x - beta * y for x, y in zip(r, m)]
        self.assertAlmostEqual(float(rows["ALPHA"]["features"]["score"]), sum(e) / statistics.pstdev(e), places=6)
        # The market ETF itself has no residual.
        self.assertEqual(rows["IDX"]["reasons"], ["RESIDUAL_VOLATILITY_ZERO"])

    def test_missing_market_bar_and_missing_market_symbol(self):
        snapshot, securities, sessions = market(skip=("IDX", 85))
        rows = {r["symbol"]: r for r in screen(snapshot, securities, sessions[-1])["candidates"]}
        self.assertIn("RESIDUAL_MARKET_UNAVAILABLE", rows["ALPHA"]["reasons"])
        with self.assertRaisesRegex(AgentError, "residual_momentum needs a preregistered market_symbol"):
            screen(snapshot, securities, sessions[-1], SelectionPolicy())


if __name__ == "__main__":
    unittest.main()
