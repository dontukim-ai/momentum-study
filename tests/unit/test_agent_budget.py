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


def test_budget_shared_across_study_names_and_cached_recovery(tmp_path):
    store = Store(tmp_path / "budget.sqlite3")
    agent = ResearchAgent(store)
    agent.init("a", policy())
    report = agent.tick("a", market())
    budget = report["status"]["budget"]
    assert budget["total_cycles"] == 8
    assert (budget["reserved"], budget["consumed"], budget["remaining"]) == (1, 1, 7)
    entries = [tuple(r) for r in store.db.execute("SELECT * FROM agent_budget_entries")]
    agent.tick("a", market())
    agent.recover(report["cycles"][0]["cycle_id"])
    assert agent.init("b", policy())["budget"] == budget
    assert entries == [
        tuple(r) for r in store.db.execute("SELECT * FROM agent_budget_entries")
    ]
    store.close()


def test_family_budget_exhaustion_rolls_back_new_study_registration(tmp_path):
    store = Store(tmp_path / "budget.sqlite3")
    agent = ResearchAgent(store)
    p = replace(policy(), max_validation_cycles=2)
    agent.init("a", p)
    report = agent.tick("a", market(460), max_cycles=2)
    assert report["validation_budget_exhausted"]
    agent.init("b", p)
    assert agent.tick("b", market(100))["validation_budget_exhausted"]
    counts = [
        store.db.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
        for table in ("agent_cycles", "agent_intervals", "agent_budget_entries")
    ]
    with pytest.raises(AgentError, match="Validation budget exhausted"):
        agent.tick("b", market(460))
    assert counts == [
        store.db.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
        for table in ("agent_cycles", "agent_intervals", "agent_budget_entries")
    ]
    assert agent.status("b")["budget"]["remaining"] == 0
    store.close()


def test_different_policy_budget_requires_explicit_allocation(tmp_path):
    store = Store(tmp_path / "budget.sqlite3")
    agent = ResearchAgent(store)
    agent.init("a", policy())
    agent.tick("a", market())
    agent.init("b", replace(policy(), max_validation_cycles=2))
    with pytest.raises(AgentError, match="explicit allocation"):
        agent.tick("b", market())
    assert agent.status("a")["budget"]["reserved"] == 1
    store.close()


def test_failed_and_abandoned_cycle_consumes_once(tmp_path, monkeypatch):
    store = Store(tmp_path / "budget.sqlite3")
    agent = ResearchAgent(store)
    agent.init("a", policy())

    def interrupt(*args):
        raise RuntimeError("interrupted")

    monkeypatch.setattr(agent, "_evaluate", interrupt)
    with pytest.raises(RuntimeError):
        agent.tick("a", market())
    budget = agent.status("a")["budget"]
    assert (budget["reserved"], budget["consumed"], budget["remaining"]) == (1, 1, 7)
    entries = [tuple(r) for r in store.db.execute("SELECT * FROM agent_budget_entries")]
    cycle_id = store.db.execute("SELECT id FROM agent_cycles").fetchone()[0]
    agent.abandon(cycle_id, "Worker stopped")
    assert agent.status("a")["budget"] == budget
    assert entries == [
        tuple(r) for r in store.db.execute("SELECT * FROM agent_budget_entries")
    ]
    store.close()


def test_failed_claim_rolls_back_reservation(tmp_path):
    store = Store(tmp_path / "budget.sqlite3")
    agent = ResearchAgent(store)
    agent.init("a", policy())
    agent.tick("a", market())
    before = [tuple(r) for r in store.db.execute("SELECT * FROM agent_budget_entries")]
    agent.init("b", policy())
    with pytest.raises(AgentError, match="consumed"):
        agent.tick("b", market())
    assert before == [
        tuple(r) for r in store.db.execute("SELECT * FROM agent_budget_entries")
    ]
    assert not store.db.execute("SELECT 1 FROM agent_cycles WHERE study='b'").fetchone()
    store.close()


@pytest.mark.parametrize(
    "state,consumed",
    [
        ("REGISTERED", 0),
        ("EVALUATED", 0),
        ("COMPLETE", 1),
        ("FAILED", 1),
        ("ABANDONED", 1),
    ],
)
def test_legacy_budget_backfill_is_state_aware_and_idempotent(
    tmp_path, state, consumed
):
    store = Store(tmp_path / "budget.sqlite3")
    agent = ResearchAgent(store)
    agent.init("a", policy())
    agent.tick("a", market())
    # Recreate the pre-ledger database, preserving all consumed intervals.
    store.db.execute("DROP TABLE agent_budget_entries")
    store.db.execute("DROP TABLE agent_budget_accounts")
    store.db.execute("UPDATE agent_cycles SET status=?", (state,))
    agent = ResearchAgent(store)
    budget = agent.status("a")["budget"]
    assert (budget["reserved"], budget["consumed"], budget["remaining"]) == (
        1,
        consumed,
        7,
    )
    entries = [tuple(r) for r in store.db.execute("SELECT * FROM agent_budget_entries")]
    assert agent.status("a")["budget"] == budget
    assert entries == [
        tuple(r) for r in store.db.execute("SELECT * FROM agent_budget_entries")
    ]
    store.close()


def test_first_registration_owns_allocation_not_initialization(tmp_path):
    store = Store(tmp_path / "budget.sqlite3")
    agent = ResearchAgent(store)
    agent.init("unstarted", policy())
    assert (
        store.db.execute("SELECT count(*) FROM agent_budget_accounts").fetchone()[0]
        == 0
    )
    agent.init("first", replace(policy(), max_validation_cycles=2))
    agent.tick("first", market())
    account = store.db.execute("SELECT * FROM agent_budget_accounts").fetchone()
    assert (account["created_by"], account["total_cycles"]) == ("first", 2)
    assert agent.status("unstarted")["budget"]["remaining"] == 1
    store.close()


def test_evaluated_cycle_reserves_until_recovery(tmp_path, monkeypatch):
    store = Store(tmp_path / "budget.sqlite3")
    agent = ResearchAgent(store)
    agent.init("a", policy())
    original = agent._finish

    def interrupt(*args):
        raise RuntimeError("process stopped")

    monkeypatch.setattr(agent, "_finish", interrupt)
    with pytest.raises(RuntimeError):
        agent.tick("a", market())
    budget = agent.status("a")["budget"]
    assert (budget["reserved"], budget["consumed"], budget["remaining"]) == (1, 0, 7)
    monkeypatch.setattr(agent, "_finish", original)
    cycle_id = store.db.execute("SELECT id FROM agent_cycles").fetchone()[0]
    agent.recover(cycle_id)
    assert agent.status("a")["budget"]["consumed"] == 1
    assert agent.status("a")["budget"]["remaining"] == 7
    store.close()
