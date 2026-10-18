"""Incremental checkpoints compared with the frozen LOOP-05b calculation."""

import json
from dataclasses import replace
from datetime import date
from decimal import Decimal
from typing import Any

import pytest
from test_agent_degradation import install_decline, market, policy, rows
from trading_agent.contracts import AgentError, aware, canonical, digest
from trading_agent.data import Snapshot
from trading_agent.logic import ranked_spec
from trading_agent.store import Store

from momentum.agent.contracts import AgentPolicy, ResearchInput
from momentum.agent.evaluator import (
    CommonEvaluator,
    require_same_contract,
)
from momentum.agent.service import FAMILY_LINEAGE_ID, ResearchAgent, build_id
from momentum.config import SpecUndefinedError


# Frozen LOOP-05b oracle: deliberately recomputes every completed window.
def full_checkpoints(
    self,
    name: str,
    data: ResearchInput,
    policy: AgentPolicy,
    observation: dict[str, Any],
) -> dict[str, Any]:
    stored = 0
    error = None
    try:
        start = date.fromisoformat(observation["start"])
        days = [
            d
            for d in data.sessions
            if start <= d <= date.fromisoformat(observation["end"])
        ]
        entries = observation["schedule"]
        registry = {
            row["revision"]: dict(row)
            for row in self.db.execute(
                "SELECT * FROM agent_champion_registry WHERE market=? AND family_lineage_id=?",
                (policy.market, FAMILY_LINEAGE_ID),
            )
        }
        schedule = [
            (
                date.fromisoformat(entry["effective_from"]),
                ranked_spec(json.loads(registry[entry["revision"]]["spec_json"])),
            )
            for entry in entries
        ]
        stream = self._observation_stream(policy.market)
        size = policy.observation_checkpoint_sessions
        previous: dict[str, Any] | None = None
        evaluator = CommonEvaluator(policy)
        for seq in range(len(days) // size):
            offset = seq * size
            window_start, end = days[offset], days[offset + size - 1]
            # Restart from the observation origin, not from a fresh wallet
            # at each window: holdings and revision-switch costs carry over.
            frozen = data.prefix(end)
            result = evaluator.evaluate_schedule(frozen, schedule, start, end)
            reference = evaluator.evaluate_benchmark(
                frozen, policy.degradation_reference, start, end
            )
            require_same_contract(result, reference)
            capital = policy.logic.selection.capital_limit
            net, drawdown = self._checkpoint_window(result, offset, size, capital)
            reference_net, _ = self._checkpoint_window(reference, offset, size, capital)
            active = next(
                e for e in reversed(entries) if e["effective_from"] <= end.isoformat()
            )
            revision = active["revision"]
            limit = policy.degradation_drawdown
            if limit is None:
                limit = policy.logic.evaluation.max_drawdown
            difference = net - reference_net
            breach = (
                drawdown > limit or difference < -policy.degradation_underperformance
            )
            consecutive = 0
            if breach:
                consecutive = 1
                if previous and previous["revision"] == revision and previous["breach"]:
                    consecutive += previous["consecutive_breaches"]
            state = "OBSERVING"
            if breach:
                state = (
                    "DEMOTION_CANDIDATE"
                    if consecutive >= policy.demotion_checkpoints
                    else "WARNING"
                )
            older = [r for r in registry if r < revision]
            target = registry[max(older)] if older else None
            payload = {
                "window_start": window_start.isoformat(),
                "window_end": end.isoformat(),
                "revision": revision,
                "spec_version": active["spec_version"],
                "window_return": net,
                "window_drawdown": drawdown,
                "reference": policy.degradation_reference,
                "reference_return": reference_net,
                "difference": difference,
                "drawdown_limit": limit,
                "underperformance_threshold": policy.degradation_underperformance,
                "demotion_checkpoints": policy.demotion_checkpoints,
                "breach": breach,
                "consecutive_breaches": consecutive,
                "demotion_target": {
                    key: target[key] for key in ("revision", "spec_version")
                }
                if target
                else None,
            }
            input_hash, result_hash = data.fingerprint(end), digest(payload)
            checkpoint_id = digest(
                {"stream": stream, "seq": seq, "result_hash": result_hash}
            )
            sealed = self._checkpoint_sealed(
                policy.market, window_start.isoformat(), end.isoformat()
            )
            with self.store.transaction():
                existing = self.db.execute(
                    "SELECT * FROM agent_observation_checkpoints WHERE stream=? AND seq=?",
                    (stream, seq),
                ).fetchone()
                if existing:
                    if (
                        existing["input_hash"] != input_hash
                        or existing["result_hash"] != result_hash
                    ):
                        raise AgentError("Stored observation checkpoint changed")
                else:
                    self.db.execute(
                        "INSERT INTO agent_observation_checkpoints VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (
                            checkpoint_id,
                            policy.market,
                            stream,
                            name,
                            seq,
                            window_start.isoformat(),
                            end.isoformat(),
                            revision,
                            active["spec_version"],
                            state,
                            int(sealed),
                            canonical(payload),
                            result_hash,
                            input_hash,
                            aware(self.clock()).isoformat(),
                        ),
                    )
                if breach and not sealed:
                    self._seal_memory(
                        existing["study"] if existing else name,
                        checkpoint_id,
                        "observation",
                        "TRACKING_" + state,
                        window_start.isoformat(),
                        end.isoformat(),
                        payload,
                    )
            stored += int(existing is None)
            previous = payload
    except (AgentError, SpecUndefinedError) as exc:
        error = str(exc)
        error_type = type(exc).__name__
    return {
        "stored": stored,
        "latest": self._latest_checkpoint(policy.market),
        **({"error": error, "error_type": error_type} if error else {}),
    }


