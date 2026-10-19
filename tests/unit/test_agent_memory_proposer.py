"""Released failure memory changes only the registered challenger subset."""

import json
from dataclasses import replace

import pytest
from test_agent_commitment import market, policy, staged
from trading_agent.contracts import AgentError
from trading_agent.logic import LogicProposer
from trading_agent.store import Store

from momentum.agent.contracts import AgentPolicy, policy_from_dict
from momentum.agent.proposals import MemoryAwareProposer
from momentum.agent.service import ResearchAgent


def memory(version, *, code="FINAL_UNDERPERFORM", study="a", cycle="c0", **extra):
    return {
        "snapshot_id": f"{cycle}-{code}",
        "source_cycle_id": cycle,
        "source_phase": "final",
        "failure_code": code,
        "study": study,
        "eligible_for_proposer": True,
        "released_at": "2024-01-01T00:00:00+00:00",
        "payload": {"selected_version": version},
        **extra,
    }


def proposer(p, memories, ordinal=1, resolve=lambda _: (None, 0)):
    return MemoryAwareProposer(
        LogicProposer(p.logic), memories, ordinal, "a", p, resolve
    )


@pytest.mark.parametrize("code", ["FINAL_UNDERPERFORM", "INSUFFICIENT_EVIDENCE"])
def test_suppression_cooldown_and_original_space(code):
    p = replace(
        policy(), memory_policy="suppress_failed_final_v1", memory_cooldown_cycles=1
    )
    champion = p.logic.champion
    original = LogicProposer(p.logic).propose(champion)
    x = original[0].version
    m = memory(x, code=code)
    wrapped = proposer(p, [m])
    assert wrapped.propose(champion) == []
    assert wrapped.metadata["suppressed"] == {x: [m["snapshot_id"]]}
    assert proposer(p, [m], 2).propose(champion) == original
    assert {s.version for s in wrapped.propose(champion)} <= {
        s.version for s in original
    }


def test_resolution_unresolved_and_released_only():
    p = replace(policy(), memory_policy="suppress_failed_final_v1")
    x = LogicProposer(p.logic).propose(p.logic.champion)[0].version
    missing = memory(x, payload={})
    wrapped = proposer(p, [missing])
    assert wrapped.propose(p.logic.champion)
    assert wrapped.metadata["unresolved"] == [missing["snapshot_id"]]
    assert (
        proposer(p, [missing], resolve=lambda _: (x, 0)).propose(p.logic.champion) == []
    )
    for extra in (
        {"eligible_for_proposer": False},
        {"market": "US"},
        {"family_lineage_id": "other"},
    ):
        assert proposer(p, [memory(x, **extra)]).propose(p.logic.champion)
    dev = memory(x, source_phase="development", code="DEV_RISK")
    assert proposer(p, [dev]).propose(p.logic.champion)


def test_other_study_counts_distinct_final_cycles_in_release_order():
    p = replace(
        policy(), memory_policy="suppress_failed_final_v1", memory_cooldown_cycles=1
    )
    x = LogicProposer(p.logic).propose(p.logic.champion)[0].version
    old = memory(x, study="b", cycle="old")
    recent = memory(
        "another-version",
        study="c",
        cycle="recent",
        released_at="2024-01-02T00:00:00+00:00",
    )
    duplicate = {**recent, "snapshot_id": "second-code"}
    assert proposer(p, [old, recent, duplicate]).propose(p.logic.champion)
    assert proposer(p, [old]).propose(p.logic.champion) == []
    # Two codes from one newer cycle leave the second cooldown slot available.
    assert (
        proposer(
            replace(p, memory_cooldown_cycles=2), [old, recent, duplicate]
        ).propose(p.logic.champion)
        == []
    )


@pytest.mark.parametrize(
    "values",
    [
        {"memory_policy": "bad"},
        {"memory_cooldown_cycles": 0},
        {"memory_cooldown_cycles": True},
    ],
)
def test_invalid_memory_policy(values):
    with pytest.raises(AgentError):
        replace(AgentPolicy(), **values)


def test_legacy_policy_and_none_order():
    p = policy_from_dict({})
    assert p.memory_policy == "none"
    original = LogicProposer(p.logic)
    wrapped = proposer(p, [memory(original.propose(p.logic.champion)[0].version)])
    assert wrapped.propose(p.logic.champion) == original.propose(p.logic.champion)
    assert wrapped.metadata == original.metadata


def test_none_matches_original_promotion_semantics(tmp_path, monkeypatch):
    data, p = market(), policy()
    results = []
    for legacy in (True, False):
        with monkeypatch.context() as patch:
            if legacy:
                patch.setattr(
                    MemoryAwareProposer,
                    "propose",
                    lambda self, champion: self.proposer.propose(champion),
                )
            store = Store(tmp_path / f"{legacy}.sqlite3")
            agent = ResearchAgent(store)
            agent.init("a", p)
            report = agent.tick("a", data)
            evidence = json.loads(
                store.db.execute(
                    "SELECT evidence_json FROM agent_cycles ORDER BY ordinal"
                ).fetchone()[0]
            )
            result = report["cycles"][0]
            results.append(
                (
                    [(t["version"], t.get("score")) for t in evidence["trials"]],
                    evidence["selected_version"],
                    {
                        key: result[key]
                        for key in (
                            "champion_after",
                            "champion_before",
                            "revision_after",
                            "gates",
                            "evidence_grade",
                        )
                    },
                )
            )
            store.close()
    assert results[0] == results[1]
    assert results[1][2]["champion_after"]["lookback"] == 2
    assert results[1][2]["revision_after"] == 1
    assert all(results[1][2]["gates"].values())
    assert results[1][2]["evidence_grade"] == "E1"


