"""``cli._data_hash`` must hash actual market content, not generation params.

A parameter hash (seed, n_securities, window) cannot detect a changed RNG
implementation, a library version bump, or a bug fix to
``momentum.synth.generate_market`` between freezing a manifest and opening
the holdout sample. This test perturbs the generated frames directly --
not the parameters that produced them -- so it actually exercises that the
hash is sensitive to content.
"""

from __future__ import annotations

from datetime import date

import polars as pl

from momentum.cli import _data_hash
from momentum.synth import generate_market


def _market(seed: int = 7, n_securities: int = 3) -> object:
    return generate_market(
        seed=seed, n_securities=n_securities, start=date(2000, 1, 3), end=date(2000, 6, 30)
    )


def test_same_market_hashes_identically() -> None:
    m1 = _market()
    m2 = _market()
    assert _data_hash(m1) == _data_hash(m2)


def test_perturbing_a_single_price_changes_the_hash() -> None:
    m = _market()
    baseline = _data_hash(m)

    perturbed_prices = m.daily_prices.with_columns(
        pl.when(pl.int_range(pl.len()) == 0)
        .then(pl.col("close_raw") + 1.0)
        .otherwise(pl.col("close_raw"))
        .alias("close_raw")
    )
    perturbed = type(m)(
        m.security_master, perturbed_prices, m.corporate_actions, m.shares_history, m.calendar
    )

    assert _data_hash(perturbed) != baseline


def test_dropping_a_row_changes_the_hash() -> None:
    m = _market()
    baseline = _data_hash(m)

    truncated_prices = m.daily_prices.slice(0, m.daily_prices.height - 1)
    perturbed = type(m)(
        m.security_master, truncated_prices, m.corporate_actions, m.shares_history, m.calendar
    )

    assert _data_hash(perturbed) != baseline
