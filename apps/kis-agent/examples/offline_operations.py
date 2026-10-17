"""Engineered release/monitoring demonstration; zero external calls or orders."""

from __future__ import annotations

import argparse
from pathlib import Path

from fixtures.markets import release_market
from trading_agent.cli import save_json
from trading_agent.contracts import (
    AgentError,
    Costs,
    D,
    StrategySpec,
    implementation_id,
    session_time,
)
from trading_agent.data import Snapshot, write_csv
from trading_agent.diagnostics import doctor
from trading_agent.releases import (
    activate_shadow,
    load_shadow_policy,
    observe_shadow,
    shadow_status,
    stage_release,
)
from trading_agent.research import BoundedLookbacks, research
from trading_agent.store import Store


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("artifacts/operations-v5"))
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    forward = release_market()
    evaluation = Snapshot(list(forward.bars[:400]))
    store = Store(args.output / "agent.sqlite3")
    try:
        report = research(evaluation, StrategySpec(20, 1, D(".4")), Costs(), store,
                          BoundedLookbacks((2,)), initial_cash=D(10000))
        if report["verdict"] != "ELIGIBLE_FOR_PAPER_REVIEW":
            raise AgentError("Engineered test fixture no longer reaches the eligible branch; inspect the implementation")
        risk, monitor = load_shadow_policy(Path("configs/shadow.example.json"))
        staged = stage_release(store, report["run_id"], evaluation, risk, monitor)
        store.create_account("engineered-shadow", D(10000))
        existing = store.db.execute("SELECT 1 FROM shadow_slots WHERE slot='engineered-cycle'").fetchone()
        if not existing:
            activate_shadow(store, "engineered-cycle", staged["release_id"], "engineered-shadow", 0,
                            "offline-example", "Engineered software fixture only; no market evidence or order authorization")
        first_day, next_day = forward.sessions[400:402]
        observed = observe_shadow(store, "engineered-cycle", forward, first_day, session_time(first_day, 16))
        stale = observe_shadow(store, "engineered-cycle", forward, first_day, session_time(first_day, 20))
        path = store.path
        store.close()
        store = Store(path)
        latched = observe_shadow(store, "engineered-cycle", forward, next_day, session_time(next_day, 16))
        status = shadow_status(store, "engineered-cycle")
        orders = store.db.execute("SELECT count(*) FROM orders").fetchone()[0]
        validation = {"mode": "ENGINEERED_OFFLINE_OPERATIONS", "implementation_id": implementation_id(),
                      "market_evidence": False, "network_calls": 0, "orders_created": orders,
                      "release_id": staged["release_id"], "first_observation": observed["state"],
                      "stale_data_stops": "STALE_FINAL_BARS" in stale["problems"],
                      "stop_survives_restart": "STOP_LATCHED" in latched["problems"],
                      "openai_service_verified": False, "kis_service_verified": False,
                      "notes": ["Repeating cycle fixture is engineered to exercise successful registration",
                                "Historical observation timestamps do not represent a forward market trial",
                                "Shadow observes local journal holdings without executing or simulating fills"]}
        assert validation["first_observation"] == "OBSERVING" and orders == 0
        assert validation["stale_data_stops"] and validation["stop_survives_restart"]
        write_csv(evaluation, args.output / "evaluation.csv")
        write_csv(forward, args.output / "forward-fixture.csv")
        for name, value in (("research", report), ("release", staged), ("observed", observed),
                            ("stale", stale), ("status", status), ("doctor", doctor()), ("validation", validation)):
            save_json(args.output / (name + ".json"), value)
        print(f"Offline operations passed: {args.output / 'validation.json'}")
    finally:
        store.close()


if __name__ == "__main__":
    main()
