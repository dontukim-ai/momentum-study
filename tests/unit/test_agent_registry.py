"""Economic and persistence regressions for the unified research runtime."""

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


def market(count=360, *, cycle=True, start=date(2024, 1, 2)):
    bars, states = [], []
    day, close = start, D(10000)
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


def registry_rows(store):
    return [
        dict(r)
        for r in store.db.execute(
            "SELECT * FROM agent_champion_registry ORDER BY revision"
        )
    ]


def test_registry_promotion_initialization_and_stale_study(tmp_path, monkeypatch):
    store = Store(tmp_path / "registry.sqlite3")
    agent = ResearchAgent(store)
    agent.init("a", policy())
    agent.init("b", policy())
    assert not agent.tick("a", market(cycle=False))["cycles"][0][
        "research_champion_updated"
    ]
    original = agent._evaluate

    def inspect(*args):
        rows = registry_rows(store)
        assert len(rows) == 1 and rows[0]["revision"] == 0
        assert rows[0]["evidence_grade"] is None
        assert rows[0]["source_cycle_id"] is None
        return original(*args)

    monkeypatch.setattr(agent, "_evaluate", inspect)
    result = agent.tick("b", market(start=date(2025, 1, 2)))["cycles"][0]
    rows = registry_rows(store)
    assert len(rows) == 2
    assert rows[1]["revision"] == 1
    assert rows[1]["previous_revision"] == 0
    assert rows[1]["evidence_grade"] == "E1"
    assert rows[1]["source_cycle_id"] == result["cycle_id"]
    assert rows[1]["commitment_id"]
    status = agent.init("c", policy())
    assert status["revision"] == 1
    assert status["champion"] == result["champion_after"]
    assert status["registry"]["current_revision"] == 1
    assert agent.champion_registry()[0]["current"]["revision"] == 1
    with pytest.raises(AgentError, match="synchronize"):
        agent.tick("a", market(460, cycle=False), max_cycles=2)
    assert agent.init("a", policy())["revision"] == 0
    restricted = replace(policy(), logic=replace(policy().logic, lookbacks=(20,)))
    with pytest.raises(AgentError, match="Registry champion is outside"):
        agent.init("outside", restricted)
    assert not store.db.execute(
        "SELECT 1 FROM agent_studies WHERE name='outside'"
    ).fetchone()
    store.close()


def test_registry_keep_does_not_append(tmp_path):
    store = Store(tmp_path / "keep.sqlite3")
    agent = ResearchAgent(store)
    agent.init("keep", policy())
    agent.tick("keep", market(cycle=False))
    assert len(registry_rows(store)) == 1
    store.close()


def test_registry_finish_conflict_is_atomic(tmp_path, monkeypatch):
    store = Store(tmp_path / "conflict.sqlite3")
    agent = ResearchAgent(store)
    agent.init("a", policy())
    original = agent._finish
    monkeypatch.setattr(
        agent, "_finish", lambda _: (_ for _ in ()).throw(RuntimeError("pause"))
    )
    with pytest.raises(RuntimeError, match="pause"):
        agent.tick("a", market())
    cycle = store.db.execute("SELECT id FROM agent_cycles").fetchone()[0]
    other = ResearchAgent(store)
    other.init("b", policy())
    assert other.tick("b", market(start=date(2025, 1, 2)))["cycles"][0][
        "research_champion_updated"
    ]
    before = registry_rows(store)
    monkeypatch.setattr(agent, "_finish", original)
    with pytest.raises(AgentError, match="Champion revision changed during research"):
        agent.recover(cycle)
    assert registry_rows(store) == before
    assert agent.status("a")["revision"] == 0
    assert (
        store.db.execute("SELECT status FROM agent_cycles").fetchone()[0] == "EVALUATED"
    )
    store.close()


def test_registry_legacy_migration_is_idempotent(tmp_path):
    store = Store(tmp_path / "legacy.sqlite3")
    agent = ResearchAgent(store)
    agent.init("a", policy())
    agent.tick("a", market())
    store.db.execute("DELETE FROM agent_champion_registry")
    for _ in range(2):
        assert agent.status("a")["registry"]["current_revision"] == 1
        rows = registry_rows(store)
        assert len(rows) == 1
        assert rows[0]["evidence_grade"] == "legacy"
        assert rows[0]["revision"] == 1
    store.close()
