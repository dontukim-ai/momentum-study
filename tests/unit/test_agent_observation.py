"""Continuous observation uses the existing economic path without persistence."""

import json
from dataclasses import replace
from datetime import date, timedelta
from decimal import Decimal as D

import pytest
from trading_agent.contracts import AgentError, Bar, session_time
from trading_agent.data import Snapshot
from trading_agent.logic import LogicSettings, ranked_spec
from trading_agent.selection import RankedStrategy, SecurityHistory, SecurityState
from trading_agent.store import Store

from momentum.agent.contracts import AgentPolicy, ResearchInput
from momentum.agent.evaluator import CommonEvaluator
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


def test_single_schedule_matches_evaluate():
    data = market(80)
    evaluator = CommonEvaluator(policy())
    spec = RankedStrategy(2)
    start, end = data.sessions[60], data.sessions[-1]
    single = evaluator.evaluate(data, spec, start, end)
    scheduled = evaluator.evaluate_schedule(data, [(start, spec)], start, end)
    for field in ("net_return", "daily_returns", "trade_log", "equity_curve"):
        assert getattr(single, field) == getattr(scheduled, field)
    assert "schedule" not in single.portfolio_diagnostics


@pytest.mark.parametrize("kind", ["empty", "reverse", "late"])
def test_invalid_schedule(kind):
    data = market(80)
    start, end = data.sessions[30], data.sessions[-1]
    spec = RankedStrategy(2)
    schedule = {
        "empty": [],
        "reverse": [(end, spec), (start, spec)],
        "late": [(data.sessions[31], spec)],
    }[kind]
    with pytest.raises(AgentError):
        CommonEvaluator(policy()).evaluate_schedule(data, schedule, start, end)


def test_revision_boundary_reconciles_and_does_not_reset():
    base = market(120)
    bars, states = [], []
    for symbol, sign in (("A", 1), ("B", -1)):
        prices = [
            D(40000) + sign * D(300 * min(i, 69) - 100 * max(0, i - 69))
            for i in range(120)
        ]
        for i, bar in enumerate(base.snapshot.bars):
            opening = prices[max(0, i - 1)]
            close = prices[i]
            bars.append(
                replace(
                    bar,
                    symbol=symbol,
                    open=opening,
                    close=close,
                    high=max(opening, close),
                    low=min(opening, close),
                )
            )
        for i, state in enumerate(base.securities.records):
            states.append(
                replace(
                    state,
                    symbol=symbol,
                    bid=prices[i] * D(".9995"),
                    ask=prices[i] * D("1.0005"),
                )
            )
    data = ResearchInput(Snapshot(bars), SecurityHistory(states))
    evaluator = CommonEvaluator(policy())
    start, switch, end = (data.sessions[i] for i in (70, 80, 99))
    schedule = [(start, RankedStrategy(20)), (switch, RankedStrategy(2))]
    result = evaluator.evaluate_schedule(data, schedule, start, end)
    boundary = result.portfolio_diagnostics["schedule"]["revision_boundaries"][0]
    trades = [t for t in result.trade_log if t["session"] == switch.isoformat()]
    assert {t["side"] for t in trades} == {"BUY", "SELL"}
    assert D(boundary["fees"]) == sum((D(t["fee"]) for t in trades), D(0))
    before, after = result.equity_curve[9:11]
    bars = data.snapshot.day(switch)
    previous = data.snapshot.day(data.sessions[79])
    market_move = sum(
        (
            q * (bars[s].close - previous[s].close)
            for s, q in before["positions"].items()
        ),
        D(0),
    )
    trading = sum(
        (
            (1 if t["side"] == "BUY" else -1)
            * t["quantity"]
            * (bars[t["symbol"]].close - D(t["price"]))
            - D(t["fee"])
            for t in trades
        ),
        D(0),
    )
    # Close-to-close loss on 6 A is 600 won. Selling before that close adds
    # 6 * 70 - 770 = -350; buying B adds 19 * 90 - 58 = 1652 won.
    assert [(t["symbol"], t["quantity"], t["price"], t["fee"]) for t in trades] == [
        ("A", 6, "59670", "770"),
        ("B", 19, "20310", "58"),
    ]
    assert market_move == D(-600)
    assert trading == D(1302)
    assert D(before["equity"]) == D(993765)
    assert D(after["equity"]) == D(994467)
    assert D(after["equity"]) - D(before["equity"]) == market_move + trading
    assert boundary["nav_before"] == before["equity"]
    assert boundary["nav_after"] == after["equity"]
    first = evaluator.evaluate(data, schedule[0][1], start, data.sessions[79])
    second = evaluator.evaluate(data, schedule[1][1], switch, end)
    assert result.daily_returns != first.daily_returns + second.daily_returns


def counts(store):
    return {
        r[0]: store.db.execute(f'SELECT COUNT(*) FROM "{r[0]}"').fetchone()[0]
        for r in store.db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'agent_%'"
        )
    }


def test_tick_observation_schedule_and_read_only(tmp_path):
    store = Store(tmp_path / "observation.sqlite3")
    agent = ResearchAgent(store)
    agent.init("a", policy())
    data = market()
    assert agent.observation_ledger("a", data) is None
    report = agent.tick("a", data)
    assert report["cycles"][0]["research_champion_updated"]
    observation = report["observation"]
    assert observation is not None
    assert observation["schedule"][0]["revision"] == 0
    result = report["cycles"][0]
    next_day = data.calendar[
        data.calendar.index(date.fromisoformat(result["holdout_end"])) + 1
    ]
    assert observation["schedule"][1]["effective_from"] == next_day.isoformat()
    before = counts(store)
    assert agent.observation_ledger("a", data) == observation
    assert counts(store) == before
    for key in ("persisted", "promotion_authority", "proposer_visible"):
        assert observation[key] is False
    assert observation["orders_submitted"] == 0
    store.close()


def test_observation_uses_registry_revision_effective_before_study_start(tmp_path):
    store = Store(tmp_path / "later-study.sqlite3")
    agent = ResearchAgent(store)
    agent.init("earlier", policy())
    agent.tick("earlier", market())
    agent.init("later", policy())
    later = market(start=date(2025, 1, 2))
    report = agent.tick("later", later)["observation"]
    assert report["schedule"][0]["revision"] == 0
    assert report["schedule"][1]["revision"] == 1
    row = store.db.execute(
        "SELECT spec_json FROM agent_champion_registry WHERE revision=1"
    ).fetchone()
    spec = ranked_spec(json.loads(row[0]))
    start, end = date.fromisoformat(report["start"]), date.fromisoformat(report["end"])
    expected = CommonEvaluator(policy()).evaluate(later.prefix(end), spec, start, end)
    assert report["ledger"]["net_return"] == expected.net_return
    store.close()
