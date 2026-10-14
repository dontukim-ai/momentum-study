"""Targeted unit tests for ledger fixes that do not belong in the shared
hand-calculated golden fixture (isolated scenarios, easier to verify by hand
in isolation than by hand-chaining into the 20+ row fixture history).
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from momentum.execution.engine import Fill
from momentum.ledger.ledger import CorporateAction, Ledger
from momentum.portfolio.targets import TargetOrder

START = Decimal("100000.00")
D0 = date(2026, 2, 2)
D1 = date(2026, 2, 3)
D2 = date(2026, 2, 4)


def test_split_with_no_close_that_day_rescales_the_stored_mark() -> None:
    """Finding 2: a split must rescale the stored mark, not just the shares.

    Without the fix, a split day with no close for that security values the
    post-split share count at the pre-split mark -- a spurious NAV jump.
    """
    led = Ledger(starting_cash=START)
    led.step_session(
        D0, D0, [], [TargetOrder("A", "buy", 10, 0)], {"A": Decimal(10)}, {"A": 1e9},
        {"A": Decimal(10)},
    )
    assert led.positions["A"] == 10
    assert led.marks["A"] == Decimal(10)
    before_positions_value = led.positions_value

    ca = CorporateAction("A", "split", D1, None, Decimal(2), None)
    led.step_session(D1, D0, [ca], [], {}, {}, {})  # no close for A today

    assert led.positions["A"] == 20
    assert led.marks["A"] == Decimal(5)
    # Net assets unchanged by the split even though nothing repriced today.
    assert led.positions_value == before_positions_value


def test_cash_is_quantized_to_the_cent_after_a_four_decimal_open() -> None:
    """Finding 4: cash must never carry sub-cent residue from a raw open."""
    led = Ledger(starting_cash=START)
    price = Decimal("10.1234")
    fill = Fill("A", "buy", 3, price, Decimal("0.03"), "filled", D0, D0, "open_raw")
    led.apply_fills([fill], D0)

    # notional = 3 * 10.1234 = 30.3702 -> rounds to 30.37
    assert led.cash == Decimal("99969.60")
    assert led.cash == led.cash.quantize(Decimal("0.01"))


def test_capped_sell_retains_the_remainder_which_completes_next_session() -> None:
    """Finding 7: the capped-exit path, end to end.

    A large exit against a small ADV must fill only the participation-capped
    size, hold the rest, and let the next session's full-size sell (as
    ``compute_targets`` re-emits it) finish the job.
    """
    led = Ledger(starting_cash=START)
    led.step_session(
        D0, D0, [], [TargetOrder("A", "buy", 1000, 0)], {"A": Decimal(10)}, {"A": 1e9},
        {"A": Decimal(10)},
    )
    assert led.positions["A"] == 1000

    # ADV=10,000 -> participation cap = floor(10000 * 0.001) = 10 shares.
    led.step_session(
        D1, D0, [], [TargetOrder("A", "sell", 1000, -1)], {"A": Decimal(10)},
        {"A": 10000.0}, {"A": Decimal(10)},
    )
    first_exit = led.fills[-1]
    assert first_exit.shares == 10
    assert first_exit.status == "reduced_participation"
    assert first_exit.reduction_reasons == ("participation_limit",)
    assert led.positions["A"] == 990

    # Next session: full-size sell of the remainder, ample ADV -> completes.
    led.step_session(
        D2, D1, [], [TargetOrder("A", "sell", 990, -1)], {"A": Decimal(10)},
        {"A": 1e9}, {"A": Decimal(10)},
    )
    second_exit = led.fills[-1]
    assert second_exit.shares == 990
    assert second_exit.status == "filled"
    assert second_exit.reduction_reasons == ()
    assert led.positions["A"] == 0
