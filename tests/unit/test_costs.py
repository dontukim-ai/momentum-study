from decimal import Decimal

from hypothesis import given
from hypothesis import strategies as st

from momentum.config import ExecutionCfg
from momentum.execution.costs import apply_caps, one_way_cost, participation_cap

CFG = ExecutionCfg()


def test_base_cost_is_ten_bps_of_notional():
    assert one_way_cost(Decimal("10000.00"), 10.0) == Decimal("10.00")


def test_cost_is_rounded_to_the_cent():
    assert one_way_cost(Decimal("1234.56"), 10.0) == Decimal("1.23")


def test_stress_cost_exceeds_base_which_exceeds_optimistic():
    n = Decimal("10000.00")
    assert one_way_cost(n, 25.0) > one_way_cost(n, 10.0) > one_way_cost(n, 5.0)


def test_participation_cap_is_one_tenth_percent_of_adv_rounded_down():
    assert participation_cap(1_234_567, CFG) == 1234


def test_order_above_cap_is_reduced_with_a_reason():
    assert apply_caps(5000, adv_shares=1_000_000, cfg=CFG) == (1000, "participation_limit")


def test_order_below_cap_is_untouched():
    assert apply_caps(500, adv_shares=1_000_000, cfg=CFG) == (500, None)


def test_zero_adv_yields_zero_shares_not_a_division_error():
    assert apply_caps(500, adv_shares=0.0, cfg=CFG) == (0, "participation_limit")


def test_cost_is_never_negative_and_zero_notional_costs_zero():
    assert one_way_cost(Decimal("0.00"), 10.0) == Decimal("0.00")
    assert one_way_cost(Decimal("10000.00"), 10.0) >= Decimal("0.00")


def test_cost_is_exactly_proportional_across_scenarios():
    n = Decimal("100000000.00")
    base = one_way_cost(n, CFG.base_cost_bps)
    stress = one_way_cost(n, CFG.stress_cost_bps)
    optimistic = one_way_cost(n, CFG.optimistic_cost_bps)
    assert stress == base * Decimal("2.5")
    assert optimistic == base * Decimal("0.5")


def test_participation_cap_floors_fractional_cap():
    # 1000 * 0.001 = 1.0 exactly is not fractional; pick an ADV that yields
    # a fractional cap and assert flooring, not rounding to nearest.
    cfg = ExecutionCfg(participation_limit=0.0006)
    # 1000 * 0.0006 = 0.6 -> floor is 0, not rounded to nearest (1)
    assert participation_cap(1000, cfg) == 0


@given(
    desired_shares=st.integers(min_value=0, max_value=10_000_000),
    adv_shares=st.floats(min_value=0.0, max_value=1e9, allow_nan=False, allow_infinity=False),
)
def test_apply_caps_contract(desired_shares, adv_shares):
    shares, reason = apply_caps(desired_shares, adv_shares=adv_shares, cfg=CFG)
    assert 0 <= shares <= desired_shares
    assert (reason is not None) == (shares < desired_shares)
