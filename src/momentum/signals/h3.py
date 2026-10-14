"""H3: one-month return intersected with monthly turnover.

Score = the one-month total return observed at the current month-end,
``close_raw[month_end] / close_raw[prior_month_end] - 1`` (the same
month-over-month construction as H2's week-over-week return, just at
monthly granularity). Selection is the INTERSECTION of two INDEPENDENT
cuts over the candidate universe: the top decile by one-month return, and
the top 30% by monthly turnover. "Independent" here means the turnover cut
is computed over every candidate with usable point-in-time share data,
regardless of whether that candidate's one-month return happens to be
computable (e.g. it may be missing a prior month-end) -- a security with no
computable return can still occupy a turnover-top-30% slot, it simply will
never be selected because it has no return to intersect with. The
alternative reading -- restricting the turnover ranking to only the
return-eligible names -- would make the "top 30%" a top-30%-of-a-subset,
which is not what "two independent cuts intersected" means. The positive-
return filter applies only to the return leg, before intersecting.

Turnover for a security in a given month is the SUM, over every session in
that month, of ``volume / shares_outstanding`` using the point-in-time
share count effective on that session -- not dollar volume, and not a
volume-surge ratio (CLAUDE.md rule 3 forbids substituting a current share
count for a historical one, which a volume/dollar-volume shortcut would
implicitly do). See ``tests/golden/test_h3_signal_handcalc.py`` /
``tests/fixtures/h3_signal_case.csv`` for the hand-derived authority on this
arithmetic.

Point-in-time shares outstanding is a first-class availability question,
not an implementation detail: ``prices`` (the same ``AsOfFrame`` every
``Signal`` receives) is expected to also carry ``shares_outstanding`` and
``shares_available_at`` columns when the caller can supply point-in-time
share counts (``momentum.cli`` merges ``shares_history`` into the shared
prices frame for every hypothesis, unconditionally -- CLAUDE.md rule 7: this
signal does not get its own private data path). Critically, ``prices``'
own visibility gate (``available_at``) governs only the PRICE row -- it is
never coupled to whether the share count is known yet, so H1/H2/H4 (which
never read the shares columns) are completely unaffected by how promptly
shares data is published. H3 therefore applies its OWN as-of check against
``shares_available_at`` on top of ``visible_at``: a share count published
after ``asof`` is treated exactly like a missing one (masked to ``None``),
never read early. If the ``shares_outstanding``/``shares_available_at``
columns are absent altogether, or a security's monthly turnover would
require a session whose share count is missing or not yet published as of
``asof``, this signal refuses to fabricate a number by falling back to a
current share count: it returns ``RankTable(frame=<empty>,
unavailable=True, reason="shares_outstanding_not_point_in_time")`` for the
ENTIRE call when the columns are absent altogether (evidence the feed is
not point-in-time at all), or simply drops the affected security from the
turnover-eligible pool when only that security's data is missing/late.
"""

from __future__ import annotations

import re
from datetime import date

import polars as pl

from momentum.asof import AsOfFrame
from momentum.config import SpecUndefinedError
from momentum.signals.base import (
    RANK_TABLE_SCHEMA,
    REGISTRY,
    RankTable,
    hypothesis_config,
)

HYPOTHESIS_ID = "H3"
UNAVAILABLE_REASON = "shares_outstanding_not_point_in_time"


def h3_return_score(
    month_end_close: float | None, prior_month_end_close: float | None
) -> float | None:
    """One-month return ``month_end_close / prior_month_end_close - 1``."""
    if (
        month_end_close is None
        or prior_month_end_close is None
        or prior_month_end_close == 0
    ):
        return None
    return month_end_close / prior_month_end_close - 1


def h3_turnover(volumes: list[float], shares: list[float]) -> float:
    """Monthly turnover: ``sum(volume / shares_outstanding)`` over the
    month's sessions, using the point-in-time share count for each session
    -- NOT dollar volume, and NOT a volume-surge ratio."""
    return sum(v / s for v, s in zip(volumes, shares))


def _turnover_top_pct(secondary_filter: str | None) -> float:
    """Parse ``turnover_top_30pct`` -> ``0.30`` from ``config/hypotheses.yaml``
    rather than hardcoding the 30% cut as a module constant -- editing the
    yaml must actually change H3's behaviour."""
    if secondary_filter is None:
        raise SpecUndefinedError("H3 requires hypotheses.yaml secondary_filter to be set")
    m = re.fullmatch(r"turnover_top_(\d+)pct", secondary_filter)
    if not m:
        raise SpecUndefinedError(
            f"unrecognised H3 secondary_filter format: {secondary_filter!r}"
        )
    return int(m.group(1)) / 100.0


def _unavailable() -> RankTable:
    return RankTable(
        frame=pl.DataFrame(schema=RANK_TABLE_SCHEMA),
        unavailable=True,
        reason=UNAVAILABLE_REASON,
    )


