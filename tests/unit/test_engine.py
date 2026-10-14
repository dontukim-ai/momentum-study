from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from momentum.config import ExecutionCfg, SpecUndefinedError
from momentum.execution.engine import execute
from momentum.portfolio.targets import TargetOrder

CFG = ExecutionCfg()
SESSION = date(2026, 1, 5)
ASOF = date(2026, 1, 2)


def _execute(orders, opens, adv, cash, cfg=CFG):
    return execute(orders, opens, adv, cash, cfg, session_date=SESSION, asof=ASOF)


def test_missing_open_cancels_the_buy_instead_of_using_prior_close() -> None:
    fills = _execute([TargetOrder("A", "buy", 10, 0)], {"A": None}, {"A": 1e9}, Decimal(30000))
    assert fills[0].status == "cancelled_no_open" and fills[0].shares == 0


def test_insufficient_cash_reduces_the_lowest_ranked_buy_first() -> None:
    orders = [TargetOrder("A", "buy", 10, 0), TargetOrder("B", "buy", 10, 1)]
    opens = {"A": Decimal(100), "B": Decimal(100)}
    fills = _execute(orders, opens, {"A": 1e9, "B": 1e9}, Decimal(1500))
    by = {f.security_id: f for f in fills}
    assert by["A"].shares == 10
    assert by["B"].shares < 10 and by["B"].status == "reduced_cash"


def test_execution_never_produces_negative_cash() -> None:
    orders = [TargetOrder("A", "buy", 1000, 0)]
    fills = _execute(orders, {"A": Decimal(100)}, {"A": 1e9}, Decimal(500))
    spent = sum((f.shares * f.price + f.cost for f in fills), Decimal(0))
    assert spent <= Decimal(500)


def test_sells_are_executed_before_buys_so_their_proceeds_are_usable() -> None:
    orders = [TargetOrder("Z", "sell", 10, -1), TargetOrder("A", "buy", 10, 0)]
    opens = {"Z": Decimal(100), "A": Decimal(100)}
    fills = _execute(orders, opens, {"Z": 1e9, "A": 1e9}, Decimal(0))
    assert next(f.security_id for f in fills) == "Z"
    assert next(f for f in fills if f.security_id == "A").shares > 0


def test_participation_limit_reduces_the_size_and_says_so() -> None:
    fills = _execute(
        [TargetOrder("A", "buy", 100, 0)], {"A": Decimal(30)}, {"A": 50000.0}, Decimal(30000)
    )
    assert fills[0].shares == 50 and fills[0].status == "reduced_participation"


def test_sell_with_no_open_is_cancelled_rather_than_filled_at_a_price_that_does_not_exist() -> None:
    fills = _execute([TargetOrder("D", "sell", 187, -1)], {"D": None}, {"D": 1e9}, Decimal(100))
    assert fills[0].status == "cancelled_no_open"
    assert fills[0].shares == 0 and fills[0].price == Decimal(0)


def test_every_fill_carries_its_session_asof_and_price_source() -> None:
    fills = _execute([TargetOrder("A", "buy", 1, 0)], {"A": Decimal(10)}, {"A": 1e9}, Decimal(100))
    f = fills[0]
    assert f.session_date == SESSION and f.asof == ASOF and f.price_source == "open_raw"


def test_a_buy_that_cash_cannot_afford_at_all_is_reported_with_zero_shares() -> None:
    fills = _execute([TargetOrder("A", "buy", 10, 0)], {"A": Decimal(100)}, {"A": 1e9}, Decimal(50))
    assert fills[0].shares == 0 and fills[0].status == "reduced_cash"


def test_costs_are_charged_on_both_sides() -> None:
    fills = _execute(
        [TargetOrder("Z", "sell", 10, -1)], {"Z": Decimal(100)}, {"Z": 1e9}, Decimal(0)
    )
    assert fills[0].cost == Decimal("1.00")


def test_a_buy_order_with_no_adv_entry_raises_rather_than_filling_zero_shares() -> None:
    # Finding 5: adv.get(sid, 0.0) used to turn missing ADV data into a
    # silent zero-share fill. Missing ADV is missing data (CLAUDE.md rule 3),
    # not proof of zero liquidity -- it must not be filled in.
    with pytest.raises(SpecUndefinedError):
        _execute([TargetOrder("A", "buy", 10, 0)], {"A": Decimal(100)}, {}, Decimal(30000))


def test_a_sell_order_with_no_adv_entry_raises_rather_than_being_suppressed() -> None:
    with pytest.raises(SpecUndefinedError):
        _execute([TargetOrder("A", "sell", 10, -1)], {"A": Decimal(100)}, {}, Decimal(30000))


def test_a_buy_cut_by_both_participation_and_cash_reports_both_reasons() -> None:
    fills = _execute(
        [TargetOrder("A", "buy", 1000, 0)],
        {"A": Decimal(100)},
        {"A": 100000.0},  # participation cap = floor(100000*0.001) = 100 shares
        Decimal(50),  # but cash can't afford even 1 share at 100 + cost
    )
    f = fills[0]
    assert f.status == "reduced_cash"
    assert "participation_limit" in f.reduction_reasons
    assert "insufficient_cash" in f.reduction_reasons


def test_a_buy_cut_only_by_participation_reports_only_that_reason() -> None:
    fills = _execute(
        [TargetOrder("A", "buy", 100, 0)], {"A": Decimal(30)}, {"A": 50000.0}, Decimal(30000)
    )
    f = fills[0]
    assert f.status == "reduced_participation"
    assert f.reduction_reasons == ("participation_limit",)


def test_an_unreduced_fill_has_no_reduction_reasons() -> None:
    fills = _execute([TargetOrder("A", "buy", 1, 0)], {"A": Decimal(10)}, {"A": 1e9}, Decimal(100))
    assert fills[0].reduction_reasons == ()
