"""Observation release safety regressions using synthetic evidence."""

import json
from dataclasses import replace
from datetime import date, timedelta
from decimal import Decimal as D
from pathlib import Path

import pytest
from trading_agent.contracts import AgentError, Bar, digest, session_time
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


@pytest.fixture
def released(tmp_path):
    data, p = (
        market(284),
        replace(
            policy(), observation_checkpoint_sessions=5, degradation_reference="cash"
        ),
    )
    now = [session_time(staged(data, p).sessions[-1], 17)]
    store = Store(tmp_path / "release.sqlite3")
    agent = ResearchAgent(store, clock=lambda: now[0])
    agent.init("a", p)
    agent.tick("a", staged(data, p))
    now[0] = session_time(data.sessions[-1], 17)
    report = agent.tick("a", data)
    assert report["cycles"][0]["research_champion_updated"]
    yield agent, store, data, p
    store.close()


def test_registration_payload_idempotency(released):
    agent, store, _data, p = released
    release = agent.register_observation_release(approved_by="reviewer")
    assert agent.register_observation_release(approved_by="reviewer") == release
    row = store.db.execute("SELECT * FROM agent_observation_releases").fetchone()
    payload = json.loads(row["payload_json"])
    assert row["order_authority"] == 0
    assert payload["order_authority"] is False
    assert payload["evidence_grade"] == "E2"
    assert payload["champion_revision"] == 1
    assert payload["spec_version"] == row["spec_version"]
    assert payload["starting_portfolio"] == {
        "kind": "cash_only",
        "capital": str(p.logic.selection.capital_limit),
    }
    assert payload["selector_policy_id"] == digest(p.logic.selection)
    assert payload["cost_model_id"] == digest(p.logic.costs)
    cycle = store.db.execute(
        "SELECT * FROM agent_cycles WHERE id=?", (payload["source_cycle_id"],)
    ).fetchone()
    evidence = json.loads(cycle["evidence_json"])
    assert (
        payload["evaluation_contract_id"]
        == evidence["holdout"]["candidate"]["evaluation_contract_id"]
    )
    assert payload["risk_limits_id"] == digest(
        {
            **{
                k: getattr(p.logic.selection, k)
                for k in (
                    "capital_limit",
                    "max_gross",
                    "max_position",
                    "max_order_notional",
                )
            },
            "max_drawdown": p.logic.evaluation.max_drawdown,
        }
    )
    assert payload["champion_key"] == {
        "market": "KRX",
        "family_lineage_id": "ranked-momentum",
    }
    assert payload["stop_conditions"] == ["DEMOTION_CANDIDATE", "DATA_FAILURE", "HUMAN"]
    assert payload["commitment_id"]
    assert (
        payload["data_exposure"]["through"]
        == store.db.execute(
            "SELECT max(end) FROM agent_intervals WHERE market='KRX'"
        ).fetchone()[0]
    )
    assert (
        store.db.execute("SELECT count(*) FROM agent_observation_releases").fetchone()[
            0
        ]
        == 1
    )


def test_validation_and_human_stop(released):
    agent, _store, _data, _p = released
    for actor in (" ", "x" * 101, None):
        with pytest.raises(AgentError):
            agent.register_observation_release(approved_by=actor)
    with pytest.raises(AgentError, match="current registry champion"):
        agent.register_observation_release(approved_by="human", revision=0)
    release = agent.register_observation_release(approved_by="human")
    event = agent.stop_observation_release(
        release["observation_release_id"], stopped_by="human"
    )
    assert event["reason"] == "HUMAN"
    assert agent.status("a")["observation_release"] is None
    assert agent.status("a")["last_release_event"]["reason"] == "HUMAN"
    with pytest.raises(AgentError, match="not active"):
        agent.stop_observation_release(
            release["observation_release_id"], stopped_by="human"
        )


@pytest.mark.parametrize("promote", [False, True])
def test_requires_e2(tmp_path, promote):
    data = market()
    store = Store(tmp_path / "e1.sqlite3")
    agent = ResearchAgent(store)
    agent.init("a", policy())
    if promote:
        agent.tick("a", data)
    with pytest.raises(AgentError, match="requires E2"):
        agent.register_observation_release(approved_by="human")
    store.close()


def test_revision_conflict_and_behind_registry(released):
    agent, store, _data, p = released
    release = agent.register_observation_release(approved_by="human")
    with store.transaction():
        agent._append_champion(
            "KRX",
            2,
            p.logic.champion,
            "a",
            "E2",
            release["source_cycle_id"],
            release["commitment_id"],
            1,
        )
    assert agent.status("a")["observation_release"]["behind_registry"] is True
    with pytest.raises(AgentError, match="Active observation release exists"):
        agent.register_observation_release(approved_by="human")
    agent.stop_observation_release(
        release["observation_release_id"], stopped_by="human"
    )
    assert (
        agent.register_observation_release(approved_by="human")["champion_revision"]
        == 2
    )


