import io
import json
import unittest
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from urllib.parse import urlparse

from fixtures.markets import release_market
from fixtures.paper_exchange import FixtureExchange
from trading_agent.cli import main
from trading_agent.contracts import (
    UTC,
    AgentError,
    Costs,
    D,
    StrategySpec,
    canonical,
    session_time,
)
from trading_agent.data import Snapshot, write_csv
from trading_agent.diagnostics import doctor, host_access
from trading_agent.execution import RiskPolicy
from trading_agent.releases import (
    MonitorPolicy,
    activate_shadow,
    get_release,
    observe_shadow,
    shadow_status,
    stage_release,
)
from trading_agent.research import BoundedLookbacks, research
from trading_agent.store import Store

ALLOW = {"http_network_policy": {"type": "restricted", "egress_rules": [{"host": "openapivts.koreainvestment.com"}]}}
DENY = {"http_network_policy": {"type": "restricted", "egress_rules": []}}
ENV = {"KIS_PAPER_APP_KEY": "private-key", "KIS_PAPER_APP_SECRET": "private-secret", "KIS_PAPER_ACCOUNT": "12345678-01"}


class DiagnosticsTests(unittest.TestCase):
    def test_presence_and_policy_never_imply_service_verification(self):
        with patch.dict("os.environ", ENV, clear=True):
            report = doctor(policy=ALLOW, transport=lambda *args: self.fail("network"))
        self.assertTrue(report["paper"]["configured"])
        self.assertFalse(report["paper"]["read_probe_verified"])
        self.assertEqual(report["http_requests"], 0)
        serialized = canonical(report)
        for private in ENV.values():
            self.assertNotIn(private, serialized)

    def test_blocked_or_missing_configuration_does_not_issue_token(self):
        with patch.dict("os.environ", ENV, clear=True):
            report = doctor(probe_paper=True, symbol="123456", policy=DENY, transport=lambda *args: self.fail("network"))
        self.assertEqual(report["paper"]["probe"], "BLOCKED_CONFIGURATION_OR_POLICY")
        with patch.dict("os.environ", {}, clear=True):
            self.assertEqual(doctor(probe_paper=True, symbol="123456", policy=ALLOW)["http_requests"], 0)

    @patch("trading_agent.kis_paper.time.sleep")
    def test_read_probe_uses_only_oauth_and_read_routes_and_marks_fixture(self, _sleep):
        exchange = FixtureExchange(datetime.now(UTC))
        with patch.dict("os.environ", ENV, clear=True):
            report = doctor(probe_paper=True, symbol="123456", policy=ALLOW, transport=exchange)
        self.assertEqual(report["paper"]["probe"], "PASSED")
        self.assertFalse(report["paper"]["read_probe_verified"])
        self.assertEqual(report["paper"]["evidence_source"], "INJECTED_TRANSPORT")
        self.assertEqual(report["http_requests"], 4)
        self.assertEqual(report["order_write_requests"], 0)
        self.assertTrue(all(method == "GET" or urlparse(url).path == "/oauth2/tokenP" for method, url, _, _ in exchange.calls))

    def test_probe_sanitizes_errors_and_does_not_retry_authentication(self):
        def failure(*args):
            raise OSError("private-key private-secret 12345678-01")
        with patch.dict("os.environ", ENV, clear=True):
            report = doctor(probe_paper=True, symbol="123456", policy=ALLOW, transport=failure)
        self.assertEqual(report["http_requests"], 1)
        self.assertEqual(report["paper"]["probe"], "FAILED")
        self.assertNotIn("private-secret", canonical(report))

    def test_policy_unknown_is_explicit_and_bad_symbol_rejected_without_io(self):
        self.assertEqual(host_access("api.openai.com", {}), "UNKNOWN")
        self.assertEqual(host_access("api.openai.com", DENY), "POLICY_BLOCKS")
        with self.assertRaises(AgentError):
            doctor(probe_paper=True, symbol="BAD", transport=lambda *a: self.fail("network"))


class ReleaseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.forward = release_market()
        cls.evaluation = Snapshot(list(cls.forward.bars[:400]))
        db = Store(":memory:")
        cls.report = research(cls.evaluation, StrategySpec(20, 1, D(".4")), Costs(), db,
                              BoundedLookbacks((2,)), initial_cash=D(10000))
        assert cls.report["verdict"] == "ELIGIBLE_FOR_PAPER_REVIEW"
        cls.holdouts = [tuple(row) for row in db.db.execute("SELECT * FROM holdouts")]
        db.close()

    def setUp(self):
        self.store = Store(":memory:")
        self.store.db.execute("INSERT INTO experiments VALUES(?,?)", (self.report["run_id"], canonical(self.report)))
        self.store.db.executemany("INSERT INTO holdouts VALUES(?,?,?,?)", self.holdouts)
        self.risk = RiskPolicy(("DEMO_CYCLE",))
        self.monitor = MonitorPolicy(D(".02"), D(".1"), 3600)
        self.store.create_account("observed", 10000)

    def tearDown(self):
        self.store.close()

    def stage(self):
        return stage_release(self.store, self.report["run_id"], self.evaluation, self.risk, self.monitor)

    def activate(self):
        staged = self.stage()
        return activate_shadow(self.store, "test", staged["release_id"], "observed", 0, "fixture-review", "engineered test only")

    def observe(self, index=400, **kwargs):
        day = self.forward.sessions[index]
        return observe_shadow(self.store, "test", kwargs.get("snapshot", self.forward), day,
                              kwargs.get("as_of", session_time(day, 16)))

    def changed_report(self, change):
        report = json.loads(canonical(self.report))
        change(report)
        self.store.db.execute("UPDATE experiments SET report_json=?", (canonical(report),))

    def test_release_is_immutable_idempotent_and_does_not_activate(self):
        first, second = self.stage(), self.stage()
        self.assertEqual(first, second)
        self.assertFalse(first["activated"])
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM shadow_slots").fetchone()[0], 0)
        self.assertEqual(first["manifest"]["mode"], "SHADOW_ONLY")
        self.assertEqual(first["manifest"]["costs"], json.loads(canonical(Costs())))

    def test_failed_candidate_or_forged_verdict_cannot_be_staged(self):
        self.changed_report(lambda r: r.update(verdict="KEEP_CHAMPION"))
        with self.assertRaisesRegex(AgentError, "did not pass"):
            self.stage()
        self.changed_report(lambda r: r["holdout"].update(cost_stress_improvement=-1))
        with self.assertRaisesRegex(AgentError, "gates"):
            self.stage()

    def test_missing_holdout_claim_changed_data_and_loose_risk_are_rejected(self):
        with self.assertRaisesRegex(AgentError, "allocation"):
            stage_release(self.store, self.report["run_id"], self.evaluation,
                          replace(self.risk, max_position=D(".2")), self.monitor)
        changed = Snapshot([replace(self.evaluation.bars[0], volume=1), *self.evaluation.bars[1:]])
        with self.assertRaisesRegex(AgentError, "hash mismatch"):
            stage_release(self.store, self.report["run_id"], changed, self.risk, self.monitor)
        self.store.db.execute("DELETE FROM holdouts")
        with self.assertRaisesRegex(AgentError, "claim"):
            self.stage()

    def test_build_evidence_or_manifest_changes_fail_closed(self):
        staged = self.stage()
        with patch("trading_agent.releases.implementation_id", return_value="new-build"):
            with self.assertRaisesRegex(AgentError, "build"):
                get_release(self.store, staged["release_id"])
        self.changed_report(lambda r: r.update(total_trials=999))
        with self.assertRaisesRegex(AgentError, "evidence changed"):
            get_release(self.store, staged["release_id"])

    def test_manifest_tampering_and_unreviewed_activation_are_rejected(self):
        staged = self.stage()
        with self.assertRaisesRegex(AgentError, "reviewer"):
            activate_shadow(self.store, "test", staged["release_id"], "observed", 0, "", "reason")
        self.store.db.execute("UPDATE releases SET manifest_json=?", (canonical({**staged["manifest"], "mode": "LIVE"}),))
        with self.assertRaisesRegex(AgentError, "integrity"):
            get_release(self.store, staged["release_id"])

    def test_halted_account_and_open_orders_prevent_activation(self):
        from trading_agent.contracts import OrderIntent, Quote
        from trading_agent.execution import OMS, SimulatedBroker
        staged = self.stage()
        self.store.halt("observed", "test_halt")
        with self.assertRaisesRegex(AgentError, "halted"):
            activate_shadow(self.store, "test", staged["release_id"], "observed", 0, "review", "reason")
        self.store.halt("observed", "")
        at = session_time(self.forward.sessions[400], 9)
        intent = OrderIntent("pending", "observed", "DEMO_CYCLE", "BUY", 1, D(100), at,
                             at + timedelta(minutes=1), "fixture", "fixture")
        OMS(self.store, SimulatedBroker(self.store), self.risk).reserve(intent, {"DEMO_CYCLE": Quote("DEMO_CYCLE", D(100), at)}, at)
        with self.assertRaisesRegex(AgentError, "open orders"):
            activate_shadow(self.store, "test", staged["release_id"], "observed", 0, "review", "reason")

    def test_review_requires_revision_and_preserves_account_binding(self):
        active = self.activate()
        with self.assertRaisesRegex(AgentError, "revision conflict"):
            activate_shadow(self.store, "test", active["release_id"], "observed", 0, "review", "reason")
        self.store.create_account("different", 10000)
        with self.assertRaisesRegex(AgentError, "account binding"):
            activate_shadow(self.store, "test", active["release_id"], "different", 1, "review", "reason")
        self.assertEqual(shadow_status(self.store, "test")["revision"], 1)

    def test_only_forward_observations_and_unmodified_history_allowed(self):
        self.activate()
        with self.assertRaisesRegex(AgentError, "post-evaluation"):
            self.observe(399)
        self.assertEqual(self.observe()["state"], "OBSERVING")
        changed = Snapshot([replace(self.forward.bars[0], volume=1), *self.forward.bars[1:]])
        report = self.observe(401, snapshot=changed)
        self.assertIn("FROZEN_HISTORY_OR_UNIVERSE_CHANGED", report["problems"])
        self.assertEqual(shadow_status(self.store, "test")["state"], "STOPPED")

    def test_observation_is_idempotent_and_rejects_rewrites(self):
        self.activate()
        first = self.observe()
        self.assertEqual(first, self.observe())
        self.assertEqual(first["orders_submitted"], 0)
        self.assertIsNotNone(first["decision"])
        self.observe(401)
        self.assertEqual(first, self.observe())
        day = self.forward.sessions[400]
        with self.assertRaisesRegex(AgentError, "time must advance"):
            self.observe(400, as_of=session_time(day, 16) + timedelta(seconds=1))
        self.store.save_wallet("observed", "LOCAL", replace(self.store.wallet("observed"), cash=D(9999)))
        with self.assertRaisesRegex(AgentError, "Conflicting observation"):
            self.observe()
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM orders").fetchone()[0], 0)

    def test_appending_unknown_future_bars_does_not_change_existing_observation(self):
        self.activate()
        first = self.observe()
        extra = release_market(405)
        self.assertEqual(first, self.observe(snapshot=extra))

    def test_data_staleness_stops_and_activation_cannot_clear_latch(self):
        active = self.activate()
        day = self.forward.sessions[400]
        stopped = self.observe(as_of=session_time(day, 20))
        self.assertIn("STALE_FINAL_BARS", stopped["problems"])
        self.assertIsNone(stopped["decision"])
        self.assertIn("STOP_LATCHED", self.observe(401)["problems"])
        with self.assertRaisesRegex(AgentError, "stopped slot"):
            activate_shadow(self.store, "test", active["release_id"], "observed", 1, "review", "reset")

    def test_unavailable_current_bar_records_stop_instead_of_emitting_signal(self):
        self.activate()
        day = self.forward.sessions[400]
        report = self.observe(as_of=session_time(day, 15, 35))
        self.assertIn("UNAVAILABLE_OR_INVALID_FINAL_BARS", report["problems"])
        self.assertIsNone(report["decision"])

    def test_daily_loss_and_peak_survive_database_restart(self):
        with TemporaryDirectory() as directory:
            durable = Store(Path(directory) / "journal.sqlite3")
            self.store.db.backup(durable.db)
            self.store.close()
            self.store = durable
            self.store.save_wallet("observed", "LOCAL", replace(self.store.wallet("observed"), cash=D(6000), positions={"DEMO_CYCLE": 40}))
            self.activate()
            first = self.observe()
            path = self.store.path
            self.store.close()
            self.store = Store(path)
            bar = self.forward.bars[401]
            lower = bar.close * D(".5")
            changed = Snapshot([*self.forward.bars[:401], replace(bar, open=lower, high=lower, low=lower, close=lower), *self.forward.bars[402:]])
            report = self.observe(401, snapshot=changed)
            self.assertIn("DAILY_LOSS_LIMIT", report["problems"])
            self.assertIn("DRAWDOWN_LIMIT", report["problems"])
            self.assertEqual(report["peak_equity"], first["peak_equity"])
            self.store.close()
            self.store = Store(path)
            self.assertIn("STOP_LATCHED", self.observe(402)["problems"])
            # Keep tearDown's connection alive after TemporaryDirectory removes files.

    def test_cli_release_review_and_observe_without_credentials(self):
        with TemporaryDirectory() as directory, patch.dict("os.environ", {}, clear=True):
            root = Path(directory)
            database, prices, policy, release = (root / name for name in ("db.sqlite3", "prices.csv", "policy.json", "release.json"))
            disk = Store(database)
            self.store.db.backup(disk.db)
            disk.close()
            write_csv(self.evaluation, prices)
            policy.write_text(canonical({"risk": self.risk, "monitor": self.monitor}))
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                self.assertEqual(main(["release", "--db", str(database), "--csv", str(prices), "--run-id", self.report["run_id"], "--policy", str(policy), "--output", str(release)]), 0)
                release_id = json.loads(release.read_text())["release_id"]
                self.assertEqual(main(["shadow", "activate", "--db", str(database), "--slot", "test", "--release-id", release_id, "--account", "observed", "--expected-revision", "0", "--reviewer", "fixture", "--reason", "test"]), 0)
                write_csv(self.forward, prices)
                day = self.forward.sessions[400]
                self.assertEqual(main(["shadow", "observe", "--db", str(database), "--slot", "test", "--csv", str(prices), "--session", str(day), "--as-of", session_time(day, 16).isoformat()]), 0)


if __name__ == "__main__":
    unittest.main()
