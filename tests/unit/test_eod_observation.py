"""Historical FSC-shaped inputs must be tradable without loosening any screen.

Explore run 2 (2026-10-21) screened every security out on every day: bars
published at 18:00 were invisible to a 16:00 decision, change-only status
records looked stale after one day, and the policy demanded live quotes. It
still spent a holdout. These regressions pin the three fixes together.
"""

from dataclasses import replace
from datetime import date, timedelta
from decimal import Decimal as D

import pytest
from trading_agent.contracts import AgentError, Bar, Costs
from trading_agent.data import Snapshot
from trading_agent.logic import LogicSettings
from trading_agent.selection import RankedStrategy, SecurityHistory, SecurityState
from trading_agent.store import Store

from momentum.agent.contracts import (
    AgentPolicy,
    ResearchInput,
    SessionConfirmedSecurities,
)
from momentum.agent.evaluator import CommonEvaluator
from momentum.agent.market import KRX
from momentum.agent.service import ResearchAgent


def historical(count=230, *, confirmed=True):
    """Bars public at 18:00; one status event per symbol; no quotes."""
    bars, sessions = [], []
    day, close = date(2024, 1, 2), D(10000)
    while len(bars) < count:
        if day.weekday() < 5:
            opening = close
            close = (
                opening * (D("1.02") if len(bars) % 20 < 10 else D(".98"))
            ).quantize(D(1))
            bars.append(
                Bar(
                    day,
                    "HIST",
                    opening,
                    max(opening, close),
                    min(opening, close),
                    close,
                    100000,
                    KRX.historical_bar_at(day),
                    "ENGINEERED_SOFTWARE_FIXTURE",
                )
            )
            sessions.append(day)
        day += timedelta(days=1)
    state = SecurityState(
        "HIST",
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
    securities = (
        SessionConfirmedSecurities([state], tuple(sessions))
        if confirmed
        else SecurityHistory([state])
    )
    return ResearchInput(Snapshot(bars), securities)


def historical_policy(**changes):
    p = AgentPolicy(
        logic=LogicSettings(
            champion=RankedStrategy(20), lookbacks=(2, 20), rankings=("momentum",)
        ),
        holdout_sessions=40,
    )
    logic = replace(
        p.logic,
        costs=Costs(D("1.5"), D(20), D(25)),
        selection=replace(p.logic.selection, spread_mode="historical_unavailable"),
    )
    return replace(p, logic=logic, **changes)


def test_observation_follows_historical_bar_publication():
    day = date(2024, 6, 3)
    assert KRX.historical_bar_at(day) <= KRX.observed_at(day)
    assert KRX.disclosure_at(day) <= KRX.observed_at(day)
    assert KRX.observed_at(day) < KRX.report_deadline(day)


def test_change_only_states_are_reconfirmed_each_observed_session():
    data = historical(30)
    sessions = data.sessions
    states = data.securities.states(KRX.observed_at(sessions[10]))
    assert states["HIST"].effective_at == KRX.open_at(sessions[10])
    # Before that session's observation only the previous session is confirmed.
    early = data.securities.states(KRX.open_at(sessions[10]))
    assert early["HIST"].effective_at == KRX.open_at(sessions[9])
    prefix = data.prefix(sessions[5])
    assert isinstance(prefix.securities, SessionConfirmedSecurities)
    late = prefix.securities.states(KRX.observed_at(sessions[20]))
    # Never reconfirmed past the prefix's last session.
    assert late["HIST"].effective_at == KRX.open_at(sessions[5])
    plain = historical(30, confirmed=False)
    assert plain.securities.id != data.securities.id
    stale = plain.securities.states(KRX.observed_at(sessions[10]))
    assert stale["HIST"].effective_at == KRX.open_at(sessions[0])


def test_historical_shaped_input_trades():
    data = historical()
    p = historical_policy()
    spec = replace(p.logic.champion, min_momentum=D("-.1"))
    result = CommonEvaluator(p).evaluate(
        data, spec, data.sessions[100], data.sessions[140]
    )
    assert result.trade_log


def test_unscreenable_input_reserves_no_validation_budget(tmp_path):
    store = Store(tmp_path / "agent.sqlite3")
    agent = ResearchAgent(store)
    agent.init("hist", historical_policy())
    with pytest.raises(AgentError, match="No security passes the selection screen"):
        agent.tick("hist", historical(confirmed=False))
    for table in ("agent_commitments", "agent_budget_entries", "agent_cycles"):
        assert store.db.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0
    store.close()
    # Switching the securities mode changes the frozen input fingerprint, so
    # the repaired input starts a new study.
    store = Store(tmp_path / "fresh.sqlite3")
    agent = ResearchAgent(store)
    agent.init("hist", historical_policy())
    report = agent.tick("hist", historical())
    assert report["cycles"]
    store.close()


def test_read_input_declares_session_confirmed_securities(tmp_path):
    from trading_agent.data import write_csv
    from trading_agent.selection import write_security_history

    from momentum.agent.cli import read_input

    data = historical(30)
    prices, securities = tmp_path / "prices.csv", tmp_path / "securities.jsonl"
    write_csv(data.snapshot, prices)
    write_security_history(data.securities, securities)
    plain = read_input(prices, securities)
    confirmed = read_input(prices, securities, securities_mode="session-confirmed")
    assert not isinstance(plain.securities, SessionConfirmedSecurities)
    assert isinstance(confirmed.securities, SessionConfirmedSecurities)
    assert confirmed.securities.sessions == data.sessions
    assert confirmed.fingerprint(data.sessions[-1]) == data.fingerprint(
        data.sessions[-1]
    )
    with pytest.raises(AgentError, match="Unknown securities mode"):
        read_input(prices, securities, securities_mode="daily")
