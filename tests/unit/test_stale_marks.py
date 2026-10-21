"""Held securities without a bar keep their last close; delisted ones go to zero.

Explore run 3b (2026-10-21) stopped because the champion held a stock on a
day without an accepted bar (halt or quarantined row). User decision the
same day: carry the last close, report it as stale, and write a delisted
holding off at zero. No price is invented and nothing trades without an open.
"""

from dataclasses import replace
from datetime import date
from decimal import Decimal as D

from test_eod_observation import historical, historical_policy
from trading_agent.data import Snapshot

from momentum.agent.contracts import ResearchInput, SessionConfirmedSecurities
from momentum.agent.evaluator import CommonEvaluator
from momentum.agent.market import KRX
from momentum.execution.engine import Fill
from momentum.ledger.ledger import CorporateAction, Ledger


def held_case(*, gap=(), delist_at=None):
    data = historical()
    sessions = data.sessions
    bars = [
        b
        for b in data.snapshot.bars
        if b.session not in {sessions[i] for i in gap}
        and (delist_at is None or b.session < sessions[delist_at])
    ]
    records = list(data.securities.records)
    if delist_at is not None:
        day = sessions[delist_at]
        records.append(
            replace(
                records[0],
                effective_at=KRX.open_at(day),
                available_at=KRX.observed_at(day),
                status="DELISTED",
            )
        )
    data = ResearchInput(
        Snapshot(bars, require_complete=False),
        SessionConfirmedSecurities(records, sessions),
        sessions=sessions,
    )
    p = historical_policy()
    spec = replace(p.logic.champion, min_momentum=D("-.1"))
    return data, CommonEvaluator(p).evaluate(data, spec, sessions[100], sessions[140])


def rows(result):
    return {row["session"]: row for row in result.equity_curve}


def test_missing_bar_carries_last_close_and_reports_stale_value():
    base_data, base = held_case()
    data, result = held_case(gap=(120, 121))
    sessions = data.sessions
    curve, base_curve = rows(result), rows(base)
    before = curve[sessions[119].isoformat()]
    assert before["positions"].get("HIST") and before["stale_value"] == "0"
    for i in (120, 121):
        row = curve[sessions[i].isoformat()]
        held = row["positions"]["HIST"]
        close = next(
            b.close for b in base_data.snapshot.bars if b.session == sessions[119]
        )
        # Carried at the last accepted close, not at the (hidden) real close.
        assert D(row["stale_value"]) == held * close
        assert not any(
            t["session"] == sessions[i].isoformat() for t in result.trade_log
        )
    assert base_curve[sessions[120].isoformat()]["stale_value"] == "0"
    assert result.evaluation_contract["valuation_basis"] == (
        "mark_to_close_carry_last_delisted_zero"
    )


def test_delisted_holding_is_written_off_at_zero():
    data, result = held_case(delist_at=125)
    sessions = data.sessions
    curve = rows(result)
    before = curve[sessions[124].isoformat()]
    held = before["positions"]["HIST"]
    assert held
    row = curve[sessions[125].isoformat()]
    assert row["written_off"] == [
        {
            "symbol": "HIST",
            "shares": held,
            "last_mark": str(
                next(b.close for b in data.snapshot.bars if b.session == sessions[124])
            ),
        }
    ]
    assert row["positions"]["HIST"] == 0
    # Only cash remains; the delisted shares carry no value.
    assert D(row["equity"]) == D(row["cash"])
    assert all(
        curve[d.isoformat()]["written_off"] == []
        for d in sessions[126:141]
    )


def test_ledger_write_off_includes_pending_bonus_shares():
    day, ex, listing = date(2024, 1, 2), date(2024, 1, 3), date(2024, 1, 10)
    ledger = Ledger(D(1000000), money_quantum=D(1))
    ledger.apply_fills(
        [Fill("A", "buy", 10, D(1000), D(0), "filled", day, day, "open_raw")], day
    )
    ledger.mark_to_market({"A": D(1000)}, day)
    ledger.apply_corporate_actions(
        [CorporateAction("A", "bonus_issue", ex, listing, D("0.5"), None)], ex
    )
    assert ledger.pending_quantity("A") == 5
    nav = ledger.nav
    assert ledger.write_off("A", ex) == (15, D(1000) / D("1.5"))
    assert ledger.pending_quantity("A") == 0 and ledger.positions["A"] == 0
    assert ledger.nav == nav - 15 * (D(1000) / D("1.5"))
    assert ledger.positions_value == 0
    assert ledger.write_off("A", ex) is None
