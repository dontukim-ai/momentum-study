import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import date, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory

from trading_agent.contracts import (
    AgentError,
    Costs,
    D,
    OrderIntent,
    Quote,
    session_time,
)
from trading_agent.execution import OMS, RiskPolicy, RiskRejected, SimulatedBroker
from trading_agent.store import Store


class ExecutionTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.path = Path(self.temp.name) / "test.sqlite3"
        self.store = Store(self.path)
        self.store.create_account("test", D(1000))
        self.costs = Costs(D(0), D(0), D(0))
        self.policy = RiskPolicy(("A", "B"), D(1), D(1), D(1000))
        self.broker = SimulatedBroker(self.store, self.costs)
        self.oms = OMS(self.store, self.broker, self.policy, self.costs)
        self.now = session_time(date(2024, 1, 3), 9)
        self.quotes = {s: Quote(s, D(10), self.now) for s in ("A", "B")}

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def intent(self, key="order-1", symbol="A", quantity=50, side="BUY", price=D(10)):
        return OrderIntent(key, "test", symbol, side, quantity, price, self.now,
                           self.now + timedelta(hours=1), "strategy-v1", "snapshot-v1")

    def submit(self, intent=None, lose_response=False):
        intent = intent or self.intent()
        self.oms.reserve(intent, self.quotes, self.now)
        return self.oms.submit(intent.key, self.quotes, self.now, lose_response)

    def test_response_lost_after_acceptance_recovers_across_restart_without_resubmit(self):
        order = self.submit(lose_response=True)
        self.assertEqual(order["state"], "UNKNOWN")
        self.assertEqual(self.store.halt_reason("test"), "ambiguous_submission")
        self.oms.submit(order["key"], self.quotes, self.now)
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM remote_orders").fetchone()[0], 1)
        with self.assertRaises(RiskRejected):
            self.oms.reserve(self.intent("new"), self.quotes, self.now)
        self.store.close()
        self.store = Store(self.path)
        self.broker = SimulatedBroker(self.store, self.costs)
        self.oms = OMS(self.store, self.broker, self.policy, self.costs)
        self.assertEqual(self.oms.reconcile("test")["unresolved"], [])
        recovered = self.store.order(order["key"])
        self.assertEqual(recovered["state"], "ACKED")
        self.broker.fill(recovered["broker_id"], 50, D(10), "fill-1")
        self.oms.reconcile("test")
        self.oms.reconcile("test")
        self.assertEqual(self.store.wallet("test").cash, D(500))
        self.assertEqual(self.store.wallet("test").positions, {"A": 50})
        self.assertEqual(self.store.status("test")["fills"], 1)

    def test_no_matching_remote_order_does_not_prove_nonacceptance(self):
        def timeout(*args, **kwargs):
            raise TimeoutError("response unavailable")
        self.broker.submit = timeout
        self.submit()
        result = self.oms.reconcile("test")
        self.assertEqual(result["unresolved"], ["order-1"])
        self.assertTrue(result["halt_reason"])
        self.assertEqual(self.store.order("order-1")["state"], "UNKNOWN")

    def test_process_death_after_remote_acceptance_leaves_recoverable_submitting(self):
        original = self.broker.submit
        def crash(intent, now, **kwargs):
            original(intent, now)
            raise KeyboardInterrupt("injected process death")
        self.broker.submit = crash
        with self.assertRaises(KeyboardInterrupt):
            self.submit()
        self.assertEqual(self.store.order("order-1")["state"], "SUBMITTING")
        self.oms.reconcile("test")
        self.assertEqual(self.store.order("order-1")["state"], "ACKED")

    def test_partial_cancel_race_and_repeated_fill_leave_correct_quantity_cash(self):
        order = self.submit()
        broker_id = order["broker_id"]
        self.broker.fill(broker_id, 20, D(10), "one")
        self.oms.reconcile("test")
        self.assertEqual(self.store.order("order-1")["state"], "PARTIAL")
        self.oms.cancel("order-1")
        self.broker.fill(broker_id, 5, D(10), "two")
        self.oms.reconcile("test")
        self.assertEqual(self.store.order("order-1")["state"], "CANCEL_PENDING")
        self.assertEqual(self.store.order("order-1")["filled_qty"], 25)
        self.broker.confirm_cancel(broker_id)
        self.oms.reconcile("test")
        self.oms.reconcile("test")
        self.assertEqual(self.store.order("order-1")["state"], "CANCELLED")
        self.assertEqual(self.store.wallet("test").positions, {"A": 25})
        self.assertEqual(self.store.wallet("test").cash, D(750))
        self.oms.reserve(self.intent("after-cancel", "B", 70), self.quotes, self.now)

    def test_reserved_cash_is_shared_between_process_connections(self):
        self.oms.reserve(self.intent(quantity=90), self.quotes, self.now)
        second_store = Store(self.path)
        try:
            second = OMS(second_store, SimulatedBroker(second_store, self.costs), self.policy, self.costs)
            with self.assertRaisesRegex(RiskRejected, "unreserved cash"):
                second.reserve(self.intent("second", "B", 20), self.quotes, self.now)
        finally:
            second_store.close()

    def test_concurrent_reservations_cannot_spend_same_cash(self):
        barrier = threading.Barrier(2)
        def reserve(symbol):
            store = Store(self.path)
            try:
                oms = OMS(store, SimulatedBroker(store, self.costs), self.policy, self.costs)
                barrier.wait(timeout=5)
                try:
                    oms.reserve(self.intent(symbol, symbol, 70), self.quotes, self.now)
                    return "accepted"
                except RiskRejected:
                    return "rejected"
            finally:
                store.close()
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(reserve, ("A", "B")))
        self.assertCountEqual(results, ["accepted", "rejected"])

    def test_pending_sells_do_not_release_cash_or_allow_double_selling(self):
        order = self.submit()
        self.broker.fill(order["broker_id"], 50, D(10))
        self.oms.reconcile("test")
        self.oms.reserve(self.intent("sell-1", quantity=30, side="SELL"), self.quotes, self.now)
        with self.assertRaisesRegex(RiskRejected, "sellable"):
            self.oms.reserve(self.intent("sell-2", quantity=25, side="SELL"), self.quotes, self.now)
        with self.assertRaisesRegex(RiskRejected, "cash"):
            self.oms.reserve(self.intent("buy-2", "B", 60), self.quotes, self.now)

    def test_position_limit_counts_pending_buys(self):
        self.oms = OMS(self.store, self.broker, replace(self.policy, max_position=D(".6")), self.costs)
        self.oms.reserve(self.intent(quantity=40), self.quotes, self.now)
        with self.assertRaisesRegex(RiskRejected, "Position"):
            self.oms.reserve(self.intent("another", quantity=30), self.quotes, self.now)

    def test_policy_and_expiry_rechecked_at_send(self):
        intent = self.intent()
        self.oms.reserve(intent, self.quotes, self.now)
        tightened = OMS(self.store, self.broker, replace(self.policy, max_position=D(".4")), self.costs)
        self.assertEqual(tightened.submit(intent.key, self.quotes, self.now)["state"], "REJECTED")
        other = self.intent("expired")
        self.oms.reserve(other, self.quotes, self.now)
        self.assertEqual(self.oms.submit(other.key, self.quotes, other.expires_at)["state"], "REJECTED")
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM remote_orders").fetchone()[0], 0)

    def test_idempotency_key_cannot_change_payload(self):
        intent = self.intent()
        self.oms.reserve(intent, self.quotes, self.now)
        self.oms.reserve(intent, self.quotes, self.now)
        with self.assertRaisesRegex(AgentError, "different order payload"):
            self.oms.reserve(replace(intent, quantity=51), self.quotes, self.now)

    def test_future_and_stale_quotes_rejected(self):
        for offset in (-61, 1):
            quotes = {**self.quotes, "A": Quote("A", D(10), self.now + timedelta(seconds=offset))}
            with self.assertRaisesRegex(RiskRejected, "stale"):
                self.oms.reserve(self.intent(), quotes, self.now)

    def test_external_balance_mismatch_halts_new_exposure(self):
        self.store.db.execute("UPDATE accounts SET cash='900' WHERE account='test' AND ledger='REMOTE'")
        result = self.oms.reconcile("test")
        self.assertFalse(result["matches"])
        self.assertEqual(result["halt_reason"], "account_mismatch_requires_review")
        with self.assertRaises(RiskRejected):
            self.oms.reserve(self.intent(), self.quotes, self.now)

    def test_reconciliation_contract_error_halts_account(self):
        order = self.submit()
        self.store.db.execute("UPDATE remote_orders SET filled_qty=1 WHERE broker_id=?", (order["broker_id"],))
        with self.assertRaisesRegex(AgentError, "cumulative"):
            self.oms.reconcile("test")
        self.assertEqual(self.store.halt_reason("test"), "reconciliation_error_requires_review")

    def test_fragmented_fill_fee_rounding_is_reserved(self):
        costs = Costs(D("1.5"), D(0), D(0))
        self.assertGreaterEqual(costs.reserve_fee("BUY", 50, D(10)),
                                costs.fee("BUY", 1, D(10)) * 50)
        self.oms = OMS(self.store, SimulatedBroker(self.store, costs), self.policy, costs)
        with self.assertRaisesRegex(RiskRejected, "cash"):
            self.oms.reserve(self.intent(quantity=100), self.quotes, self.now)


if __name__ == "__main__":
    unittest.main()
