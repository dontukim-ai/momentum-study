"""Opt-in signal rankings (user decision 2026-10-25, strategy-alternatives §5.4)."""

import unittest
from datetime import date, timedelta

from trading_agent.contracts import AgentError, Bar, Costs, D, session_time
from trading_agent.data import Snapshot
from trading_agent.logic import LogicSettings
from trading_agent.selection import (
    DEFAULT_RANKINGS,
    MAX_FILTER_DAILY_RETURN,
    RANKINGS,
    SIGNAL_RANKINGS,
    RankedStrategy,
    SecurityHistory,
    SecurityState,
    SelectionPolicy,
    select_candidates,
)

SESSIONS = 80


def rate(symbol, i):
    """Daily close multiplier; the last 10 sessions (70..79) decide the lookback."""
    if symbol == "WINNER":
        return D("1.03") if i % 2 else D(".99")
    if symbol == "LOSER":
        return (D(".97") if i % 2 else D(".995")) if i >= 70 else D(1)
    if symbol == "SMOOTH":
        return D("1.001")
    return D("1.29") if i == 75 else D(1)  # JUMPY: one near-limit-up day


def market():
    bars, records, day = [], [], date(2024, 1, 2)
    prices = dict.fromkeys(("JUMPY", "LOSER", "SMOOTH", "WINNER"), D(10000))
    sessions = []
    while len(sessions) < SESSIONS:
        if day.weekday() < 5:
            sessions.append(day)
            i = len(sessions) - 1
            for symbol in prices:
                opening = prices[symbol]
                closing = opening * rate(symbol, i) if i else opening
                prices[symbol] = closing
                bars.append(Bar(day, symbol, opening, max(opening, closing), min(opening, closing), closing,
                                1000000, session_time(day, 15, 40)))
                records.append(SecurityState(symbol, session_time(day, 15, 30), session_time(day, 15, 40), "NORMAL",
                                             "STOCK", "KOSPI", False, False, False, False, closing, closing, "TEST"))
        day += timedelta(days=1)
    return Snapshot(bars), SecurityHistory(records), sessions


class SignalRankingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.snapshot, cls.securities, cls.sessions = market()

    def screen(self, ranking, top_n=1):
        day = self.sessions[-1]
        at = session_time(day, 16)
        return select_candidates(RankedStrategy(10, top_n, D(".4"), ranking=ranking),
                                 self.snapshot.view(at, day, allow_missing=True), self.securities,
                                 SelectionPolicy(), Costs(), at + timedelta(hours=1))

    def rows(self, report):
        return {r["symbol"]: r for r in report["candidates"]}

    def test_plain_momentum_takes_the_jump_and_the_max_filter_removes_it(self):
        self.assertEqual(self.screen("momentum")["selected"], ["JUMPY"])
        report = self.screen("momentum_ex_max")
        self.assertEqual(report["selected"], ["WINNER"])
        jumpy = self.rows(report)["JUMPY"]
        self.assertIn("RECENT_MAX_RETURN_ABOVE_LIMIT", jumpy["reasons"])
        self.assertGreater(jumpy["features"]["max_daily_return"], MAX_FILTER_DAILY_RETURN)
        # The losing stock still fails the ordinary momentum threshold.
        self.assertIn("MOMENTUM_BELOW_THRESHOLD", self.rows(report)["LOSER"]["reasons"])

    def test_short_reversal_buys_only_losers_ranked_by_loss(self):
        report = self.screen("short_reversal", top_n=2)
        self.assertEqual(report["selected"], ["LOSER"])
        rows = self.rows(report)
        self.assertEqual(rows["LOSER"]["features"]["score"], -rows["LOSER"]["features"]["momentum"])
        for symbol in ("JUMPY", "SMOOTH", "WINNER"):
            self.assertEqual(rows[symbol]["reasons"], ["REVERSAL_BELOW_THRESHOLD"])

    def test_low_volatility_ignores_the_momentum_sign_and_ranks_by_lookback_volatility(self):
        report = self.screen("low_volatility", top_n=4)
        self.assertEqual(report["selected"][0], "SMOOTH")
        rows = self.rows(report)
        # A falling stock stays eligible: this is a risk sort, not a trend filter.
        self.assertEqual(rows["LOSER"]["status"], "SELECTED")
        vols = {s: rows[s]["features"]["lookback_volatility"] for s in rows}
        order = sorted(vols, key=lambda s: (vols[s], s))
        self.assertEqual(report["selected"], order)

    def test_default_search_space_and_existing_features_are_unchanged(self):
        self.assertEqual(DEFAULT_RANKINGS, ("momentum", "risk_adjusted", "trend_stability"))
        self.assertEqual(LogicSettings().rankings, DEFAULT_RANKINGS)
        self.assertEqual(set(SIGNAL_RANKINGS), {"short_reversal", "low_volatility", "momentum_ex_max"})
        self.assertLessEqual({*DEFAULT_RANKINGS, *SIGNAL_RANKINGS}, set(RANKINGS))
        for ranking in DEFAULT_RANKINGS:
            features = self.rows(self.screen(ranking))["WINNER"]["features"]
            self.assertNotIn("max_daily_return", features)
            self.assertNotIn("lookback_volatility", features)

    def test_signal_rankings_must_be_preregistered_and_respect_the_challenger_budget(self):
        LogicSettings(champion=RankedStrategy(20), lookbacks=(20, 40, 60),
                      rankings=("momentum", *SIGNAL_RANKINGS), candidate_budget=11)
        with self.assertRaisesRegex(AgentError, "challenger budget"):
            LogicSettings(champion=RankedStrategy(20), lookbacks=(20, 40, 60),
                          rankings=(*DEFAULT_RANKINGS, *SIGNAL_RANKINGS), candidate_budget=12)
        with self.assertRaisesRegex(AgentError, "allowlisted"):
            RankedStrategy(20, ranking="macd")


if __name__ == "__main__":
    unittest.main()
