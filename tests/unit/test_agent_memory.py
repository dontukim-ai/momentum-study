"""Economic and persistence regressions for the unified research runtime."""

import json
from dataclasses import replace
from datetime import date, timedelta
from decimal import Decimal as D

import pytest
from trading_agent.contracts import AgentError, Bar, session_time
from trading_agent.data import Snapshot
from trading_agent.logic import LogicSettings
from trading_agent.selection import RankedStrategy, SecurityHistory, SecurityState
from trading_agent.store import Store

from momentum.agent.contracts import AgentPolicy, ResearchInput
from momentum.agent.service import ResearchAgent


def market(count=360, *, cycle=True):
    bars, states = [], []
    day, close = date(2024, 1, 2), D(10000)
    while len(bars) < count:
        if day.weekday() < 5:
            opening = close
            close = (
                (opening * (D("1.02") if len(bars) % 20 < 10 else D(".98"))).quantize(
                    D(1)
                )
                if cycle
                else close
            )
            b = Bar(
                day,
                "FIXTURE",
                opening,
                max(opening, close),
                min(opening, close),
                close,
                100000,
                session_time(day, 15, 40),
                "ENGINEERED_SOFTWARE_FIXTURE",
            )
            bars.append(b)
            states.append(
                SecurityState(
                    "FIXTURE",
                    session_time(day, 15, 30),
                    session_time(day, 15, 40),
                    "NORMAL",
                    "STOCK",
                    "KOSPI",
                    False,
                    False,
                    False,
                    False,
                    close * D(".9995"),
                    close * D("1.0005"),
                    "ENGINEERED_SOFTWARE_FIXTURE",
                )
            )
        day += timedelta(days=1)
    return ResearchInput(Snapshot(bars), SecurityHistory(states))


def policy():
    return AgentPolicy(
        logic=LogicSettings(
            champion=RankedStrategy(20), lookbacks=(2, 20), rankings=("momentum",)
        ),
        holdout_sessions=80,
    )


def staged(data, p):
    end = p.warmup_sessions + p.development_sessions - 1
    return replace(data.prefix(data.sessions[end]), calendar=data.sessions)


def memory_agent(tmp_path, data, p):
    store = Store(tmp_path / "memory.sqlite3")
    agent = ResearchAgent(store, clock=lambda: session_time(data.sessions[-1], 17))
    agent.init("a", p)
    return store, agent


def test_development_sealed_memory_and_proposer_inputs(tmp_path):
    data, p = market(cycle=False), policy()
    store, agent = memory_agent(tmp_path, data, p)
    assert agent.development_memory() == []
    agent.tick("a", staged(data, p))
    memories = agent.development_memory()
    assert "DEV_NO_IMPROVEMENT" in {m["failure_code"] for m in memories}
    assert all(m["source_phase"] == "development" for m in memories)
    assert all(
        m["eligible_for_proposer"] and m["consumed_windows"] == [] for m in memories
    )
    assert all(m["sealed_at"] == m["released_at"] for m in memories)
    payload = json.loads(
        store.db.execute("SELECT payload_json FROM agent_commitments").fetchone()[0]
    )
    assert payload["proposer_inputs"] == []
    before = memories
    agent.tick("a", staged(data, p))
    assert agent.development_memory() == before
    assert agent.status("a")["memory"]["snapshots"] == len(memories)
    assert agent.status("a")["memory"]["released"] == len(memories)
    assert agent.development_memory("US") == []
    store.close()