def test_unavailable_isolated_stop(released, monkeypatch):
    agent, _store, data, _p = released
    agent.register_observation_release(approved_by="human")

    def broken(*args):
        raise AgentError("injected observation data error")

    monkeypatch.setattr(agent, "observation_ledger", broken)
    result = agent.tick("a", data, max_cycles=1)
    assert result["release_events"][0]["reason"] == "DATA_FAILURE"
    assert result["observation"]["status"] == "unavailable"
    assert result["status"]["revision"] == 1
    assert agent.tick("a", data, max_cycles=1)["release_events"] == []


def test_no_order_imports():
    import ast

    for path in Path("src/momentum/agent").glob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            modules = (
                [node.module or ""]
                if isinstance(node, ast.ImportFrom)
                else [n.name for n in node.names]
                if isinstance(node, ast.Import)
                else []
            )
            assert not any(
                m.startswith("trading_agent.")
                and any(word in m.lower() for word in ("order", "account", "broker"))
                for m in modules
            )


@pytest.mark.parametrize("revision,sealed", [(1, False), (0, False), (1, True)])
def test_checkpoint_safety_stop(released, monkeypatch, revision, sealed):
    agent, store, data, _p = released
    agent.register_observation_release(approved_by="human")
    before = [
        tuple(r) for r in store.db.execute("SELECT * FROM agent_champion_registry")
    ]
    row = dict(
        store.db.execute(
            "SELECT * FROM agent_observation_checkpoints ORDER BY seq DESC LIMIT 1"
        ).fetchone()
    )
    row.update(
        checkpoint_id="safety", seq=100, state="DEMOTION_CANDIDATE", revision=revision
    )
    with store.transaction():
        store.db.execute(
            "INSERT INTO agent_observation_checkpoints VALUES("
            + ",".join("?" for _ in row)
            + ")",
            tuple(row.values()),
        )
    monkeypatch.setattr(
        agent,
        "_isolated_checkpoints",
        lambda *a: {"stored": 0, "latest": agent._latest_checkpoint("KRX")},
    )
    monkeypatch.setattr(agent, "_checkpoint_sealed", lambda *a: sealed)
    result = agent.tick("a", data, max_cycles=1)
    assert [
        tuple(r) for r in store.db.execute("SELECT * FROM agent_champion_registry")
    ] == before
    if revision == 1:
        event = result["release_events"][0]
        assert event["reason"] == "DEMOTION_CANDIDATE"
        assert event["detail"] == {
            "seq": 100,
            "window_start": row["window_start"],
            "window_end": row["window_end"],
        }
        assert result["status"]["observation_release"] is None
    else:
        assert result["release_events"] == []
        assert result["status"]["observation_release"]


def test_checkpoint_error_stop(released, monkeypatch):
    agent, _store, data, _p = released
    agent.register_observation_release(approved_by="human")
    monkeypatch.setattr(
        agent,
        "_isolated_checkpoints",
        lambda *a: {"error": "injected checkpoint failure"},
    )
    result = agent.tick("a", data, max_cycles=1)
    assert result["release_events"][0]["reason"] == "DATA_FAILURE"


def test_cli_round_trip(released, tmp_path):
    from momentum.agent.cli import parser, run

    _agent, store, _data, _p = released
    common = [
        "--db",
        str(store.path),
        "--study",
        "a",
        "--output",
        str(tmp_path / "output"),
    ]
    release = run(
        parser().parse_args(
            ["release-observation", "--approved-by", "reviewer", *common]
        )
    )
    result = run(
        parser().parse_args(
            [
                "release-stop",
                "--release-id",
                release["observation_release_id"],
                "--stopped-by",
                "reviewer",
                *common,
            ]
        )
    )
    assert result["reason"] == "HUMAN"
    assert json.loads((tmp_path / "output/release-stop.json").read_text()) == result


def test_checkpoint_failure_preserves_error_kind(released, monkeypatch):
    agent, _store, data, _p = released
    agent.register_observation_release(approved_by="human")

    def broken(*args):
        raise AgentError("injected checkpoint data defect")

    monkeypatch.setattr(agent, "_checkpoint_window", broken)
    result = agent.tick("a", data, max_cycles=1)
    assert result["release_events"][0]["detail"] == {
        "error_type": "AgentError",
        "error": "injected checkpoint data defect",
    }
