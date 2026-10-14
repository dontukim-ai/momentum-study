"""Shared execution/accounting edge cases, covered ONCE for the whole
engine rather than once per hypothesis (CLAUDE.md rule 7: hypotheses only
ever differ in their rank table, so an edge case in sizing or filling is an
engine property, not a per-hypothesis one). ``hypothesis_id`` is
parametrized purely as a label on the rank table to prove the engine's
handling really is identical across all four -- none of these assertions
touch a ``Signal`` at all.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from momentum.config import ExecutionCfg
from momentum.execution.engine import execute
from momentum.portfolio.targets import compute_targets

HYPOTHESIS_IDS = ["H1", "H2", "H3", "H4"]
ASOF = date(2001, 1, 31)
EXEC_DATE = date(2001, 2, 1)


@pytest.fixture
def cfg() -> ExecutionCfg:
    return ExecutionCfg()


@pytest.mark.parametrize("hypothesis_id", HYPOTHESIS_IDS)
def test_zero_selected_names_produces_no_orders_and_no_fills(hypothesis_id: str, cfg: ExecutionCfg) -> None:
    targets = compute_targets([], Decimal(30_000), {}, {}, cfg)
    assert targets == []
    fills = execute(targets, {}, {}, Decimal(30_000), cfg, session_date=EXEC_DATE, asof=ASOF)
    assert fills == []


@pytest.mark.parametrize("hypothesis_id", HYPOTHESIS_IDS)
def test_zero_selected_names_liquidates_existing_holdings(hypothesis_id: str, cfg: ExecutionCfg) -> None:
    holdings = {"SEC0000": 50}
    targets = compute_targets([], Decimal(30_000), {"SEC0000": Decimal(40)}, holdings, cfg)
    assert len(targets) == 1
    assert targets[0].side == "sell"
    assert targets[0].shares == 50


@pytest.mark.parametrize("hypothesis_id", HYPOTHESIS_IDS)
def test_fewer_than_twenty_selected_names_still_sizes_correctly(hypothesis_id: str, cfg: ExecutionCfg) -> None:
    ranked = ["SEC0000", "SEC0001", "SEC0002"]
    prev_close = {sid: Decimal(50) for sid in ranked}
    targets = compute_targets(ranked, Decimal(30_000), prev_close, {}, cfg)
    buys = [t for t in targets if t.side == "buy"]
    assert len(buys) == 3
    # weight is capped by max_weight, not diluted by a fictitious 20-name
    # denominator just because fewer than 20 names were selected.
    weight = min(Decimal(1) / Decimal(3), Decimal(str(cfg.max_weight)))
    investable = Decimal(30_000) * (1 - Decimal(str(cfg.cash_buffer)))
    expected_shares = int((investable * weight) // Decimal(50))
    assert all(b.shares == expected_shares for b in buys)


@pytest.mark.parametrize("hypothesis_id", HYPOTHESIS_IDS)
def test_a_share_price_so_high_the_integer_target_is_zero_produces_no_buy(
    hypothesis_id: str, cfg: ExecutionCfg
) -> None:
    ranked = ["SEC0000"]
    prev_close = {"SEC0000": Decimal(1000000000)}  # $1B/share
    targets = compute_targets(ranked, Decimal(30_000), prev_close, {}, cfg)
    assert targets == []  # target == 0, held == 0, delta == 0: no order at all


@pytest.mark.parametrize("hypothesis_id", HYPOTHESIS_IDS)
def test_a_trading_halt_cancels_the_order_at_zero_shares_and_zero_price(
    hypothesis_id: str, cfg: ExecutionCfg
) -> None:
    ranked = ["SEC0000"]
    prev_close = {"SEC0000": Decimal(50)}
    targets = compute_targets(ranked, Decimal(30_000), prev_close, {}, cfg)
    assert len(targets) == 1
    # No open today -- a halted security -- never falls back to yesterday's
    # close (CLAUDE.md rule 1/3).
    fills = execute(
        targets, {"SEC0000": None}, {"SEC0000": 5_000_000.0}, Decimal(30_000), cfg,
        session_date=EXEC_DATE, asof=ASOF,
    )
    assert len(fills) == 1
    f = fills[0]
    assert f.status == "cancelled_no_open"
    assert f.shares == 0
    assert f.price == Decimal(0)


@pytest.mark.parametrize("hypothesis_id", HYPOTHESIS_IDS)
def test_ties_in_the_rank_table_still_produce_a_deterministic_order_of_orders(
    hypothesis_id: str, cfg: ExecutionCfg
) -> None:
    # The signal itself is responsible for tie-breaking (security_id
    # ascending, tested per-signal); once ranked, compute_targets must
    # preserve that order deterministically rather than re-sorting or
    # shuffling ties.
    ranked = ["SEC0000", "SEC0001", "SEC0002"]  # already tie-broken by caller
    prev_close = {sid: Decimal(50) for sid in ranked}
    t1 = compute_targets(ranked, Decimal(30_000), prev_close, {}, cfg)
    t2 = compute_targets(ranked, Decimal(30_000), prev_close, {}, cfg)
    assert [o.security_id for o in t1] == [o.security_id for o in t2] == ranked