@pytest.fixture
def setup_agent(tmp_path):
    store = Store(tmp_path / "incremental.sqlite3")
    agent = ResearchAgent(store)
    p = replace(
        policy(), observation_checkpoint_sessions=5, degradation_reference="cash"
    )
    agent.init("a", p)
    yield agent, store, p
    store.close()


def checkpoint_calls(monkeypatch, agent):
    # tick also evaluates the live observation report. Count only checkpoint
    # work, where incremental reuse is promised.
    calls = []
    active = [False]
    original = CommonEvaluator.evaluate_schedule
    checkpoint = agent._isolated_checkpoints

    def evaluate(self, *args, **kwargs):
        if active[0]:
            calls.append(args[-1])
        return original(self, *args, **kwargs)

    def isolated(*args, **kwargs):
        active[0] = True
        try:
            return checkpoint(*args, **kwargs)
        finally:
            active[0] = False

    monkeypatch.setattr(CommonEvaluator, "evaluate_schedule", evaluate)
    monkeypatch.setattr(agent, "_isolated_checkpoints", isolated)
    return calls


def test_only_new_checkpoint_is_evaluated(setup_agent, monkeypatch):
    agent, store, _ = setup_agent
    calls = checkpoint_calls(monkeypatch, agent)
    agent.tick("a", market(281))
    assert len(rows(store)) == len(calls) == 3
    assert all(
        json.loads(r["payload_json"])["build"] == build_id() for r in rows(store)
    )
    saved = rows(store)
    calls.clear()
    agent.tick("a", market(281))
    assert calls == [date.fromisoformat(saved[-1]["window_end"])]
    assert rows(store) == saved
    calls.clear()
    report = agent.tick("a", market(286))
    assert calls == [
        date.fromisoformat(saved[-1]["window_end"]),
        market(286).sessions[-1],
    ]
    assert report["observation_checkpoints"]["stored"] == 1


def test_incremental_payload_matches_loop05b(setup_agent, tmp_path, monkeypatch):
    install_decline(monkeypatch)
    agent, store, p = setup_agent
    agent.tick("a", market(276))
    report = agent.tick("a", market(286))
    # Backup preserves the same frozen registry and observation schedule.
    oracle_store = Store(tmp_path / "oracle.sqlite3")
    store.db.backup(oracle_store.db)
    oracle = ResearchAgent(oracle_store)
    oracle.db.execute("DELETE FROM agent_observation_checkpoints")
    expected = full_checkpoints(oracle, "a", market(286), p, report["observation"])
    assert "error" not in expected
    for actual, legacy in zip(rows(store), rows(oracle_store), strict=True):
        payload = json.loads(actual["payload_json"])
        payload.pop("build")
        assert payload == json.loads(legacy["payload_json"])
        for key in ("state", "result_hash", "input_hash", "checkpoint_id"):
            assert actual[key] == legacy[key]
    oracle_store.close()


