"""H3 signal: one-month return intersected with monthly turnover, and the
first-class "point-in-time shares outstanding unavailable" result path."""

from __future__ import annotations

from datetime import date

import polars as pl

from momentum.asof import AsOfFrame
from momentum.signals import REGISTRY
from momentum.signals.h3 import UNAVAILABLE_REASON, h3_turnover

PRIOR_MONTH_END = date(2000, 5, 31)
OBS = date(2000, 6, 30)  # month-end
MONTH_SESSIONS = [date(2000, 6, d) for d in (28, 29, 30)]  # this month's sessions


def _universe(ids: list[str]) -> pl.DataFrame:
    return pl.DataFrame(
        {
            "effective_date": [OBS] * len(ids),
            "security_id": ids,
            "adv_usd": [50_000_000.0] * len(ids),
            "rank": list(range(len(ids))),
            "included": [True] * len(ids),
            "exclusion_reason": [None] * len(ids),
        }
    )


PIT_SCHEMA = {
    "security_id": pl.Utf8,
    "session_date": pl.Date,
    "close_raw": pl.Float64,
    "volume": pl.Float64,
    "available_at": pl.Date,
    "shares_outstanding": pl.Float64,
    "shares_available_at": pl.Date,
}

NO_PIT_SCHEMA = {
    "security_id": pl.Utf8,
    "session_date": pl.Date,
    "close_raw": pl.Float64,
    "volume": pl.Float64,
    "available_at": pl.Date,
}

CURRENT_SHARES_SCHEMA = {
    "security_id": pl.Utf8,
    "session_date": pl.Date,
    "close_raw": pl.Float64,
    "volume": pl.Float64,
    "available_at": pl.Date,
    "shares_outstanding": pl.Float64,  # present, but never tracked point-in-time
}


def _build_pit(ids: list[str], returns: dict[str, float], volumes: dict[str, list[float]], shares: dict[str, float]) -> AsOfFrame:
    rows = []
    for sid in ids:
        rows.append(
            {
                "security_id": sid, "session_date": PRIOR_MONTH_END, "close_raw": 100.0,
                "volume": 1_000_000.0, "available_at": PRIOR_MONTH_END,
                "shares_outstanding": shares[sid], "shares_available_at": PRIOR_MONTH_END,
            }
        )
        for d, vol in zip(MONTH_SESSIONS, volumes[sid]):
            close = 100.0 if d != OBS else 100.0 * (1 + returns[sid])
            rows.append(
                {
                    "security_id": sid, "session_date": d, "close_raw": close,
                    "volume": vol, "available_at": d,
                    "shares_outstanding": shares[sid], "shares_available_at": d,
                }
            )
    return AsOfFrame(pl.DataFrame(rows, schema=PIT_SCHEMA))


def test_h3_returns_unavailable_when_shares_history_has_no_available_at() -> None:
    ids = ["SEC0000"]
    rows = [
        {
            "security_id": "SEC0000", "session_date": d, "close_raw": 100.0,
            "volume": 1_000_000.0, "available_at": d,
        }
        for d in [PRIOR_MONTH_END, *MONTH_SESSIONS]
    ]
    prices = AsOfFrame(pl.DataFrame(rows, schema=NO_PIT_SCHEMA))
    universe = _universe(ids)

    rt = REGISTRY["H3"].rank(OBS, universe, prices)

    assert rt.unavailable is True
    assert rt.reason == UNAVAILABLE_REASON
    assert rt.frame.height == 0
    assert rt.frame.columns == ["hypothesis_id", "security_id", "asof", "score", "rank"]


def test_h3_never_substitutes_current_shares_for_historical_ones() -> None:
    ids = ["SEC0000"]
    rows = [
        {
            "security_id": "SEC0000", "session_date": d, "close_raw": 100.0,
            "volume": 1_000_000.0, "available_at": d, "shares_outstanding": 50_000_000.0,
        }
        for d in [PRIOR_MONTH_END, *MONTH_SESSIONS]
    ]
    prices = AsOfFrame(pl.DataFrame(rows, schema=CURRENT_SHARES_SCHEMA))
    universe = _universe(ids)

    rt = REGISTRY["H3"].rank(OBS, universe, prices)

    assert rt.unavailable is True
    assert rt.reason == UNAVAILABLE_REASON
    assert rt.frame.height == 0


def test_h3_turnover_is_the_sum_of_daily_volume_over_point_in_time_shares() -> None:
    vols = [1_000_000.0, 2_000_000.0, 500_000.0]
    shares_pit = [50_000_000.0, 51_000_000.0, 51_000_000.0]
    expected = sum(v / s for v, s in zip(vols, shares_pit))
    assert round(h3_turnover(vols, shares_pit), 8) == round(expected, 8)


def test_h3_selects_the_intersection_of_return_top_10pct_and_turnover_top_30pct() -> None:
    ids = [f"SEC{i:04d}" for i in range(20)]
    # Returns: highest for SEC0000, SEC0001 (top 10% of 20 = 2 names).
    returns = {sid: 0.20 - i * 0.005 for i, sid in enumerate(ids)}
    # Turnover: highest for SEC0002..SEC0007 (top 30% of 20 = 6 names) --
    # deliberately NOT overlapping the return leaders except for one name.
    shares = {sid: 100_000_000.0 for sid in ids}
    high_turnover_ids = {f"SEC{i:04d}" for i in range(1, 7)}  # includes SEC0001
    volumes = {
        sid: [
            (10_000_000.0 if sid in high_turnover_ids else 100_000.0)
            for _ in MONTH_SESSIONS
        ]
        for sid in ids
    }
    prices = _build_pit(ids, returns, volumes, shares)
    universe = _universe(ids)

    rt = REGISTRY["H3"].rank(OBS, universe, prices)

    ret_top = sorted(ids, key=lambda sid: -returns[sid])[:2]
    expected = set(ret_top) & high_turnover_ids
    assert set(rt.frame["security_id"]) == expected
    assert expected == {"SEC0001"}


def test_h3_applies_the_positive_return_filter() -> None:
    ids = [f"SEC{i:04d}" for i in range(20)]
    returns = {sid: -0.10 - i * 0.001 for i, sid in enumerate(ids)}  # all negative
    shares = {sid: 100_000_000.0 for sid in ids}
    volumes = {sid: [10_000_000.0] * len(MONTH_SESSIONS) for sid in ids}
    prices = _build_pit(ids, returns, volumes, shares)
    universe = _universe(ids)

    rt = REGISTRY["H3"].rank(OBS, universe, prices)

    assert rt.frame.height == 0


def test_h3_only_reads_data_visible_at_the_observation_date() -> None:
    ids = ["SEC0000"]
    returns = {"SEC0000": 0.10}
    shares = {"SEC0000": 100_000_000.0}
    volumes = {"SEC0000": [10_000_000.0] * len(MONTH_SESSIONS)}
    prices = _build_pit(ids, returns, volumes, shares)
    universe = _universe(ids)

    REGISTRY["H3"].rank(OBS, universe, prices)
    assert prices.raw_access_count == 0
