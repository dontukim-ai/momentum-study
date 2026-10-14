"""Point-in-time investable universe with auditable exclusion reasons.

``build_universe`` answers "which securities were eligible to trade next
month, as of this month-end" -- and, critically, emits a row for *every*
candidate security, included or not, carrying an explicit reason when it is
excluded. Silently dropping ineligible rows is the classic way a backtest
manufactures survivorship bias: a human auditing the run later must be able
to see that a name dropped out because it was genuinely illiquid, not
because of a data gap or a bug in this function.

Exclusion reasons are evaluated in a fixed priority order, and the first
match wins -- later conditions are never evaluated once an earlier one has
matched:

    not_listed -> share_type -> min_history_days -> min_close_usd ->
    min_adv_usd -> max_names

This ordering is a contract, not an implementation detail: it determines,
for example, that a security with too little history is reported as
``min_history_days`` even if it would also fail on liquidity -- the
liquidity check is never reached for it.
"""

from __future__ import annotations

from datetime import date

import polars as pl

from momentum.asof import AsOfFrame
from momentum.calendar import TradingCalendar
from momentum.config import UniverseCfg


def build_universe(
    prices: AsOfFrame,
    master: AsOfFrame,
    cal: TradingCalendar,
    cfg: UniverseCfg,
    effective_date: date,
) -> pl.DataFrame:
    """Point-in-time eligible universe as of ``effective_date``.

    Returns one row per security present in ``master`` as visible at
    ``effective_date``, with columns ``effective_date, security_id, adv_usd,
    rank, included(bool), exclusion_reason(str | None)``. A security that is
    included has ``exclusion_reason is None``; every excluded security keeps
    its row with the first reason (in priority order) that matched.

    ``rank`` is the security's position (0-indexed) in the full output,
    sorted by ``adv_usd`` descending with ``security_id`` ascending as the
    tiebreak, over *all* candidates -- included and excluded alike. For an
    excluded row this is simply "where this security would have sorted by
    ADV among every candidate that reached this function," not a rank among
    only the included names, and not a rank restricted to whichever
    liquidity/history/listing group it was excluded for. A security excluded
    for ``min_history_days`` (and therefore usually carrying a null
    ``adv_usd``) sorts to the bottom via ``nulls_last``, so its ``rank`` is
    typically the largest in the output. Do not read ``rank`` as "rank among
    the reasons for exclusion" or as a stable ordinal that survives a
    different candidate set.

    ADV (``adv_usd``) is the mean daily dollar volume (``close_raw *
    volume``) over the trailing ``cfg.adv_lookback_days`` *sessions* strictly
    as of ``effective_date`` (via ``prices.visible_at``). The trailing window
    is taken after an explicit ``sort_by("session_date")`` inside the
    aggregation, not via positional ``.tail()`` on whatever row order the
    rows happen to arrive in within a ``group_by`` group -- polars does not
    guarantee that order, so an unsorted ``.tail(n)`` can silently average
    the wrong N sessions while still producing a plausible-looking number.

    Ties in the ``max_names`` cut (two securities with identical ADV) are
    broken by ``security_id`` ascending -- the tiebreaker used everywhere
    else in this project -- so that repeated runs over the same input always
    keep the same names.

    ``cal`` is accepted for interface symmetry with other point-in-time
    builders in this codebase (and to leave room for a calendar-aware
    lookback window later); the current lookback is a plain trailing-N
    session count taken directly from the visible price rows, not from the
    calendar.
    """
    px = prices.visible_at(effective_date)
    ms = master.visible_at(effective_date)

    hist = px.group_by("security_id").agg(
        pl.len().alias("n_days"),
        (pl.col("close_raw") * pl.col("volume"))
        .sort_by(pl.col("session_date"))
        .tail(cfg.adv_lookback_days)
        .mean()
        .alias("adv_usd"),
        pl.col("close_raw").sort_by(pl.col("session_date")).last().alias("last_close"),
    )

    df = ms.join(hist, on="security_id", how="left")

    reason = (
        pl.when(
            pl.col("effective_to").is_not_null() & (pl.col("effective_to") < effective_date)
        )
        .then(pl.lit("not_listed"))
        .when(pl.col("share_type") != "common")
        .then(pl.lit("share_type"))
        .when(pl.col("n_days").fill_null(0) < cfg.min_history_days)
        .then(pl.lit("min_history_days"))
        .when(pl.col("last_close").fill_null(0.0) < cfg.min_close_usd)
        .then(pl.lit("min_close_usd"))
        .when(pl.col("adv_usd").fill_null(0.0) < cfg.min_adv_usd)
        .then(pl.lit("min_adv_usd"))
        .otherwise(None)
        .alias("exclusion_reason")
    )
    df = df.with_columns(reason)

    # Rank candidates by ADV (descending), security_id ascending as the fixed
    # tiebreaker, so which names survive a max_names cut is deterministic
    # even when two securities have identical dollar volume.
    df = df.sort(["adv_usd", "security_id"], descending=[True, False], nulls_last=True)

    surviving_ids = df.filter(pl.col("exclusion_reason").is_null())["security_id"].to_list()
    keep = set(surviving_ids[: cfg.max_names])

    df = df.with_columns(
        pl.when(pl.col("exclusion_reason").is_not_null())
        .then(pl.col("exclusion_reason"))
        .when(~pl.col("security_id").is_in(list(keep)))
        .then(pl.lit("max_names"))
        .otherwise(None)
        .alias("exclusion_reason")
    )

    df = df.with_columns(
        pl.col("exclusion_reason").is_null().alias("included"),
        pl.lit(effective_date).alias("effective_date"),
        pl.int_range(pl.len()).alias("rank"),
    )

    return df.select(
        ["effective_date", "security_id", "adv_usd", "rank", "included", "exclusion_reason"]
    )