class H3:
    """Signal implementation for hypothesis H3."""

    hypothesis_id = HYPOTHESIS_ID

    def rank(self, asof: date, universe: pl.DataFrame, prices: AsOfFrame) -> RankTable:
        hcfg = hypothesis_config(HYPOTHESIS_ID)
        turnover_top_pct = _turnover_top_pct(hcfg.secondary_filter)

        candidates = universe.filter(pl.col("included"))["security_id"].to_list()
        if not candidates:
            return RankTable(
                frame=pl.DataFrame(schema=RANK_TABLE_SCHEMA),
                unavailable=True,
                reason="no eligible securities in universe",
            )

        if "shares_outstanding" not in prices.columns or "shares_available_at" not in prices.columns:
            return _unavailable()

        px = prices.visible_at(asof).filter(pl.col("security_id").is_in(candidates))

        # H3's OWN as-of gate on the shares columns: ``prices.visible_at``
        # only gates the PRICE row's own ``available_at`` (Finding 3 --
        # coupling the two would silently delay H1/H2/H4's price visibility
        # whenever shares data lags). A share count not yet published as of
        # ``asof`` is masked to null here, exactly like a genuinely missing
        # one -- never read early, and never leaking into H1/H2/H4 because
        # they never select these columns at all.
        px = px.with_columns(
            pl.when(
                pl.col("shares_available_at").is_not_null()
                & (pl.col("shares_available_at") <= asof)
            )
            .then(pl.col("shares_outstanding"))
            .otherwise(None)
            .alias("shares_outstanding"),
            pl.when(
                pl.col("shares_available_at").is_not_null()
                & (pl.col("shares_available_at") <= asof)
            )
            .then(pl.col("shares_available_at"))
            .otherwise(None)
            .alias("shares_available_at"),
        )

        by_sid = (
            px.sort("session_date")
            .group_by("security_id")
            .agg(
                pl.col("session_date"),
                pl.col("close_raw"),
                pl.col("volume"),
                pl.col("shares_outstanding"),
                pl.col("shares_available_at"),
            )
        )
        per_security: dict[str, dict[date, dict[str, float | None]]] = {
            row["security_id"]: {
                d: {"close": c, "volume": v, "shares": s, "shares_at": sa}
                for d, c, v, s, sa in zip(
                    row["session_date"],
                    row["close_raw"],
                    row["volume"],
                    row["shares_outstanding"],
                    row["shares_available_at"],
                )
            }
            for row in by_sid.iter_rows(named=True)
        }

        returns: dict[str, float] = {}
        turnovers: dict[str, float] = {}

        for sid in candidates:
            rows = per_security.get(sid, {})
            dates_sorted = sorted(d for d in rows if d <= asof)
            if not dates_sorted or dates_sorted[-1] != asof:
                continue

            months: dict[tuple[int, int], list[date]] = {}
            for d in dates_sorted:
                months.setdefault((d.year, d.month), []).append(d)
            month_keys = sorted(months)

            month_end = max(months[month_keys[-1]])
            if month_end != asof:
                continue

            # Turnover leg: independent of whether a return is computable
            # at all -- every candidate observable today with usable
            # point-in-time shares for its whole month is turnover-
            # eligible, so the turnover-top-30% cut is a real independent
            # cut, not "top 30% of the return-eligible subset."
            month_sessions = months[month_keys[-1]]
            vols: list[float] = []
            shrs: list[float] = []
            usable = True
            for d in month_sessions:
                cell = rows[d]
                shares = cell["shares"]
                shares_at = cell["shares_at"]
                vol = cell["volume"]
                if shares is None or shares_at is None or shares <= 0 or vol is None:
                    usable = False
                    break
                vols.append(vol)
                shrs.append(shares)
            if usable:
                turnovers[sid] = h3_turnover(vols, shrs)

            # Return leg: needs a prior month-end too.
            if len(month_keys) < 2:
                continue
            prior_month_end = max(months[month_keys[-2]])
            score = h3_return_score(
                rows[month_end]["close"], rows[prior_month_end]["close"]
            )
            if score is not None:
                returns[sid] = score

        if not returns and not turnovers:
            return RankTable(frame=pl.DataFrame(schema=RANK_TABLE_SCHEMA))

        # require_positive applies to the return leg.
        eligible_returns = returns
        if hcfg.require_positive:
            eligible_returns = {sid: sc for sid, sc in returns.items() if sc > 0}

        ranked_by_return = sorted(eligible_returns, key=lambda sid: (-eligible_returns[sid], sid))
        n_return_top = int(len(candidates) * hcfg.top_pct)
        return_top = set(ranked_by_return[:n_return_top])

        ranked_by_turnover = sorted(turnovers, key=lambda sid: (-turnovers[sid], sid))
        n_turnover_top = int(len(candidates) * turnover_top_pct)
        turnover_top = set(ranked_by_turnover[:n_turnover_top])

        selected = return_top & turnover_top
        ordered = sorted(selected, key=lambda sid: (-eligible_returns[sid], sid))

        frame = pl.DataFrame(
            {
                "hypothesis_id": [HYPOTHESIS_ID] * len(ordered),
                "security_id": ordered,
                "asof": [asof] * len(ordered),
                "score": [eligible_returns[sid] for sid in ordered],
                "rank": list(range(len(ordered))),
            },
            schema=RANK_TABLE_SCHEMA,
        )
        return RankTable(frame=frame)


REGISTRY[HYPOTHESIS_ID] = H3()
