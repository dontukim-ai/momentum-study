"""The hand-calculated ledger fixture is the authority for the accounting.

Every expected number in ``tests/fixtures/handcalc_ledger.csv`` was worked out
by hand from the fixture's own inputs (opens, closes, corporate actions,
10 bps one-way cost, 0.1% ADV participation limit) BEFORE ``Ledger`` existed.
It was not captured from this implementation's output: a golden file
regenerated from the code under test proves only that the code agrees with
itself. If the implementation and this file disagree, CLAUDE.md says the
fixture is right.
"""

from __future__ import annotations

import csv
import pathlib
from datetime import date
from decimal import Decimal

from momentum.ledger.ledger import Ledger

FIX = pathlib.Path(__file__).resolve().parents[1] / "fixtures" / "handcalc_ledger.csv"

START = Decimal("30000.00")


def _rows() -> list[dict[str, str]]:
    with FIX.open(encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def test_ledger_matches_hand_calculation_to_the_cent() -> None:
    led = Ledger(starting_cash=START)
    for row in _rows():
        led.step(row)
        assert led.cash == Decimal(row["cash"]), row["session_date"]
        assert led.positions_value == Decimal(row["positions_value"]), row["session_date"]
        assert led.dividend_receivable == Decimal(row["dividend_receivable"]), (
            row["session_date"]
        )
        assert led.nav == Decimal(row["nav"]), row["session_date"]


def test_accounting_identity_holds_every_single_day() -> None:
    led = Ledger(starting_cash=START)
    for row in _rows():
        led.step(row)
        assert led.cash + led.positions_value + led.dividend_receivable == led.nav


def test_cash_never_goes_negative() -> None:
    led = Ledger(starting_cash=START)
    for row in _rows():
        led.step(row)
        assert led.cash >= 0


def test_net_assets_do_not_jump_on_the_dividend_ex_date_or_pay_date() -> None:
    """Rule 4's cheapest detector: a double-counted dividend shows up here.

    2026-01-08 is the last quiet day before AAA's ex-date. The ex-date close
    drops by the 0.50 dividend and the receivable takes it up; the pay date
    only moves 50.00 from receivable to cash. NAV must be identical on all
    three days.
    """
    navs: dict[str, Decimal] = {}
    led = Ledger(starting_cash=START)
    for row in _rows():
        led.step(row)
        navs[row["session_date"]] = led.nav
    assert navs["2026-01-08"] == navs["2026-01-09"] == navs["2026-01-12"]


def test_split_doubles_shares_halves_basis_and_leaves_net_assets_unchanged() -> None:
    led = Ledger(starting_cash=START)
    before_shares = before_basis = None
    for row in _rows():
        if row["session_date"] == "2026-01-13":
            before_shares = led.positions["AAA"]
            before_basis = led.cost_basis["AAA"]
        led.step(row)
        if row["session_date"] == "2026-01-13":
            assert before_shares is not None and before_basis is not None
            assert led.positions["AAA"] == before_shares * 2
            assert led.cost_basis["AAA"] == before_basis / 2
    # NAV on the split day equals NAV on the day before the split.
    navs: dict[str, Decimal] = {}
    led2 = Ledger(starting_cash=START)
    for row in _rows():
        led2.step(row)
        navs[row["session_date"]] = led2.nav
    assert navs["2026-01-12"] == navs["2026-01-13"]


def test_delisted_position_is_retained_and_produces_no_successful_fill() -> None:
    led = Ledger(starting_cash=START)
    for row in _rows():
        led.step(row)
        if row["session_date"] == "2026-01-15":
            assert led.positions["CCC"] == 187
            statuses = [f.status for f in led.fills if f.session_date.isoformat() == row["session_date"]]
            assert statuses == ["cancelled_no_open"]


def test_overlapping_dividends_on_the_same_security_each_pay_their_own_amount() -> None:
    """Finding 3: receivables keyed by (security_id, pay_date).

    AAA gets a second dividend (ex 2026-01-20, pay 2026-01-22) whose ex-date
    falls before the first dividend's pay-date (2026-01-21) -- a routine
    overlap for a monthly payer. Keying the receivable by security_id alone
    would pay the second dividend early on 2026-01-21 (popping the whole
    combined balance) and then pay nothing on 2026-01-22. Keyed by
    (security_id, pay_date), each pay-date releases only its own accrual.
    """
    led = Ledger(starting_cash=START)
    cash_by_date: dict[str, Decimal] = {}
    receivable_by_date: dict[str, Decimal] = {}
    for row in _rows():
        led.step(row)
        cash_by_date[row["session_date"]] = led.cash
        receivable_by_date[row["session_date"]] = led.dividend_receivable

    # Both accruals are outstanding once the second dividend goes ex-date.
    assert receivable_by_date["2026-01-20"] == Decimal("400.00")
    # 2026-01-21 pays exactly the first dividend's accrual (240.00), not both.
    assert cash_by_date["2026-01-21"] - cash_by_date["2026-01-20"] == Decimal("240.00")
    assert receivable_by_date["2026-01-21"] == Decimal("160.00")
    # 2026-01-22 pays exactly the second dividend's accrual (160.00).
    assert cash_by_date["2026-01-22"] - cash_by_date["2026-01-21"] == Decimal("160.00")
    assert receivable_by_date["2026-01-22"] == Decimal("0.00")


def test_stale_marked_positions_value_is_exposed_after_delisting() -> None:
    """Finding 8: a stale mark is visible, not silently carried forever.

    CCC has no close from 2026-01-14 onward (the delisting fixture rows).
    ``stale_positions_value`` must report its full value as stale on every
    later session, so Tasks 12/13 can flag or exclude it instead of having
    it inflate NAV unchallenged.
    """
    led = Ledger(starting_cash=START)
    for row in _rows():
        led.step(row)
        if row["session_date"] in ("2026-01-15", "2026-01-16"):
            d = date.fromisoformat(row["session_date"])
            stale = led.stale_positions_value(d)
            assert stale == Decimal("7667.00"), row["session_date"]
            # AAA got a fresh close every one of these days, so it is not
            # counted as stale.
            assert stale < led.positions_value


def test_every_fill_is_traceable_to_its_signal_date_and_price_source() -> None:
    led = Ledger(starting_cash=START)
    for row in _rows():
        led.step(row)
    assert led.fills, "fixture must produce fills"
    for f in led.fills:
        assert f.price_source == "open_raw"
        assert f.asof < f.session_date