def test_breach_continues_from_sealed_payload(setup_agent, monkeypatch):
    install_decline(monkeypatch)
    agent, store, _ = setup_agent
    monkeypatch.setattr(agent, "_checkpoint_sealed", lambda *args: True)
    agent.tick("a", market(276))
    assert [r["state"] for r in rows(store)] == ["WARNING", "WARNING"]
    calls = checkpoint_calls(monkeypatch, agent)
    report = agent.tick("a", market(281))
    assert len(calls) == 2
    latest = report["observation_checkpoints"]["latest"]
    assert latest["state"] == "DEMOTION_CANDIDATE"
    assert "consecutive_breaches" not in latest
    assert json.loads(rows(store)[-1]["payload_json"])["consecutive_breaches"] == 3


def test_changed_input_is_isolated_without_recalculation(setup_agent, monkeypatch):
    agent, store, _ = setup_agent
    data = market(294)
    agent.tick("a", data)
    saved = rows(store)
    bars = list(data.snapshot.bars)
    bars[286] = replace(
        bars[286], close=bars[286].close + Decimal(1), high=bars[286].high + Decimal(1)
    )
    calls = checkpoint_calls(monkeypatch, agent)
    report = agent.tick("a", replace(data, snapshot=Snapshot(bars)))
    assert (
        report["observation_checkpoints"]["error"]
        == "Stored observation checkpoint changed"
    )
    assert calls == []
    assert rows(store) == saved
    assert report["status"]["cycles"][0]["status"] == "COMPLETE"


@pytest.mark.parametrize("legacy", [False, True])
def test_other_or_missing_build_revalidates_all(setup_agent, monkeypatch, legacy):
    agent, store, p = setup_agent
    data = market(281)
    observation = agent.tick("a", data)["observation"]
    if legacy:
        for row in rows(store):
            payload = json.loads(row["payload_json"])
            payload.pop("build", None)
            store.db.execute(
                "UPDATE agent_observation_checkpoints SET payload_json=? WHERE seq=?",
                (canonical(payload), row["seq"]),
            )
    saved = rows(store)
    monkeypatch.setattr("momentum.agent.service.build_id", lambda: "different-build")
    calls = checkpoint_calls(monkeypatch, agent)
    # Study manifests have their own build lock; exercise the checkpoint path
    # directly so that this tests checkpoint compatibility, not study recovery.
    result = agent._isolated_checkpoints("a", data, p, observation)
    assert "error" not in result
    assert len(calls) == 3
    assert rows(store) == saved


def test_unsealing_releases_all_memories_with_only_latest_revalidation(
    setup_agent, monkeypatch
):
    install_decline(monkeypatch)
    agent, store, _ = setup_agent
    sealed = [True]
    monkeypatch.setattr(agent, "_checkpoint_sealed", lambda *args: sealed[0])
    agent.tick("a", market(281))
    saved = rows(store)
    ids = {row["checkpoint_id"] for row in saved}
    assert not [m for m in agent.development_memory() if m["source_cycle_id"] in ids]
    calls = checkpoint_calls(monkeypatch, agent)
    sealed[0] = False
    agent.tick("a", market(281))
    assert len(calls) == 1
    assert rows(store) == saved
    memories = [m for m in agent.development_memory() if m["source_cycle_id"] in ids]
    assert len(memories) == 3
    calls.clear()
    agent.tick("a", market(281))
    assert len(calls) == 1
    assert (
        len([m for m in agent.development_memory() if m["source_cycle_id"] in ids]) == 3
    )


def test_changed_build_rejects_changed_results(setup_agent, monkeypatch):
    agent, store, p = setup_agent
    data = market(281)
    observation = agent.tick("a", data)["observation"]
    saved = rows(store)
    install_decline(monkeypatch)
    monkeypatch.setattr("momentum.agent.service.build_id", lambda: "changed-economics")
    result = agent._isolated_checkpoints("a", data, p, observation)
    assert result["error"] == "Stored observation checkpoint changed"
    assert rows(store) == saved


def test_latest_same_build_rejects_changed_results(setup_agent, monkeypatch):
    agent, store, _ = setup_agent
    data = market(281)
    agent.tick("a", data)
    saved = rows(store)
    install_decline(monkeypatch)
    calls = checkpoint_calls(monkeypatch, agent)
    report = agent.tick("a", data)
    assert calls == [date.fromisoformat(saved[-1]["window_end"])]
    assert (
        report["observation_checkpoints"]["error"]
        == "Stored observation checkpoint changed"
    )
    assert rows(store) == saved