def test_final_release_drives_next_commitment_and_all_suppressed_keep(
    tmp_path, monkeypatch
):
    data = market(count=430)
    p = replace(
        policy(),
        holdout_sessions=40,
        memory_policy="suppress_failed_final_v1",
        memory_cooldown_cycles=1,
    )
    store = Store(tmp_path / "release.sqlite3")
    agent = ResearchAgent(store)
    agent.init("a", p)
    finish = agent._finish
    monkeypatch.setattr(
        agent, "_finish", lambda *a: (_ for _ in ()).throw(RuntimeError("stop"))
    )
    with pytest.raises(RuntimeError, match="stop"):
        agent.tick("a", data)
    assert not any(m["source_phase"] == "final" for m in agent.development_memory())
    monkeypatch.setattr(agent, "_finish", finish)
    row = store.db.execute("SELECT * FROM agent_cycles").fetchone()
    cycle = row["id"]
    x_before_release = json.loads(row["evidence_json"])["selected_version"]
    sealed_dev = agent._develop(
        data,
        p,
        p.logic.champion,
        agent._plan(data, p, row),
        agent.development_memory(),
        1,
        "a",
    )
    assert x_before_release in {t["version"] for t in sealed_dev["trials"]}
    assert sealed_dev["proposal"]["suppressed"] == {}
    agent.recover(cycle)
    assert agent._resolve_proposal_source(cycle) == (x_before_release, 0)
    final = [m for m in agent.development_memory() if m["source_phase"] == "final"]
    assert final
    x = final[0]["payload"]["selected_version"]
    agent.tick("a", data, max_cycles=3)
    rows = store.db.execute(
        "SELECT ordinal,payload_json,commitment_id FROM agent_commitments ORDER BY ordinal"
    ).fetchall()
    second = json.loads(rows[1]["payload_json"])
    dev = second["development_evidence"]
    assert x not in {t["version"] for t in dev["trials"]}
    assert dev["selected_version"] == dev["champion_version"]
    assert set(dev["proposal"]["suppressed"][x]) == {m["snapshot_id"] for m in final}
    assert set(dev["proposal"]["suppressed"][x]) <= set(second["proposer_inputs"])
    assert any(
        m["failure_code"] == "DEV_NO_IMPROVEMENT"
        and m["source_cycle_id"] == rows[1]["commitment_id"]
        for m in agent.development_memory()
    )
    third = json.loads(rows[2]["payload_json"])
    assert x in {t["version"] for t in third["development_evidence"]["trials"]}
    store.close()


def test_remaining_candidates_keep_original_order_and_space():
    p = replace(
        policy(),
        logic=replace(policy().logic, lookbacks=(2, 5, 10, 20)),
        memory_policy="suppress_failed_final_v1",
    )
    original = LogicProposer(p.logic).propose(p.logic.champion)
    wrapped = proposer(p, [memory(original[1].version)])
    proposed = wrapped.propose(p.logic.champion)
    assert proposed == [original[0], original[2]]
    assert {s.version for s in proposed} < {s.version for s in original}
    assert "ordering_basis" not in wrapped.metadata


def test_commitment_fallback_and_single_memory_read(tmp_path, monkeypatch):
    data = market()
    p = replace(policy(), memory_policy="suppress_failed_final_v1")
    store = Store(tmp_path / "fallback.sqlite3")
    agent = ResearchAgent(store)
    agent.init("a", p)
    read = agent.development_memory
    calls = []

    def recording_read(market="KRX"):
        result = read(market)
        calls.append(result)
        return result

    monkeypatch.setattr(agent, "development_memory", recording_read)
    agent.tick("a", staged(data, p))
    assert calls == [[]]
    row = store.db.execute("SELECT * FROM agent_commitments").fetchone()
    payload = json.loads(row["payload_json"])
    x = payload["selected_version"]
    assert agent._resolve_proposal_source(row["commitment_id"]) == (x, 0)
    assert agent._resolve_proposal_source("unknown") == (None, None)
    m = memory(x, cycle=row["commitment_id"], payload={})
    wrapped = proposer(p, [m], resolve=agent._resolve_proposal_source)
    assert wrapped.propose(p.logic.champion) == []
    assert wrapped.metadata["unresolved"] == []
    preferred = proposer(
        p,
        [memory("payload-version", cycle=row["commitment_id"])],
        resolve=agent._resolve_proposal_source,
    )
    assert preferred.propose(p.logic.champion)
    assert preferred.metadata["suppressed"] == {
        "payload-version": [f"{row['commitment_id']}-FINAL_UNDERPERFORM"]
    }
    assert payload["proposer_inputs"] == []
    store.close()
