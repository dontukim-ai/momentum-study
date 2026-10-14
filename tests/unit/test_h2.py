"""H2 signal: one-week return ranking behaviour."""

from __future__ import annotations

from datetime import date, timedelta

import polars as pl

from momentum.asof import AsOfFrame
from momentum.signals import REGISTRY

# 2000-06-23 (Fri) is the last session of that ISO week; 2000-06-30 (Fri) is
# the last session of the following week. Both are genuine Fridays.
PRIOR_WEEK_END = date(2000, 6, 23)
OBS = date(2000, 6, 30)


def _week_sessions(week_end: date) -> list[date]:
    return [week_end - timedelta(days=d) for d in range(4, -1, -1)]  # Mon..Fri


def _universe(ids: list[str], asof: date) -> pl.DataFrame:
    return pl.DataFrame(
        {
            "effective_date": [asof] * len(ids),
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


def _build(ids: list[str], scores: dict[str, float]) -> AsOfFrame:
    rows = []
    for sid in ids:
        for d in _week_sessions(PRIOR_WEEK_END):
            rows.append({"security_id": sid, "session_date": d, "close_raw": 100.0, "available_at": d})
        for d in _week_sessions(OBS):
            close = 100.0 if d != OBS else 100.0 * (1 + scores[sid])
            rows.append({"security_id": sid, "session_date": d, "close_raw": close, "available_at": d})
    return _prices(rows)


def test_h2_signal_spans_last_session_of_week_over_last_session_of_prior_week() -> None:
    assert OBS == date(2000, 6, 30)
    assert PRIOR_WEEK_END == date(2000, 6, 23)
    ids = [f"SEC{i:04d}" for i in range(20)]
    scores = {sid: 0.10 - i * 0.001 for i, sid in enumerate(ids)}
    prices = _build(ids, scores)
    universe = _universe(ids, OBS)

    rt = REGISTRY["H2"].rank(OBS, universe, prices)

    top_sid = rt.frame.sort("rank")["security_id"][0]
    got = rt.frame.filter(pl.col("security_id") == top_sid)["score"][0]
    assert round(got, 10) == round(scores[top_sid], 10)


def test_h2_applies_positive_return_filter() -> None:
    ids = [f"SEC{i:04d}" for i in range(20)]
    scores = {sid: -0.05 - i * 0.001 for i, sid in enumerate(ids)}
    prices = _build(ids, scores)
    universe = _universe(ids, OBS)

    rt = REGISTRY["H2"].rank(OBS, universe, prices)
    assert rt.frame.height == 0


def test_h2_selects_top_decile() -> None:
    ids = [f"SEC{i:04d}" for i in range(20)]
    scores = {sid: 0.10 - i * 0.001 for i, sid in enumerate(ids)}
    prices = _build(ids, scores)
    universe = _universe(ids, OBS)

    rt = REGISTRY["H2"].rank(OBS, universe, prices)
    assert rt.frame.height == 2  # int(20 * 0.10)


def test_h2_only_reads_data_visible_at_the_observation_date() -> None:
    ids = ["SEC0000"]
    scores = {"SEC0000": 0.10}
    prices = _build(ids, scores)
    universe = _universe(ids, OBS)

    REGISTRY["H2"].rank(OBS, universe, prices)
    assert prices.raw_access_count == 0
