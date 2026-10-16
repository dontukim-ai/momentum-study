import io
import json
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

from fixtures.paper_exchange import FixtureExchange
from trading_agent.cli import main
from trading_agent.contracts import KST, UTC, AgentError, D, canonical
from trading_agent.diagnostics import doctor
from trading_agent.kis_real import REAL_CAPITAL_LIMIT, KisRealReadOnly

REAL_ENV = {"KIS_APP_KEY": "real-private-key", "KIS_APP_SECRET": "real-private-secret", "KIS_ACCOUNT": "12345678-01"}
ALLOW_REAL = {"http_network_policy": {"type": "restricted", "egress_rules": [{"host": "openapi.koreainvestment.com"}]}}
DENY = {"http_network_policy": {"type": "restricted", "egress_rules": []}}


@patch("trading_agent.kis_real.time.sleep")
class RealKeyTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict("os.environ", REAL_ENV, clear=True)
        self.env.start()
        self.exchange = FixtureExchange(datetime.now(UTC))
        self.client = KisRealReadOnly(self.exchange)

    def tearDown(self):
        self.env.stop()

    def test_paper_credentials_never_substitute_for_real_credentials(self, _sleep):
        with patch.dict("os.environ", {"KIS_PAPER_APP_KEY": "paper", "KIS_PAPER_APP_SECRET": "paper", "KIS_PAPER_ACCOUNT": "12345678-01"}, clear=True):
            with self.assertRaisesRegex(AgentError, "KIS_APP_KEY"):
                KisRealReadOnly(self.exchange)
            report = doctor(probe_real=True, symbol="123456", policy=ALLOW_REAL, transport=self.exchange)
        self.assertEqual(self.exchange.calls, [])
        self.assertEqual(report["real"]["probe"], "BLOCKED_CONFIGURATION_OR_POLICY")

    def test_real_host_tr_ids_identity_and_read_only_methods(self, _sleep):
        self.client.quote("123456")
        self.client.balance()
        self.client.orders(datetime.now(KST).date())
        self.assertEqual(len(self.exchange.calls), 4)
        self.assertEqual({c[2].get("tr_id") for c in self.exchange.calls if c[0] == "GET"}, {"FHKST01010100", "TTTC8434R", "TTTC0081R"})
        for method, url, headers, body in self.exchange.calls:
            parsed = urlparse(url)
            self.assertEqual(parsed.netloc, "openapi.koreainvestment.com:9443")
            if method == "POST":
                self.assertEqual(parsed.path, "/oauth2/tokenP")
                self.assertEqual(body["appkey"], REAL_ENV["KIS_APP_KEY"])
            else:
                self.assertEqual(method, "GET")
                self.assertIsNone(body)
                if "trading/" in parsed.path:
                    self.assertEqual(parse_qs(parsed.query)["CANO"], ["12345678"])

    def test_order_and_cancel_operations_rejected_before_authentication(self, _sleep):
        for operation in ("buy", "sell", "cancel", "/uapi/domestic-stock/v1/trading/order-cash"):
            with self.assertRaisesRegex(AgentError, "reads are allowed"):
                self.client.request(operation, self.client.account_params())
        self.assertEqual(self.exchange.calls, [])
        self.assertFalse(hasattr(self.client, "submit"))

    def test_another_account_cannot_be_queried_using_same_client(self, _sleep):
        with self.assertRaisesRegex(AgentError, "identity mismatch"):
            self.client.request("balance", {"CANO": "99999999", "ACNT_PRDT_CD": "01"})
        self.assertEqual(self.exchange.calls, [])

    def test_cash_fields_are_distinct_and_not_called_buying_power(self, _sleep):
        self.exchange.cash = D(2000000)
        result = self.client.balance()
        self.assertEqual(result["expected_settlement_cash"], D(2000000))
        self.assertEqual(result["deposit_cash"], D(0))
        self.assertFalse(result["cash_is_verified_buying_power"])
        report = doctor(policy=ALLOW_REAL)
        self.assertEqual(report["real"]["capital_limit_krw"], str(REAL_CAPITAL_LIMIT))
        self.assertFalse(report["real"]["orders_enabled"])

    def test_real_probe_records_fixture_success_without_claiming_service_verification(self, _sleep):
        report = doctor(probe_real=True, symbol="123456", policy=ALLOW_REAL, transport=self.exchange)
        self.assertEqual(report["real"]["probe"], "PASSED")
        self.assertFalse(report["real"]["read_probe_verified"])
        self.assertFalse(report["real"]["full_order_contract_verified"])
        self.assertEqual(report["paper"]["probe"], "NOT_REQUESTED")
        self.assertEqual(report["http_requests"], 4)
        self.assertEqual(report["order_write_requests"], 0)
        for private in REAL_ENV.values():
            self.assertNotIn(private, canonical(report))

    def test_network_policy_block_and_provider_errors_do_not_leak_or_retry(self, _sleep):
        report = doctor(probe_real=True, symbol="123456", policy=DENY, transport=self.exchange)
        self.assertEqual(report["http_requests"], 0)
        self.assertEqual(self.exchange.calls, [])
        def failure(*args):
            raise OSError("real-private-secret 12345678-01")
        report = doctor(probe_real=True, symbol="123456", policy=ALLOW_REAL, transport=failure)
        self.assertEqual(report["real"]["probe"], "FAILED")
        self.assertEqual(report["http_requests"], 1)
        self.assertNotIn("real-private-secret", canonical(report))

    def test_missing_and_repeated_continuation_fail_instead_of_accepting_partial_cash(self, _sleep):
        def malformed(method, url, headers, body):
            result = self.exchange(method, url, headers, body)
            if method == "GET":
                result.pop("_response_headers")
            return result
        with self.assertRaisesRegex(AgentError, "headers"):
            KisRealReadOnly(malformed).balance()
        def repeat(method, url, headers, body):
            result = self.exchange(method, url, headers, body)
            if method == "GET":
                result.update(_response_headers={"tr_cont": "M"}, ctx_area_fk100="repeat", ctx_area_nk100="")
            return result
        with self.assertRaisesRegex(AgentError, "repeated"):
            KisRealReadOnly(repeat).balance()

    def test_private_cache_is_reused_and_separated_from_paper(self, _sleep):
        from trading_agent.kis_paper import PaperSession
        with TemporaryDirectory() as directory:
            cache = Path(directory)/"private"
            KisRealReadOnly(self.exchange, cache).quote("123456")
            KisRealReadOnly(self.exchange, cache).quote("123456")
            self.assertEqual(sum(c[0] == "POST" for c in self.exchange.calls), 1)
            with patch.dict("os.environ", {"KIS_PAPER_APP_KEY": REAL_ENV["KIS_APP_KEY"], "KIS_PAPER_APP_SECRET": REAL_ENV["KIS_APP_SECRET"], "KIS_PAPER_ACCOUNT": REAL_ENV["KIS_ACCOUNT"]}):
                PaperSession(self.exchange, token_cache_dir=cache).request("quote", {"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": "123456"})
            self.assertEqual(sum(c[0] == "POST" for c in self.exchange.calls), 2)

    def test_cli_supports_real_probe_with_no_configuration_and_no_network(self, _sleep):
        with TemporaryDirectory() as directory, patch.dict("os.environ", {}, clear=True):
            output = Path(directory)/"readiness.json"
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                self.assertEqual(main(["doctor", "--probe-real", "--symbol", "123456", "--output", str(output)]), 0)
            report = json.loads(output.read_text())
            self.assertEqual(report["real"]["probe"], "BLOCKED_CONFIGURATION_OR_POLICY")
            self.assertEqual(report["http_requests"], 0)


if __name__ == "__main__":
    unittest.main()
