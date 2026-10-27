"""compute_weight_targets: a target-weight table sized on the previous close only (v14 §11)."""

from decimal import Decimal

import pytest

from momentum.config import ExecutionCfg, SpecUndefinedError
from momentum.portfolio.targets import TargetOrder, compute_weight_targets

CFG = ExecutionCfg(max_weight=1.0, cash_buffer=0.005)


def test_hand_calculated_rebalance():
    # NAV 1,000,000, buffer 0.5% -> 995,000 investable.
    # A 60% at 1,000 -> 597 shares (hold 700: sell 103); B 40% at 3,000 -> 132 (buy 132); C not wanted: sell all 10.
    orders = compute_weight_targets({"A": 0.6, "B": 0.4}, Decimal(1_000_000),
                                    {"A": Decimal(1000), "B": Decimal(3000), "C": Decimal(50)},
                                    {"A": 700, "C": 10}, CFG)
    assert orders == [TargetOrder("A", "sell", 103, -1), TargetOrder("C", "sell", 10, -1),
                      TargetOrder("B", "buy", 132, 1)]


def test_a_missing_previous_close_leaves_that_security_alone():
    orders = compute_weight_targets({"A": 0.5, "B": 0.5}, Decimal(1_000_000), {"A": Decimal(1000)}, {"B": 40}, CFG)
    assert orders == [TargetOrder("A", "buy", 497, 0)]           # B: no close, no order either way


def test_at_target_means_no_order():
    assert compute_weight_targets({"A": 1.0}, Decimal(1_000_000), {"A": Decimal(1000)}, {"A": 995}, CFG) == []


@pytest.mark.parametrize("weights", [{"A": 0.7, "B": 0.4}, {"A": -0.1}, {"A": 0.06}])
def test_bad_weights_are_refused_not_trimmed(weights):
    cfg = CFG if "B" in weights or weights["A"] < 0 else ExecutionCfg(max_weight=0.05)
    with pytest.raises(SpecUndefinedError):
        compute_weight_targets(weights, Decimal(1_000_000), {"A": Decimal(1000), "B": Decimal(10)}, {}, cfg)


def test_signature_cannot_see_the_execution_price():
    import inspect
    assert not {"open_px", "open_price", "fill_price", "opens"} & set(inspect.signature(compute_weight_targets).parameters)
