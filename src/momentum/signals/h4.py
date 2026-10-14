"""H4: classic 12-month-minus-1-month cross-sectional momentum.

Score = TR(month-end one month before ``asof``) / TR(month-end twelve months
before ``asof``) - 1. The most recent month (the one ending at ``asof``
itself) is excluded from both the numerator and the accumulation window, so
the actual window spanned is ELEVEN months, not twelve -- see the golden
fixture in ``tests/golden/test_h4_signal_handcalc.py``, which is the formula's
final authority (CLAUDE.md).

No positive-return filter (``require_positive: false`` in
``config/hypotheses.yaml``): a security can be selected with a negative
score, unlike H1-H3. Ties on score are broken by ``security_id`` ascending,
the tiebreak used everywhere else in this project. A security with
insufficient history (either boundary month missing from its TR series)
scores ``None`` and is dropped from the ranking entirely -- never partially
windowed.

``rank`` reads price data exclusively through ``AsOfFrame.visible_at`` -- it
never calls ``prices.raw()`` -- so a leakage audit (``raw_access_count``)
always shows zero raw accesses attributable to this signal.
"""

from __future__ import annotations

from datetime import date

import polars as pl

from momentum.asof import AsOfFrame
from momentum.signals.base import (
    RANK_TABLE_SCHEMA,
    REGISTRY,
    RankTable,
    hypothesis_config,
)

HYPOTHESIS_ID = "H4"


def _shift_month(d: date, months_back: int) -> tuple[int, int]:
    """(year, month) of the calendar month ``months_back`` months before ``d``."""
    total = d.year * 12 + (d.month - 1) - months_back
    return total // 12, total % 12 + 1


def h4_score(tr_by_month_end: dict[date, float], asof: date) -> float | None:
    """H4 formula, applied directly to a security's month-end TR series.

    ``tr_by_month_end`` maps a month-end trading date to that month's
    total-return index level. ``asof`` is itself expected to be a month-end
    observation date; the calendar month it falls in is treated as the
    (excluded) most recent month. Returns ``None`` -- never a partially
    windowed score -- when either the numerator month (one month before
    ``asof``) or the denominator month (twelve months before ``asof``) is
    absent from ``tr_by_month_end``, or when the denominator level is zero.
    """
    num_key = _shift_month(asof, 1)
    den_key = _shift_month(asof, 12)

    num = next(
        (v for k, v in tr_by_month_end.items() if (k.year, k.month) == num_key), None
    )
    den = next(
        (v for k, v in tr_by_month_end.items() if (k.year, k.month) == den_key), None
    )
    if num is None or den is None or den == 0:
        return None
    return num / den - 1


class H4:
    """Signal implementation for hypothesis H4."""

    hypothesis_id = HYPOTHESIS_ID

    def rank(self, asof: date, universe: pl.DataFrame, prices: AsOfFrame) -> RankTable:
        hcfg = hypothesis_config(HYPOTHESIS_ID)
        candidates = universe.filter(pl.col("included"))["security_id"].to_list()
        if not candidates:
            return RankTable(
                frame=pl.DataFrame(schema=RANK_TABLE_SCHEMA),
                unavailable=True,
                reason="no eligible securities in universe",
            )

        px = prices.visible_at(asof).filter(pl.col("security_id").is_in(candidates))

        month_end = (
            px.sort("session_date")
            .with_columns(
                pl.col("session_date").dt.year().alias("_y"),
                pl.col("session_date").dt.month().alias("_m"),
            )
            .group_by(["security_id", "_y", "_m"])
            .agg(
                pl.col("session_date").last().alias("month_end"),
                pl.col("close_raw").last().alias("tr"),
            )
        )

        by_security = month_end.group_by("security_id").agg(
            pl.struct(["month_end", "tr"]).alias("pairs")
        )
        tr_maps: dict[str, dict[date, float]] = {
            row["security_id"]: {p["month_end"]: p["tr"] for p in row["pairs"]}
            for row in by_security.iter_rows(named=True)
        }

        scored: list[tuple[str, float]] = []
        for sid in candidates:
            score = h4_score(tr_maps.get(sid, {}), asof)
            if score is not None:
                scored.append((sid, score))

        if hcfg.require_positive:
            scored = [(sid, sc) for sid, sc in scored if sc > 0]

        # Rank by score descending; ties broken by security_id ascending.
        scored.sort(key=lambda pair: (-pair[1], pair[0]))

        n_top = int(len(candidates) * hcfg.top_pct)
        top = scored[:n_top]

        frame = pl.DataFrame(
            {
                "hypothesis_id": [HYPOTHESIS_ID] * len(top),
                "security_id": [sid for sid, _ in top],
                "asof": [asof] * len(top),
                "score": [sc for _, sc in top],
                "rank": list(range(len(top))),
            },
            schema=RANK_TABLE_SCHEMA,
        )
        return RankTable(frame=frame)


REGISTRY[HYPOTHESIS_ID] = H4()
