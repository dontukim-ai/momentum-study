"""Bonus issues and capital reductions (policy B, 2026-10-20)."""

from dataclasses import replace
from datetime import date
from decimal import Decimal as D

import pytest
from test_agent import market, policy
from trading_agent.contracts import AgentError, Costs, session_time
from trading_agent.data import Snapshot

from momentum.agent.contracts import AnnouncedAction
from momentum.agent.evaluator import CommonEvaluator
from momentum.config import SpecUndefinedError
from momentum.execution.engine import Fill
from momentum.ledger.ledger import CorporateAction, Ledger

EX, LISTING = date(2024, 3, 4), date(2024, 3, 25)


def holding(qty, mark):
    ledger = Ledger(D(0), money_quantum=D(1))
    ledger.positions["A"] = qty
    ledger.marks["A"] = D(mark)
    ledger.cost_basis["A"] = D(mark)
    ledger._nav = D(qty * mark)
    return ledger


def identity(ledger):
    return ledger.cash + ledger.positions_value + ledger.dividend_receivable == (
        ledger.nav.quantize(D(1))
    )


def fill(sid, side, shares, price):
    return Fill(sid, side, shares, price, D(0), "filled", EX, EX, "open")


def bonus(ratio):
    return CorporateAction("A", "bonus_issue", EX, LISTING, D(ratio), None)


def test_bonus_shares_are_pending_until_listing_and_keep_net_assets():
    ledger = holding(10, 1500)
    ledger.apply_corporate_actions([bonus("0.5")], EX)
    assert ledger.positions["A"] == 10 and ledger.pending_shares == {("A", LISTING): 5}
    assert ledger.nav == D(15000) and ledger.marks["A"] == D(1000)
    ledger.mark_to_market({"A": D(1100)}, EX)
    assert ledger.nav == D(16500) and identity(ledger)
    # Selling every listed share leaves the unlisted ones valued at the close.
    ledger.apply_fills([fill("A", "sell", 10, D(1100))], date(2024, 3, 5))
    assert ledger.marks["A"] == D(1100) and identity(ledger)
    ledger.mark_to_market({"A": D(1200)}, date(2024, 3, 5))
    assert ledger.positions_value == D(6000)
    ledger.apply_corporate_actions([bonus("0.5")], LISTING)
    assert ledger.positions["A"] == 5 and ledger.pending_shares == {}
    assert ledger.cost_basis["A"] == D(1000)


def test_fractional_bonus_share_is_dropped_at_its_value():
    ledger = holding(3, 1500)
    ledger.apply_corporate_actions([bonus("0.5")], EX)
    assert ledger.pending_shares == {("A", LISTING): 1}
    assert ledger.nav == D(4000)  # half a share at 1000 is lost
    assert identity(ledger)


def test_capital_reduction_on_relisting_day():
    ledger = holding(25, 100)
    cut = CorporateAction("A", "capital_reduction", EX, None, D("0.1"), None)
    ledger.apply_corporate_actions([cut], EX)
    assert ledger.positions["A"] == 2 and ledger.marks["A"] == D(1000)
    assert ledger.nav == D(2000) and identity(ledger)
    small = holding(5, 100)
    small.apply_corporate_actions([cut], EX)
    assert small.positions["A"] == 0 and "A" not in small.marks
    assert small.nav == D(0)


def test_incomplete_actions_are_refused():
    with pytest.raises(SpecUndefinedError, match="later listing date"):
        holding(1, 1).apply_corporate_actions(
            [CorporateAction("A", "bonus_issue", EX, EX, D(1), None)], EX
        )
    with pytest.raises(SpecUndefinedError, match="below one"):
        holding(1, 1).apply_corporate_actions(
            [CorporateAction("A", "capital_reduction", EX, None, D(2), None)], EX
        )
    seen = session_time(date(2024, 2, 1), 18)
    with pytest.raises(AgentError, match="Unsupported"):
        AnnouncedAction(CorporateAction("A", "bonus_issue", EX, None, D(1), None), seen)
    with pytest.raises(AgentError, match="Unsupported"):
        AnnouncedAction(
            CorporateAction("A", "capital_reduction", EX, None, D("1.5"), None), seen
        )
    assert AnnouncedAction(bonus("1"), seen).action.pay_date == LISTING


def test_evaluator_keeps_net_assets_through_bonus_issue():
    data = market(cycle=False)
    p = policy()
    p = replace(p, logic=replace(p.logic, costs=Costs(D(0), D(0), D(0))))
    spec = replace(p.logic.champion, min_momentum=D("-.9"))
    ex, listing = data.sessions[101], data.sessions[103]
    action = AnnouncedAction(
        CorporateAction("FIXTURE", "bonus_issue", ex, listing, D(1), None),
        session_time(data.sessions[99], 16),
    )
    rows = [
        replace(b, open=D(5000), high=D(5000), low=D(5000), close=D(5000))
        if b.session >= ex
        else b
        for b in data.snapshot.bars
    ]
    r = CommonEvaluator(p).evaluate(
        replace(data, snapshot=Snapshot(rows), actions=(action,)),
        spec,
        data.sessions[100],
        data.sessions[104],
    )
    assert r.net_return == 0
    curve = r.equity_curve
    assert curve[1]["positions"]["FIXTURE"] == 40  # ex-date: 40 more are pending
    assert curve[1]["positions_value"] == "400000"
    # The strategy sells the 40 listed shares; the pending 40 stay valued.
    assert curve[2]["positions"]["FIXTURE"] == 0
    assert curve[2]["positions_value"] == "200000"
    assert curve[3]["positions"]["FIXTURE"] == 40  # listed on the listing date
    assert all(
        D(row["cash"]) + D(row["positions_value"]) + D(row["receivable"])
        == D(row["equity"])
        for row in r.equity_curve
    )


def test_orders_planned_before_a_bonus_day_are_cancelled():
    from test_agent_tradability import capacity_market, evaluate

    data, p, spec = capacity_market(volume=1000)
    ex = data.sessions[71]
    action = AnnouncedAction(
        CorporateAction(
            "A", "bonus_issue", ex, data.sessions[71] + (LISTING - EX), D(1), None
        ),
        session_time(data.sessions[68], 16),
    )
    e = evaluate(replace(data, actions=(action,)), p, spec).execution_events[0]
    assert e["status"] == "cancelled_action_uncertainty" and e["quantity"] == 0
