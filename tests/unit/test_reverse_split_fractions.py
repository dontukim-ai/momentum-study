"""Reverse splits that leave a fraction of a share (2026-10-21 user decision).

Explore v2 n20 stopped in its final window: 21 shares of 206400 met a 5:1
reverse split (ratio 0.2), leaving 4.2 shares. KRX research now rounds down
and values the fraction at zero, as for capital reductions (policy B). The
shared ledger keeps raising by default, so other studies are unchanged.
"""

from datetime import date
from decimal import Decimal as D

import pytest

from momentum.config import SpecUndefinedError
from momentum.execution.engine import Fill
from momentum.ledger.ledger import CorporateAction, Ledger

BUY, EX = date(2020, 10, 20), date(2020, 10, 28)
SPLIT = CorporateAction("206400", "split", EX, None, D("0.2"), None)


def held(policy="raise"):
    ledger = Ledger(D(1000000), money_quantum=D(1), fractional_split=policy)
    ledger.apply_fills(
        [Fill("206400", "buy", 21, D(1000), D(0), "filled", BUY, BUY, "open_raw")], BUY
    )
    ledger.mark_to_market({"206400": D(1000)}, BUY)
    return ledger


def test_default_ledger_still_refuses_fractional_split():
    with pytest.raises(SpecUndefinedError, match="not a whole number"):
        held().apply_corporate_actions([SPLIT], EX)


def test_krx_policy_rounds_down_and_drops_the_fraction_at_zero():
    ledger = held("drop")
    nav = ledger.nav
    ledger.apply_corporate_actions([SPLIT], EX)
    assert ledger.positions["206400"] == 4
    assert ledger.marks["206400"] == D(5000)
    # 0.2 of a post-split share (5,000 won) is lost: 1,000 won.
    assert ledger.nav == nav - D(1000)
    assert ledger.cash + ledger.positions_value == ledger.nav
    assert ledger.fractions_dropped == [("206400", EX, 21, D("0.2"))]


def test_position_below_one_share_disappears():
    ledger = Ledger(D(1000000), money_quantum=D(1), fractional_split="drop")
    ledger.apply_fills(
        [Fill("206400", "buy", 3, D(1000), D(0), "filled", BUY, BUY, "open_raw")], BUY
    )
    ledger.mark_to_market({"206400": D(1000)}, BUY)
    nav = ledger.nav
    ledger.apply_corporate_actions([SPLIT], EX)
    assert ledger.positions["206400"] == 0
    assert ledger.nav == nav - D(3000)
    assert ledger.positions_value == 0


def test_invalid_policy_is_rejected():
    with pytest.raises(ValueError):
        Ledger(D(1), fractional_split="cash")
