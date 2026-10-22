"""LOOP-05c: indexed snapshots and grouped checkpoints change cost, not results.

The 8-cycle monthly explore run (2026-10-22) spent hours after its last cycle
on the observation checkpoints: every window replayed the observation from its
origin on a freshly built, freshly hashed prefix snapshot. These regressions pin
that the faster paths return exactly what the frozen paths return.
"""

from dataclasses import replace
from datetime import date, timedelta
from decimal import Decimal as D

import pytest
from test_agent_checkpoint_incremental import checkpoint_calls, full_checkpoints
from test_agent_degradation import market, policy, rows
from trading_agent.contracts import AgentError, Bar, digest
from trading_agent.data import Snapshot
from trading_agent.logic import LogicSettings
from trading_agent.selection import RankedStrategy, SecurityHistory, SecurityState
from trading_agent.store import Store

from momentum.agent.contracts import (
    AgentPolicy,
    AnnouncedAction,
    ResearchInput,
    SessionConfirmedSecurities,
)
from momentum.agent.evaluator import CommonEvaluator
from momentum.agent.market import KRX
from momentum.agent.service import ResearchAgent, checkpoint_groups
from momentum.agent.snapshot import IndexedSnapshot
from momentum.ledger.ledger import CorporateAction


def ragged(count=90):
    """Three symbols: one with gaps, one listed late, one bar published late."""
    bars, sessions = [], []
    day = date(2024, 1, 2)
    while len(sessions) < count:
        if day.weekday() < 5:
            k = len(sessions)
            sessions.append(day)
            for n, symbol in enumerate(("AAA", "BBB", "CCC")):
                if symbol == "BBB" and k % 7 == 3:
                    continue
                if symbol == "CCC" and k < 30:
                    continue
                close = D(10000 + 37 * ((k * (n + 3)) % 23) - 200 * n)
                published = KRX.historical_bar_at(day)
                if symbol == "AAA" and k == 50:
                    published += timedelta(days=3)
                bars.append(
                    Bar(
                        day,
                        symbol,
                        close - 50,
                        close + 80,
                        close - 90,
                        close,
                        200000 + 1000 * n,
                        published,
                        "ENGINEERED_SOFTWARE_FIXTURE",
                    )
                )
        day += timedelta(days=1)
    return bars, tuple(sessions)


def states(sessions):
    return [
        SecurityState(
            symbol,
            KRX.open_at(sessions[0]),
            KRX.observed_at(sessions[0]),
            "NORMAL",
            "STOCK",
            "KOSPI",
            False,
            False,
            False,
            False,
            None,
            None,
            "ENGINEERED_SOFTWARE_FIXTURE",
        )
        for symbol in ("AAA", "BBB", "CCC")
    ]


def test_indexed_snapshot_matches_snapshot_everywhere():
    bars, sessions = ragged()
    plain = Snapshot(bars, require_complete=False)
    indexed = IndexedSnapshot(reversed(bars), require_complete=False)
    assert indexed.bars == plain.bars
    assert indexed.symbols == plain.symbols
    assert indexed.sessions == plain.sessions
    assert indexed.id == plain.id
    for day in (*sessions, sessions[-1] + timedelta(days=1)):
        assert indexed.day(day) == plain.day(day)
    for day in sessions[1::4] + sessions[48:56]:
        for at in (KRX.open_at(day), KRX.observed_at(day), KRX.observed_at(day) + timedelta(days=5)):
            assert indexed.view(at, day, allow_missing=True) == plain.view(
                at, day, allow_missing=True
            )
    with pytest.raises(AgentError, match="unavailable or stale"):
        indexed.view(KRX.observed_at(sessions[3]), sessions[3])
    assert IndexedSnapshot.of(plain).id == plain.id
    assert IndexedSnapshot.of(plain).view(
        KRX.observed_at(sessions[60]), sessions[60], allow_missing=True
    ) == plain.view(KRX.observed_at(sessions[60]), sessions[60], allow_missing=True)


def test_indexed_prefixes_match_rebuilt_prefixes():
    bars, sessions = ragged()
    indexed = IndexedSnapshot(bars, require_complete=False)
    # Ask for later prefixes first so earlier ids come from stored states.
    for end in (sessions[70], sessions[10], sessions[40], sessions[-1], sessions[0]):
        child = indexed.until(end)
        rebuilt = Snapshot([b for b in bars if b.session <= end], require_complete=False)
        assert (child.bars, child.symbols, child.sessions) == (
            rebuilt.bars,
            rebuilt.symbols,
            rebuilt.sessions,
        )
        assert child.id == rebuilt.id == digest(rebuilt.bars)
        for day in sessions[:3] + sessions[-3:]:
            assert child.day(day) == rebuilt.day(day)
        assert child.view(
            KRX.observed_at(end), end, allow_missing=True
        ) == rebuilt.view(KRX.observed_at(end), end, allow_missing=True)
        assert child.until(sessions[0]).id == Snapshot(
            [b for b in bars if b.session == sessions[0]], require_complete=False
        ).id
    with pytest.raises(AgentError, match="Dataset is empty"):
        indexed.until(sessions[0] - timedelta(days=1))


