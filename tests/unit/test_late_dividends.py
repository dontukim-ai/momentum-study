"""Dividends published after their ex-date (policy A, 2026-10-20)."""

import json
from dataclasses import replace
from datetime import date
from decimal import Decimal as D

import pytest
from test_agent import market, policy
from trading_agent.contracts import AgentError, digest, session_time
from trading_agent.data import Snapshot
from trading_agent.logic import Costs

from momentum.agent.cli import read_input
from momentum.agent.contracts import LateDividend
from momentum.agent.evaluator import CommonEvaluator
from momentum.agent.market import KRX
from momentum.config import SpecUndefinedError
from momentum.ledger.ledger import CorporateAction, Ledger


def test_ledger_books_entitlement_from_ex_date_holding_on_recognition():
    ledger = Ledger(D(1000), money_quantum=D(1))
    ledger.positions["A"] = 10
    ledger.marks["A"] = D(50)
    ex, seen, pay = date(2023, 12, 27), date(2024, 2, 8), date(2024, 4, 5)
    ledger.apply_corporate_actions([], date(2023, 12, 26))
    ledger.apply_corporate_actions([], ex)
    ledger.positions["A"] = 3  # sold after the ex-date: still entitled to 10
    ledger.apply_corporate_actions([], seen)
    nav_before = ledger.nav
    ledger.accrue_entitlement("A", ex, D(7), pay, seen)
    assert ledger.receivables == {("A", pay): D(70)}
    assert ledger.nav == nav_before + 70
    ledger.apply_corporate_actions(
        [CorporateAction("A", "cash_dividend", ex, pay, None, D(7))], pay
    )
    assert ledger.receivables == {} and ledger.cash == D(1070)


def test_ledger_entitlement_edges():
    ledger = Ledger(D(1000), money_quantum=D(1))
    ledger.apply_corporate_actions([], date(2024, 1, 3))
    ledger.accrue_entitlement(
        "A", date(2024, 1, 2), D(7), date(2024, 4, 5), date(2024, 1, 3)
    )
    assert ledger.receivables == {}  # ledger started after the ex-date
    ledger.apply_corporate_actions([], date(2024, 1, 5))
    with pytest.raises(SpecUndefinedError, match="was not processed"):
        ledger.accrue_entitlement(
            "A", date(2024, 1, 4), D(7), date(2024, 4, 5), date(2024, 1, 5)
        )
    with pytest.raises(SpecUndefinedError, match="ex_date < recognition < pay_date"):
        ledger.accrue_entitlement(
            "A", date(2024, 1, 3), D(7), date(2024, 1, 5), date(2024, 1, 5)
        )


def late(**change):
    values = {
        "security_id": "A",
        "ex_date": date(2023, 12, 27),
        "recognized_on": date(2024, 2, 13),
        "pay_date": date(2024, 4, 5),
        "cash_amount": D(525),
        "available_at": session_time(date(2024, 2, 8), 18),
    }
    values.update(change)
    return LateDividend(**values)


def test_late_dividend_contract():
    assert late().cash_amount == D(525)
    with pytest.raises(AgentError, match="recognized before"):
        late(recognized_on=date(2024, 2, 8))
    with pytest.raises(AgentError, match="ordinary action"):
        late(available_at=session_time(date(2023, 12, 26), 18))
    with pytest.raises(AgentError, match="ex_date < recognized_on < pay_date"):
        late(pay_date=date(2024, 2, 13))
    with pytest.raises(AgentError, match="non-negative"):
        late(cash_amount=D(-1))


def evaluation_case(available_index=102, recognized_index=103):
    data = market(cycle=False)
    p = policy()
    p = replace(
        p,
        logic=replace(
            p.logic,
            costs=Costs(D(0), D(0), D(0)),
            selection=replace(p.logic.selection, max_daily_volatility=D(1)),
        ),
    )
    spec = replace(p.logic.champion, min_momentum=D("-.9"))
    ex, recognized, pay = (
        data.sessions[101],
        data.sessions[recognized_index],
        data.sessions[105],
    )
    dividend = LateDividend(
        "FIXTURE",
        ex,
        recognized,
        pay,
        D(5000),
        session_time(data.sessions[available_index], 18),
    )
    bars = [
        replace(b, open=D(5000), high=D(5000), low=D(5000), close=D(5000))
        if b.session >= ex
        else b
        for b in data.snapshot.bars
    ]
    return data, p, spec, bars, dividend


