import unittest
from dataclasses import replace
from datetime import date, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory

from trading_agent.contracts import (
    AgentError,
    Bar,
    Costs,
    D,
    StrategySpec,
    Wallet,
    session_time,
)
from trading_agent.data import Snapshot, read_csv, synthetic_snapshot, write_csv
from trading_agent.evaluation import backtest
from trading_agent.strategy import decide


class MarketTests(unittest.TestCase):
    def test_unpublished_final_bar_is_not_visible(self):
        snapshot = synthetic_snapshot(60)
        day = snapshot.sessions[20]
        with self.assertRaisesRegex(AgentError, "unavailable"):
            snapshot.view(session_time(day, 15, 35), day)
        delayed = [replace(b, available_at=session_time(day, 16, 5)) if b.session == day else b for b in snapshot.bars]
        with self.assertRaises(AgentError):
            Snapshot(delayed).view(session_time(day, 16), day)

    def test_future_prices_cannot_change_past_decision(self):
        original = synthetic_snapshot(60)
        day = original.sessions[30]
        modified = Snapshot([replace(b, open=b.open * 100, high=b.high * 100,
                                     low=b.low * 100, close=b.close * 100)
                             if b.session > day else b for b in original.bars])
        expiry = session_time(original.sessions[31], 10)
        a = decide(StrategySpec(), original.view(session_time(day, 16), day), expiry)
        b = decide(StrategySpec(), modified.view(session_time(day, 16), day), expiry)
        self.assertEqual(a.target_weights, b.target_weights)
        self.assertNotEqual(a.snapshot_id, b.snapshot_id)

    def test_duplicates_and_sparse_universe_fail_closed(self):
        snapshot = synthetic_snapshot(60)
        with self.assertRaisesRegex(AgentError, "Duplicate"):
            Snapshot(list(snapshot.bars) + [snapshot.bars[0]])
        with self.assertRaisesRegex(AgentError, "Incomplete"):
            Snapshot(list(snapshot.bars)[1:])

    def test_roundtrip_preserves_snapshot_id(self):
        snapshot = synthetic_snapshot(60)
        with TemporaryDirectory() as directory:
            path = Path(directory) / "prices.csv"
            write_csv(snapshot, path)
            restored = read_csv(path)
            self.assertEqual(snapshot.id, restored.id)

    def test_signal_fills_at_next_open_not_observed_close(self):
        bars = []
        for i, (opening, closing) in enumerate(((10, 10), (10, 12), (12, 20), (100, 100), (100, 100))):
            day = date(2024, 1, 8) + timedelta(days=i)
            bars.append(Bar(day, "A", D(opening), D(max(opening, closing)), D(min(opening, closing)),
                            D(closing), 100000, session_time(day, 15, 40)))
        result = backtest(Snapshot(bars), StrategySpec(2, 1, D(".4")), Costs(D(0), D(0), D(0)), D(1000))
        self.assertEqual(result.trade_log[0]["price"], "100.00")
        self.assertEqual(result.trade_log[0]["signal_session"], "2024-01-10")
        self.assertEqual(result.trade_log[0]["session"], "2024-01-11")
        self.assertEqual(result.net_return, 0)

    def test_no_borrowing_or_shorting_in_fill_ledger(self):
        wallet = Wallet(D(100))
        with self.assertRaises(AgentError):
            wallet.fill("A", "BUY", 11, D(10), D(0))
        with self.assertRaises(AgentError):
            wallet.fill("A", "SELL", 1, D(10), D(0))
        self.assertEqual(wallet.cash, D(100))

    def test_nonfinite_inputs_rejected(self):
        for value in ("NaN", "Infinity", "-Infinity"):
            with self.assertRaises(AgentError):
                Costs(commission_bps=value)
        with self.assertRaises(AgentError):
            StrategySpec(gross_weight="NaN")

    def test_empty_momentum_slots_stay_in_cash(self):
        snapshot = synthetic_snapshot(60)
        day = snapshot.sessions[30]
        decision = decide(StrategySpec(min_momentum=D(100)),
                          snapshot.view(session_time(day, 16), day), session_time(snapshot.sessions[31], 10))
        self.assertEqual(decision.target_weights, {})


if __name__ == "__main__":
    unittest.main()
