"""H4 signal: ranking behaviour beyond the formula itself.

The formula's numeric correctness lives in
``tests/golden/test_h4_signal_handcalc.py``. These tests cover the parts of
``H4.rank`` the golden fixture does not: the top-10% cut with no
positive-return filter, the security_id tiebreak, and that ranking never
reads unpublished data.
"""

from __future__ import annotations

from datetime import date

import polars as pl

from momentum.asof import AsOfFrame
from momentum.signals import REGISTRY

ASOF = date(2001, 1, 31)
NUM_MONTH = date(2000, 12, 31)  # asof - 1 month
DEN_MONTH = date(2000, 1, 31)  # asof - 12 months


def _universe(ids: list[str]) -> pl.DataFrame:
    return pl.DataFrame(
        {
            "effective_date": [ASOF] * len(ids),
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


def test_h4_selects_top_ten_percent_without_a_positive_return_filter() -> None:
    ids = [f"SEC{i:04d}" for i in range(100)]
    rows = []
    for i, sid in enumerate(ids):
        den_px = 100.0 - i * 0.01
        num_px = den_px * (1.0 - 0.05 - i * 0.0001)  # every security loses money
        rows.append(
            {"security_id": sid, "session_date": DEN_MONTH, "close_raw": den_px, "available_at": DEN_MONTH}
        )
        rows.append(
            {"security_id": sid, "session_date": NUM_MONTH, "close_raw": num_px, "available_at": NUM_MONTH}
        )
    prices = _prices(rows)
    universe = _universe(ids)

    rt = REGISTRY["H4"].rank(ASOF, universe, prices)

    assert rt.frame.height == 10
    assert (rt.frame["score"] < 0).all()


def test_ties_are_broken_by_security_id_ascending() -> None:
    ids = [f"SEC{i:04d}" for i in range(30)]
    rows = []
    for i, sid in enumerate(ids):
        den_px = 100.0
        # First three securities tie for the best (least negative) score;
        # everyone else is worse. top_pct=0.10 of 30 candidates keeps
        # exactly 3 -- precisely the tied group.
        num_px = 95.0 if i < 3 else 90.0 - i
        rows.append(
            {"security_id": sid, "session_date": DEN_MONTH, "close_raw": den_px, "available_at": DEN_MONTH}
        )
        rows.append(
            {"security_id": sid, "session_date": NUM_MONTH, "close_raw": num_px, "available_at": NUM_MONTH}
        )
    prices = _prices(rows)
    universe = _universe(ids)

    rt = REGISTRY["H4"].rank(ASOF, universe, prices)

    tied_score = 95.0 / 100.0 - 1
    tied_ids = rt.frame.filter(pl.col("score") == tied_score)["security_id"].to_list()
    assert tied_ids == sorted(tied_ids)
    assert tied_ids == ["SEC0000", "SEC0001", "SEC0002"]


def test_h4_only_reads_data_visible_at_the_observation_date() -> None:
    ids = ["SEC0000", "SEC0001"]
    rows = [
        {"security_id": "SEC0000", "session_date": DEN_MONTH, "close_raw": 100.0, "available_at": DEN_MONTH},
        {"security_id": "SEC0000", "session_date": NUM_MONTH, "close_raw": 110.0, "available_at": NUM_MONTH},
        {"security_id": "SEC0001", "session_date": DEN_MONTH, "close_raw": 100.0, "available_at": DEN_MONTH},
        # Published in the future relative to ASOF -- must be invisible.
        {
            "security_id": "SEC0001",
            "session_date": NUM_MONTH,
            "close_raw": 999.0,
            "available_at": date(2001, 6, 30),
        },
    ]
    prices = _prices(rows)
    universe = _universe(ids)

    REGISTRY["H4"].rank(ASOF, universe, prices)

    assert prices.raw_access_count == 0
