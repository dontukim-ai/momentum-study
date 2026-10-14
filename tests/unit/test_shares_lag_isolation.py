"""Finding 3: a delayed ``shares_history`` publish must never delay price
visibility for H1/H2/H4 (which never read the shares columns at all), and
must correctly narrow -- or make unavailable -- H3's selection, which DOES
read them. This is the whole point of NOT widening the merged frame's
``available_at`` to ``max(price_available_at, shares_available_at)``
(reverted in this task after being flagged in review): that widening would
have silently coupled every hypothesis's price visibility to how promptly
shares data happens to be published.
"""

from __future__ import annotations

from datetime import date, timedelta

import polars as pl

from momentum.asof import AsOfFrame
from momentum.signals import REGISTRY

PRIOR_MONTH_END = date(2000, 5, 31)
MONTH_SESSIONS = [date(2000, 6, 28), date(2000, 6, 29), date(2000, 6, 30)]
OBS = date(2000, 6, 30)

PIT_SCHEMA = {
    "security_id": pl.Utf8,
    "session_date": pl.Date,
    "close_raw": pl.Float64,
    "volume": pl.Float64,
    "available_at": pl.Date,
    "shares_outstanding": pl.Float64,
    "shares_available_at": pl.Date,
}


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


def _build(ids: list[str], shares_lag_days: int) -> AsOfFrame:
    """``shares_lag_days`` delays ``shares_available_at`` behind the
    session it describes -- the PRICE row's own ``available_at`` is always
    same-day, regardless of the lag, exactly as ``cli.py`` builds it."""
    rows = []
    for i, sid in enumerate(ids):
        rows.append(
            {
                "security_id": sid, "session_date": PRIOR_MONTH_END, "close_raw": 100.0,
                "volume": 1_000_000.0, "available_at": PRIOR_MONTH_END,
                "shares_outstanding": 50_000_000.0,
                "shares_available_at": PRIOR_MONTH_END + timedelta(days=shares_lag_days),
            }
        )
        for d in MONTH_SESSIONS:
            close = 100.0 if d != OBS else 100.0 * (1.20 - i * 0.01)  # top names have positive return
            rows.append(
                {
                    "security_id": sid, "session_date": d, "close_raw": close,
                    "volume": 10_000_000.0, "available_at": d,
                    "shares_outstanding": 50_000_000.0,
                    "shares_available_at": d + timedelta(days=shares_lag_days),
                }
            )
    return AsOfFrame(pl.DataFrame(rows, schema=PIT_SCHEMA))


def test_h1_ranking_is_byte_identical_regardless_of_shares_publish_lag() -> None:
    ids = [f"SEC{i:04d}" for i in range(10)]
    no_lag = _build(ids, shares_lag_days=0)
    lagged = _build(ids, shares_lag_days=30)  # shares for OBS won't be visible until 30 days later
    universe = _universe(ids)

    rt_no_lag = REGISTRY["H1"].rank(OBS, universe, no_lag)
    rt_lagged = REGISTRY["H1"].rank(OBS, universe, lagged)

    assert rt_no_lag.frame.equals(rt_lagged.frame)


def test_h2_ranking_is_byte_identical_regardless_of_shares_publish_lag() -> None:
    ids = [f"SEC{i:04d}" for i in range(10)]
    no_lag = _build(ids, shares_lag_days=0)
    lagged = _build(ids, shares_lag_days=30)
    universe = _universe(ids)

    rt_no_lag = REGISTRY["H2"].rank(OBS, universe, no_lag)
    rt_lagged = REGISTRY["H2"].rank(OBS, universe, lagged)

    assert rt_no_lag.frame.equals(rt_lagged.frame)


def test_h4_ranking_is_byte_identical_regardless_of_shares_publish_lag() -> None:
    # H4 needs a 12-month lookback; give it two clean month-end points and
    # a lag long enough it would matter to H3 but is irrelevant to H4.
    ids = [f"SEC{i:04d}" for i in range(10)]

    def build_h4(shares_lag_days: int) -> AsOfFrame:
        rows = []
        num_month = date(2000, 5, 31)
        den_month = date(1999, 6, 30)
        for i, sid in enumerate(ids):
            for d, close in ((den_month, 100.0), (num_month, 110.0 - i * 0.1)):
                rows.append(
                    {
                        "security_id": sid, "session_date": d, "close_raw": close,
                        "volume": 1_000_000.0, "available_at": d,
                        "shares_outstanding": 50_000_000.0,
                        "shares_available_at": d + timedelta(days=shares_lag_days),
                    }
                )
        return AsOfFrame(pl.DataFrame(rows, schema=PIT_SCHEMA))

    universe = _universe(ids)
    rt_no_lag = REGISTRY["H4"].rank(date(2000, 6, 30), universe, build_h4(0))
    rt_lagged = REGISTRY["H4"].rank(date(2000, 6, 30), universe, build_h4(365))

    assert rt_no_lag.frame.equals(rt_lagged.frame)


def test_h3_selection_narrows_when_shares_are_published_after_the_observation_date() -> None:
    ids = [f"SEC{i:04d}" for i in range(10)]
    no_lag = _build(ids, shares_lag_days=0)
    # Published 5 days after each session -- so as of OBS (2000-06-30),
    # the shares_available_at for the LAST couple of month sessions is
    # still in the future: those sessions' turnover becomes unusable, and
    # this security drops out of the turnover-eligible pool entirely.
    lagged = _build(ids, shares_lag_days=5)
    universe = _universe(ids)

    rt_no_lag = REGISTRY["H3"].rank(OBS, universe, no_lag)
    rt_lagged = REGISTRY["H3"].rank(OBS, universe, lagged)

    # Not unavailable (the columns exist) -- but strictly narrower, since
    # every security's turnover becomes unusable once its month's shares
    # are not fully published as of the observation date.
    assert rt_lagged.unavailable is False
    assert rt_lagged.frame.height == 0
    assert rt_no_lag.frame.height > 0
    assert set(rt_lagged.frame["security_id"]) <= set(rt_no_lag.frame["security_id"])
