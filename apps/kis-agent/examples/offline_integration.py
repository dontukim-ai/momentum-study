"""End-to-end development tool using only synthetic data and handwritten fixtures."""

from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from fixtures.paper_exchange import FixtureExchange
from trading_agent.cli import save_json
from trading_agent.contracts import (
    KST,
    Costs,
    D,
    OrderIntent,
    Quote,
    StrategySpec,
    implementation_id,
    session_time,
)
from trading_agent.data import synthetic_snapshot, write_csv
from trading_agent.execution import OMS, RiskPolicy
from trading_agent.kis_paper import KisPaperBroker, PaperSession
from trading_agent.proposals import (
    StructuredProposer,
    development_context,
    response_artifact,
)
from trading_agent.research import research
from trading_agent.store import Store


def demo(output: Path) -> dict:
    output.mkdir(parents=True, exist_ok=True)
    snapshot = synthetic_snapshot(220)
    champion = StrategySpec()
    context = development_context(snapshot, champion)
    recorded = json.loads(Path("fixtures/llm-response.json").read_text())
    artifact = response_artifact(context, "handwritten-fixture", recorded)
    with TemporaryDirectory() as directory:
        research_store = Store(Path(directory) / "research.sqlite3")
        try:
            report = research(snapshot, champion, Costs(), research_store, StructuredProposer(artifact, context))
        finally:
            research_store.close()
        day = datetime.now(KST).date()
        while day.weekday() >= 5:
            day -= timedelta(days=1)
        now = session_time(day, 10)
        exchange = FixtureExchange(now)
        with patch.dict(os.environ, {"KIS_PAPER_APP_KEY": "offline-placeholder", "KIS_PAPER_APP_SECRET": "offline-placeholder",
                                     "KIS_PAPER_ACCOUNT": "12345678-01"}):
            session = PaperSession(exchange, .1)
        database = Path(directory) / "paper.sqlite3"
        store = Store(database)
        broker = KisPaperBroker(store, session, "offline-paper", {"123456": D(1)})
        broker.bootstrap()
        costs = Costs(D(0), D(0), D(0))
        policy = RiskPolicy(("123456",), D(".8"), D(".4"), D(20000))
        oms = OMS(store, broker, policy, costs)
        quotes = {"123456": Quote("123456", D(400), now)}
        def intent(key, quantity):
            return OrderIntent(key, "offline-paper", "123456", "BUY", quantity, D(400), now,
                               now + timedelta(minutes=2), "offline-review-only", "offline-synthetic-intent")
        first = intent("partial-fill", 3)
        oms.reserve(first, quotes, now)
        oms.submit(first.key, quotes, now)
        exchange.fill("00000001", 1, D(333))
        oms.reconcile("offline-paper")
        exchange.fill("00000001", 2, D(667))
        oms.reconcile("offline-paper")
        second = intent("response-loss", 1)
        exchange.drop_order_response = True
        oms.reserve(second, quotes, now)
        assert oms.submit(second.key, quotes, now)["state"] == "UNKNOWN"
        unknown = oms.reconcile("offline-paper")
        assert unknown["unresolved"] == [second.key]
        oms.submit(second.key, quotes, now)
        assert len(exchange.rows) == 2
        reviewed_id = broker.adopt(second, day, "12345", "00000002")
        store.db.execute("UPDATE orders SET broker_id=? WHERE key=?", (reviewed_id, second.key))
        oms.reconcile("offline-paper")
        exchange.fill("00000002", 1, D(400))
        assert oms.reconcile("offline-paper")["matches"]
        before = store.status("offline-paper")
        store.close()
        store = Store(database)
        broker = KisPaperBroker(store, session, "offline-paper", {"123456": D(1)})
        oms = OMS(store, broker, policy, costs)
        reconciliation = oms.reconcile("offline-paper")
        after = store.status("offline-paper")
        assert before["wallet"] == after["wallet"] and before["fills"] == after["fills"] == 3
        assert after["wallet"].cash == D(98600) and after["wallet"].positions == {"123456": 4}
        broker_report = {"verification": "HANDWRITTEN_FIXTURE_ONLY", "status": after, "reconciliation": reconciliation,
                         "unknown_not_retransmitted": True, "explicit_identity_recovery": True,
                         "restart_preserved_wallet_and_fills": True,
                         "events": [dict(r) for r in store.db.execute("SELECT event,details FROM events ORDER BY id")]}
        store.close()
    write_csv(snapshot, output / "synthetic.csv")
    save_json(output / "proposal.json", artifact)
    save_json(output / "research.json", report)
    save_json(output / "broker.json", broker_report)
    summary = {"mode": "OFFLINE_FIXTURES_ONLY", "implementation_id": implementation_id(),
               "network_calls": 0, "proposal_context_ends": context["development_end"],
               "proposal_provider": artifact["provenance"]["provider"], "research_verdict": report["verdict"],
               "paper_filled_orders": after["orders"].get("FILLED", 0), "normalized_fill_events": after["fills"],
               "final_cash": str(after["wallet"].cash), "final_positions": after["wallet"].positions,
               "reconciliation_matches": reconciliation["matches"], "unresolved_submissions": len(reconciliation["unresolved"]),
               "restart_preserved_wallet_and_fills": True, "cancel_post_enabled": False}
    save_json(output / "validation.json", summary)
    (output / "report.md").write_text(
        "**Offline integration — synthetic data and handwritten API fixtures only**\n\n"
        f"Research: {report['verdict']}. Proposal provider: recorded response; no LLM API call.\n\n"
        "Two paper-shaped orders produced three normalized fill events. Exact cash: 98600; holdings: 4 fixture shares. "
        "A lost acknowledgement remained UNKNOWN until an explicit broker identity was linked. Restart did not change the wallet or fill count.\n\n"
        "No KIS authentication, market data, order service, cancelability, fee contract or tradable alpha was verified. "
        "Actual cancel POST remains disabled pending a verified paper cancelability contract.\n", encoding="utf-8")
    return summary


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=Path("artifacts/integration-v2"))
    print(json.dumps(demo(parser.parse_args().output), indent=2))
