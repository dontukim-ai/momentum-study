import io
import json
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta
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

    def test_account_without_hyphen_is_the_same_8_digit_account(self, _sleep):
        with patch.dict("os.environ", {"KIS_ACCOUNT": "1234567801"}):
            plain = KisRealReadOnly(self.exchange)
            self.assertTrue(doctor(policy=DENY)["real"]["configured"])
        self.assertEqual(plain.account_params(), {"CANO": "12345678", "ACNT_PRDT_CD": "01"})
        self.assertEqual(plain._cache_id, self.client._cache_id)
        for bad in ("12345678", "1234567802", "123456789-01", "12345678-1", " 1234567801"):
            with patch.dict("os.environ", {"KIS_ACCOUNT": bad}):
                with self.assertRaisesRegex(AgentError, "KIS_ACCOUNT"):
                    KisRealReadOnly(self.exchange)
                self.assertFalse(doctor(policy=DENY)["real"]["configured"])
        self.assertEqual(self.exchange.calls, [])

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


class MarketDataReadTests(unittest.TestCase):
    """intraday-cost-probe-v10 §7: order-book and minute-bar reads only."""

    def setUp(self):
        self.env = patch.dict("os.environ", REAL_ENV, clear=True)
        self.env.start()
        self.calls = []
        self.client = KisRealReadOnly(self.transport, minimum_interval=.1)

    def tearDown(self):
        self.env.stop()

    def transport(self, method, url, headers, body):
        self.calls.append((method, url, headers, body))
        if method == "POST":
            return {"access_token": "token", "expires_in": 86400}
        path = urlparse(url).path
        if path.endswith("inquire-asking-price-exp-ccn"):
            return {"rt_cd": "0", "output1": {"askp1": "35005", "bidp1": "35000", "aspr_acpt_hour": "100000"},
                    "output2": {}}
        return {"rt_cd": "0", "output1": {}, "output2": [
            {"stck_bsop_date": "20261027", "stck_cntg_hour": "100000", "stck_prpr": "35000"}]}

    def test_reads_use_their_tr_ids_without_account_identity(self):
        book = self.client.asking("069500")
        bars = self.client.minute_bars("069500", "100000")
        self.assertEqual(book["output1"]["askp1"], "35005")
        self.assertEqual(bars["output2"][0]["stck_cntg_hour"], "100000")
        gets = [c for c in self.calls if c[0] == "GET"]
        self.assertEqual([c[2]["tr_id"] for c in gets], ["FHKST01010200", "FHKST03010200"])
        for _, url, _, body in gets:
            query = parse_qs(urlparse(url).query)
            self.assertEqual(query["FID_INPUT_ISCD"], ["069500"])
            self.assertNotIn("CANO", query)
            self.assertIsNone(body)
        self.assertEqual(parse_qs(urlparse(gets[1][1]).query)["FID_INPUT_HOUR_1"], ["100000"])

    def test_bad_inputs_are_rejected_before_any_request(self):
        for call in (lambda: self.client.asking("69500"), lambda: self.client.minute_bars("069500", "1000"),
                     lambda: self.client.minute_bars("069500", "170000")):
            with self.assertRaises(AgentError):
                call()
        self.assertEqual(self.calls, [])

    def test_orders_stay_rejected(self):
        with self.assertRaisesRegex(AgentError, "reads are allowed"):
            self.client.request("/uapi/domestic-stock/v1/trading/order-cash", {})
        self.assertFalse(hasattr(self.client, "submit"))
        self.assertEqual(REAL_CAPITAL_LIMIT, D("1000000"))


class PastMinuteBarTests(unittest.TestCase):
    """Past-date one-minute bars (FHKST03010230), read-only market data."""

    def setUp(self):
        self.env = patch.dict("os.environ", REAL_ENV, clear=True)
        self.env.start()
        self.calls = []
        self.client = KisRealReadOnly(self.transport, minimum_interval=.1)

    def tearDown(self):
        self.env.stop()

    def transport(self, method, url, headers, body):
        self.calls.append((method, url, headers, body))
        if method == "POST":
            return {"access_token": "token", "expires_in": 86400}
        return {"rt_cd": "0", "output1": {}, "output2": [
            {"stck_bsop_date": "20260105", "stck_cntg_hour": "100000", "stck_prpr": "35000"}]}

    def test_past_day_bars_use_their_tr_id_and_date(self):
        day = datetime.now(KST).date() - timedelta(days=30)
        result = self.client.minute_bars_on("069500", day, "100000")
        self.assertEqual(result["output2"][0]["stck_cntg_hour"], "100000")
        method, url, headers, _ = self.calls[-1]
        self.assertEqual((method, headers["tr_id"]), ("GET", "FHKST03010230"))
        query = parse_qs(urlparse(url).query)
        self.assertEqual(query["FID_INPUT_DATE_1"], [day.strftime("%Y%m%d")])
        self.assertEqual(query["FID_INPUT_HOUR_1"], ["100000"])
        self.assertNotIn("CANO", query)

    def test_alphanumeric_krx_codes_are_read_but_other_shapes_are_not(self):
        # KRX has issued codes such as 0167A0 since 2025 (explore v6 PR #89 for DART).
        day = datetime.now(KST).date() - timedelta(days=30)
        self.client.minute_bars_on("0167A0", day, "100000")
        self.assertEqual(parse_qs(urlparse(self.calls[-1][1]).query)["FID_INPUT_ISCD"], ["0167A0"])
        for bad in ("0167a0", "167A0", "0167A00", "01-7A0"):
            with self.assertRaisesRegex(AgentError, "six-character"):
                self.client.minute_bars_on(bad, day, "100000")

    def test_future_or_older_than_a_year_and_a_month_is_rejected_before_any_request(self):
        today = datetime.now(KST).date()
        for day in (today + timedelta(days=1), today - timedelta(days=400)):
            with self.assertRaisesRegex(AgentError, "retained minute-bar window"):
                self.client.minute_bars_on("069500", day, "100000")
        self.assertEqual(self.calls, [])
