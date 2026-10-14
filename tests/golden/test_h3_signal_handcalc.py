"""Golden fixture for the H3 formula: hand-calculated, final authority.

Loaded from ``tests/fixtures/h3_signal_case.csv`` -- four sessions for one
security: the prior month-end (2000-05-31) plus the three sessions of the
current month (2000-06-28, 06-29, 06-30, the last of which is the
month-end observation date). If this test and any prose description (in
``h3.py`` or the task-11 report) ever disagree, THIS fixture wins
(CLAUDE.md).

Hand calculation, done on paper BEFORE running any code against this
fixture:

    close(2000-05-31) = 100.0   (prior month-end)
    close(2000-06-30) = 110.0   (this month-end, the observation date)
    return = 110.0 / 100.0 - 1 = 0.10

    This month's sessions and their volume/shares_outstanding:
        2000-06-28: volume=2,000,000  shares=50,000,000 -> 0.04
        2000-06-29: volume=1,500,000  shares=50,000,000 -> 0.03
        2000-06-30: volume=1,000,000  shares=50,000,000 -> 0.02
    turnover = 0.04 + 0.03 + 0.02 = 0.09

Note this is SUM(volume / shares_outstanding) for each session -- NOT
dollar volume (volume * close) and NOT a volume-surge ratio (e.g. today's
volume over trailing average volume). Substituting either of those traps
would still produce *a* number here, which is exactly why a hand-derived
fixture -- not just "the code ran and produced something" -- is the
authority: 0.09 is uniquely the turnover-as-specified answer; a
dollar-volume mistake would give roughly 100x that (volumes were chosen at
~100 so the two are easy to tell apart by eye), and a volume-surge mistake
would not even be denominated in the same units.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import polars as pl

from momentum.signals.h3 import h3_return_score, h3_turnover

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "h3_signal_case.csv"

PRIOR_MONTH_END = date(2000, 5, 31)
MONTH_END = date(2000, 6, 30)
THIS_MONTH_SESSIONS = [date(2000, 6, 28), date(2000, 6, 29), date(2000, 6, 30)]


def _load() -> pl.DataFrame:
    return pl.read_csv(
        FIXTURE,
        schema_overrides={
            "session_date": pl.Date,
            "close_raw": pl.Float64,
            "volume": pl.Float64,
            "shares_outstanding": pl.Float64,
        },
    )


df = _load()


def test_h3_return_is_month_end_over_prior_month_end() -> None:
    closes = dict(zip(df["session_date"].to_list(), df["close_raw"].to_list()))
    score = h3_return_score(closes[MONTH_END], closes[PRIOR_MONTH_END])
    assert round(score, 10) == round(0.10, 10)


def test_h3_turnover_is_the_hand_calculated_sum_not_dollar_volume_or_a_surge_ratio() -> None:
    month = df.filter(pl.col("session_date").is_in(THIS_MONTH_SESSIONS))
    volumes = month["volume"].to_list()
    shares = month["shares_outstanding"].to_list()

    turnover = h3_turnover(volumes, shares)
    assert round(turnover, 10) == round(0.09, 10)

    # The traps named in the spec would both produce a different number:
    dollar_volume_mistake = sum(month["volume"] * month["close_raw"])
    assert round(turnover, 6) != round(dollar_volume_mistake, 6)

    surge_mistake = month["volume"][-1] / (sum(volumes) / len(volumes))
    assert round(turnover, 6) != round(surge_mistake, 6)
