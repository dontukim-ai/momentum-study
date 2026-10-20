"""Golden regression: build_universe over the seed=7 synthetic market.

The fixture in ``tests/fixtures/universe_2000-06-30.csv`` was produced by
running ``build_universe`` once over the deterministic synthetic market from
``momentum.synth.generate_market(seed=7, ...)`` and saving the result. This
is a regression lock, not an independently hand-derived golden value: the
synthetic market itself has no "correct" universe to hand-compute against,
so what this test protects is that a future change to ``build_universe`` (or
to the synth generator) does not silently change the eligible universe for
this fixed, reproducible input.

This runs on the **production ``UniverseCfg()`` defaults, unmodified**
(``min_close_usd=5.0``, ``min_adv_usd=20_000_000``, ``adv_lookback_days=60``,
``min_history_days=252``, ``max_names=1000``). An earlier version of this
fixture used scaled-down thresholds because the market only spanned ~130
sessions; that was flagged in review because it protected a configuration
nothing else in the codebase uses. The market here has been extended to
1999-01-04 .. 2001-12-31 (~781 sessions) specifically so the real
``min_history_days=252`` + ``adv_lookback_days=60`` requirements have enough
runway to be satisfied by 2000-06-30, and the defaults are used with no
overrides at all.

CALIBRATION NOTE (see task-6-report.md "Finding 3" section for the full
writeup): at seed=7 / 2000-06-30 under these exact production defaults,
**every one of the 20 synthetic securities is included and
``exclusion_reason`` is null for all of them.** Synthetic dollar volume in
this market runs ~$97M-$742M, an order of magnitude above the $20M ADV
floor, and by 2000-06-30 every security (including the two that eventually
delist on 2000-07-03, three days later) has ~390 sessions of history, well
past the 252-day floor. So this particular fixture, while now genuinely
exercising the production thresholds end-to-end, happens to exercise none
of the six exclusion reasons -- it only proves that a clean, liquid,
long-history synthetic universe is *not* falsely excluded by the production
config. The exclusion-reason *logic itself* (priority order, each reason
individually, and reasons that fight each other) is covered exhaustively by
the hand-built fixtures in ``tests/unit/test_universe.py``, which remains
the authoritative test for that per CLAUDE.md's "hand-calculated golden
fixture" rule -- this golden test's job is narrower: lock the production
config's *quantitative* behavior (ADV numbers, ranks) against regression.
"""

from datetime import date

import polars as pl

from momentum.asof import AsOfFrame
from momentum.calendar import TradingCalendar
from momentum.config import UniverseCfg
from momentum.synth import generate_market
from momentum.universe import build_universe

FIX = "tests/fixtures/universe_2000-06-30.csv"

CFG = UniverseCfg()
START = date(1999, 1, 4)
END = date(2001, 12, 31)
EFFECTIVE_DATE = date(2000, 6, 30)


def _synth_inputs(seed: int) -> tuple[AsOfFrame, AsOfFrame, TradingCalendar, UniverseCfg]:
    m = generate_market(seed=seed, n_securities=20, start=START, end=END)
    prices = AsOfFrame(m.daily_prices)
    master = AsOfFrame(m.security_master)
    cal = TradingCalendar.from_dates(m.calendar["session_date"].to_list())
    return prices, master, cal, CFG


# ``adv_usd`` is a float mean whose last bits depend on the summation order,
# which differs between CPU architectures (2026-10-20: arm64 differed from
# the fixture in the 15th-16th significant digit on 7 of 20 rows; x86
# matched). Decisions (rank, inclusion, reason) must match exactly; the float
# must match to a relative 1e-12, far below any money the $20M floor sees.
ADV_REL_TOL = 1e-12


def test_universe_for_seed7_is_unchanged():
    got = build_universe(*_synth_inputs(seed=7), effective_date=EFFECTIVE_DATE)
    want = pl.read_csv(FIX, try_parse_dates=True).with_columns(
        pl.col("included").cast(pl.Boolean)
    )
    assert got.schema == want.schema
    exact = [c for c in want.columns if c != "adv_usd"]
    assert got.select(exact).equals(want.select(exact))
    for g, w in zip(got["adv_usd"].to_list(), want["adv_usd"].to_list(), strict=True):
        assert (g is None) == (w is None)
        if w is not None:
            assert abs(g - w) <= ADV_REL_TOL * abs(w)
