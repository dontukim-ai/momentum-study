"""H1 signal: one-day reversal/continuation ranking behaviour, plus the
shared engine's daily-holding-period consequences (which live outside this
signal but which H1's daily cadence is the thing that actually exercises)."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import polars as pl

from momentum.asof import AsOfFrame
from momentum.execution.engine import execute
from momentum.portfolio.targets import compute_targets
from momentum.signals import REGISTRY

T_MINUS_1 = date(2001, 3, 1)
T = date(2001, 3, 2)
T_PLUS_1 = date(2001, 3, 5)
T_PLUS_2 = date(2001, 3, 6)


def _universe(ids: list[str]) -> pl.DataFrame:
    return pl.DataFrame(
        {
            "effective_date": [T] * len(ids),
            "security_id": ids,
            "adv_usd": [50_000_000.0] * len(ids),
            "rank": list(range(len(ids))),
            "included": [True] * len(ids),
            "exclusion_reason": [None] * len(ids),
        }
    )


def _prices(rows: list[dict]) -> AsOfFrame:
    df = pl.DataFrame(
        rows,
        schema={
            "security_id": pl.Utf8,
            "session_date": pl.Date,
            "close_raw": pl.Float64,
            "available_at": pl.Date,
        },
    )
    return AsOfFrame(df)


def test_h1_signal_is_the_one_day_total_return_at_the_close_of_t() -> None:
    ids = [f"SEC{i:04d}" for i in range(20)]
    rows = []
    for i, sid in enumerate(ids):
        prev = 100.0
        cur = prev * (1.0 + 0.10 - i * 0.001)
        rows.append({"security_id": sid, "session_date": T_MINUS_1, "close_raw": prev, "available_at": T_MINUS_1})
        rows.append({"security_id": sid, "session_date": T, "close_raw": cur, "available_at": T})
    prices = _prices(rows)
    universe = _universe(ids)

    rt = REGISTRY["H1"].rank(T, universe, prices)

    top_sid = rt.frame.sort("rank")["security_id"][0]
    expected = 100.0 * (1.10) / 100.0 - 1
    got = rt.frame.filter(pl.col("security_id") == top_sid)["score"][0]
    assert round(got, 10) == round(expected, 10)


def test_h1_applies_positive_return_filter() -> None:
    ids = [f"SEC{i:04d}" for i in range(20)]
    rows = []
    for i, sid in enumerate(ids):
        prev = 100.0
        cur = prev * (0.9 - i * 0.001)  # every security loses money
        rows.append({"security_id": sid, "session_date": T_MINUS_1, "close_raw": prev, "available_at": T_MINUS_1})
        rows.append({"security_id": sid, "session_date": T, "close_raw": cur, "available_at": T})
    prices = _prices(rows)
    universe = _universe(ids)

    rt = REGISTRY["H1"].rank(T, universe, prices)

    assert rt.frame.height == 0


def test_ties_are_broken_by_security_id_ascending() -> None:
    ids = [f"SEC{i:04d}" for i in range(30)]
    rows = []
    for i, sid in enumerate(ids):
        prev = 100.0
        cur = 105.0 if i < 3 else 100.5 + i * 0.001
        rows.append({"security_id": sid, "session_date": T_MINUS_1, "close_raw": prev, "available_at": T_MINUS_1})
        rows.append({"security_id": sid, "session_date": T, "close_raw": cur, "available_at": T})
    prices = _prices(rows)
    universe = _universe(ids)

    rt = REGISTRY["H1"].rank(T, universe, prices)
    tied = rt.frame.filter((pl.col("score") - 0.05).abs() < 1e-9)["security_id"].to_list()
    assert sorted(tied) == tied
    assert tied == ["SEC0000", "SEC0001", "SEC0002"]


def test_h1_only_reads_data_visible_at_the_observation_date() -> None:
    ids = ["SEC0000", "SEC0001"]
    rows = [
        {"security_id": "SEC0000", "session_date": T_MINUS_1, "close_raw": 100.0, "available_at": T_MINUS_1},
        {"security_id": "SEC0000", "session_date": T, "close_raw": 110.0, "available_at": T},
        {"security_id": "SEC0001", "session_date": T_MINUS_1, "close_raw": 100.0, "available_at": T_MINUS_1},
        {"security_id": "SEC0001", "session_date": T, "close_raw": 999.0, "available_at": date(2001, 6, 1)},
    ]
    prices = _prices(rows)
    universe = _universe(ids)

    REGISTRY["H1"].rank(T, universe, prices)

    assert prices.raw_access_count == 0


def test_h1_enters_at_the_next_open_and_exits_at_the_one_after() -> None:
    from momentum.config import ExecutionCfg

    cfg = ExecutionCfg()
    order = compute_targets(["SEC0000"], Decimal(30000), {"SEC0000": Decimal(100)}, {}, cfg)
    opens_entry = {"SEC0000": Decimal(101)}
    fills_entry = execute(order, opens_entry, {"SEC0000": 1_000_000.0}, Decimal(30000), cfg,
                           session_date=T_PLUS_1, asof=T)
    entry = next(f for f in fills_entry if f.side == "buy")
    assert entry.session_date == T_PLUS_1
    assert entry.price_source == "open_raw"

    held = entry.shares
    exit_orders = compute_targets([], Decimal(30000), {"SEC0000": Decimal(101)}, {"SEC0000": held}, cfg)
    opens_exit = {"SEC0000": Decimal(102)}
    fills_exit = execute(exit_orders, opens_exit, {"SEC0000": 1_000_000.0}, Decimal(30000), cfg,
                          session_date=T_PLUS_2, asof=T_PLUS_1)
    exit_ = next(f for f in fills_exit if f.side == "sell")
    assert exit_.session_date == T_PLUS_2
    assert exit_.price_source == "open_raw"


def test_h1_never_buys_at_the_signal_day_close() -> None:
    from momentum.config import ExecutionCfg

    cfg = ExecutionCfg()
    order = compute_targets(["SEC0000"], Decimal(30000), {"SEC0000": Decimal(100)}, {}, cfg)
    fills = execute(order, {"SEC0000": Decimal(101)}, {"SEC0000": 1_000_000.0}, Decimal(30000), cfg,
                     session_date=T_PLUS_1, asof=T)
    assert all(f.session_date > f.asof for f in fills)


def test_h1_keeps_a_reselected_position_instead_of_round_tripping() -> None:
    """A security selected on two consecutive daily rebalances must be
    HELD across the rebalance, never sold and rebought -- this is
    compute_targets' netting behaviour (CLAUDE.md rule 7: no per-hypothesis
    duplication of that logic), which H1's daily cadence exercises far more
    than H4's monthly one ever does."""
    from momentum.config import ExecutionCfg

    cfg = ExecutionCfg()
    # Already sitting exactly at the weight-capped target for a single-name
    # selection (nav * (1-buffer) * max_weight // price) -- reselected the
    # next day too, so compute_targets sees SEC0000 already held and in the
    # new selection with nothing left to trade.
    target = int((Decimal(30000) * (1 - Decimal(str(cfg.cash_buffer))) * Decimal(str(cfg.max_weight))) // Decimal(100))
    holdings = {"SEC0000": target}
    orders = compute_targets(["SEC0000"], Decimal(30000), {"SEC0000": Decimal(100)}, holdings, cfg)
    sells = [o for o in orders if o.security_id == "SEC0000" and o.side == "sell"]
    assert sells == []