def test_evaluator_books_late_dividend_only_after_publication():
    data, p, spec, bars, dividend = evaluation_case()
    data = replace(data, snapshot=Snapshot(bars), late_dividends=(dividend,))
    result = CommonEvaluator(p).evaluate(
        data, spec, data.sessions[100], data.sessions[106]
    )
    rows = {r["session"]: r for r in result.equity_curve}
    ex, recognized, pay = (data.sessions[i].isoformat() for i in (101, 103, 105))
    # Ex-date: the price drop is visible, the amount is not yet known.
    assert D(rows[ex]["receivable"]) == 0 and D(rows[ex]["equity"]) == D(800000)
    assert D(rows[data.sessions[102].isoformat()]["receivable"]) == 0
    # Recognition: entitlement of the 80 shares held into the ex-date.
    assert D(rows[recognized]["receivable"]) == D(200000)
    assert D(rows[recognized]["equity"]) == D(1000000)
    assert D(rows[pay]["receivable"]) == 0
    assert all(D(r["cash"]) >= 0 for r in result.equity_curve)


def test_prefix_hides_unpublished_late_dividends_and_fingerprint_is_stable():
    data, _, _, _, dividend = evaluation_case()
    plain = data.fingerprint(data.sessions[110])
    expected = digest(
        {
            "prices": data.prefix(data.sessions[110]).snapshot.id,
            "securities": data.prefix(data.sessions[110]).securities.id,
            "actions": (),
            "sessions": data.prefix(data.sessions[110]).sessions,
            "basis": data.corporate_action_basis,
        }
    )
    assert plain == expected  # inputs without late dividends keep old fingerprints
    with_late = replace(data, late_dividends=(dividend,))
    # Published at 18:00 on session 102: visible from that day's 18:00
    # observation (LOOP-01b moved it from 16:00), never the day before.
    assert with_late.prefix(data.sessions[101]).late_dividends == ()
    assert with_late.prefix(data.sessions[102]).late_dividends == (dividend,)
    assert with_late.fingerprint(data.sessions[110]) != plain
    assert with_late.fingerprint(data.sessions[101]) == data.fingerprint(
        data.sessions[101]
    )


def test_read_input_parses_late_dividends(tmp_path):
    prices = tmp_path / "prices.csv"
    prices.write_text(
        "session,symbol,open,high,low,close,volume,available_at,source\n"
        + "".join(
            f"{d},033780,100,100,100,100,1,{KRX.final_bar_at(date.fromisoformat(d)).isoformat()},t\n"
            for d in ("2023-12-27", "2024-02-13", "2024-04-05")
        )
    )
    securities = tmp_path / "securities.jsonl"
    securities.write_text(
        json.dumps(
            {
                "symbol": "033780",
                "effective_at": "2023-01-01T00:00:00+00:00",
                "available_at": "2023-01-01T00:00:00+00:00",
                "status": "NORMAL",
                "asset_type": "STOCK",
                "market": "KOSPI",
                "managed": False,
                "caution": False,
                "leveraged": False,
                "inverse": False,
                "bid": None,
                "ask": None,
                "source": "test",
            }
        )
        + "\n"
    )
    actions = tmp_path / "actions.json"
    actions.write_text(
        json.dumps(
            [
                {
                    "security_id": "033780",
                    "action_type": "late_cash_dividend",
                    "ex_date": "2023-12-27",
                    "recognized_on": "2024-02-13",
                    "pay_date": "2024-04-05",
                    "split_ratio": None,
                    "cash_amount": "5000",
                    "available_at": "2024-02-07T18:00:00+09:00",
                }
            ]
        )
    )
    data = read_input(prices, securities, actions)
    assert data.actions == ()
    (parsed,) = data.late_dividends
    assert parsed.recognized_on == date(2024, 2, 13) and parsed.cash_amount == D(5000)
