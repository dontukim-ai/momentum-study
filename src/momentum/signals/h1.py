"""H1: one-day reversal/continuation.

Score = one-day total return observed at session ``t``'s close, i.e.
``close_raw[t] / close_raw[t-1] - 1``. Rebalanced daily (``config/
hypotheses.yaml``: ``rebalance: daily``); top decile of candidates by score,
with the positive-return filter applied (``require_positive: true``) --
unlike H4, a negative score is never selected. Entry happens at the open of
``t+1`` and the position is held exactly one session, exiting at the open of
``t+2`` -- but H1 itself never touches execution: it only returns a
``RankTable`` for the observation date ``t``. The one-day holding period and
the "hold instead of round-trip" behaviour for a security reselected on
consecutive days both fall out of the shared engine (``compute_targets`` /
``Ledger``), not out of anything this module does (CLAUDE.md rule 7).

Ties on score are broken by ``security_id`` ascending, as everywhere else in
this project. A security missing either the ``t`` or ``t-1`` close (e.g. not
yet listed, or a data gap) scores ``None`` and is dropped from ranking
entirely.

``rank`` reads price data exclusively through ``AsOfFrame.visible_at`` --
never ``prices.raw()`` -- so no lookahead is possible.
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

HYPOTHESIS_ID = "H1"


def h1_score(close_t: float | None, close_t_minus_1: float | None) -> float | None:
    """One-day total return ``close_t / close_t_minus_1 - 1``.

    Returns ``None`` (never a fabricated value) when either close is
    missing or the prior close is zero.
    """
    if close_t is None or close_t_minus_1 is None or close_t_minus_1 == 0:
        return None
    return close_t / close_t_minus_1 - 1


class H1:
    """Signal implementation for hypothesis H1."""

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
            dates_sorted = sorted(d for d in closes if d <= asof)
            if len(dates_sorted) < 2 or dates_sorted[-1] != asof:
                continue
            t, t_minus_1 = dates_sorted[-1], dates_sorted[-2]
            score = h1_score(closes.get(t), closes.get(t_minus_1))
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


REGISTRY[HYPOTHESIS_ID] = H1()
