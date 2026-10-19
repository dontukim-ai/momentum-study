"""ExecutionRelease regressions: paper-only, E3-gated, no order placement."""

import json
from dataclasses import replace
from decimal import Decimal as D

import pytest
from test_agent_release import market, policy, staged
from trading_agent.contracts import AgentError, session_time
from trading_agent.store import Store

from momentum.agent.contracts import AgentPolicy
from momentum.agent.service import ResearchAgent


def build(tmp_path, *, min_sessions=5, spread_mode="quoted"):
    p = replace(
        policy(),
        observation_checkpoint_sessions=5,
        degradation_reference="cash",
        execution_min_observation_sessions=min_sessions,
    )
    if spread_mode != "quoted":
        logic = p.logic
        p = replace(
            p,
            logic=replace(
                logic,
                selection=replace(logic.selection, spread_mode=spread_mode),
                costs=replace(logic.costs, slippage_bps=D(25)),
            ),
        )
    data = market(284)
    now = [session_time(staged(data, p).sessions[-1], 17)]
    store = Store(tmp_path / "exec.sqlite3")
    agent = ResearchAgent(store, clock=lambda: now[0])
    agent.init("a", p)
    agent.tick("a", staged(data, p))
    now[0] = session_time(data.sessions[-1], 17)
    assert agent.tick("a", data)["cycles"][0]["research_champion_updated"]
    observation = agent.register_observation_release(approved_by="reviewer")
    return agent, store, now, observation


def observe_more(agent, now, count=300):
    data = market(count)
    now[0] = session_time(data.sessions[-1], 17)
    return agent.tick("a", data, max_cycles=2)


def test_policy_field_defaults_and_validation():
    assert AgentPolicy().execution_min_observation_sessions == 60
    with pytest.raises(AgentError, match="execution observation"):
        AgentPolicy(execution_min_observation_sessions=0)


def test_requires_e3_observation_after_approval(tmp_path):
    agent, store, now, _ = build(tmp_path, min_sessions=5)
    with pytest.raises(AgentError, match="Insufficient E3 observation: 0 of 5"):
        agent.register_execution_release(approved_by="human", capital_scope="500000")
    observe_more(agent, now)
    release = agent.register_execution_release(approved_by="human", capital_scope="500000")
    payload = json.loads(
        store.db.execute("SELECT payload_json FROM agent_execution_releases").fetchone()[0]
    )
    assert payload["e3_window"]["observed_sessions"] >= 5
    assert payload["e3_window"]["min_sessions"] == 5
    assert payload["e3_window"]["start"] > release["observation_approved_at"][:10]
    assert payload["target_mode"] == "paper"
    assert payload["capital_scope"] == "500000"
    assert payload["orders_submitted"] == 0
    assert payload["rollback_release_id"] is None
    store.close()


def test_minimum_not_met_with_short_observation(tmp_path):
    agent, store, now, _ = build(tmp_path, min_sessions=50)
    observe_more(agent, now)
    with pytest.raises(AgentError, match="Insufficient E3 observation"):
        agent.register_execution_release(approved_by="human", capital_scope="500000")
    store.close()


def test_paper_only_capital_limits_and_approver(tmp_path):
    agent, store, now, _ = build(tmp_path)
    observe_more(agent, now)
    with pytest.raises(AgentError, match="Live execution release is outside"):
        agent.register_execution_release(
            approved_by="human", capital_scope="500000", target_mode="live"
        )
    for capital in ("0", "-1", "1000001", "100.5", "abc"):
        with pytest.raises(AgentError, match="capital scope"):
            agent.register_execution_release(approved_by="human", capital_scope=capital)
    with pytest.raises(AgentError):
        agent.register_execution_release(approved_by=" ", capital_scope="500000")
    store.close()


def test_requires_active_and_execution_eligible_observation(tmp_path):
    agent, store, now, observation = build(tmp_path, spread_mode="historical_unavailable")
    observe_more(agent, now)
    with pytest.raises(AgentError, match="not execution eligible"):
        agent.register_execution_release(approved_by="human", capital_scope="500000")
    agent.stop_observation_release(observation["observation_release_id"], stopped_by="human")
    with pytest.raises(AgentError, match="requires an active observation release"):
        agent.register_execution_release(approved_by="human", capital_scope="500000")
    store.close()


def test_idempotent_conflict_stop_and_rollback(tmp_path):
    agent, store, now, _ = build(tmp_path)
    observe_more(agent, now)
    first = agent.register_execution_release(approved_by="human", capital_scope="500000")
    assert agent.register_execution_release(approved_by="human", capital_scope="500000") == first
    with pytest.raises(AgentError, match="Active execution release exists"):
        agent.register_execution_release(approved_by="human", capital_scope="400000")
    event = agent.stop_execution_release(
        first["execution_release_id"], stopped_by="human", reason="RECONCILIATION_MISMATCH"
    )
    assert event["reason"] == "RECONCILIATION_MISMATCH"
    with pytest.raises(AgentError, match="not active"):
        agent.stop_execution_release(first["execution_release_id"], stopped_by="human")
    second = agent.register_execution_release(approved_by="human", capital_scope="400000")
    assert second["rollback_release_id"] == first["execution_release_id"]
    status = agent.status("a")
    assert status["execution_release"]["execution_release_id"] == second["execution_release_id"]
    assert status["last_execution_release_event"]["reason"] == "RECONCILIATION_MISMATCH"
    store.close()


def test_stops_automatically_when_observation_release_stops(tmp_path):
    agent, store, now, observation = build(tmp_path)
    observe_more(agent, now)
    release = agent.register_execution_release(approved_by="human", capital_scope="500000")
    agent.stop_observation_release(observation["observation_release_id"], stopped_by="human")
    report = observe_more(agent, now, 310)
    assert [e["reason"] for e in report["execution_release_events"]] == [
        "OBSERVATION_RELEASE_STOPPED"
    ]
    assert report["execution_release_events"][0]["execution_release_id"] == (
        release["execution_release_id"]
    )
    assert agent.status("a")["execution_release"] is None
    # Registry and champion are untouched by an execution stop.
    assert agent.status("a")["revision"] == 1
    store.close()


def test_cli_round_trip(tmp_path, monkeypatch):
    from momentum.agent import cli

    agent, store, now, _ = build(tmp_path)
    observe_more(agent, now)
    store.close()

    class Fixed(cli.ResearchAgent):
        def __init__(self, s, **kwargs):
            super().__init__(s, clock=lambda: now[0])

    monkeypatch.setattr(cli, "ResearchAgent", Fixed)
    common = ["--db", str(tmp_path / "exec.sqlite3"), "--output", str(tmp_path / "out")]
    created = cli.run(
        cli.parser().parse_args(
            ["release-execution", *common, "--approved-by", "human", "--capital-scope", "300000"]
        )
    )
    assert created["capital_scope"] == "300000"
    stopped = cli.run(
        cli.parser().parse_args(
            [
                "release-execution-stop",
                *common,
                "--release-id",
                created["execution_release_id"],
                "--stopped-by",
                "human",
            ]
        )
    )
    assert stopped["reason"] == "HUMAN"


def test_execution_release_module_places_no_orders():
    from pathlib import Path

    source = Path("src/momentum/agent/service.py").read_text(encoding="utf-8")
    for forbidden in ("kis_real", "paper_cli", "place_order", "submit_order", "OrderIntent"):
        assert forbidden not in source