def test_final_memory_only_after_complete_and_abandoned_has_none(tmp_path, monkeypatch):
    data, p = market(), replace(policy(), holdout_sessions=40)
    store, agent = memory_agent(tmp_path, data, p)
    agent.tick("a", staged(data, p))
    assert not any(m["source_phase"] == "final" for m in agent.development_memory())
    original = agent._finish
    monkeypatch.setattr(
        agent, "_finish", lambda *args: (_ for _ in ()).throw(RuntimeError("stop"))
    )
    with pytest.raises(RuntimeError):
        agent.tick("a", data)
    assert not any(m["source_phase"] == "final" for m in agent.development_memory())
    monkeypatch.setattr(agent, "_finish", original)
    cycle_id = store.db.execute("SELECT id FROM agent_cycles").fetchone()[0]
    agent.recover(cycle_id)
    final = [m for m in agent.development_memory() if m["source_phase"] == "final"]
    assert "INSUFFICIENT_EVIDENCE" in {m["failure_code"] for m in final}
    assert all(m["consumed_windows"] for m in final)
    assert all("daily_returns" not in json.dumps(m["payload"]) for m in final)
    before = agent.development_memory()
    agent.recover(cycle_id)
    assert agent.development_memory() == before
    store.close()


def test_abandoned_has_no_final_memory(tmp_path, monkeypatch):
    data, p = market(), policy()
    store, agent = memory_agent(tmp_path, data, p)
    monkeypatch.setattr(
        agent, "_evaluate", lambda *args: (_ for _ in ()).throw(RuntimeError("stop"))
    )
    with pytest.raises(RuntimeError):
        agent.tick("a", data)
    cycle_id = store.db.execute("SELECT id FROM agent_cycles").fetchone()[0]
    agent.abandon(cycle_id, "interrupted")
    assert all(m["source_phase"] == "development" for m in agent.development_memory())
    store.close()


def test_shadow_closed_snapshot_is_immutable(tmp_path, monkeypatch):
    data, p = market(), policy()
    store, agent = memory_agent(tmp_path, data, p)
    end = (
        p.warmup_sessions
        + p.development_sessions
        + p.embargo_sessions
        + p.holdout_sessions
    )
    # Force a degraded report using the evaluator's drawdown metric, leaving
    # the observation count and the runtime sealing decision under test.
    from momentum.agent.evaluator import CommonEvaluator

    original = CommonEvaluator.evaluate

    def degraded(self, *args, **kwargs):
        result = original(self, *args, **kwargs)
        if args[2] >= data.sessions[end]:
            return replace(result, max_drawdown=1.0)
        return result

    monkeypatch.setattr(CommonEvaluator, "evaluate", degraded)
    report = agent.tick("a", data.prefix(data.sessions[end + 4]))
    assert report["shadow"]["deterioration_detected"]
    assert report["shadow"]["observations"] < p.shadow_sessions
    assert not any(
        m["failure_code"] == "TRACKING_DEGRADED" for m in agent.development_memory()
    )
    report = agent.tick("a", data.prefix(data.sessions[end + p.shadow_sessions - 1]))
    assert report["shadow"]["observations"] == p.shadow_sessions
    snapshots = [
        dict(r)
        for r in store.db.execute(
            "SELECT * FROM agent_failure_snapshots WHERE source_phase='observation'"
        )
    ]
    assert len(snapshots) == 1
    assert any(
        m["failure_code"] == "TRACKING_DEGRADED" for m in agent.development_memory()
    )
    agent.tick("a", data)
    assert [
        dict(r)
        for r in store.db.execute(
            "SELECT * FROM agent_failure_snapshots WHERE source_phase='observation'"
        )
    ] == snapshots
    store.close()


def test_released_only_order_hashes_and_next_proposer_inputs(tmp_path):
    from trading_agent.contracts import canonical, digest

    data, p = market(cycle=False), policy()
    store, agent = memory_agent(tmp_path, data, p)
    payload = {"reason": "sealed synthetic defect"}
    result_hash = digest(payload)
    identity = {
        "source_cycle_id": "prior",
        "source_phase": "development",
        "failure_code": "DATA_DEFECT",
        "observed_through": "2023-01-03",
        "result_hash": result_hash,
    }
    snapshot_id = digest(identity)
    store.db.execute(
        "INSERT INTO agent_failure_snapshots VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (
            snapshot_id,
            "a",
            "prior",
            "development",
            "DATA_DEFECT",
            "2023-01-02",
            "2023-01-03",
            "2023-01-03",
            canonical(payload),
            result_hash,
            "2023-01-04T00:00:00+00:00",
        ),
    )
    assert agent.development_memory() == []
    store.db.execute(
        "INSERT INTO agent_memory_releases VALUES(?,?,?)",
        (snapshot_id, "2023-01-04T00:00:00+00:00", "development_sealed"),
    )
    agent.tick("a", staged(data, p))
    commitment = json.loads(
        store.db.execute("SELECT payload_json FROM agent_commitments").fetchone()[0]
    )
    assert commitment["proposer_inputs"] == [snapshot_id]
    assert agent.development_memory()[0]["snapshot_id"] == snapshot_id
    for row in store.db.execute("SELECT * FROM agent_failure_snapshots"):
        assert row["result_hash"] == digest(json.loads(row["payload_json"]))
        assert row["snapshot_id"] == digest({key: row[key] for key in identity})
    store.close()


