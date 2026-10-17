"""Repository integration regressions: pre-open sizing and frozen evaluation inputs."""

import json
import unittest
from datetime import date, timedelta
from unittest.mock import patch

from trading_agent.contracts import Bar, Costs, D, StrategySpec, digest, session_time
from trading_agent.data import Snapshot
from trading_agent.evaluation import backtest
from trading_agent.research import BoundedLookbacks, research
from trading_agent.runtime import paper_replay
from trading_agent.store import Store


def gap_market(open_price):
    bars = []
    for i, price in enumerate((10, 11, 12, open_price, open_price)):
        day = date(2024, 1, 8) + timedelta(days=i)
        bars.append(Bar(day, "A", D(price), D(price), D(price), D(price), 1000000, session_time(day, 15, 40)))
    return Snapshot(bars)


class RepositoryInvariantTests(unittest.TestCase):
    def test_opening_gap_down_cannot_increase_the_quantity_planned_from_prior_close(self):
        spec, costs = StrategySpec(2, 1, D(".4")), Costs(D(0), D(0), D(0))
        ordinary = backtest(gap_market(12), spec, costs, D(1200))
        down_gap = backtest(gap_market(6), spec, costs, D(1200))
        self.assertEqual(ordinary.trade_log[0]["quantity"], 40)
        self.assertEqual(down_gap.trade_log[0]["quantity"], 40)
        self.assertEqual(down_gap.trade_log[0]["price"], "6.00")

    def test_replay_uses_the_same_prior_close_quantity_plan(self):
        snapshot, spec, costs = gap_market(6), StrategySpec(2, 1, D(".4")), Costs(D(0), D(0), D(0))
        store = Store(":memory:")
        try:
            paper_replay(snapshot, spec, costs, store, initial_cash=D(1200))
            first = store.db.execute("SELECT quantity FROM orders ORDER BY rowid LIMIT 1").fetchone()
            self.assertEqual(first[0], 40)
            result = backtest(snapshot, spec, costs, D(1200))
            self.assertEqual(str(store.wallet("demo").equity({"A": D(6)})), result.final_equity)
        finally:
            store.close()

    def test_manifest_is_frozen_before_the_holdout_is_opened(self):
        from fixtures.logic_market import cyclic_logic_market
        snapshot, _ = cyclic_logic_market()
        store = Store(":memory:")
        observed = []
        def inspect_manifest(*args):
            manifests = store.db.execute("SELECT run_id,manifest_json FROM evaluation_manifests").fetchall()
            self.assertEqual(len(manifests), 1)
            manifest = json.loads(manifests[0]["manifest_json"])
            self.assertEqual(digest(manifest), manifests[0]["run_id"])
            self.assertEqual(manifest["snapshot_id"], snapshot.id)
            self.assertTrue(store.db.execute("SELECT 1 FROM holdouts WHERE run_id=?", (manifests[0]["run_id"],)).fetchone())
            observed.append(True)
            raise KeyboardInterrupt("stop after checking the already-frozen contract")
        try:
            with patch("trading_agent.research.paired_block_interval", side_effect=inspect_manifest):
                with self.assertRaises(KeyboardInterrupt):
                    research(snapshot, StrategySpec(20, 1, D(".4")), Costs(), store, BoundedLookbacks((2,)), initial_cash=D(1000000))
            self.assertEqual(observed, [True])
            self.assertEqual(store.db.execute("SELECT count(*) FROM experiments").fetchone()[0], 0)
        finally:
            store.close()
