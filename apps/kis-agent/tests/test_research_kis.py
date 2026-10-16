import unittest
from dataclasses import replace
from datetime import date, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

from trading_agent.config import load_settings
from trading_agent.contracts import (
    AgentError,
    Bar,
    Costs,
    D,
    StrategySpec,
    session_time,
)
from trading_agent.data import Snapshot
from trading_agent.evaluation import backtest
from trading_agent.execution import OMS
from trading_agent.kis import DAILY_PATH, KisReadOnly
from trading_agent.research import BoundedLookbacks, research
from trading_agent.runtime import paper_replay
from trading_agent.store import Store


def cyclic_market(count=160):
    bars = []
    day = date(2024, 1, 2)
    closing = D(100)
    while len(bars) < count:
        if day.weekday() < 5:
            opening = closing
            closing = (opening * (D("1.02") if len(bars) % 20 < 10 else D(".98"))).quantize(D(".01"))
            bars.append(Bar(day, "A", opening, max(opening, closing), min(opening, closing),
                            closing, 100000, session_time(day, 15, 40), "SYNTHETIC_CYCLIC"))
        day += timedelta(days=1)
    return Snapshot(bars)


class ResearchTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:")
        self.snapshot = cyclic_market()
        self.champion = StrategySpec(20, 1, D(".4"))
        self.costs = Costs(D(0), D(0), D(0))

    def tearDown(self):
        self.store.close()

    def test_holdout_consumed_once_and_identical_run_returns_saved_report(self):
        report = research(self.snapshot, self.champion, self.costs, self.store, BoundedLookbacks((2,)))
        self.assertTrue(report["holdout"]["consumed"])
        self.assertNotEqual(report["selected_version"], self.champion.version)
        self.assertFalse(report["live_promotion"])
        count = self.store.db.execute("SELECT count(*) FROM holdouts").fetchone()[0]
        again = research(self.snapshot, self.champion, self.costs, self.store, BoundedLookbacks((2,)))
        self.assertTrue(again["cached_report"])
        self.assertEqual(report["run_id"], again["run_id"])
        self.assertEqual(count, self.store.db.execute("SELECT count(*) FROM holdouts").fetchone()[0])
        with self.assertRaisesRegex(AgentError, "consumed"):
            research(self.snapshot, self.champion, self.costs, self.store, BoundedLookbacks((2, 3)))

    def test_editing_csv_cannot_reset_consumed_holdout(self):
        research(self.snapshot, self.champion, self.costs, self.store, BoundedLookbacks((2,)))
        last = self.snapshot.bars[-1]
        changed = Snapshot(list(self.snapshot.bars[:-1]) + [replace(last, high=last.high + 1)])
        with self.assertRaisesRegex(AgentError, "consumed"):
            research(changed, self.champion, self.costs, self.store, BoundedLookbacks((2,)))

    def test_keeping_champion_does_not_consume_holdout(self):
        result = research(self.snapshot, self.champion, self.costs, self.store, BoundedLookbacks((20,)))
        self.assertEqual(result["verdict"], "KEEP_CHAMPION")
        self.assertFalse(result["holdout"]["consumed"])
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM holdouts").fetchone()[0], 0)

    def test_proposal_cannot_change_risk_allocation(self):
        class BadProposer:
            def propose(inner, champion):
                return [replace(champion, gross_weight=D(".9"))]
        with self.assertRaisesRegex(AgentError, "boundary"):
            research(self.snapshot, self.champion, self.costs, self.store, BadProposer())
        with self.assertRaises(AgentError):
            BoundedLookbacks((2, 3), budget=1).propose(self.champion)
        with self.assertRaises(AgentError):
            BoundedLookbacks((61,)).propose(self.champion)

    def test_replay_rerun_is_idempotent_and_rebind_rejected(self):
        first = paper_replay(self.snapshot, self.champion, self.costs, self.store, initial_cash=D(10000))
        second = paper_replay(self.snapshot, self.champion, self.costs, self.store, initial_cash=D(10000))
        self.assertEqual(first["wallet"], second["wallet"])
        self.assertEqual(first["fills"], second["fills"])
        self.assertTrue(second["reconciliation"]["matches"])
        baseline = backtest(self.snapshot, self.champion, self.costs, D(10000))
        self.assertEqual(first["final_equity"], baseline.final_equity)
        with self.assertRaisesRegex(AgentError, "bound"):
            paper_replay(self.snapshot, replace(self.champion, lookback=2), self.costs,
                         self.store, initial_cash=D(10000))

    def test_live_configuration_is_rejected(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "live.toml"
            path.write_text('[runtime]\nlive_enabled = true\n')
            with self.assertRaisesRegex(AgentError, "live orders"):
                load_settings(path)

    def test_costed_screening_and_durable_replay_agree(self):
        costs = Costs()
        spec = replace(self.champion, lookback=2)
        result = backtest(self.snapshot, spec, costs, D(10000))
        replay = paper_replay(self.snapshot, spec, costs, self.store, initial_cash=D(10000))
        self.assertEqual(replay["final_equity"], result.final_equity)
        self.assertEqual(replay["fills"], result.trades)
        self.assertEqual(replay["rejections"], [])

    def test_mid_rebalance_restart_preserves_plan_after_filled_order(self):
        snapshot = cyclic_market(143)
        spec = replace(self.champion, lookback=2)
        original = OMS.reconcile
        calls = []
        def crash_after_fill(oms, account):
            result = original(oms, account)
            calls.append(account)
            if len(calls) == 2:
                raise KeyboardInterrupt("death after ledger update, before completion marker")
            return result
        with patch.object(OMS, "reconcile", crash_after_fill):
            with self.assertRaises(KeyboardInterrupt):
                paper_replay(snapshot, spec, Costs(), self.store, "interrupted", D(10000), True)
        resumed = paper_replay(snapshot, spec, Costs(), self.store, "interrupted", D(10000), True)
        clean = paper_replay(snapshot, spec, Costs(), self.store, "clean", D(10000), True)
        self.assertEqual(resumed["wallet"], clean["wallet"])
        self.assertEqual(resumed["fills"], clean["fills"])
        self.assertTrue(resumed["reconciliation"]["matches"])


class KisTests(unittest.TestCase):
    def setUp(self):
        self.calls = []
        self.env = patch.dict("os.environ", {"KIS_PAPER_APP_KEY": "test-key", "KIS_PAPER_APP_SECRET": "test-secret"}, clear=True)
        self.env.start()

    def tearDown(self):
        self.env.stop()

    def transport(self, method, url, headers, body):
        self.calls.append((method, url, headers, body))
        if method == "POST":
            return {"access_token": "test-access-token", "expires_in": 86400}
        cursor = parse_qs(urlparse(url).query)["FID_INPUT_DATE_2"][0]
        days = ["20240105", "20240104"] if cursor >= "20240105" else ["20240103", "20240102"]
        return {"rt_cd": "0", "output2": [
            {"stck_bsop_date": day, "stck_oprc": "100", "stck_hgpr": "110", "stck_lwpr": "90",
             "stck_clpr": "105", "acml_vol": "1000"} for day in days]}

    @patch("trading_agent.kis.time.sleep")
    def test_readonly_pagination_token_reuse_and_raw_price_flag(self, _sleep):
        client = KisReadOnly(transport=self.transport, minimum_interval=.1)
        snapshot = client.collect(["123456"], date(2024, 1, 2), date(2024, 1, 5), True)
        self.assertEqual(len(snapshot.bars), 4)
        self.assertEqual(sum(c[0] == "POST" for c in self.calls), 1)
        for method, url, headers, _ in self.calls:
            self.assertEqual(urlparse(url).netloc, "openapivts.koreainvestment.com:29443")
            if method == "GET":
                self.assertEqual(headers["tr_id"], "FHKST03010100")
                self.assertEqual(urlparse(url).path, DAILY_PATH)
                self.assertEqual(parse_qs(urlparse(url).query)["FID_ORG_ADJ_PRC"], ["1"])
        self.assertTrue(all("ASSUMED" in b.source for b in snapshot.bars))

    @patch("trading_agent.kis.time.sleep")
    def test_collection_timestamp_is_not_backdated_by_default(self, _sleep):
        snapshot = KisReadOnly(transport=self.transport).collect(["123456"], date(2024, 1, 2), date(2024, 1, 5))
        self.assertTrue(all("OBSERVED_AT_COLLECTION" in b.source for b in snapshot.bars))
        with self.assertRaisesRegex(AgentError, "unavailable"):
            snapshot.view(session_time(date(2024, 1, 5), 16), date(2024, 1, 5))

    def test_order_endpoint_is_not_callable_even_with_credentials(self):
        client = KisReadOnly(transport=self.transport)
        with self.assertRaisesRegex(AgentError, "Only the daily"):
            client._request("/uapi/domestic-stock/v1/trading/order-cash")
        self.assertEqual(self.calls, [])

    def test_missing_credentials_fail_without_network(self):
        with patch.dict("os.environ", {}, clear=True):
            with self.assertRaisesRegex(AgentError, "KIS_PAPER_APP_KEY"):
                KisReadOnly(transport=self.transport)
        self.assertEqual(self.calls, [])


if __name__ == "__main__":
    unittest.main()