def test_data_defect_and_dev_risk_sealed_at_commitment(tmp_path, monkeypatch):
    from momentum.agent.evaluator import CommonEvaluator

    data, p = market(), policy()
    store, agent = memory_agent(tmp_path, data, p)
    original = CommonEvaluator.evaluate

    def defective(self, data, spec, *args, **kwargs):
        if spec.lookback == 2:
            raise AgentError("synthetic missing input")
        return original(self, data, spec, *args, **kwargs)

    monkeypatch.setattr(CommonEvaluator, "evaluate", defective)
    agent.tick("a", staged(data, p))
    memory = agent.development_memory()
    defect = next(m for m in memory if m["failure_code"] == "DATA_DEFECT")
    assert "synthetic missing input" in json.dumps(defect["payload"])
    assert any(m["failure_code"] == "DEV_RISK" for m in memory)
    assert {
        r[0] for r in store.db.execute("SELECT rule FROM agent_memory_releases")
    } == {"development_sealed"}
    store.close()


def test_final_gate_codes_and_atomic_release(tmp_path, monkeypatch):
    from trading_agent.contracts import digest

    from momentum.agent.service import promotion_gates

    data, p = market(), policy()
    store, agent = memory_agent(tmp_path, data, p)
    evaluate = agent._evaluate
    seal = agent._seal_memory

    def failing_final(*args):
        # Supply explicit aggregate gate evidence to exercise both independent
        # failure classifications, without changing the selection or thresholds.
        evidence = evaluate(*args)
        evidence["holdout"].update(
            candidate_return=-1.0, stress_improvement=-1.0, interval=(-1.0, 1.0)
        )
        evidence["gates"] = promotion_gates(evidence, p)
        return evidence

    def interrupted_seal(*args):
        seal(*args)
        if args[2] == "final":
            raise RuntimeError("interrupt release transaction")

    monkeypatch.setattr(agent, "_evaluate", failing_final)
    monkeypatch.setattr(agent, "_seal_memory", interrupted_seal)
    with pytest.raises(RuntimeError, match="interrupt release"):
        agent.tick("a", data)
    row = store.db.execute("SELECT * FROM agent_cycles").fetchone()
    assert row["status"] == "EVALUATED"
    assert not any(m["source_phase"] == "final" for m in agent.development_memory())
    assert (
        store.db.execute(
            "SELECT count(*) FROM agent_failure_snapshots WHERE source_phase='final'"
        ).fetchone()[0]
        == 0
    )
    monkeypatch.setattr(agent, "_seal_memory", seal)
    agent.recover(row["id"])
    final = [m for m in agent.development_memory() if m["source_phase"] == "final"]
    assert {m["failure_code"] for m in final} == {
        "FINAL_UNDERPERFORM",
        "INSUFFICIENT_EVIDENCE",
    }
    assert all(m["source_cycle_id"] == row["id"] for m in final)
    assert all(m["observed_through"] == m["source_window_end"] for m in final)
    assert all(m["result_hash"] == digest(m["payload"]) for m in final)
    assert {
        r[0]
        for r in store.db.execute(
            "SELECT rule FROM agent_memory_releases r JOIN agent_failure_snapshots s USING(snapshot_id) WHERE s.source_phase='final'"
        )
    } == {"final_decision_confirmed"}
    store.close()
