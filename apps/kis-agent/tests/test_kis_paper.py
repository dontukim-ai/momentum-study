import unittest
from datetime import datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

from fixtures.paper_exchange import FixtureExchange
from trading_agent.contracts import (
    KST,
    AgentError,
    Costs,
    D,
    OrderIntent,
    Quote,
    session_time,
)
from trading_agent.diagnostics import doctor
from trading_agent.execution import OMS, RiskPolicy, SimulatedBroker
from trading_agent.kis_paper import PAPER_HOST, KisPaperBroker, PaperSession
from trading_agent.store import Store


class PaperBrokerTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.store = Store(Path(self.temp.name) / "paper.sqlite3")
        self.env = patch.dict("os.environ", {"KIS_PAPER_APP_KEY": "fixture-key", "KIS_PAPER_APP_SECRET": "fixture-secret", "KIS_PAPER_ACCOUNT": "12345678-01"}, clear=True)
        self.env.start()
        self.sleep = patch("trading_agent.kis_paper.time.sleep")
        self.sleep.start()
        day = datetime.now(KST).date()
        while day.weekday() >= 5:
            day -= timedelta(days=1)
        self.now = session_time(day, 10)
        self.exchange = FixtureExchange(self.now)
        self.session = PaperSession(self.exchange, .1)
        self.broker = KisPaperBroker(self.store, self.session, "paper", {"123456": D(1)})
        self.broker.bootstrap()
        self.policy = RiskPolicy(("123456",), D(".8"), D(".4"), D(20000))
        self.costs = Costs(D(0), D(0), D(0))
        self.oms = OMS(self.store, self.broker, self.policy, self.costs)
        self.quotes = {"123456": Quote("123456", D(1000), self.now)}

    def tearDown(self):
        self.store.close()
        self.sleep.stop()
        self.env.stop()
        self.temp.cleanup()

    def intent(self, quantity=10, price=D(1000), key="paper-order"):
        return OrderIntent(key, "paper", "123456", "BUY", quantity, price, self.now,
                           self.now + timedelta(minutes=2), "reviewed-strategy", "reviewed-snapshot")

    def send(self, intent=None):
        intent = intent or self.intent()
        self.oms.reserve(intent, self.quotes, self.now)
        return self.oms.submit(intent.key, self.quotes, self.now)

    def test_paper_host_tr_ids_and_uppercase_payload_no_live_fallback(self):
        order = self.send()
        self.assertEqual(order["state"], "ACKED")
        for _, url, headers, body in self.exchange.calls:
            self.assertTrue(url.startswith(PAPER_HOST + "/"))
            if "tr_id" in headers:
                self.assertTrue(headers["tr_id"].startswith(("V", "FHK")))
            if url.endswith("order-cash"):
                self.assertEqual(body["ORD_DVSN"], "00")
                self.assertEqual(body["EXCG_ID_DVSN_CD"], "KRX")
                self.assertEqual(body["ORD_QTY"], "10")
                self.assertEqual(headers["tr_id"], "VTTC0012U")
        self.assertEqual(self.store.wallet("paper").cash, D(100000))
        self.assertEqual(self.store.status("paper")["mode"], "KIS_PAPER")

    def test_cumulative_partial_fills_book_exact_notional_once_across_restart(self):
        order = self.send(self.intent(3, D(400)))
        self.exchange.fill("00000001", 1, D(333))
        self.oms.reconcile("paper")
        self.exchange.fill("00000001", 2, D(667))
        self.oms.reconcile("paper")
        self.oms.reconcile("paper")
        self.assertEqual(self.store.wallet("paper").cash, D(99000))
        self.assertEqual(self.store.wallet("paper").positions, {"123456": 3})
        self.assertEqual(self.store.order(order["key"])["state"], "FILLED")
        self.store.close()
        self.store = Store(Path(self.temp.name) / "paper.sqlite3")
        self.broker = KisPaperBroker(self.store, self.session, "paper", {"123456": D(1)})
        self.oms = OMS(self.store, self.broker, self.policy, self.costs)
        self.assertTrue(self.oms.reconcile("paper")["matches"])
        self.assertEqual(self.store.status("paper")["fills"], 2)

    def test_lost_acceptance_requires_explicit_identity_link_even_if_unique(self):
        self.exchange.drop_order_response = True
        order = self.send()
        self.assertEqual(order["state"], "UNKNOWN")
        self.assertEqual(self.broker.find(self.intent()), [])
        self.assertEqual(self.oms.reconcile("paper")["unresolved"], [order["key"]])
        self.oms.submit(order["key"], self.quotes, self.now)
        self.assertEqual(len(self.exchange.rows), 1)
        broker_id = self.broker.adopt(self.intent(), self.now.astimezone(KST).date(), "12345", "00000001")
        self.store.db.execute("UPDATE orders SET broker_id=? WHERE key=?", (broker_id, order["key"]))
        self.assertEqual(self.oms.reconcile("paper")["halt_reason"], "")

    def test_missing_actual_fees_stop_new_orders_even_when_cash_matches(self):
        self.send()
        del self.exchange.rows["00000001"]["tot_fee"]
        self.exchange.fill("00000001", 10, D(10000))
        result = self.oms.reconcile("paper")
        self.assertTrue(result["matches"])
        self.assertEqual(result["halt_reason"], "broker_contract_requires_review")
        self.assertIn("broker_issues", result)
        with self.assertRaises(AgentError):
            self.send(self.intent(key="another"))

    def test_cancel_without_paper_cancelability_contract_sends_no_cancel(self):
        order = self.send()
        with self.assertRaisesRegex(AgentError, "cancelability"):
            self.oms.cancel(order["key"])
        self.assertFalse(any(url.endswith("order-rvsecncl") for _, url, _, _ in self.exchange.calls))

    def test_cancel_ack_is_not_confirmation_and_fill_can_race(self):
        self.broker.cancelability_confirmed = True  # Only a verified fixture contract.
        order = self.send()
        self.exchange.fill("00000001", 2, D(2000))
        self.oms.reconcile("paper")
        row = self.exchange.rows["00000001"]
        row["psbl_qty"] = row["rmn_qty"]  # Fixture-only verified cancelability field.
        self.oms.cancel(order["key"])
        self.exchange.fill("00000001", 1, D(1000))
        self.oms.reconcile("paper")
        self.assertEqual(self.store.order(order["key"])["state"], "CANCEL_PENDING")
        row["cncl_cfrm_qty"], row["rmn_qty"] = row["rmn_qty"], "0"
        self.oms.reconcile("paper")
        self.assertEqual(self.store.order(order["key"])["state"], "CANCELLED")
        self.assertEqual(self.store.wallet("paper").positions, {"123456": 3})

    def test_private_disk_token_cache_reused_across_sessions(self):
        directory = Path(self.temp.name) / ".secrets"
        one = PaperSession(self.exchange, .1, directory)
        two = PaperSession(self.exchange, .1, directory)
        params = {"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": "123456"}
        before = sum(url.endswith("tokenP") for _, url, _, _ in self.exchange.calls)
        one.request("quote", params)
        two.request("quote", params)
        after = sum(url.endswith("tokenP") for _, url, _, _ in self.exchange.calls)
        self.assertEqual(after - before, 1)
        self.assertEqual(directory.stat().st_mode & 0o077, 0)
        self.assertTrue(all(p.stat().st_mode & 0o077 == 0 for p in directory.iterdir()))

    def test_backend_identity_mixing_is_blocked(self):
        with self.assertRaisesRegex(AgentError, "different broker"):
            SimulatedBroker(self.store).wallet("paper")
        with patch.dict("os.environ", {"KIS_PAPER_ACCOUNT": "87654321-01"}):
            other = KisPaperBroker(self.store, PaperSession(self.exchange), "paper", {"123456": D(1)})
            with self.assertRaisesRegex(AgentError, "binding"):
                other.bootstrap()
        duplicate = KisPaperBroker(self.store, self.session, "duplicate-alias", {"123456": D(1)})
        with self.assertRaisesRegex(AgentError, "already has an account alias"):
            duplicate.bootstrap()

    def test_account_without_hyphen_is_the_same_8_digit_account(self):
        with patch.dict("os.environ", {"KIS_PAPER_ACCOUNT": "1234567801"}):
            plain = PaperSession(self.exchange, .1)
            self.assertTrue(doctor(policy={})["paper_account_format_valid"])
        self.assertEqual(plain.identity, self.session.identity)
        for bad in ("12345678", "1234567802", "123456789-01"):
            with patch.dict("os.environ", {"KIS_PAPER_ACCOUNT": bad}):
                with self.assertRaisesRegex(AgentError, "KIS_PAPER_ACCOUNT"):
                    PaperSession(self.exchange, .1)
                self.assertFalse(doctor(policy={})["paper_account_format_valid"])

    def test_explicit_business_rejection_is_terminal_not_unknown(self):
        self.exchange.business_reject = True
        self.assertEqual(self.send()["state"], "REJECTED")
        self.assertEqual(self.exchange.rows, {})

    def test_pagination_requires_completion_and_new_cursor(self):
        original = self.exchange.__call__
        def pages(method, url, headers, body):
            if urlparse(url).path.endswith("inquire-daily-ccld"):
                query = parse_qs(urlparse(url).query)
                if "CTX_AREA_NK100" not in query:
                    return {"rt_cd": "0", "output1": [], "output2": {}, "ctx_area_fk100": "fk", "ctx_area_nk100": "next", "_response_headers": {"tr_cont": "M"}}
                self.assertEqual(headers["tr_cont"], "N")
                return {"rt_cd": "0", "output1": [], "output2": {}, "_response_headers": {"tr_cont": "D"}}
            return original(method, url, headers, body)
        self.session._transport = pages
        self.assertEqual(self.broker.orders(self.now.astimezone(KST).date()), [])
        self.session._transport = lambda *a: {"rt_cd": "0", "output1": [], "ctx_area_fk100": "fk", "ctx_area_nk100": "same", "_response_headers": {"tr_cont": "M"}}
        with self.assertRaisesRegex(AgentError, "cursor"):
            self.broker.orders(self.now.astimezone(KST).date())

    def test_regressed_fill_or_missing_settlement_cash_fails_closed(self):
        self.send()
        self.exchange.fill("00000001", 2, D(2000))
        self.oms.reconcile("paper")
        self.exchange.rows["00000001"].update(tot_ccld_qty="1", rmn_qty="9", tot_ccld_amt="1000")
        with self.assertRaisesRegex(AgentError, "regressed"):
            self.oms.reconcile("paper")
        self.assertTrue(self.store.halt_reason("paper"))

    def test_cumulative_average_cannot_hide_incremental_limit_violation(self):
        self.send()
        self.exchange.fill("00000001", 1, D(100))
        self.oms.reconcile("paper")
        self.exchange.fill("00000001", 1, D(1500))
        with self.assertRaisesRegex(AgentError, "Incremental"):
            self.oms.reconcile("paper")

    def test_wrong_backend_reconciliation_does_not_halt_a_valid_account(self):
        oms = OMS(self.store, SimulatedBroker(self.store), self.policy)
        with self.assertRaises(AgentError):
            oms.reconcile("paper")
        self.assertEqual(self.store.halt_reason("paper"), "")

    def test_readonly_preflight_failure_never_marks_submission_or_sends_order(self):
        original = self.session._transport
        def failing(method, url, headers, body):
            if urlparse(url).path.endswith("inquire-psbl-order"):
                raise AgentError("fixture read failure")
            return original(method, url, headers, body)
        self.session._transport = failing
        with self.assertRaisesRegex(AgentError, "read failure"):
            self.send()
        self.assertEqual(self.store.order("paper-order")["state"], "RESERVED")
        self.assertEqual(self.exchange.rows, {})

    def test_deposit_cash_is_not_used_as_a_missing_settlement_cash_fallback(self):
        original = self.session._transport
        def missing(method, url, headers, body):
            result = original(method, url, headers, body)
            if urlparse(url).path.endswith("inquire-balance"):
                result["output2"] = [{"dnca_tot_amt": "100000"}]
            return result
        self.session._transport = missing
        with self.assertRaisesRegex(AgentError, "deposit cash is not substituted"):
            self.oms.reconcile("paper")
        self.assertEqual(self.store.halt_reason("paper"), "reconciliation_error_requires_review")


if __name__ == "__main__":
    unittest.main()