def test_indexed_snapshot_keeps_validation_messages():
    bars, _ = ragged(5)
    with pytest.raises(AgentError, match="Dataset is empty"):
        IndexedSnapshot([])
    with pytest.raises(AgentError, match="Duplicate session/symbol"):
        IndexedSnapshot([*bars, bars[0]], require_complete=False)
    with pytest.raises(AgentError, match="Incomplete universe/session grid"):
        IndexedSnapshot(bars)
    fractional = [replace(bars[0], close=bars[0].close + D(".5"), high=bars[0].high + D(1))]
    later = IndexedSnapshot([*bars[1:], *fractional], require_complete=False)
    assert not later.whole_prices(D(1))
    assert later.until(bars[0].session).whole_prices(D(1)) is False
    clean = IndexedSnapshot(bars, require_complete=False)
    assert clean.whole_prices(D(1))


@pytest.mark.parametrize("confirmed", [False, True])
def test_research_input_prefix_and_fingerprint_are_unchanged(confirmed):
    bars, sessions = ragged()
    history = (
        SessionConfirmedSecurities(states(sessions), sessions)
        if confirmed
        else SecurityHistory(states(sessions))
    )
    plain = ResearchInput(Snapshot(bars, require_complete=False), history)
    indexed = ResearchInput(IndexedSnapshot(bars, require_complete=False), history)
    for end in (sessions[20], sessions[55], sessions[-1]):
        assert indexed.fingerprint(end) == plain.fingerprint(end)
        assert indexed.prefix(end).snapshot.bars == plain.prefix(end).snapshot.bars
    assert indexed.fingerprint(sessions[20]) is indexed.fingerprint(sessions[20])


def test_evaluation_is_identical_on_indexed_input():
    bars, sessions = ragged(160)
    p = AgentPolicy(
        logic=LogicSettings(
            champion=RankedStrategy(5, top_n=2), lookbacks=(5,), rankings=("momentum",)
        )
    )
    p = replace(
        p,
        logic=replace(
            p.logic,
            costs=replace(p.logic.costs, slippage_bps=D(25)),
            selection=replace(
                p.logic.selection,
                spread_mode="historical_unavailable",
                min_average_turnover=D(1),
                min_average_volume=D(1),
            ),
        ),
    )
    history = SessionConfirmedSecurities(states(sessions), sessions)
    plain = ResearchInput(Snapshot(bars, require_complete=False), history)
    indexed = ResearchInput(IndexedSnapshot(bars, require_complete=False), history)
    evaluator = CommonEvaluator(p)
    spec = replace(p.logic.champion, min_momentum=D(-1))
    a = evaluator.evaluate(plain, spec, sessions[80], sessions[-1])
    b = evaluator.evaluate(indexed, spec, sessions[80], sessions[-1])
    assert a.trade_log, "fixture must trade to be meaningful"
    assert a == b


def late(action_day, published):
    action = AnnouncedAction(
        CorporateAction("FIXTURE", "split", action_day, None, D(2), None),
        KRX.open_at(action_day),
    )
    # Valid inputs cannot carry this; force it to pin the guard's boundary.
    object.__setattr__(action, "available_at", published)
    return action


def test_checkpoint_groups_split_only_on_late_items():
    data = market(120)
    ends = [data.sessions[i] for i in (39, 59, 79, 99)]
    assert checkpoint_groups(data, ends) == [[0, 1, 2, 3]]
    acting_early = replace(
        data, actions=(late(data.sessions[50], KRX.observed_at(data.sessions[70])),)
    )
    # Published after window 1 (ends[1]) but acting before it: window 1 must
    # not be served by a run that already knows the action.
    assert checkpoint_groups(acting_early, ends) == [[0, 1], [2, 3]]
    acting_later = replace(
        data, actions=(late(data.sessions[65], KRX.observed_at(data.sessions[70])),)
    )
    assert checkpoint_groups(acting_later, ends) == [[0, 1, 2, 3]]


def test_grouped_checkpoints_match_frozen_oracle(tmp_path, monkeypatch):
    store = Store(tmp_path / "grouped.sqlite3")
    agent = ResearchAgent(store)
    p = replace(policy(), observation_checkpoint_sessions=5, degradation_reference="cash")
    agent.init("a", p)
    data = market(296)
    data = replace(data, snapshot=IndexedSnapshot.of(data.snapshot))
    calls = checkpoint_calls(monkeypatch, agent)
    report = agent.tick("a", data)
    checkpoints = rows(store)
    assert len(checkpoints) >= 5
    # Every window comes from one replay through the last complete window.
    assert calls == [date.fromisoformat(checkpoints[-1]["window_end"])]
    oracle_store = Store(tmp_path / "oracle.sqlite3")
    store.db.backup(oracle_store.db)
    oracle = ResearchAgent(oracle_store)
    oracle.db.execute("DELETE FROM agent_observation_checkpoints")
    plain = market(296)
    expected = full_checkpoints(oracle, "a", plain, p, report["observation"])
    assert "error" not in expected
    for actual, legacy in zip(checkpoints, rows(oracle_store), strict=True):
        for key in ("state", "result_hash", "input_hash", "checkpoint_id", "window_end"):
            assert actual[key] == legacy[key]
    oracle_store.close()
    store.close()
