"""Golden fixture for the H4 formula: hand-calculated, final authority.

Loaded from ``tests/fixtures/h4_signal_case.csv`` -- 13 month-end total
return index values for one security. If this test and the module
docstring ever disagree, the fixture wins (CLAUDE.md).
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import polars as pl

from momentum.signals.h4 import h4_score

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "h4_signal_case.csv"


def _load() -> dict[date, float]:
    df = pl.read_csv(FIXTURE, schema_overrides={"month_end": pl.Date, "tr": pl.Float64})
    return dict(zip(df["month_end"].to_list(), df["tr"].to_list()))


tr_by_month_end = _load()


def test_h4_score_is_tr_from_twelve_months_ago_to_one_month_ago() -> None:
    # TR(1999-12-31)=100, TR(2000-11-30)=110 -> score = 0.10
    # asof = 2000-12-29 (the most recent month is excluded, so the actual
    # accumulation window is eleven months: Dec 1999 -> Nov 2000).
    assert round(h4_score(tr_by_month_end, asof=date(2000, 12, 29)), 10) == round(
        110 / 100 - 1, 10
    )


def test_the_most_recent_month_is_excluded_so_a_december_spike_cannot_move_the_score() -> None:
    spiked = {**tr_by_month_end, date(2000, 12, 29): 165.0}
    assert h4_score(spiked, asof=date(2000, 12, 29)) == h4_score(
        tr_by_month_end, asof=date(2000, 12, 29)
    )


def test_insufficient_history_yields_no_score_rather_than_a_partial_window() -> None:
    # Only three month-ends of history: no month twelve months before
    # 2000-03-31 exists, so the score must be None, not a shorter-window
    # approximation.
    short_history = {
        date(2000, 1, 31): 100.0,
        date(2000, 2, 29): 101.0,
        date(2000, 3, 31): 102.0,
    }
    assert h4_score(short_history, asof=date(2000, 3, 31)) is None
