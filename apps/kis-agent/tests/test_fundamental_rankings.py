"""Annual-report rankings and the market-cap universe (explore v7, user decision 2026-10-26)."""

import tempfile
import unittest
from dataclasses import replace
from datetime import date, timedelta
from pathlib import Path

from trading_agent.contracts import AgentError, Bar, Costs, D, Wallet, session_time
from trading_agent.data import Snapshot
from trading_agent.fundamentals import (
    FinancialRecord,
    FundamentalHistory,
    ShareCount,
    months_after,
    read_fundamentals,
)
from trading_agent.logic import LogicProposer, LogicSettings, candidate_grid
from trading_agent.selection import (
    RankedStrategy,
    SecurityHistory,
    SecurityState,
    SelectionEngine,
    SelectionPolicy,
    select_candidates,
)

SESSIONS = 80
SHARES = {"A": 1000000, "B": 2000000, "C": 3000000, "D": 500000, "E": 1500000, "F": 800000, "G": 900000}
FY2023 = date(2023, 12, 31)
FILED_2024 = session_time(date(2024, 3, 20), 15, 30)


def financial(symbol, equity, assets, operating_income, period_end=FY2023, available_at=FILED_2024, statement="CFS"):
    return FinancialRecord(symbol, period_end, available_at, statement, equity, assets, operating_income, "TEST")


def market():
    bars, records, day, sessions = [], [], date(2024, 1, 2), []
    prices = dict.fromkeys((*SHARES, "H", "ETF1"), D(10000))
    while len(sessions) < SESSIONS:
        if day.weekday() < 5:
            sessions.append(day)
            for symbol in prices:
                opening = prices[symbol]
                closing = prices[symbol] = opening * D("1.001") if len(sessions) > 1 else opening
                bars.append(Bar(day, symbol, opening, closing, opening, closing, 1000000, session_time(day, 15, 40)))
                records.append(SecurityState(symbol, session_time(day, 15, 30), session_time(day, 15, 40), "NORMAL",
                                             "ETF" if symbol == "ETF1" else "STOCK", "KOSPI", False, False, False, False,
                                             closing, closing, "TEST"))
        day += timedelta(days=1)
    shares = [ShareCount(s, sessions[0], sessions[-1], session_time(sessions[0], 15, 40), n, "TEST")
              for s, n in SHARES.items()]
    financials = [
        financial("A", 5400000000, 10000000000, 1000000000),
        financial("B", 2100000000, 10000000000, 3000000000),
        financial("C", 32000000000, 10000000000, None),          # no operating income: never filled in
        financial("D", -1, 10000000000, 200000000),               # negative book equity
        financial("E", 13000000000, 10000000000, 2000000000),
        financial("F", 9000000000, 10000000000, 500000000,        # published after the decision
                  available_at=session_time(date(2024, 12, 2), 9)),
        financial("G", 9000000000, 10000000000, 500000000,        # FY2022, older than 15 months
                  period_end=date(2022, 12, 31), available_at=session_time(date(2023, 3, 20), 15)),
    ]
    return Snapshot(bars), SecurityHistory(records), FundamentalHistory(financials, shares), sessions


class FundamentalRankingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.snapshot, cls.securities, cls.fundamentals, cls.sessions = market()

    def screen(self, ranking, top_n=3, policy=None, fundamentals="default"):
        day = self.sessions[-1]
        at = session_time(day, 16)
        return select_candidates(RankedStrategy(20, top_n, D(".3"), ranking=ranking),
                                 self.snapshot.view(at, day, allow_missing=True), self.securities,
                                 policy or SelectionPolicy(), Costs(), at + timedelta(hours=1),
                                 fundamentals=self.fundamentals if fundamentals == "default" else fundamentals)

    def rows(self, report):
        return {r["symbol"]: r for r in report["candidates"]}

    def test_value_ranks_book_to_market_from_close_times_listed_shares(self):
        report = self.screen("value")
        rows = self.rows(report)
        close = rows["A"]["features"]["price"]
        self.assertEqual(rows["A"]["features"]["market_cap"], close * SHARES["A"])
        self.assertEqual(rows["A"]["features"]["book_to_market"], D(5400000000) / (close * SHARES["A"]))
        bm = {s: rows[s]["features"]["book_to_market"] for s in ("A", "B", "C", "E")}
        self.assertEqual(report["selected"], sorted(bm, key=lambda s: -bm[s])[:3])
        self.assertEqual(rows["D"]["reasons"], ["BOOK_EQUITY_NONPOSITIVE"])
        self.assertEqual(rows["H"]["reasons"], ["FINANCIALS_UNAVAILABLE"])
        # Fundamental rankings do not apply the momentum threshold.
        self.assertFalse(any("MOMENTUM_BELOW_THRESHOLD" in r["reasons"] for r in rows.values()))

    def test_unpublished_and_stale_reports_are_not_used(self):
        rows = self.rows(self.screen("value"))
        self.assertEqual(rows["F"]["reasons"], ["FINANCIALS_UNAVAILABLE"])
        self.assertEqual(rows["G"]["reasons"], ["FINANCIALS_STALE"])
        self.assertEqual(months_after(date(2022, 12, 31), 15), date(2024, 3, 31))

    def test_profitability_rejects_missing_operating_income_instead_of_filling_it(self):
        report = self.screen("profitability")
        rows = self.rows(report)
        self.assertEqual(rows["C"]["reasons"], ["FINANCIALS_UNAVAILABLE"])
        self.assertEqual(rows["B"]["features"]["operating_profitability"], D("0.3"))
        self.assertEqual(report["selected"], ["B", "E", "A"])

    def test_value_profit_averages_mid_rank_percentiles_of_both_eligible_ratios(self):
        report = self.screen("value_profit")
        rows = self.rows(report)
        # Eligible for both ratios: A, B, E (C lacks operating income, D has negative equity).
        # B/M order B < A < E; profitability order A < E < B.
        expected = {"A": (D(2) / 3 + D(1) / 3) / 2, "B": (D(1) / 3 + D(3) / 3) / 2, "E": (D(3) / 3 + D(2) / 3) / 2}
        for symbol, score in expected.items():
            self.assertAlmostEqual(float(rows[symbol]["features"]["score"]), float(score), places=12)
        self.assertEqual(report["selected"], ["E", "B", "A"])
        self.assertEqual(rows["C"]["reasons"], ["FINANCIALS_UNAVAILABLE"])

    def test_market_cap_universe_is_the_same_for_every_ranking_and_skips_etfs(self):
        policy = SelectionPolicy(max_market_cap_rank=3)
        outside = {}
        for ranking in ("momentum", "value"):
            rows = self.rows(self.screen(ranking, policy=policy))
            outside[ranking] = {s for s, r in rows.items() if "OUTSIDE_MARKET_CAP_UNIVERSE" in r["reasons"]}
            self.assertEqual(rows["H"]["reasons"][0], "MARKET_CAP_UNAVAILABLE")
            self.assertEqual([rows[s]["features"]["market_cap_rank"] for s in ("C", "B", "E")], [1, 2, 3])
            self.assertNotIn("OUTSIDE_MARKET_CAP_UNIVERSE", rows["ETF1"]["reasons"])
        self.assertEqual(outside["momentum"], {"A", "D", "F", "G"})
        self.assertEqual(outside["momentum"], outside["value"])
        self.assertEqual(self.screen("momentum", policy=policy)["selected"], ["B", "C", "E"])

    def test_fundamental_inputs_are_required_and_bound_to_the_audit_id(self):
        with self.assertRaisesRegex(AgentError, "point-in-time fundamentals"):
            self.screen("value", fundamentals=None)
        with self.assertRaisesRegex(AgentError, "point-in-time fundamentals"):
            self.screen("momentum", policy=SelectionPolicy(max_market_cap_rank=3), fundamentals=None)
        with self.assertRaisesRegex(AgentError, "1..10000"):
            SelectionPolicy(max_market_cap_rank=0)
        plain = self.screen("momentum", fundamentals=None)
        self.assertNotEqual(plain["visible_input_id"], self.screen("momentum")["visible_input_id"])
        self.assertEqual(plain["selected"], self.screen("momentum")["selected"])

    def test_engine_passes_fundamentals_to_selection(self):
        day = self.sessions[-1]
        at = session_time(day, 16)
        engine = SelectionEngine(self.securities, SelectionPolicy(), Costs(), self.fundamentals)
        decision = engine(RankedStrategy(20, 3, D(".3"), ranking="value"),
                          self.snapshot.view(at, day, allow_missing=True), at + timedelta(hours=1), Wallet(D(1000000)))
        self.assertEqual(set(decision.target_weights), set(self.screen("value")["selected"]))


