"""H2: one-week return.

Score = the return from the last session of the prior ISO week to the last
session of the current ISO week, observed at the current week's last
session: ``close_raw[week_end] / close_raw[prior_week_end] - 1``. Rebalanced
weekly (``config/hypotheses.yaml``: ``rebalance: weekly``); top decile of
candidates, positive-return filter applied.

Weeks here are ISO weeks (``date.isocalendar()[:2]``), matching
``TradingCalendar.is_week_end`` -- a week that spans a year boundary is not
split into two. ``rank`` never receives a ``TradingCalendar``, so it derives
week membership itself from the session dates actually present in the
visible price history for each security: since ``prices.visible_at(asof)``
can never see anything after ``asof`` (CLAUDE.md rule 2), the most recent
session date visible for a security up to ``asof`` is treated as that
security's "last session of the current week." The next holding period (next
week's first session open, held one week) is entirely a consequence of the
shared engine's rebalance schedule and target computation -- this module
only returns a rank table for the observation date.

Ties are broken by ``security_id`` ascending. A security whose visible
history spans fewer than two distinct ISO weeks scores ``None`` and is
dropped from ranking.
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

HYPOTHESIS_ID = "H2"


def h2_score(week_end_close: float | None, prior_week_end_close: float | None) -> float | None:
    """One-week return ``week_end_close / prior_week_end_close - 1``."""
    if week_end_close is None or prior_week_end_close is None or prior_week_end_close == 0:
        return None
    return week_end_close / prior_week_end_close - 1


class H2:
    """Signal implementation for hypothesis H2."""

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
        by_sid = px.sort("session_date").group_by("security_id").agg(
            pl.col("session_date"), pl.col("close_raw")
        )
        series: dict[str, dict[date, float]] = {
            row["security_id"]: dict(zip(row["session_date"], row["close_raw"]))
            for row in by_sid.iter_rows(named=True)
        }

        scored: list[tuple[str, float]] = []
        for sid in candidates:
            closes = series.get(sid, {})
            dates_sorted = [d for d in sorted(closes) if d <= asof]
            if not dates_sorted or dates_sorted[-1] != asof:
                continue
            weeks: dict[tuple[int, int], list[date]] = {}
            for d in dates_sorted:
                weeks.setdefault(d.isocalendar()[:2], []).append(d)
            week_keys = sorted(weeks)
            if len(week_keys) < 2:
                continue
            current_week_end = max(weeks[week_keys[-1]])
            prior_week_end = max(weeks[week_keys[-2]])
            if current_week_end != asof:
                continue
            score = h2_score(closes.get(current_week_end), closes.get(prior_week_end))
            if score is not None:
                scored.append((sid, score))

        if hcfg.require_positive:
            scored = [(sid, sc) for sid, sc in scored if sc > 0]
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


REGISTRY[HYPOTHESIS_ID] = H2()
