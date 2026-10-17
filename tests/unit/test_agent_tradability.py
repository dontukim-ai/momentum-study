"""Hand-calculated execution capacity, with no broker or market data."""

from dataclasses import replace
from decimal import Decimal as D

import pytest
from trading_agent.contracts import Costs, session_time
from trading_agent.data import Snapshot
from trading_agent.selection import SecurityHistory

from momentum.agent.contracts import AgentPolicy, AnnouncedAction
from momentum.agent.demo import fixture
from momentum.agent.evaluator import CommonEvaluator
from momentum.ledger.ledger import CorporateAction


def capacity_market(*, volume=100000, symbols=("A",), missing=()):
    data = fixture(72, flat=True)
    bars = [
        replace(
            b,
            symbol=s,
            open=D(4000),
            high=D(4000),
            low=D(4000),
            close=D(4000),
            volume=volume,
        )
        for s in symbols
        for b in data.snapshot.bars
        if (s, b.session) not in missing
    ]
    states = [
        replace(
            s,
            symbol=name,
            bid=D(4000),
            ask=D(4000),
            caution=s.effective_at.date() == data.sessions[69],
        )
        for name in symbols
        for s in data.securities.records
    ]
    data = replace(
        data,
        snapshot=Snapshot(bars, require_complete=False),
        securities=SecurityHistory(states),
    )
    p = AgentPolicy()
    p = replace(
        p,
        logic=replace(
            p.logic,
            costs=Costs(D(0), D(0), D(0)),
            selection=replace(
                p.logic.selection, min_average_volume=D(0), min_average_turnover=D(0)
            ),
        ),
    )
    spec = replace(p.logic.champion, min_momentum=D("-.1"))
    return data, p, spec


def evaluate(data, p, spec):
    return CommonEvaluator(p).evaluate(data, spec, data.sessions[70], data.sessions[71])


def test_tradability_preserves_pre_cap_demand():
    data, p, spec = capacity_market(volume=1000)
    r = evaluate(data, p, spec)
    event = r.execution_events[0]
    assert event["desired_quantity"] == 100
    assert event["volume_capped_quantity"] == event["planned_quantity"] == 10
    assert event["cash_risk_capped_quantity"] == event["quantity"] == 10
    assert event["reference_price"] == "4000"
    assert r.tradability["fill_ratio"] == pytest.approx(0.10)
    assert r.tradability["volume_cap_reduction"] == pytest.approx(0.90)
    assert r.trade_log[0]["desired_quantity"] == 100
    assert r.turnover_on_initial_capital == r.turnover == 0.04
    nav_mean = sum(D(row["equity"]) for row in r.equity_curve) / 2
    assert r.turnover_on_average_nav == float(D(40000) / nav_mean)


def test_missing_open_stays_in_reference_notional_denominator():
    day = fixture(72, flat=True).sessions[71]
    data, p, spec = capacity_market(symbols=("A", "B"), missing=(("B", day),))
    r = evaluate(data, p, spec)
    assert r.tradability["fill_ratio"] == 0.5
    assert r.tradability["cancel_reduction"] == 0.5
    assert r.tradability["cancel_reasons"] == {"cancelled_no_open": 0.5}
    assert r.tradability["order_count_fill_ratio"] == 0.5


def test_zero_volume_cap_orders_are_not_silently_dropped():
    data, p, spec = capacity_market(volume=0)
    r = evaluate(data, p, spec)
    assert r.execution_events[0]["status"] == "cancelled_volume_cap"
    assert r.execution_events[0]["desired_quantity"] == 100
    assert r.execution_events[0]["quantity"] == 0
    assert r.tradability["fill_ratio"] == 0
    assert r.tradability["volume_cap_reduction"] == 1