class FundamentalHistoryTests(unittest.TestCase):
    def test_correction_replaces_a_period_only_from_its_own_publication(self):
        original = financial("A", 100, 1000, 10)
        corrected = financial("A", 90, 1000, 10, available_at=session_time(date(2024, 5, 10), 9))
        next_year = financial("A", 120, 1100, 12, period_end=date(2024, 12, 31),
                              available_at=session_time(date(2025, 3, 21), 15))
        history = FundamentalHistory([next_year, corrected, original], [])
        self.assertIsNone(history.financials("A", session_time(date(2024, 3, 20), 15)))
        self.assertEqual(history.financials("A", session_time(date(2024, 4, 1), 16)).equity, 100)
        self.assertEqual(history.financials("A", session_time(date(2024, 5, 10), 16)).equity, 90)
        self.assertEqual(history.financials("A", session_time(date(2025, 3, 21), 16)).equity, 120)

    def test_invalid_records_are_rejected(self):
        with self.assertRaisesRegex(AgentError, "before its period ends"):
            financial("A", 1, 1, 1, available_at=session_time(date(2023, 12, 31), 16))
        with self.assertRaisesRegex(AgentError, "statement kind"):
            financial("A", 1, 1, 1, statement="XFS")
        with self.assertRaisesRegex(AgentError, "positive integer"):
            ShareCount("A", date(2024, 1, 2), date(2024, 1, 2), session_time(date(2024, 1, 2), 16), 0, "TEST")
        with self.assertRaisesRegex(AgentError, "ordered session run"):
            ShareCount("A", date(2024, 1, 3), date(2024, 1, 2), session_time(date(2024, 1, 3), 16), 5, "TEST")
        share = ShareCount("A", date(2024, 1, 2), date(2024, 1, 5), session_time(date(2024, 1, 2), 16), 5, "TEST")
        with self.assertRaisesRegex(AgentError, "Overlapping share count runs"):
            FundamentalHistory([], [share, ShareCount("A", date(2024, 1, 5), date(2024, 1, 9),
                                                      session_time(date(2024, 1, 5), 16), 6, "TEST")])
        self.assertIsNone(FundamentalHistory([], [share]).listed_shares("A", date(2024, 1, 2),
                                                                        session_time(date(2024, 1, 2), 15)))

    def test_share_runs_answer_only_inside_a_run_and_after_publication(self):
        runs = [ShareCount("A", date(2024, 1, 2), date(2024, 1, 3), session_time(date(2024, 1, 2), 16), 5, "TEST"),
                # 2024-01-04 was quoted without a count, so the builder split the run there.
                ShareCount("A", date(2024, 1, 5), date(2024, 1, 9), session_time(date(2024, 1, 5), 16), 10, "TEST")]
        history = FundamentalHistory([], runs)
        late = session_time(date(2024, 2, 1), 16)
        self.assertEqual(history.listed_shares("A", date(2024, 1, 3), late), 5)
        self.assertIsNone(history.listed_shares("A", date(2024, 1, 4), late))
        self.assertEqual(history.listed_shares("A", date(2024, 1, 8), late), 10)
        self.assertIsNone(history.listed_shares("A", date(2024, 1, 10), late))
        self.assertIsNone(history.listed_shares("A", date(2024, 1, 1), late))
        self.assertIsNone(history.listed_shares("A", date(2024, 1, 5), session_time(date(2024, 1, 5), 15)))
        self.assertIsNone(history.listed_shares("B", date(2024, 1, 5), late))
        prefix = history.until(date(2024, 1, 8), late)
        self.assertEqual([r.through for r in prefix.share_records], [date(2024, 1, 3), date(2024, 1, 8)])
        self.assertIsNone(prefix.listed_shares("A", date(2024, 1, 9), late))
        extended = FundamentalHistory([], [runs[0], replace(runs[1], through=date(2024, 1, 31))])
        self.assertEqual(extended.until(date(2024, 1, 8), late).id, prefix.id)
        self.assertEqual(len(history.until(date(2024, 1, 3), session_time(date(2024, 1, 3), 16)).share_records), 1)

    def test_files_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "financials.jsonl").write_text(
                '{"symbol":"A","period_end":"2023-12-31","available_at":"2024-03-20T06:30:00+00:00",'
                '"statement":"OFS","equity":"100","assets":"1000","operating_income":null,"source":"TEST"}\n',
                encoding="utf-8")
            (root / "shares.csv").write_text(
                "symbol,session,through,available_at,listed_shares,source\n"
                "A,2024-03-21,2024-03-29,2024-03-21T06:40:00+00:00,500,TEST\n",
                encoding="utf-8")
            history = read_fundamentals(root / "financials.jsonl", root / "shares.csv")
            record = history.financials("A", session_time(date(2024, 3, 21), 16))
            self.assertEqual((record.statement, record.equity, record.operating_income), ("OFS", 100, None))
            self.assertEqual(history.listed_shares("A", date(2024, 3, 28), session_time(date(2024, 3, 28), 16)), 500)
            (root / "shares.csv").write_text("symbol,session\nA,2024-03-21\n", encoding="utf-8")
            with self.assertRaisesRegex(AgentError, "line 2"):
                read_fundamentals(root / "financials.jsonl", root / "shares.csv")


class FundamentalGridTests(unittest.TestCase):
    def test_fundamental_rankings_are_proposed_once_at_the_champion_lookback(self):
        champion = RankedStrategy(20)
        rankings = ("momentum", "value", "profitability", "value_profit")
        grid = candidate_grid(champion, (20, 40, 60), rankings)
        self.assertEqual([(s.ranking, s.lookback) for s in grid],
                         [("momentum", 40), ("momentum", 60), ("value", 20), ("profitability", 20), ("value_profit", 20)])
        settings = LogicSettings(champion=champion, lookbacks=(20, 40, 60), rankings=rankings, candidate_budget=5)
        self.assertEqual(LogicProposer(settings).propose(champion), grid)
        with self.assertRaisesRegex(AgentError, "challenger budget"):
            LogicSettings(champion=champion, lookbacks=(20, 40, 60), rankings=rankings, candidate_budget=4)


if __name__ == "__main__":
    unittest.main()
