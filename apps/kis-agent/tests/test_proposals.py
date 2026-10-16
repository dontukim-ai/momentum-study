import json
import unittest
from dataclasses import replace
from unittest.mock import patch
from urllib.request import Request

from trading_agent.contracts import AgentError, Costs, StrategySpec
from trading_agent.data import Snapshot, synthetic_snapshot
from trading_agent.proposals import (
    StructuredProposer,
    development_context,
    generate_proposal,
    response_artifact,
    validate_bundle,
)
from trading_agent.research import research
from trading_agent.store import Store
from trading_agent.transport import NoRedirect, open_authenticated


def response(bundle=None):
    return {"status": "completed", "output": [{"type": "message", "content": [{"type": "output_text", "text": json.dumps(bundle or {
        "hypothesis": "Shorter lookbacks may improve adaptation in development data.",
        "falsification": "Reject if unseen risk-adjusted net results do not improve.", "lookbacks": [5, 15, 30]})}]}],
        "usage": {"input_tokens": 100, "output_tokens": 40, "total_tokens": 140}}


class ProposalTests(unittest.TestCase):
    def setUp(self):
        self.snapshot = synthetic_snapshot(160)
        self.champion = StrategySpec()
        self.context = development_context(self.snapshot, self.champion)
        self.store = Store(":memory:")

    def tearDown(self):
        self.store.close()

    def test_heldout_price_changes_cannot_change_llm_context(self):
        cutoff = self.context["development_end"]
        changed = Snapshot([replace(b, open=b.open * 10, high=b.high * 10, low=b.low * 10, close=b.close * 10)
                            if b.session.isoformat() > cutoff else b for b in self.snapshot.bars])
        self.assertEqual(self.context, development_context(changed, self.champion))
        self.assertNotIn("holdout", self.context)

    @patch.dict("os.environ", {"OPENAI_API_KEY": "test-key"}, clear=True)
    def test_structured_request_cached_and_no_credentials_in_report(self):
        calls = []
        def transport(url, headers, body):
            calls.append((url, body))
            self.assertEqual(url, "https://api.openai.com/v1/responses")
            self.assertEqual(body["text"]["format"]["type"], "json_schema")
            self.assertTrue(body["text"]["format"]["strict"])
            self.assertFalse(body["store"])
            return response()
        artifact = generate_proposal(self.context, self.store, "test-model", transport=transport)
        again = generate_proposal(self.context, self.store, "test-model", transport=transport)
        self.assertEqual(len(calls), 1)
        self.assertEqual(artifact, again)
        self.assertNotIn("test-key", json.dumps(artifact))
        self.assertFalse(artifact["provenance"]["network_verified"])
        proposer = StructuredProposer(artifact, self.context)
        for spec in proposer.propose(self.champion):
            self.assertEqual(replace(spec, lookback=self.champion.lookback), self.champion)
        report = research(self.snapshot, self.champion, Costs(), self.store, proposer)
        self.assertIn("proposal", report["configuration"])
        self.assertFalse(report["live_promotion"])

    @patch.dict("os.environ", {"OPENAI_API_KEY": "test-key"}, clear=True)
    def test_malformed_response_consumes_attempt_without_evaluation(self):
        calls = []
        def bad(*args):
            calls.append(1)
            return response({"hypothesis": "a", "falsification": "b", "lookbacks": [5], "code": "unsafe"})
        with self.assertRaisesRegex(AgentError, "audited attempt"):
            generate_proposal(self.context, self.store, "test-model", transport=bad)
        with self.assertRaisesRegex(AgentError, "budget consumed"):
            generate_proposal(self.context, self.store, "test-model", transport=bad)
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM experiments").fetchone()[0], 0)
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM holdouts").fetchone()[0], 0)

    def test_parameter_smuggling_boolean_duplicates_and_budget_rejected(self):
        base = {"hypothesis": "a", "falsification": "b", "lookbacks": [5]}
        for values in ([True], [5, 5], [61], [1], [], [5.0]):
            with self.assertRaises(AgentError):
                validate_bundle({**base, "lookbacks": values})
        with self.assertRaises(AgentError):
            validate_bundle({**base, "risk": "change"})
        with self.assertRaises(AgentError):
            validate_bundle({**base, "lookbacks": [5, 10]}, 1)

    def test_refusal_incomplete_output_and_wrong_context_rejected(self):
        for raw in ({"status": "incomplete"}, {"status": "completed", "output": []}):
            with self.assertRaises(AgentError):
                response_artifact(self.context, "fixture", raw)
        artifact = response_artifact(self.context, "fixture", response())
        self.assertEqual(artifact["provenance"]["provider"], "recorded-openai-response")
        with self.assertRaisesRegex(AgentError, "context"):
            StructuredProposer(artifact, {**self.context, "as_of": "future"})

    def test_evaluator_rejects_a_proposal_prepared_for_a_different_split(self):
        artifact = response_artifact(self.context, "fixture", response())
        proposer = StructuredProposer(artifact, self.context)
        with self.assertRaisesRegex(AgentError, "actual development split"):
            research(self.snapshot, self.champion, Costs(), self.store, proposer, holdout_fraction=.3)
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM experiments").fetchone()[0], 0)
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM holdouts").fetchone()[0], 0)

    @patch.dict("os.environ", {}, clear=True)
    def test_missing_credentials_do_not_consume_attempt(self):
        with self.assertRaisesRegex(AgentError, "OPENAI_API_KEY"):
            generate_proposal(self.context, self.store, "test-model")
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM proposal_requests").fetchone()[0], 0)

    def test_authenticated_redirect_and_unknown_destination_are_blocked(self):
        request = Request("https://api.openai.com/v1/responses", headers={"Authorization": "Bearer test"})
        with self.assertRaisesRegex(AgentError, "redirects"):
            NoRedirect().redirect_request(request, None, 302, "redirect", {}, "https://unknown.example/")
        with self.assertRaisesRegex(AgentError, "destination"):
            open_authenticated(Request("https://unknown.example/"), 1)


if __name__ == "__main__":
    unittest.main()