def test_gap_risk_reduction_and_average_nav_turnover_are_separate():
    from trading_agent.contracts import digest

    data, p, spec = capacity_market()
    last = data.sessions[71]
    data = replace(
        data,
        snapshot=Snapshot(
            [
                replace(b, open=D(8000), high=D(8000)) if b.session == last else b
                for b in data.snapshot.bars
            ]
        ),
    )
    r = evaluate(data, p, spec)
    e = r.execution_events[0]
    assert e["desired_quantity"] == e["volume_capped_quantity"] == 100
    assert e["cash_risk_capped_quantity"] == e["quantity"] == 50
    assert r.tradability["fill_ratio"] == r.tradability["cash_risk_reduction"] == 0.5
    assert r.tradability["cancel_reduction"] == 0
    assert r.turnover == r.turnover_on_initial_capital == 0.4
    assert r.turnover_on_average_nav == float(D(400000) / D(900000))
    assert r.evaluation_id == digest(
        {
            "data": data.fingerprint(last),
            "policy": p,
            "spec": spec,
            "start": data.sessions[70],
            "end": last,
            "stress": False,
        }
    )


def test_no_orders_has_no_defined_fill_ratios():
    data, p, spec = capacity_market()
    r = evaluate(data, p, replace(spec, min_momentum=D(1)))
    assert not r.execution_events
    for key in (
        "fill_ratio",
        "volume_cap_reduction",
        "cash_risk_reduction",
        "cancel_reduction",
        "order_count_fill_ratio",
    ):
        assert r.tradability[key] is None


def test_buy_sell_notionals_are_added_and_missing_reference_invalidates_ratios():
    from momentum.agent.evaluator import tradability_metrics

    events = [
        {
            "session": "2024-01-02",
            "symbol": s,
            "side": side,
            "desired_quantity": 10,
            "volume_capped_quantity": 10,
            "cash_risk_capped_quantity": 10,
            "quantity": q,
            "reference_price": "1000",
            "status": "filled" if q else "cancelled_no_open",
        }
        for s, side, q in (("A", "BUY", 10), ("B", "SELL", 0))
    ]
    assert tradability_metrics(events)["fill_ratio"] == 0.5
    events[1]["reference_price"] = None
    metrics = tradability_metrics(events)
    assert metrics["fill_ratio"] is None
    assert metrics["cancel_reduction"] is None
    assert metrics["missing_reference_price"] == [
        {"session": "2024-01-02", "symbol": "B", "side": "SELL"}
    ]


def test_split_adjusts_all_planned_quantity_stages_consistently():
    data, p, spec = capacity_market(volume=1000)
    ex = data.sessions[71]
    split = AnnouncedAction(
        CorporateAction("A", "split", ex, None, D(2), None),
        session_time(data.sessions[68], 16),
    )
    data = replace(
        data,
        actions=(split,),
        snapshot=Snapshot(
            [
                replace(b, open=D(2000), high=D(2000), low=D(2000), close=D(2000))
                if b.session == ex
                else b
                for b in data.snapshot.bars
            ]
        ),
    )
    r = evaluate(data, p, spec)
    e = r.execution_events[0]
    assert e["desired_quantity"] == 200
    assert e["volume_capped_quantity"] == e["planned_quantity"] == 20
    assert e["quantity"] == 20
    assert r.tradability["fill_ratio"] == 0.1


def test_fractional_desired_split_does_not_cancel_integer_capped_execution():
    data, p, spec = capacity_market(volume=1000)
    ex = data.sessions[71]
    split = AnnouncedAction(
        CorporateAction("A", "split", ex, None, D("1.5"), None),
        session_time(data.sessions[68], 16),
    )
    data = replace(
        data,
        actions=(split,),
        snapshot=Snapshot(
            [
                replace(
                    b,
                    open=D(2640) if b.session == ex else D(3960),
                    high=D(2640) if b.session == ex else D(3960),
                    low=D(2640) if b.session == ex else D(3960),
                    close=D(2640) if b.session == ex else D(3960),
                )
                for b in data.snapshot.bars
            ]
        ),
    )
    e = evaluate(data, p, spec).execution_events[0]
    assert e["quantity"] == e["planned_quantity"] == 15
    assert D(str(e["desired_quantity"])) == D("151.5")
